"""Tests for the optional per-scene END FRAME (start→end interpolation).

A scene can carry an end pose; each character is swapped into it so that scene's
video animates from the approved image to the swapped end frame (Kling 3.0
only). Covers the set/clear endpoints, lock-after-movement, the regen-on-set
trigger, and the end_image plumbing down to the fal Kling client. Hermetic:
direct endpoint calls with a stub store + tmp output dir; events.publish patched.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from starlette.datastructures import Headers, UploadFile

from character_swap import api, runner
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


class _FakeStore:
    def __init__(self, job: Job) -> None:
        self._job = job

    def get_job(self, job_id: str):
        return self._job if job_id == self._job.job_id else None

    def update_job(self, job: Job) -> None:
        self._job = job


class _FakeBG:
    """Captures background tasks without running them."""
    def __init__(self):
        self.tasks: list[tuple] = []

    def add_task(self, fn, *a, **k):
        self.tasks.append((fn, a, k))


def _job() -> Job:
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/a.png",
        status=CharStatus.APPROVED,
        images=[GeneratedImage(variant_id="vA1", path="/a.png", prompt="p",
                               scene_id="s1", status=VariantStatus.READY)],
        approved_variant_ids=["vA1"], approved_variant_id="vA1",
    )
    return Job(
        job_id="j_ef", title="t", scene_id="s1", scene_image_path="/p1.png",
        scene_ids=["s1", "s2"], scene_image_paths=["/p1.png", "/p2.png"],
        characters={"cA": jc},
    )


def _png() -> UploadFile:
    return UploadFile(filename="end.png",
                      file=io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"end-frame-bytes"),
                      headers=Headers({"content-type": "image/png"}))


@pytest.fixture(autouse=True)
def _patch(monkeypatch, tmp_path):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(api.events, "publish", _noop)
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")


def _run(coro):
    return asyncio.run(coro)


def test_set_and_clear_end_frame(monkeypatch):
    store = _FakeStore(_job())
    monkeypatch.setattr(api, "store", lambda: store)

    result = _run(api.set_scene_end_frame("j_ef", "s1", _FakeBG(), file=_png()))
    s1 = next(s for s in result["scenes"] if s["scene_id"] == "s1")
    assert s1["end_frame_url"] is not None          # set + served
    # File actually written under the tmp output dir.
    saved = store.get_job("j_ef").end_frames_by_scene["s1"]
    assert Path(saved).exists()

    # Other scene untouched.
    s2 = next(s for s in result["scenes"] if s["scene_id"] == "s2")
    assert s2["end_frame_url"] is None

    cleared = _run(api.clear_scene_end_frame("j_ef", "s1"))
    s1c = next(s for s in cleared["scenes"] if s["scene_id"] == "s1")
    assert s1c["end_frame_url"] is None
    assert "s1" not in store.get_job("j_ef").end_frames_by_scene


def test_set_end_frame_triggers_regen_when_variants_exist(monkeypatch):
    """With Step-3 variants already present, setting an end pose schedules a
    background regeneration so the preview end frame matches the new pose."""
    store = _FakeStore(_job())   # char cA already has a variant image
    monkeypatch.setattr(api, "store", lambda: store)
    bg = _FakeBG()
    _run(api.set_scene_end_frame("j_ef", "s1", bg, file=_png()))
    scheduled = [args for (_fn, args, _kw) in bg.tasks]
    assert any(runner.regen_scene_end_frames in args for args in scheduled)


def test_end_frame_unknown_scene_404(monkeypatch):
    monkeypatch.setattr(api, "store", lambda: _FakeStore(_job()))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.set_scene_end_frame("j_ef", "nope", _FakeBG(), file=_png()))
    assert ei.value.status_code == 404


def test_end_frame_locked_after_movement(monkeypatch):
    job = _job()
    job.movement_prompts = {"s1": "do it"}    # movement submitted
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.set_scene_end_frame("j_ef", "s1", _FakeBG(), file=_png()))
    assert ei.value.status_code == 409


# --- end_image plumbing: submit_video → fal Kling client ------------------

def test_submit_video_forwards_end_image_to_fal_kling(monkeypatch, tmp_path):
    """pipeline.submit_video(model="kling-v3", end_image=...) must hand the end
    frame to the fal Kling client (which uploads it as end_image_url)."""
    from character_swap import pipeline
    from character_swap.clients import fal_kling

    captured: dict = {}

    def fake_submit(*, image, prompt, duration_secs=5, end_image=None,
                    app_job_id=None, **kw):
        captured["end_image"] = end_image
        return "req_123"

    monkeypatch.setattr(fal_kling, "submit_image_to_video", fake_submit)
    start = tmp_path / "start.png"; start.write_bytes(b"s")
    end = tmp_path / "end.png"; end.write_bytes(b"e")

    rid = pipeline.submit_video(
        image=start, movement_prompt="move", character_name="A",
        model="kling-v3", end_image=end,
    )
    assert rid == "req_123"
    assert captured["end_image"] == end


def test_submit_image_to_video_uploads_end_image_url(monkeypatch, tmp_path):
    """fal_kling.submit_image_to_video passes end_image_url to fal.submit when an
    end frame is supplied."""
    import contextlib

    from character_swap.clients import fal_kling

    # Keep it hermetic — don't write a real calls.jsonl entry.
    @contextlib.contextmanager
    def _fake_record(**kw):
        yield {}
    monkeypatch.setattr(fal_kling.call_log, "record", _fake_record)

    seen: dict = {}

    class _Handler:
        request_id = "rid_1"

    class _FakeFal:
        def upload_file(self, p):
            return f"https://fal/{Path(p).name}"

        def submit(self, endpoint, arguments=None):
            seen["arguments"] = arguments
            return _Handler()

    monkeypatch.setattr(fal_kling, "_client", lambda: _FakeFal())
    start = tmp_path / "start.png"; start.write_bytes(b"s")
    end = tmp_path / "end.png"; end.write_bytes(b"e")

    rid = fal_kling.submit_image_to_video(
        image=start, prompt="m", duration_secs=7, end_image=end,
    )
    assert rid == "rid_1"
    assert seen["arguments"]["start_image_url"].endswith("start.png")
    assert seen["arguments"]["end_image_url"].endswith("end.png")
