"""Per-CLIP (per-scene) video-model override — Swap + Reengineer.

Opt-in: each scene can animate with a different provider via
`Job.video_models_by_scene` (scene_id → model slug). Empty → every scene uses
`job.video_model`. These tests lock the resolution, the End-frame soft-degrade
(only kling-v3 honors end poses), the `/movement` provider validation, and the
Reengineer redo/reanimate bridge that must carry the override onto the job.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, runner, runner_reengineer
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus, VideoStatus,
    VideoVariant,
)


def _run(coro):
    return asyncio.run(coro)


def _two_scene_job(**overrides) -> tuple[Job, JobCharacter]:
    """One character, two scenes (scA, scB), one approved variant each."""
    imgs = [
        GeneratedImage(variant_id="v_a", path="/tmp/a.png", prompt="(x)",
                       scene_id="scA", status=VariantStatus.READY),
        GeneratedImage(variant_id="v_b", path="/tmp/b.png", prompt="(x)",
                       scene_id="scB", status=VariantStatus.READY),
    ]
    jc = JobCharacter(
        char_id="c1", name="Cooper", source_image_path="/tmp/a.png",
        status=CharStatus.APPROVED, images=imgs,
        approved_variant_ids=["v_a", "v_b"], approved_variant_id="v_a",
    )
    overrides.setdefault("video_model", "grok-imagine")
    job = Job(
        job_id="j_test", title="t",
        scene_id="scA", scene_image_path="/tmp/scene.png",
        scene_ids=["scA", "scB"], scene_image_paths=["/tmp/a.png", "/tmp/b.png"],
        characters={"c1": jc},
        **overrides,
    )
    return job, jc


# --- 1. Runner resolution (covers submit + salvage + resume read-sites) -------

def test_eff_video_model_resolves_per_scene() -> None:
    job, jc = _two_scene_job(video_models_by_scene={"scB": "veo"})
    vid_a = VideoVariant(video_id="vid_a", grok_job_id="g1", source_variant_id="v_a")
    vid_b = VideoVariant(video_id="vid_b", grok_job_id="g2", source_variant_id="v_b")

    # Scene B has an override; scene A falls back to the job default.
    assert runner._eff_video_model(job, jc, vid_b) == "veo"
    assert runner._eff_video_model(job, jc, vid_a) == "grok-imagine"
    # The same resolution drives end-frame gating, salvage re-poll and resume.
    assert runner._eff_video_model_for_scene(job, "scB") == "veo"
    assert runner._eff_video_model_for_scene(job, "scA") == "grok-imagine"
    assert runner._eff_video_model_for_scene(job, None) == "grok-imagine"


def test_eff_video_model_back_compat_empty_dict() -> None:
    # Old jobs (no per-scene dict) resolve to the job default, never KeyError.
    job, jc = _two_scene_job()  # video_models_by_scene defaults to {}
    assert job.video_models_by_scene == {}
    assert runner._eff_video_model_for_scene(job, "scA") == "grok-imagine"
    job.video_model = ""  # ultimate fallback
    assert runner._eff_video_model_for_scene(job, "scA") == "grok-imagine"


# --- 2. End-frame soft-degrade: only kling-v3 honors a scene's end pose -------

def test_resolve_end_image_gated_per_scene(tmp_path: Path) -> None:
    end_pose = tmp_path / "end.png"
    end_pose.write_bytes(b"x")
    pre_swapped = tmp_path / "pre.png"
    pre_swapped.write_bytes(b"y")

    job, jc = _two_scene_job(
        video_model="kling-v3",                       # run default = Kling 3.0
        video_models_by_scene={"scB": "veo"},         # scene B overridden
        end_frames_by_scene={"scA": str(end_pose), "scB": str(end_pose)},
    )
    # Scene A (effective kling-v3) with a pre-generated swapped end frame → used.
    jc.end_frame_paths = {"scA": str(pre_swapped)}
    assert _run(runner._resolve_end_image(job, jc, "scA")) == pre_swapped

    # Scene B (overridden to veo) → end pose ignored, no swap attempted.
    def _boom(*a, **k):  # pragma: no cover - must NOT be called
        raise AssertionError("end-frame swap ran for a non-Kling scene")
    import character_swap.runner as _r
    orig = _r._ensure_end_frame_swap
    _r._ensure_end_frame_swap = _boom
    try:
        assert _run(runner._resolve_end_image(job, jc, "scB")) is None
    finally:
        _r._ensure_end_frame_swap = orig


# --- 3. /movement validates the chosen providers upfront (422 when locked) ----

class _FakeStore:
    def __init__(self, job: Job) -> None:
        self._job = job

    def get_job(self, job_id: str) -> Job | None:
        return self._job if job_id == self._job.job_id else None

    def update_job(self, job: Job) -> None:
        self._job = job


def test_movement_rejects_locked_per_scene_model(monkeypatch: pytest.MonkeyPatch) -> None:
    job, _ = _two_scene_job()
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))
    # Job default (xai) available; the per-scene veo override (gemini) is not.
    monkeypatch.setattr(type(api.settings), "has_provider",
                        lambda self, provider: provider != "gemini")

    body = api.MovementBody(
        movement_prompts={"scA": "a", "scB": "b"},
        video_model="grok-imagine",
        video_models_by_scene={"scB": "veo"},
    )
    with pytest.raises(HTTPException) as ei:
        _run(api.set_movement("j_test", body, BackgroundTasks()))
    assert ei.value.status_code == 422
    assert "Veo" in ei.value.detail  # names the locked model


def test_movement_stores_per_scene_models(monkeypatch: pytest.MonkeyPatch) -> None:
    job, _ = _two_scene_job()
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)

    body = api.MovementBody(
        movement_prompts={"scA": "a", "scB": "b"},
        video_model="grok-imagine",
        video_models_by_scene={"scB": "veo", "scA": ""},  # "" = same as job
    )
    _run(api.set_movement("j_test", body, BackgroundTasks()))
    # Blank entry dropped; only the real override persists.
    assert job.video_models_by_scene == {"scB": "veo"}
    assert job.video_model == "grok-imagine"


# --- 4. Reengineer bridge: redo/reanimate carry the per-scene model + duration -

class _NoopStore:
    def update_job(self, job: Job) -> None:  # _sync_movement_from_state calls this
        pass


def test_reengineer_sync_carries_model_and_model_aware_duration(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner_reengineer, "store", lambda: _NoopStore())
    state = {
        "video_model": "kling-v3",  # run default
        "scenes": [
            # Kling scene, original length 10s → _kling_duration = floor(10)+2 = 12
            {"scene_id": "scA", "idx": 0, "motion_prompt": "He waves", "duration": 10.0},
            # Veo override, same 10s → must use veo's default (8), NOT _kling_duration
            {"scene_id": "scB", "idx": 1, "motion_prompt": "He nods",
             "duration": 10.0, "video_model": "veo"},
        ],
    }
    job = Job(job_id="j_re", scene_id="scA", scene_image_path="/tmp/s.png",
              scene_ids=["scA", "scB"], characters={})

    runner_reengineer._sync_movement_from_state(job, state)

    assert job.video_models_by_scene == {"scA": "kling-v3", "scB": "veo"}
    assert job.durations_by_scene["scA"] == 12          # Kling auto-length intact
    assert job.durations_by_scene["scB"] == 8           # veo default, model-aware
    # Sanity: the Kling auto-length helper would have given 12 for scB too —
    # proving the non-Kling branch did NOT route through _kling_duration.
    assert runner_reengineer._kling_duration(state["scenes"][1]) == 12


# --- 5. Review fix: non-Kling clip length is NOT clamped to Kling's 15s ------

def test_clamp_scene_secs_is_model_aware() -> None:
    # Kling 3.0 takes any whole second in [3,15].
    assert api._clamp_scene_secs("kling-v3", 20.0) == 15.0
    assert api._clamp_scene_secs("kling-v3", 2.0) == 3.0
    # A model whose options exceed 15s keeps the longer pick (no silent clamp).
    assert api._clamp_scene_secs("sora-2", 20.0) == 20.0          # 20 is a valid option
    # Off-grid value snaps to the model's NEAREST option, not Kling's ceiling.
    assert api._clamp_scene_secs("veo", 20.0) == 8.0             # veo opts [4,6,8]
    assert api._clamp_scene_secs("veo-3.1-fast", 6.0) == 6.0


def test_scene_duration_honors_long_nonkling_override() -> None:
    # A Sora-2 scene whose stored length is 20s must resolve to 20 (the old
    # [3,15] clamp would have stored 15 and this would silently render short).
    state = {"video_model": "kling-v3"}
    entry = {"video_model": "sora-2", "kling_secs": 20.0, "duration": 6.0}
    assert runner_reengineer._scene_duration(entry, state) == 20


# --- 6. Review fix: retry pre-flight keys on the per-scene effective model ----

def _job_with_failed_clip(models_by_scene, *, video_model) -> Job:
    """One char, approved variant on scene scB, with a FAILED video clip."""
    img = GeneratedImage(variant_id="v1", path="/tmp/a.png", prompt="(x)",
                         scene_id="scB", status=VariantStatus.READY)
    vid = VideoVariant(video_id="vd1", grok_job_id="",
                       status=VideoStatus.FAILED, source_variant_id="v1")
    jc = JobCharacter(char_id="c1", name="T", source_image_path="/tmp/a.png",
                      status=CharStatus.APPROVED, images=[img],
                      approved_variant_ids=["v1"], approved_variant_id="v1",
                      videos=[vid])
    return Job(job_id="j1", scene_id="scA", scene_image_path="/tmp/s.png",
               scene_ids=["scA", "scB"], characters={"c1": jc},
               video_model=video_model, movement_prompt="go",
               video_models_by_scene=models_by_scene)


def test_retry_failed_preflights_the_override_provider(monkeypatch) -> None:
    # Job default = grok (xai) but the failed clip's scene is overridden to veo
    # (gemini): the pre-flight must check GEMINI (the provider it'll resubmit
    # under), not xai — otherwise the worker silently 401s.
    job = _job_with_failed_clip({"scB": "veo"}, video_model="grok-imagine")
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))
    seen = []
    monkeypatch.setattr(type(api.settings), "require_keys",
                        lambda self, *names: seen.extend(names))
    from fastapi import BackgroundTasks
    _run(api.retry_failed_videos("j1", BackgroundTasks()))
    assert "gemini" in seen          # veo override's key pre-flighted
    assert "xai" not in seen         # NOT keyed on the job default


def test_retry_failed_no_false_reject_for_available_override(monkeypatch) -> None:
    # Inverse: job default = veo (gemini) but the failed clip is overridden to
    # grok (xai). Pre-flight must check xai only — NOT gemini (which would
    # wrongly 422 a retry that never submits to veo).
    job = _job_with_failed_clip({"scB": "grok-imagine"}, video_model="veo")
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))
    seen = []
    monkeypatch.setattr(type(api.settings), "require_keys",
                        lambda self, *names: seen.extend(names))
    from fastapi import BackgroundTasks
    _run(api.retry_failed_videos("j1", BackgroundTasks()))
    assert "xai" in seen
    assert "gemini" not in seen
