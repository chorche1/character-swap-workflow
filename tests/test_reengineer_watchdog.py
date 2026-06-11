"""Progress-based swap-phase watchdog (replaces the fixed 30-min deadline).

The old `_SWAP_PHASE_TIMEOUT_SECS = 30 min` fired BELOW the realistic duration
of large gpt-image runs (128s median/call → any run > ~27 slots breached it
with zero failures), marked the run failed, and never cancelled the swap task
— generation kept going and billing. The new watchdog only fails a run when
NOTHING has progressed (no terminal flips, no qc_attempts bumps) for
settings.swap_stall_timeout_secs, cancels the supplied tasks, and marks
in-flight variants failed so the UI isn't stuck on skeletons forever.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import runner_reengineer
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


def _job(*variants: GeneratedImage) -> Job:
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/a.png",
                      status=CharStatus.GENERATING, images=list(variants))
    return Job(job_id="j1", title="t", scene_id="s1",
               scene_image_path="/p.png", characters={"cA": jc})


def _variant(vid: str, status: VariantStatus, qc_attempts: int = 1) -> GeneratedImage:
    return GeneratedImage(variant_id=vid, path=f"/{vid}.png", prompt="p",
                          scene_id="s1", status=status, qc_attempts=qc_attempts)


class _Store:
    def __init__(self, job):
        self.job = job
        self.updated = []

    def get_job(self, jid):
        return self.job if jid == self.job.job_id else None

    def update_job(self, job):
        self.updated.append(job)


def _wire(monkeypatch, job, *, stall=0.15, ceiling=999.0):
    """Fast poll + tiny stall window + fake store/state plumbing."""
    store = _Store(job)
    updates: dict = {}
    monkeypatch.setattr(runner_reengineer, "_POLL_SECS", 0.01)
    monkeypatch.setattr(runner_reengineer, "store", lambda: store)
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.update(kw))
    monkeypatch.setattr(type(settings), "swap_stall_timeout_secs",
                        property(lambda self: stall), raising=False)
    monkeypatch.setattr(type(settings), "swap_phase_max_secs",
                        property(lambda self: ceiling), raising=False)
    return store, updates


async def _hang():
    await asyncio.sleep(3600)


def test_stall_cancels_tasks_and_fails_run(monkeypatch):
    job = _job(_variant("v1", VariantStatus.GENERATING))
    store, updates = _wire(monkeypatch, job, stall=0.05)

    async def run():
        hung = asyncio.create_task(_hang())
        await runner_reengineer._watch_swap_phase("re_1", "j1", tasks=[hung])
        await asyncio.sleep(0)          # let the cancellation land
        return hung

    hung = asyncio.run(run())
    assert hung.cancelled()
    assert updates["status"] == "failed"
    assert "stalled" in updates["error"]
    # In-flight slot flipped to failed + persisted, so the run is reviewable.
    assert job.characters["cA"].images[0].status == VariantStatus.FAILED
    assert "stalled" in job.characters["cA"].images[0].error
    assert store.updated


def test_progress_resets_stall_clock(monkeypatch):
    """A slot flipping terminal partway through must reset the stall window;
    the watcher then exits normally once everything is terminal."""
    v1 = _variant("v1", VariantStatus.READY)
    v2 = _variant("v2", VariantStatus.GENERATING)
    job = _job(v1, v2)
    _, updates = _wire(monkeypatch, job, stall=0.15)

    async def run():
        async def finish_later():
            await asyncio.sleep(0.10)   # inside the stall window
            v2.qc_attempts = 2          # progress signal 1: QC retry bump
            await asyncio.sleep(0.10)   # would have stalled without the bump
            v2.status = VariantStatus.READY
        side = asyncio.create_task(finish_later())
        await runner_reengineer._watch_swap_phase("re_1", "j1")
        await side

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    # Exited via the terminal break — never marked failed.
    assert updates.get("status") != "failed"


def test_qc_attempt_bump_counts_as_progress(monkeypatch):
    """qc_attempts incrementing (the runner bumps it in place before each
    generation attempt) must keep the watchdog quiet even when no slot has
    reached a terminal state yet."""
    v = _variant("v1", VariantStatus.GENERATING)
    job = _job(v)
    _, updates = _wire(monkeypatch, job, stall=0.12)

    async def run():
        async def keep_bumping():
            for _ in range(4):
                await asyncio.sleep(0.06)
                v.qc_attempts += 1
            v.status = VariantStatus.READY
        side = asyncio.create_task(keep_bumping())
        await runner_reengineer._watch_swap_phase("re_1", "j1")
        await side

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert updates.get("status") != "failed"


def test_hard_ceiling_fires_even_with_progress(monkeypatch):
    v = _variant("v1", VariantStatus.GENERATING)
    job = _job(v)
    _, updates = _wire(monkeypatch, job, stall=999.0, ceiling=0.08)

    async def run():
        async def keep_bumping():
            while True:
                await asyncio.sleep(0.02)
                v.qc_attempts += 1
        side = asyncio.create_task(keep_bumping())
        try:
            await runner_reengineer._watch_swap_phase("re_1", "j1")
        finally:
            side.cancel()

    asyncio.run(asyncio.wait_for(run(), timeout=5))
    assert updates["status"] == "failed"
    assert "ceiling" in updates["error"]


def test_lost_job_still_fails_fast(monkeypatch):
    job = _job(_variant("v1", VariantStatus.GENERATING))
    store, updates = _wire(monkeypatch, job)
    store.job = Job(job_id="other", title="t", scene_id="s1",
                    scene_image_path="/p.png", characters={})
    asyncio.run(asyncio.wait_for(
        runner_reengineer._watch_swap_phase("re_1", "j1"), timeout=5))
    assert updates["status"] == "failed"
    assert "disappeared" in updates["error"]
