"""Backlog #25 (2026-06-12): deleting an ACTIVE Reengineer run.

Before: DELETE rmtree'd the run dir, but (a) in-process watchers still held
the state dict and their next save_state() resurrected a ghost state.json,
and (b) the underlying swap job kept generating — and billing — with no
parent. Now: the re_id is tombstoned (all further state writes refuse), the
animation guards are cleared, and the run-owned job's future provider calls
are cancelled (slots fail with 'job cancelled' instead of billing).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import reengineer, runner
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


@pytest.fixture(autouse=True)
def _clean_tombstones():
    reengineer._DELETED_RUNS.clear()
    runner._CANCELLED_JOBS.clear()
    yield
    reengineer._DELETED_RUNS.clear()
    runner._CANCELLED_JOBS.clear()


def test_save_state_refuses_writes_after_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "output_dir", tmp_path, raising=False)
    run_dir = tmp_path / "reengineer" / "re_x"
    run_dir.mkdir(parents=True)
    state = {"re_id": "re_x", "status": "swapping"}
    reengineer.save_state(state)
    assert reengineer.state_path("re_x").exists()

    # DELETE flow: tombstone + rmtree.
    reengineer.mark_deleted("re_x")
    import shutil
    shutil.rmtree(run_dir)

    # A watcher still holding the dict tries to write — must be a no-op.
    state["status"] = "failed"
    reengineer.save_state(state)
    assert not reengineer.state_path("re_x").exists()   # no ghost
    assert reengineer.is_deleted("re_x")


def test_cancelled_job_generates_nothing_and_fails_slot(tmp_path, monkeypatch):
    dest = tmp_path / "variant_v1.png"
    v = GeneratedImage(variant_id="v1", path=str(dest), prompt="p",
                       scene_id="s1", status=VariantStatus.GENERATING)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/char.png",
                      status=CharStatus.GENERATING, images=[v])
    job = Job(job_id="j_dead", title="t", scene_id="s1",
              scene_image_path="/scene.png", scene_ids=["s1"],
              scene_image_paths=["/scene.png"], characters={"cA": jc})

    def never_generate(**kw):
        raise AssertionError("cancelled job must not call the provider")
    monkeypatch.setattr(runner.pipeline, "generate_variant", never_generate)
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_variant", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_scene_path_for_variant",
                        lambda j, vv: Path("/scene.png"))

    async def noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", noop)

    runner.cancel_job_generation("j_dead")
    asyncio.run(runner._generate_one_variant(job, jc, v, asyncio.Semaphore(1)))

    assert v.status == VariantStatus.FAILED
    assert "cancelled" in (v.error or "")
