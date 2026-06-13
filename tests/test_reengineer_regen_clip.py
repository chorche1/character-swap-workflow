"""Per-clip regenerate-with-edited-prompt in the Reengineer tab (Hugo 2026-06-13).

The Reengineer tab now shows each scene's generated clip under the image it was
animated from, and lets the user regenerate ONE clip with an edited motion
prompt. `POST /api/reengineer/{re_id}/regen_clip` marks the final stale and
queues runner.retry_one_video with the per-clip override (replace-in-place); the
live poll keeps refreshing because the new clip is pending/processing.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api
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
                       source_variant_id="c0-v1", status=VideoStatus.DONE,
                       final_video_path="/clip.mp4")
    jc = JobCharacter(char_id="c0", name="c0", source_image_path="/c.png",
                      status=CharStatus.APPROVED, images=[img],
                      approved_variant_ids=["c0-v1"], videos=[vid])
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p1.png",
               scene_ids=["s1"], scene_image_paths=["/p1.png"],
               characters={"c0": jc}, origin="reengineer:re_t",
               movement_prompt="animate", movement_prompts={"s1": "old"},
               durations_by_scene={"s1": 5})


def _state(status="done", finals=True):
    st = {"re_id": "re_t", "status": status, "job_id": "j1", "n_scenes": 1,
          "scenes": [{"idx": 0, "scene_id": "s1", "start": 0.0, "end": 5.0,
                      "duration": 5.0, "motion_prompt": "p one", "speech": "",
                      "summary": "one"}]}
    if finals:
        st["finals"] = {"c0": {"status": "done", "final_path": "/f.mp4"}}
    return st


@pytest.fixture
def wired(monkeypatch):
    box = {"job": None, "states": {}}

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j1" else None

        def update_job(self, j):
            box["job_updated"] = True

        def get_scene(self, sid):
            return None

    monkeypatch.setattr(api, "store", lambda: _S())
    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)

    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    return box


def test_regen_clip_marks_stale_and_queues(wired):
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("done", finals=True)
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_regen_clip(
        "re_t", bg,
        api.ReClipRegenBody(char_id="c0", video_id="c0-vid1", prompt="pour slower")))
    # Final is flagged so the user re-assembles with "▶ Bygg ihop igen".
    assert wired["states"]["re_t"]["finals_stale"] is True
    # retry_one_video queued with the per-clip override prompt.
    assert len(bg.tasks) == 1
    assert bg.tasks[0].args[1:] == ("j1", "c0", "c0-vid1", "pour slower")
    # A view is returned so the client can splice it in.
    assert out["re_id"] == "re_t"


def test_regen_clip_blank_prompt_becomes_none(wired):
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("done", finals=True)
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_regen_clip(
        "re_t", bg,
        api.ReClipRegenBody(char_id="c0", video_id="c0-vid1", prompt="   ")))
    # Whitespace-only prompt = a fresh take on the same prompt (override None).
    assert bg.tasks[0].args[1:] == ("j1", "c0", "c0-vid1", None)


def test_regen_clip_404_unknown_video(wired):
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("done", finals=True)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_regen_clip(
            "re_t", BackgroundTasks(),
            api.ReClipRegenBody(char_id="c0", video_id="nope")))
    assert ei.value.status_code == 404


def test_regen_clip_no_finals_leaves_flag_unset(wired):
    # _mark_finals_stale only flips the flag when finals exist; still queues.
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("awaiting_assembly", finals=False)
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_regen_clip(
        "re_t", bg,
        api.ReClipRegenBody(char_id="c0", video_id="c0-vid1", prompt="x")))
    assert "finals_stale" not in wired["states"]["re_t"]
    assert len(bg.tasks) == 1


def test_regen_clip_409_when_run_not_editable(wired):
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("animating", finals=True)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_regen_clip(
            "re_t", BackgroundTasks(),
            api.ReClipRegenBody(char_id="c0", video_id="c0-vid1")))
    assert ei.value.status_code == 409
