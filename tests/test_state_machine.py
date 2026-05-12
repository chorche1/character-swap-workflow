"""Unit tests for runner._maybe_complete_char state machine."""
from __future__ import annotations

import pytest

from character_swap import runner
from character_swap.models import (
    CharStatus,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
)


@pytest.fixture
def fake_store(monkeypatch):
    """Replace runner.store() with an in-memory no-op so _persist doesn't touch disk."""

    class _Fake:
        def __init__(self):
            self.calls: list[Job] = []

        def update_job(self, job: Job) -> None:
            self.calls.append(job)

    fake = _Fake()
    monkeypatch.setattr(runner, "store", lambda: fake)
    return fake


def _make_jc(*statuses: VideoStatus, char_status: CharStatus = CharStatus.ANIMATING) -> JobCharacter:
    videos = [
        VideoVariant(video_id=f"v_{i}", grok_job_id=f"g_{i}", status=s)
        for i, s in enumerate(statuses)
    ]
    return JobCharacter(
        char_id="ch_1",
        name="Alex",
        source_image_path="/tmp/x.png",
        status=char_status,
        videos=videos,
    )


def _make_job(jc: JobCharacter) -> Job:
    return Job(
        job_id="j_1",
        scene_id="sc_1",
        scene_image_path="/tmp/scene.png",
        characters={jc.char_id: jc},
    )


def test_no_videos_is_noop(fake_store):
    jc = _make_jc()  # no videos
    job = _make_job(jc)
    runner._maybe_complete_char(job, jc)
    assert jc.status == CharStatus.ANIMATING  # unchanged
    assert fake_store.calls == []


def test_all_done_marks_char_done(fake_store):
    jc = _make_jc(VideoStatus.DONE, VideoStatus.DONE)
    job = _make_job(jc)
    runner._maybe_complete_char(job, jc)
    assert jc.status == CharStatus.DONE
    assert len(fake_store.calls) == 1


def test_all_failed_marks_char_failed(fake_store):
    jc = _make_jc(VideoStatus.FAILED, VideoStatus.ERROR)
    job = _make_job(jc)
    runner._maybe_complete_char(job, jc)
    assert jc.status == CharStatus.FAILED


def test_mixed_done_and_failed_marks_char_done(fake_store):
    """Any successful video means the character is considered DONE."""
    jc = _make_jc(VideoStatus.DONE, VideoStatus.FAILED, VideoStatus.ERROR)
    job = _make_job(jc)
    runner._maybe_complete_char(job, jc)
    assert jc.status == CharStatus.DONE


def test_still_in_flight_does_not_complete(fake_store):
    """One PROCESSING video keeps the character ANIMATING."""
    jc = _make_jc(VideoStatus.DONE, VideoStatus.PROCESSING)
    job = _make_job(jc)
    runner._maybe_complete_char(job, jc)
    assert jc.status == CharStatus.ANIMATING
    assert fake_store.calls == []


def test_pending_blocks_completion(fake_store):
    jc = _make_jc(VideoStatus.FAILED, VideoStatus.PENDING)
    job = _make_job(jc)
    runner._maybe_complete_char(job, jc)
    assert jc.status == CharStatus.ANIMATING
