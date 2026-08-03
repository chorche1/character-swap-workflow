"""Retaking / importing ONE clip while the run is still rendering (Hugo
2026-08-04: "gör så man kan retry clips även om alla klipp för körningen inte
är renderade").

re_… with 90 clips: 65 done, 18 still rendering, 7 refused by the video model's
content filter. The only usable recovery for a refused clip is ✎↻ with reworded
wording — and it answered 409 "cannot edit while run status is 'animating'" for
the ~40 min the other 18 took. Same for 📥 eget klipp, which additionally hit a
run-level `_ANIMATING` refusal.

Both endpoints are per-CLIP: they touch one video row and nothing else. The gate
is therefore `_PER_CLIP_RUN_STATES` (the editable states PLUS the two rendering
ones), and the protection moved to the right grain — the TARGET clip must be
idle, enforced by `_refuse_busy_clip` here and `ClipBusyError` inside
`attach_imported_clip`. The run's own `_watch_video_phase` waits for every clip
to go terminal, so a clip retaken mid-phase is simply waited for and the
auto-build still fires exactly once (covered in test_auto_assemble_gate.py).
"""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from character_swap import api, runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


def _job():
    img = GeneratedImage(variant_id="c0-v1", path="/1.png", prompt="s1",
                         scene_id="s1", status=VariantStatus.READY)
    vid = VideoVariant(video_id="c0-vid1", grok_job_id="g1",
                       source_variant_id="c0-v1", status=VideoStatus.FAILED,
                       error="content policy")
    jc = JobCharacter(char_id="c0", name="c0", source_image_path="/c.png",
                      status=CharStatus.ANIMATING, images=[img],
                      approved_variant_ids=["c0-v1"], videos=[vid])
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p1.png",
               scene_ids=["s1"], scene_image_paths=["/p1.png"],
               characters={"c0": jc}, origin="reengineer:re_t",
               movement_prompt="animate", movement_prompts={"s1": "old"})


def _state(status):
    return {"re_id": "re_t", "status": status, "job_id": "j1", "n_scenes": 1,
            "finals": {"c0": {"status": "done", "final_path": "/f.mp4"}},
            "scenes": [{"idx": 0, "scene_id": "s1", "start": 0.0, "end": 5.0,
                        "duration": 5.0, "motion_prompt": "p", "speech": "",
                        "summary": "one"}]}


@pytest.fixture
def wired(monkeypatch):
    box = {"job": None, "states": {}}

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j1" else None

        def update_job(self, j):
            box["job"] = j

        def get_scene(self, sid):
            return None

    monkeypatch.setattr(api, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return json.loads(json.dumps(s)) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = json.loads(json.dumps(s))

    for mod in (reengineer_mod, runner_reengineer.reengineer):
        monkeypatch.setattr(mod, "load_state", load_state)
        monkeypatch.setattr(mod, "save_state", save_state)
    return box


def _upload(name="clip.mp4", data=b"mp4"):
    return UploadFile(file=io.BytesIO(data), filename=name)


# --------------------------------------------------------------- import_clip

@pytest.mark.parametrize("status", ["animating", "reanimating"])
def test_import_clip_allowed_while_run_still_rendering(wired, monkeypatch, status):
    wired["job"] = _job()
    wired["states"]["re_t"] = _state(status)

    async def fake_import(job_id, char_id, file, *, variant_id=None,
                          video_id=None):
        assert variant_id == "c0-v1"       # scene's approved image resolved
        return SimpleNamespace(video_id="vid_new")
    monkeypatch.setattr(api, "_import_clip_common", fake_import)
    monkeypatch.setattr(runner_reengineer, "_scene_all_imported",
                        lambda job, sid: True)

    out = asyncio.run(api.reengineer_import_clip(
        "re_t", 0, char_id="c0", file=_upload()))
    assert out["regen_variants"] == {"c0": "vid_new"}
    assert wired["states"]["re_t"]["finals_stale"] is True


def test_import_clip_still_refuses_a_busy_target_clip(wired, monkeypatch):
    # The run-level guard is gone, but the per-CLIP one is what mattered:
    # attach_imported_clip raises ClipBusyError on a PENDING/PROCESSING slot
    # and the shared helper maps it to a loud 409.
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("animating")

    async def busy_import(job_id, char_id, file, *, variant_id=None,
                          video_id=None):
        raise HTTPException(409, "Klippet renderas fortfarande — vänta tills "
                                 "det är klart innan du importerar ett eget.")
    monkeypatch.setattr(api, "_import_clip_common", busy_import)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_import_clip(
            "re_t", 0, char_id="c0", file=_upload()))
    assert ei.value.status_code == 409
    assert "renderas fortfarande" in ei.value.detail


def test_import_clip_still_refuses_pre_clip_phases(wired, monkeypatch):
    # Phases with no clips at all keep refusing — nothing to import over.
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("swapping")
    monkeypatch.setattr(api, "_import_clip_common", None)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_import_clip(
            "re_t", 0, char_id="c0", file=_upload()))
    assert ei.value.status_code == 409


# ------------------------------------------------------ the gate itself

def test_per_clip_states_cover_both_rendering_phases():
    assert {"animating", "reanimating"} <= api._PER_CLIP_RUN_STATES
    # …and stay a superset of the run-level edit gate minus its pre-clip state.
    assert (runner_reengineer._EDITABLE_RUN_STATES - {"awaiting_approval"}
            <= api._PER_CLIP_RUN_STATES)


def test_refuse_busy_clip_only_blocks_in_flight():
    for st in (VideoStatus.PENDING, VideoStatus.PROCESSING):
        with pytest.raises(HTTPException) as ei:
            api._refuse_busy_clip(SimpleNamespace(status=st))
        assert ei.value.status_code == 409
    for st in (VideoStatus.DONE, VideoStatus.FAILED, VideoStatus.ERROR):
        api._refuse_busy_clip(SimpleNamespace(status=st))   # no raise


def test_retry_all_failed_needs_no_run_status(wired, monkeypatch):
    # The blunt "↻ Ta om misslyckade" already went straight at the job (no run
    # gate) — locked here so a future tightening can't quietly re-block it
    # while the run animates.
    monkeypatch.setattr(api, "_preflight_video_keys", lambda models: None)
    wired["job"] = _job()
    bg = BackgroundTasks()
    asyncio.run(api.retry_failed_videos("j1", bg, None))
    assert bg.tasks[0].args[1:] == ("j1", None)
