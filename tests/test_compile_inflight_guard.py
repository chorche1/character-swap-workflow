"""In-flight guard on Step-6 compile / 🔁 repurpose submits (re-audit 2026-07-03).

A second POST while a compile/repurpose was already running passed the
eligibility gate, flipped the chars to "compiling" again and scheduled a
SECOND runner_compile pipeline racing the first onto the same
output/<job_id>/compiled/<char_id>[__repurpose].mp4 (torn copyfile writes,
edit_id describing different bytes than the disk, double Whisper/ElevenLabs
billing). The endpoints now 409 when a targeted char is already in flight —
mirroring the reengineer _ASSEMBLING/_REPURPOSING guard semantics. Startup
recovery flips restart-orphaned "compiling" to failed, so a live "compiling"
always means genuinely running.
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


class _FakeStore:
    def __init__(self, job: Job) -> None:
        self._job = job
        self.updated: list[Job] = []

    def get_job(self, jid):
        return self._job if jid == self._job.job_id else None

    def update_job(self, job: Job) -> None:
        self.updated.append(job)
        self._job = job


def _char(cid: str, name: str) -> JobCharacter:
    return JobCharacter(
        char_id=cid, name=name, source_image_path="/c.png",
        status=CharStatus.APPROVED,
        images=[GeneratedImage(variant_id=f"v{cid}", path="/a.png", prompt="p",
                               scene_id="s1", status=VariantStatus.READY)],
        approved_variant_ids=[f"v{cid}"],
        videos=[VideoVariant(video_id=f"vid{cid}", grok_job_id="g",
                             status=VideoStatus.DONE,
                             source_variant_id=f"v{cid}",
                             final_video_path="/v.mp4")])


def _job() -> Job:
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               movement_prompt="x",
               characters={"cA": _char("cA", "A"), "cB": _char("cB", "B")})


@pytest.fixture
def wired(monkeypatch):
    job = _job()
    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)
    monkeypatch.setattr(type(api.settings), "require_keys",
                        lambda self, *a, **k: None, raising=False)
    return job, store


def test_compile_videos_409_when_char_already_compiling(wired):
    job, store = wired
    job.characters["cA"].compile_status = "compiling"

    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.compile_job_videos(
            "j1", api.CompileVideosBody(), BackgroundTasks()))

    assert ei.value.status_code == 409
    assert "already running" in str(ei.value.detail)
    assert "A" in str(ei.value.detail)          # names the busy character
    assert store.updated == []                  # nothing flipped/persisted
    assert job.characters["cB"].compile_status is None


def test_repurpose_videos_409_when_char_already_repurposing(wired):
    job, store = wired
    job.characters["cB"].repurpose_status = "compiling"

    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.repurpose_job_videos(
            "j1", api.RepurposeVideosBody(), BackgroundTasks()))

    assert ei.value.status_code == 409
    assert "already running" in str(ei.value.detail)
    assert store.updated == []


def test_compile_videos_allows_busy_char_outside_filter(wired):
    """The per-character ↻ retry (char_ids filter) must still work while a
    DIFFERENT character's compile is running — only overlap is refused."""
    job, store = wired
    job.characters["cA"].compile_status = "compiling"

    out = asyncio.run(api.compile_job_videos(
        "j1", api.CompileVideosBody(char_ids=["cB"]), BackgroundTasks()))

    assert out["characters"]["cB"]["compile_status"] == "compiling"
    assert job.characters["cA"].compile_status == "compiling"  # untouched
    assert store.updated                        # cB's flip persisted


def test_compile_videos_409_when_busy_char_inside_filter(wired):
    job, _ = wired
    job.characters["cA"].compile_status = "compiling"
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.compile_job_videos(
            "j1", api.CompileVideosBody(char_ids=["cA"]), BackgroundTasks()))
    assert ei.value.status_code == 409


def test_compile_not_blocked_by_repurpose_in_flight(wired):
    """Compile and repurpose write DIFFERENT output files (<cid>.mp4 vs
    <cid>__repurpose.mp4) with separate status fields — one must not lock
    out the other."""
    job, _ = wired
    job.characters["cA"].repurpose_status = "compiling"

    out = asyncio.run(api.compile_job_videos(
        "j1", api.CompileVideosBody(), BackgroundTasks()))

    assert out["characters"]["cA"]["compile_status"] == "compiling"


def test_compile_allowed_again_after_previous_run_finished(wired):
    job, _ = wired
    job.characters["cA"].compile_status = "done"
    job.characters["cB"].compile_status = "failed"

    out = asyncio.run(api.compile_job_videos(
        "j1", api.CompileVideosBody(), BackgroundTasks()))

    assert out["characters"]["cA"]["compile_status"] == "compiling"
    assert out["characters"]["cB"]["compile_status"] == "compiling"
