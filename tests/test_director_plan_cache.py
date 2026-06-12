"""Director-plan cache versioning (backlog #1, 2026-06-12).

Cached SwapDirectorPlans are stamped with `prompt_fingerprint()` — a hash of
prompt_director.py's source. Plans written by an OLDER prompt generation
(including pre-versioning plans where prompt_version is None) are treated as
absent: `runner._parse_director_plan` returns None so regens fall back to the
CURRENT template chain instead of stale Director prompts, and
`runner._maybe_run_director_swap` re-plans + overwrites the cache instead of
short-circuiting. Observed motivation: re_42d1dc8938 / re_10fe66db8b regens
kept resurfacing drift modes the prompt upgrades had already fixed, because
reject/regen reused plans cached by the old prompt logic.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from character_swap import prompt_director, runner
from character_swap.models import CharStatus, Job, JobCharacter


def _plan() -> prompt_director.SwapDirectorPlan:
    return prompt_director.plan_from_scene_prompts(
        "swap_only", {"s1": "tailored prompt"}, [("cA", "A")])


def _job(director_json: str | None) -> Job:
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/char.png",
                      status=CharStatus.QUEUED, images=[])
    return Job(job_id="j1", title="t", scene_id="s1",
               scene_image_path="/scene.png", scene_ids=["s1"],
               scene_image_paths=["/scene.png"], characters={"cA": jc},
               use_director=True, director_prompts_json=director_json)


def test_plans_are_stamped_with_current_fingerprint():
    plan = _plan()
    fp = prompt_director.prompt_fingerprint()
    assert plan.prompt_version == fp
    assert len(fp) == 16
    int(fp, 16)  # hex


def test_fresh_plan_parses():
    job = _job(_plan().model_dump_json())
    parsed = runner._parse_director_plan(job)
    assert parsed is not None
    assert parsed.lookup("cA", "s1") == ["tailored prompt"]


def test_stale_version_is_treated_as_absent():
    plan = _plan()
    plan.prompt_version = "0" * 16
    assert runner._parse_director_plan(_job(plan.model_dump_json())) is None


def test_legacy_plan_without_version_is_treated_as_absent():
    plan = _plan()
    plan.prompt_version = None
    legacy_json = plan.model_dump_json()
    assert runner._parse_director_plan(_job(legacy_json)) is None


def _run_maybe(job, monkeypatch, direct_swap):
    monkeypatch.setattr(prompt_director, "direct_swap", direct_swap)

    async def fake_emit(job_id, kind, **kw):
        pass
    monkeypatch.setattr(runner, "_emit", fake_emit)
    saved = []
    s = SimpleNamespace(update_job=lambda j: saved.append(j))
    asyncio.run(runner._maybe_run_director_swap(job, s))
    return saved


def test_maybe_run_director_replans_stale_cache(monkeypatch):
    stale = _plan()
    stale.prompt_version = "0" * 16
    job = _job(stale.model_dump_json())
    calls = []

    def fake_direct_swap(**kw):
        calls.append(kw)
        return _plan()

    saved = _run_maybe(job, monkeypatch, fake_direct_swap)
    assert len(calls) == 1                      # re-planned (one re-bill)
    assert saved == [job]                       # overwrite persisted
    refreshed = runner._parse_director_plan(job)
    assert refreshed is not None
    assert refreshed.prompt_version == prompt_director.prompt_fingerprint()


def test_maybe_run_director_short_circuits_fresh_cache(monkeypatch):
    job = _job(_plan().model_dump_json())
    before = job.director_prompts_json

    def boom(**kw):
        raise AssertionError("fresh cached plan must never re-bill")

    _run_maybe(job, monkeypatch, boom)
    assert job.director_prompts_json == before
