"""Per-CLIP video-model + length override in the "Regenerate this clip" modal
(Hugo 2026-07-16).

The regen modal (both flows: Swap Step-5 /retry_video AND Reengineer
/regen_clip) can now re-render ONE character's clip with a different provider
and length WITHOUT touching the scene's shared model/length (which changes every
character). The override is keyed on the clip's `source_variant_id` via
`Job.video_models_by_variant` + `Job.durations_by_variant`, so it sticks to that
clip across re-animate / rebuild.

These lock: the runner resolution precedence (per-variant > per-scene > job),
the end-frame gate following the per-clip model, the shared override-writer's
set / clear / clamp / lock behavior, and both endpoints persisting it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, runner
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus, VideoStatus,
    VideoVariant,
)


def _run(coro):
    return asyncio.run(coro)


def _job(**overrides) -> tuple[Job, JobCharacter, VideoVariant]:
    """One char, one scene (scA), one approved variant v_a, one DONE clip."""
    img = GeneratedImage(variant_id="v_a", path="/tmp/a.png", prompt="(x)",
                         scene_id="scA", status=VariantStatus.READY)
    vid = VideoVariant(video_id="vid1", grok_job_id="g1", source_variant_id="v_a",
                       status=VideoStatus.DONE, final_video_path="/tmp/c.mp4")
    jc = JobCharacter(char_id="c1", name="Cooper", source_image_path="/tmp/a.png",
                      status=CharStatus.APPROVED, images=[img],
                      approved_variant_ids=["v_a"], approved_variant_id="v_a",
                      videos=[vid])
    overrides.setdefault("video_model", "grok-imagine")
    job = Job(job_id="j1", title="t", scene_id="scA", scene_image_path="/tmp/s.png",
              scene_ids=["scA"], scene_image_paths=["/tmp/a.png"],
              characters={"c1": jc}, movement_prompt="go",
              movement_prompts={"scA": "go"}, **overrides)
    return job, jc, vid


class _FakeStore:
    def __init__(self, job: Job) -> None:
        self._job = job
        self.updated = False

    def get_job(self, job_id: str) -> Job | None:
        return self._job if job_id == self._job.job_id else None

    def update_job(self, job: Job) -> None:
        self._job = job
        self.updated = True


# --- 1. Runner resolution: per-variant override wins over per-scene / job ------

def test_eff_video_model_for_variant_precedence() -> None:
    job, jc, _ = _job(video_models_by_scene={"scA": "kling-v3"},
                      video_models_by_variant={"v_a": "seedance-2.0"})
    # per-variant beats per-scene beats job default.
    assert runner._eff_video_model_for_variant(job, jc, "v_a") == "seedance-2.0"
    # Drop the per-variant entry → falls back to the per-scene override.
    job.video_models_by_variant = {}
    assert runner._eff_video_model_for_variant(job, jc, "v_a") == "kling-v3"
    # Drop both → job default.
    job.video_models_by_scene = {}
    assert runner._eff_video_model_for_variant(job, jc, "v_a") == "grok-imagine"


def test_eff_video_model_reads_per_variant_via_video() -> None:
    job, jc, vid = _job(video_models_by_variant={"v_a": "veo-3.1-fast"})
    # The clip carries source_variant_id=v_a → resolves the per-clip override.
    assert runner._eff_video_model(job, jc, vid) == "veo-3.1-fast"


def test_eff_video_model_back_compat_no_variant_dict() -> None:
    job, jc, vid = _job()  # video_models_by_variant defaults {}
    assert job.video_models_by_variant == {}
    assert runner._eff_video_model(job, jc, vid) == "grok-imagine"


# --- 2. End-frame gate follows the PER-CLIP model -----------------------------

def test_resolve_end_image_uses_per_clip_model(tmp_path: Path) -> None:
    pose = tmp_path / "pose.png"
    pose.write_bytes(b"x")
    swapped = tmp_path / "swap.png"
    swapped.write_bytes(b"y")
    # Scene default is Grok (no end frame) but the clip is overridden to Kling.
    job, jc, _ = _job(video_model="grok-imagine",
                      end_frames_by_scene={"scA": str(pose)})
    jc.end_frame_paths = {"scA": str(swapped)}
    # Passing the per-clip model (kling-v3) → the pre-swapped end frame is used
    # even though the SCENE default (grok) would have dropped it.
    got = _run(runner._resolve_end_image(job, jc, "scA", video_model="kling-v3"))
    assert got == swapped
    # And the inverse: a per-clip model without end-frame support drops it.
    got2 = _run(runner._resolve_end_image(job, jc, "scA", video_model="grok-imagine"))
    assert got2 is None


# --- 3. Shared override-writer: set / clear / clamp / lock ---------------------

def test_apply_override_sets_model_and_clamps_length(monkeypatch) -> None:
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    job, jc, vid = _job()
    # veo-3.1-fast accepts [4,6,8]; 7 must snap to 6 (nearest).
    changed = api._apply_per_clip_video_override(
        job, jc, vid, video_model="veo-3.1-fast", duration_secs=7)
    assert changed is True
    assert job.video_models_by_variant == {"v_a": "veo-3.1-fast"}
    assert job.durations_by_variant == {"v_a": 6}


def test_apply_override_reports_no_change_for_scen_standard(monkeypatch) -> None:
    # "Scen-standard" (empty/0) with NO prior override doesn't mutate → False,
    # so the caller keeps the timeout-salvage fast-path.
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    job, jc, vid = _job()
    changed = api._apply_per_clip_video_override(
        job, jc, vid, video_model="", duration_secs=0)
    assert changed is False
    # Clearing a PRIOR override IS a change (force a fresh render).
    job.video_models_by_variant = {"v_a": "kling-v3"}
    changed2 = api._apply_per_clip_video_override(
        job, jc, vid, video_model="", duration_secs=0)
    assert changed2 is True


def test_apply_override_clears_on_empty(monkeypatch) -> None:
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    job, jc, vid = _job(video_models_by_variant={"v_a": "kling-v3"},
                        durations_by_variant={"v_a": 8})
    # "" model + 0 secs = revert to the scene/job default (drop the entries).
    api._apply_per_clip_video_override(job, jc, vid,
                                       video_model="", duration_secs=0)
    assert job.video_models_by_variant == {}
    assert job.durations_by_variant == {}


def test_apply_override_none_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    job, jc, vid = _job(video_models_by_variant={"v_a": "kling-v3"},
                        durations_by_variant={"v_a": 8})
    # Both None (an old caller) leaves the existing override untouched.
    api._apply_per_clip_video_override(job, jc, vid,
                                       video_model=None, duration_secs=None)
    assert job.video_models_by_variant == {"v_a": "kling-v3"}
    assert job.durations_by_variant == {"v_a": 8}


def test_apply_override_locked_model_422(monkeypatch) -> None:
    # Provider key missing → 422 before anything is written.
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: False)
    job, jc, vid = _job()
    with pytest.raises(HTTPException) as ei:
        api._apply_per_clip_video_override(job, jc, vid,
                                           video_model="kling-v3", duration_secs=0)
    assert ei.value.status_code == 422


def test_apply_override_length_clamps_against_chosen_model(monkeypatch) -> None:
    # The length must clamp to the NEWLY chosen model, not the old default:
    # pick sora-2 (accepts 20) + 20s → kept, where grok's default would differ.
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    job, jc, vid = _job()
    api._apply_per_clip_video_override(job, jc, vid,
                                       video_model="sora-2", duration_secs=20)
    assert job.durations_by_variant == {"v_a": 20}


# --- 4. /retry_video (Swap Step-5) persists the per-clip override --------------

def test_retry_video_writes_override(monkeypatch) -> None:
    job, jc, _ = _job()
    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    monkeypatch.setattr(type(api.settings), "require_keys", lambda self, *n: None)
    bg = BackgroundTasks()
    _run(api.retry_video("j1", api.RetryVideoBody(
        char_id="c1", video_id="vid1",
        video_model="veo-3.1-fast", duration_secs=8), bg))
    assert store.updated is True
    assert job.video_models_by_variant == {"v_a": "veo-3.1-fast"}
    assert job.durations_by_variant == {"v_a": 8}
    # retry_one_video queued (char_id, video_id, prompt override).
    assert bg.tasks[0].args[1:] == ("j1", "c1", "vid1", None)


def test_retry_video_no_override_leaves_maps_empty(monkeypatch) -> None:
    # Back-compat: an old client that omits video_model/duration_secs writes
    # nothing (a fresh take at the same model/length).
    job, jc, _ = _job()
    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)
    monkeypatch.setattr(type(api.settings), "require_keys", lambda self, *n: None)
    _run(api.retry_video("j1", api.RetryVideoBody(
        char_id="c1", video_id="vid1", prompt_override="x"), BackgroundTasks()))
    assert job.video_models_by_variant == {}
    assert job.durations_by_variant == {}


def test_retry_video_forces_fresh_on_changed_params_clears_grok_id(monkeypatch) -> None:
    # A timed-out clip (grok_job_id set, FAILED, "timed out") whose length the
    # user changes must FRESH-render, not salvage the old take: the endpoint
    # clears the old provider id so retry_one_video's salvage guard can't fire.
    job, jc, vid = _job()
    vid.status = VideoStatus.FAILED
    vid.grok_job_id = "old_req_123"
    vid.error = "video timed out after 600s"
    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    monkeypatch.setattr(type(api.settings), "require_keys", lambda self, *n: None)
    _run(api.retry_video("j1", api.RetryVideoBody(
        char_id="c1", video_id="vid1", duration_secs=8), BackgroundTasks()))
    assert vid.grok_job_id == ""          # salvage fast-path disarmed
    assert job.durations_by_variant == {"v_a": 8}


def test_retry_video_keeps_grok_id_when_no_change(monkeypatch) -> None:
    # No per-clip change (Scen-standard, no prior override) → the old provider
    # id is left intact so the timeout-salvage optimization still works.
    job, jc, vid = _job()
    vid.status = VideoStatus.FAILED
    vid.grok_job_id = "old_req_123"
    vid.error = "video timed out after 600s"
    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)
    monkeypatch.setattr(type(api.settings), "require_keys", lambda self, *n: None)
    _run(api.retry_video("j1", api.RetryVideoBody(
        char_id="c1", video_id="vid1", video_model="", duration_secs=0),
        BackgroundTasks()))
    assert vid.grok_job_id == "old_req_123"
    assert job.video_models_by_variant == {}


def test_job_dict_serializes_variant_maps() -> None:
    job, _, _ = _job(video_models_by_variant={"v_a": "kling-v3"},
                     durations_by_variant={"v_a": 8},
                     video_models_by_scene={"scA": "seedance-2.0"},
                     durations_by_scene={"scA": 5})
    d = api._job_to_dict(job)
    assert d["video_models_by_variant"] == {"v_a": "kling-v3"}
    assert d["durations_by_variant"] == {"v_a": 8}
    assert d["video_models_by_scene"] == {"scA": "seedance-2.0"}
    assert d["durations_by_scene"] == {"scA": 5}
