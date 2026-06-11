"""Two video-lifecycle bugs found 2026-06-11 when a server restart at 21:56
stranded 16 of 45 Kling clips in reengineer run re_345deead2e:

1. RESUME GAP — `runner.resume_pending` only resumed in-flight videos that
   already had a `grok_job_id`. Placeholders created by `_animate_character`
   whose submit never ran before the restart sat `pending` forever, so the
   reengineer watcher never saw all-terminal and timed the run out. Fix
   (Hugo's pick 2026-06-12): mark them FAILED on resume — no auto-resubmit
   (no billing without a click); the "↻ retry all failed" button recovers
   them in one go.

2. ORPHAN ROWS — `runner.retry_one_video` replaces `jc.videos[idx]` in place
   with a fresh video_id, then persisted via the granular SQLite fast path,
   which upserts by video_id and CANNOT delete the old row. On the next
   restart the orphan resurrected as a ghost video (observed: 61 rows for a
   45-video job). Fix: id-replacement persists structurally (full
   `update_job` = DELETE+reinsert of children).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

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
from character_swap.state import SqliteStateStore


def _job(videos: list[VideoVariant], *,
         char_status: CharStatus = CharStatus.ANIMATING) -> Job:
    v1 = GeneratedImage(variant_id="v1", path="/v1.png", prompt="P",
                        scene_id="s1", status=VariantStatus.READY)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/a.png",
                      status=char_status, images=[v1], videos=videos,
                      approved_variant_ids=["v1"], approved_variant_id="v1")
    return Job(job_id="j1", title="t", scene_id="s1",
               scene_image_path="/scene.png", movement_prompt="walks away",
               movement_prompts={"s1": "walks away"}, characters={"cA": jc})


class _FakeStore:
    """update_job-only store (like the JSON backend) — _persist falls back
    to the full update path on it."""

    def __init__(self, job: Job):
        self.job = job
        self.updates = 0

    def get_job(self, jid):
        return self.job if jid == self.job.job_id else None

    def update_job(self, job):
        self.job = job
        self.updates += 1


# --- bug 1: resume gap ------------------------------------------------------------------


def test_resume_marks_stranded_pending_videos_failed(monkeypatch):
    """A pending video with an empty grok_job_id (submit never reached the
    provider before the restart) must flip to FAILED — not sit pending
    forever blocking _videos_terminal."""
    stranded = VideoVariant(video_id="vd_gap", grok_job_id="",
                            status=VideoStatus.PENDING, source_variant_id="v1")
    inflight = VideoVariant(video_id="vd_fly", grok_job_id="g2",
                            status=VideoStatus.PROCESSING, source_variant_id="v1")
    job = _job([stranded, inflight])
    fake = _FakeStore(job)
    monkeypatch.setattr(runner, "store", lambda: fake)

    resumed: list[str] = []

    async def fake_resume(j, jc, v):
        resumed.append(v.video_id)

    monkeypatch.setattr(runner, "_resume_video", fake_resume)

    async def main():
        await runner.resume_pending("j1")
        await asyncio.sleep(0)  # let create_task'd resumers tick

    asyncio.run(main())

    assert stranded.status == VideoStatus.FAILED
    assert "interrupted" in (stranded.error or "")
    assert fake.updates >= 1
    # The in-flight one is untouched by the marking pass and re-polled.
    assert inflight.status == VideoStatus.PROCESSING
    assert resumed == ["vd_fly"]
    # One clip still in flight → char must NOT be closed out yet.
    assert job.characters["cA"].status == CharStatus.ANIMATING


def test_resume_closes_out_char_when_only_stranded_remained(monkeypatch):
    """re_345deead2e shape: every non-done slot was stranded. After marking
    them failed the char's videos are all terminal — the char must leave
    `animating` (here → DONE since a clip succeeded) so watchers see
    all-terminal instead of timing out."""
    done = VideoVariant(video_id="vd_ok", grok_job_id="g1",
                        status=VideoStatus.DONE, source_variant_id="v1")
    stranded = VideoVariant(video_id="vd_gap", grok_job_id="",
                            status=VideoStatus.PENDING, source_variant_id="v1")
    job = _job([done, stranded])
    fake = _FakeStore(job)
    monkeypatch.setattr(runner, "store", lambda: fake)

    asyncio.run(runner.resume_pending("j1"))

    assert stranded.status == VideoStatus.FAILED
    assert job.characters["cA"].status == CharStatus.DONE


def test_resume_leaves_terminal_videos_alone(monkeypatch):
    """DONE/FAILED clips (terminal, with or without a provider id) must not
    be touched by the stranded-marking pass."""
    done = VideoVariant(video_id="vd_ok", grok_job_id="g1",
                        status=VideoStatus.DONE, source_variant_id="v1")
    failed = VideoVariant(video_id="vd_no", grok_job_id="",
                          status=VideoStatus.FAILED, error="boom",
                          source_variant_id="v1")
    job = _job([done, failed], char_status=CharStatus.DONE)
    fake = _FakeStore(job)
    monkeypatch.setattr(runner, "store", lambda: fake)

    asyncio.run(runner.resume_pending("j1"))

    assert done.status == VideoStatus.DONE
    assert failed.error == "boom"          # not overwritten with "interrupted"
    assert fake.updates == 0               # nothing dirty → no write


# --- bug 2: orphan rows -----------------------------------------------------------------


@pytest.fixture
def sqlite_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


def test_retry_one_video_leaves_no_orphan_row_in_sqlite(sqlite_db_path, monkeypatch):
    """Retry a video on a SQLite-backed store, reload the job from a FRESH
    connection: the video count must be unchanged and the old video_id gone.
    Pre-fix, the granular fast path left the old row behind and it
    resurrected as a ghost pending video after restart."""
    s1 = SqliteStateStore(db_path=sqlite_db_path)
    old = VideoVariant(video_id="vd_old", grok_job_id="g1",
                       status=VideoStatus.FAILED, error="boom",
                       source_variant_id="v1")
    keep = VideoVariant(video_id="vd_keep", grok_job_id="g2",
                        status=VideoStatus.DONE, source_variant_id="v1")
    job = _job([old, keep])
    s1.add_job(job)
    monkeypatch.setattr(runner, "store", lambda: s1)

    async def no_animate(*args, **kwargs):
        pass

    monkeypatch.setattr(runner, "_animate_one_video", no_animate)

    asyncio.run(runner.retry_one_video("j1", "cA", "vd_old"))

    s2 = SqliteStateStore(db_path=sqlite_db_path)
    loaded = s2.get_job("j1")
    ids = [v.video_id for v in loaded.characters["cA"].videos]
    assert len(ids) == 2, f"orphan row resurrected: {ids}"
    assert "vd_old" not in ids
    assert "vd_keep" in ids
    fresh_id = next(i for i in ids if i != "vd_keep")
    fresh = next(v for v in loaded.characters["cA"].videos
                 if v.video_id == fresh_id)
    assert fresh.status == VideoStatus.PENDING
    assert fresh.source_variant_id == "v1"


# --- retry all failed (the recovery button) ----------------------------------------------


def test_retry_failed_videos_runner_retries_each_failed_slot(monkeypatch):
    f1 = VideoVariant(video_id="vd_f1", grok_job_id="",
                      status=VideoStatus.FAILED, source_variant_id="v1")
    f2 = VideoVariant(video_id="vd_f2", grok_job_id="g2",
                      status=VideoStatus.ERROR, source_variant_id="v1")
    ok = VideoVariant(video_id="vd_ok", grok_job_id="g3",
                      status=VideoStatus.DONE, source_variant_id="v1")
    busy = VideoVariant(video_id="vd_busy", grok_job_id="g4",
                        status=VideoStatus.PROCESSING, source_variant_id="v1")
    job = _job([f1, f2, ok, busy])
    fake = _FakeStore(job)
    monkeypatch.setattr(runner, "store", lambda: fake)

    retried: list[tuple[str, str]] = []

    async def fake_retry(job_id, char_id, video_id, prompt_override=None):
        retried.append((char_id, video_id))

    monkeypatch.setattr(runner, "retry_one_video", fake_retry)

    asyncio.run(runner.retry_failed_videos("j1"))

    assert sorted(retried) == [("cA", "vd_f1"), ("cA", "vd_f2")]


def test_retry_failed_videos_endpoint_schedules_and_409s_when_clean(monkeypatch):
    failed = VideoVariant(video_id="vd_f1", grok_job_id="",
                          status=VideoStatus.FAILED, source_variant_id="v1")
    job = _job([failed])
    job.video_model = "sora-2"   # not in _VIDEO_MODEL_KEYS → no key required
    fake = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: fake)

    bg = BackgroundTasks()
    out = asyncio.run(api.retry_failed_videos("j1", bg))
    assert out["job_id"] == "j1"
    assert len(bg.tasks) == 1

    # All clips healthy → 409, nothing scheduled.
    failed.status = VideoStatus.DONE
    bg2 = BackgroundTasks()
    with pytest.raises(HTTPException) as exc:
        asyncio.run(api.retry_failed_videos("j1", bg2))
    assert exc.value.status_code == 409
    assert not bg2.tasks
