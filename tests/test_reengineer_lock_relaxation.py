"""Movement-lock relaxation for Reengineer-origin jobs (edit mode).

The Swap flow locks approvals/variant-edits once movement is submitted —
video is the expensive step and re-pointing approvals under it would corrupt
the run. Reengineer EDIT MODE legitimately mutates after videos exist (its
own approval flow gates the expensive work), so four guards are relaxed for
jobs with origin="reengineer:<re_id>" ONLY. Plain Swap jobs (origin=None)
keep the exact locked behavior — pinned here.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, runner
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


def _job(origin: str | None):
    v = GeneratedImage(variant_id="v1", path="/v1.png", prompt="P",
                       scene_id="s1", status=VariantStatus.READY)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/a.png",
                      status=CharStatus.APPROVED, images=[v],
                      approved_variant_ids=["v1"])
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
               scene_ids=["s1"], scene_image_paths=["/p.png"],
               characters={"cA": jc}, origin=origin,
               movement_prompt="animate", movement_prompts={"s1": "animate"})


def _stub_runner(monkeypatch, job):
    class _S:
        def get_job(self, jid):
            return job if jid == "j1" else None

        def update_job(self, j):
            pass
    monkeypatch.setattr(runner, "store", lambda: _S())
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_variant", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    started: list[str] = []

    async def fake_gen(job_, jc_, variant, sem):
        started.append(variant.variant_id)
    monkeypatch.setattr(runner, "_generate_one_variant", fake_gen)
    return started


# ---------------------------------------------------------------- runner

def test_retry_single_variant_relaxed_for_reengineer(monkeypatch):
    job = _job(origin="reengineer:re_x")
    started = _stub_runner(monkeypatch, job)
    asyncio.run(runner.retry_single_variant("j1", "cA", "v1"))
    assert started == ["v1"]


def test_retry_single_variant_still_locked_for_swap(monkeypatch):
    job = _job(origin=None)
    started = _stub_runner(monkeypatch, job)
    asyncio.run(runner.retry_single_variant("j1", "cA", "v1"))
    assert started == []


def test_regen_scene_variants_relaxed_for_reengineer(monkeypatch):
    job = _job(origin="reengineer:re_x")
    started = _stub_runner(monkeypatch, job)
    asyncio.run(runner.regen_scene_variants("j1", "cA", "s1"))
    assert len(started) == 1          # images_per_character=1 placeholder


def test_regen_scene_variants_still_locked_for_swap(monkeypatch):
    job = _job(origin=None)
    started = _stub_runner(monkeypatch, job)
    asyncio.run(runner.regen_scene_variants("j1", "cA", "s1"))
    assert started == []


def test_regen_scene_variants_uses_shared_sem(monkeypatch):
    job = _job(origin="reengineer:re_x")
    seen_sems = []

    class _S:
        def get_job(self, jid):
            return job

        def update_job(self, j):
            pass
    monkeypatch.setattr(runner, "store", lambda: _S())
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)

    async def fake_gen(job_, jc_, variant, sem):
        seen_sems.append(sem)
    monkeypatch.setattr(runner, "_generate_one_variant", fake_gen)

    shared = asyncio.Semaphore(3)
    asyncio.run(runner.regen_scene_variants("j1", "cA", "s1", sem=shared))
    assert seen_sems and all(s is shared for s in seen_sems)


# ---------------------------------------------------------------- api

@pytest.fixture
def api_store(monkeypatch):
    holder = {}

    class _S:
        def get_job(self, jid):
            return holder.get("job") if jid == "j1" else None

        def update_job(self, j):
            pass
    monkeypatch.setattr(api, "store", lambda: _S())
    return holder


def test_approve_relaxed_for_reengineer(api_store):
    job = _job(origin="reengineer:re_x")
    api_store["job"] = job

    body = api.ApproveBody(char_id="cA", action="approve", variant_id="v1")
    bg = BackgroundTasks()
    out = asyncio.run(api.approve("j1", body, bg))
    # Toggle: v1 was approved → now unapproved.
    assert out["characters"]["cA"]["approved_variant_ids"] == []


def test_approve_still_locked_for_swap(api_store):
    job = _job(origin=None)
    api_store["job"] = job
    body = api.ApproveBody(char_id="cA", action="approve", variant_id="v1")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.approve("j1", body, BackgroundTasks()))
    assert e.value.status_code == 409


def test_retry_variant_endpoint_relaxed_for_reengineer(api_store):
    job = _job(origin="reengineer:re_x")
    api_store["job"] = job
    bg = BackgroundTasks()
    asyncio.run(api.retry_variant("j1", "cA", "v1", bg))
    assert len(bg.tasks) == 1


def test_retry_variant_endpoint_still_locked_for_swap(api_store):
    job = _job(origin=None)
    api_store["job"] = job
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.retry_variant("j1", "cA", "v1", BackgroundTasks()))
    assert e.value.status_code == 409
