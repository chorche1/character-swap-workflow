"""Per-scene END FRAMES in Reengineer (Hugo 2026-06-13).

Upload an end pose AFTER the scenes exist → every character is swapped into
it (job-level end-frame machinery from 2026-06-08) → the scene's Kling 3.0
clip interpolates start → end. NOT a new scene. Enablers under test:

1. The three end-frame endpoints' movement lock is relaxed for
   Reengineer-origin jobs (plain Swap jobs stay locked).
2. `retry_one_video` and `generate_more_videos` — the paths Reengineer's
   reanimate uses — now carry the end frame to the Kling submit (they
   silently dropped it before).
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from character_swap import api, runner
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


def _job(*, origin: str | None = "reengineer:re_t", movement: bool = True,
         video_model: str = "kling-v3") -> Job:
    v = GeneratedImage(variant_id="va", path="/a.png", prompt="p",
                       scene_id="s1", status=VariantStatus.READY)
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/c.png",
        status=CharStatus.APPROVED, images=[v],
        approved_variant_ids=["va"],
        videos=[VideoVariant(video_id="vidA", grok_job_id="g1",
                             status=VideoStatus.DONE, source_variant_id="va",
                             final_video_path="/clip.mp4")])
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               characters={"cA": jc}, origin=origin,
               video_model=video_model,
               movement_prompt=("animate" if movement else None),
               movement_prompts=({"s1": "animate"} if movement else {}))


class _Store:
    def __init__(self, job):
        self.job = job

    def get_job(self, jid):
        return self.job if jid == "j1" else None

    def update_job(self, j):
        pass


def _png() -> UploadFile:
    return UploadFile(io.BytesIO(b"\x89PNG\r\n\x1a\nend-frame"),
                      filename="pose.png")


@pytest.fixture
def wired(monkeypatch, tmp_path):
    job = _job()
    monkeypatch.setattr(api, "store", lambda: _Store(job))

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(api.events, "publish", _noop)
    monkeypatch.setattr(type(api.settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)
    return job


# ----------------------------------------------- lock relaxed for reengineer

def test_set_end_frame_allowed_post_gate_for_reengineer(wired):
    bg = BackgroundTasks()
    out = asyncio.run(api.set_scene_end_frame("j1", "s1", bg, file=_png()))
    assert wired.end_frames_by_scene.get("s1")
    assert Path(wired.end_frames_by_scene["s1"]).exists()
    # Variants exist → the per-character swap regeneration is scheduled.
    assert any(runner.regen_scene_end_frames in t.args for t in bg.tasks)
    assert out["job_id"] == "j1"


def test_regen_and_clear_allowed_post_gate_for_reengineer(wired, tmp_path):
    pose = tmp_path / "pose.png"
    pose.write_bytes(b"x")
    wired.end_frames_by_scene = {"s1": str(pose)}
    wired.characters["cA"].end_frame_errors = {"s1": "boom"}
    bg = BackgroundTasks()
    asyncio.run(api.regen_scene_end_frame("j1", "s1", bg))
    assert "s1" not in wired.characters["cA"].end_frame_errors
    assert len(bg.tasks) == 1
    asyncio.run(api.clear_scene_end_frame("j1", "s1"))
    assert wired.end_frames_by_scene == {}


def test_end_frame_still_locked_for_plain_swap_jobs(wired, monkeypatch):
    plain = _job(origin=None)
    monkeypatch.setattr(api, "store", lambda: _Store(plain))
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.set_scene_end_frame("j1", "s1", BackgroundTasks(),
                                            file=_png()))
    assert e.value.status_code == 409


# ------------------------------------- end frame rides on retries / extra takes

@pytest.fixture
def video_wired(monkeypatch, tmp_path):
    job = _job()
    end = tmp_path / "end_va.png"
    end.write_bytes(b"swapped end frame")
    job.characters["cA"].end_frame_paths = {"s1": str(end)}
    monkeypatch.setattr(runner, "store", lambda: _Store(job))
    seen = []

    async def fake_animate(j, jc, v, mp, dur, end_image=None):
        seen.append({"video_id": v.video_id, "end_image": end_image})
    monkeypatch.setattr(runner, "_animate_one_video", fake_animate)
    return job, end, seen


def test_retry_one_video_carries_end_frame(video_wired):
    job, end, seen = video_wired
    asyncio.run(runner.retry_one_video("j1", "cA", "vidA"))
    assert len(seen) == 1
    assert seen[0]["end_image"] == end


def test_generate_more_videos_carries_end_frame(video_wired):
    job, end, seen = video_wired
    asyncio.run(runner.generate_more_videos("j1", "cA", 1,
                                            source_variant_id="va"))
    assert len(seen) == 1
    assert seen[0]["end_image"] == end


def test_retry_one_video_no_end_frame_for_other_models(video_wired):
    job, end, seen = video_wired
    job.video_model = "grok-imagine"
    asyncio.run(runner.retry_one_video("j1", "cA", "vidA"))
    assert seen[0]["end_image"] is None


def test_resolve_end_image_swaps_on_demand(monkeypatch, tmp_path):
    """No pre-generated swap but an end pose exists → swap now (the
    _ensure_end_frame_swap fallback), errors surfaced on end_frame_errors."""
    job = _job()
    pose = tmp_path / "pose.png"
    pose.write_bytes(b"pose")
    job.end_frames_by_scene = {"s1": str(pose)}
    out = tmp_path / "swapped.png"

    def fake_ensure(j, jc, sid, p):
        out.write_bytes(b"s")
        return out
    monkeypatch.setattr(runner, "_ensure_end_frame_swap", fake_ensure)
    got = asyncio.run(runner._resolve_end_image(
        job, job.characters["cA"], "s1"))
    assert got == out

    def boom(j, jc, sid, p):
        raise RuntimeError("content policy")
    monkeypatch.setattr(runner, "_ensure_end_frame_swap", boom)
    job.characters["cA"].end_frame_paths = {}
    got = asyncio.run(runner._resolve_end_image(
        job, job.characters["cA"], "s1"))
    assert got is None
    assert "content policy" in job.characters["cA"].end_frame_errors["s1"]
