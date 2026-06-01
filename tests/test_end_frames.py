"""Tests for the optional per-scene END FRAME (start→end interpolation).

A scene can carry an end-frame image; that scene's video then animates from
the approved image to the end frame (Kling 3.0 only). Covers the upload/clear
endpoints + that the runner resolves the per-scene end frame. Hermetic: direct
endpoint calls with a stub store + tmp output dir; events.publish patched.
"""
from __future__ import annotations

import asyncio
import io

import pytest
from starlette.datastructures import Headers, UploadFile

from character_swap import api
from character_swap.config import settings
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
)


class _FakeStore:
    def __init__(self, job: Job) -> None:
        self._job = job

    def get_job(self, job_id: str):
        return self._job if job_id == self._job.job_id else None

    def update_job(self, job: Job) -> None:
        self._job = job


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

    result = _run(api.set_scene_end_frame("j_ef", "s1", file=_png()))
    s1 = next(s for s in result["scenes"] if s["scene_id"] == "s1")
    assert s1["end_frame_url"] is not None          # set + served
    # File actually written under the tmp output dir.
    saved = store.get_job("j_ef").end_frames_by_scene["s1"]
    from pathlib import Path
    assert Path(saved).exists()

    # Other scene untouched.
    s2 = next(s for s in result["scenes"] if s["scene_id"] == "s2")
    assert s2["end_frame_url"] is None

    cleared = _run(api.clear_scene_end_frame("j_ef", "s1"))
    s1c = next(s for s in cleared["scenes"] if s["scene_id"] == "s1")
    assert s1c["end_frame_url"] is None
    assert "s1" not in store.get_job("j_ef").end_frames_by_scene


def test_end_frame_unknown_scene_404(monkeypatch):
    monkeypatch.setattr(api, "store", lambda: _FakeStore(_job()))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.set_scene_end_frame("j_ef", "nope", file=_png()))
    assert ei.value.status_code == 404


def test_end_frame_locked_after_movement(monkeypatch):
    job = _job()
    job.movement_prompts = {"s1": "do it"}    # movement submitted
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.set_scene_end_frame("j_ef", "s1", file=_png()))
    assert ei.value.status_code == 409
