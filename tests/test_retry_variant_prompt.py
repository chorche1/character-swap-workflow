"""Retrying a failed variant can override its prompt (edit-and-regenerate).

runner.retry_single_variant(..., prompt="...") swaps the slot's prompt before
re-running so the user can fix the prompt that failed and regenerate in place.
Hermetic: the actual image gen (_generate_one_variant) is stubbed.
"""
from __future__ import annotations

import asyncio

from character_swap import runner
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


def _job_with_failed_variant() -> tuple[Job, JobCharacter]:
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/a.png", status=CharStatus.FAILED,
        images=[GeneratedImage(variant_id="v1", path="/v1.png", prompt="OLD PROMPT",
                               scene_id="s1", status=VariantStatus.FAILED)],
    )
    job = Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
              scene_ids=["s1"], scene_image_paths=["/p.png"], characters={"cA": jc})
    return job, jc


def _stub_runner(monkeypatch, job):
    class _S:
        def get_job(self, jid):
            return job if jid == job.job_id else None
        def update_job(self, j):
            pass
    monkeypatch.setattr(runner, "store", lambda: _S())
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_variant", lambda *a, **k: None)
    async def _noop_emit(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop_emit)
    captured = {}
    async def fake_gen(job_, jc_, target, sem):
        captured["prompt"] = target.prompt
    monkeypatch.setattr(runner, "_generate_one_variant", fake_gen)
    return captured


def test_retry_overrides_prompt(monkeypatch):
    job, jc = _job_with_failed_variant()
    captured = _stub_runner(monkeypatch, job)
    asyncio.run(runner.retry_single_variant("j1", "cA", "v1", prompt="NEW EDITED PROMPT"))
    assert captured["prompt"] == "NEW EDITED PROMPT"   # used for the regen
    assert jc.images[0].prompt == "NEW EDITED PROMPT"  # persisted on the slot


def test_retry_without_prompt_keeps_existing(monkeypatch):
    job, jc = _job_with_failed_variant()
    captured = _stub_runner(monkeypatch, job)
    asyncio.run(runner.retry_single_variant("j1", "cA", "v1"))            # no override
    assert captured["prompt"] == "OLD PROMPT"
    assert jc.images[0].prompt == "OLD PROMPT"
