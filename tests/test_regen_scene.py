"""Per-scene regeneration: runner.regen_scene_variants(job, char, scene) adds
fresh variants for ONE (character, scene) pair WITHOUT wiping the character's
other scenes or its approvals.

Primary use: recover a scene whose variants were all deleted (the UI shows it
as "0 variants") with one click, instead of regenerating every scene.

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


def _job_two_scenes_one_deleted() -> tuple[Job, JobCharacter]:
    # Scene s1 has a kept+approved variant; scene s2 has NONE (deleted).
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/a.png",
        status=CharStatus.APPROVED,
        images=[GeneratedImage(variant_id="v1", path="/v1.png", prompt="P1",
                               scene_id="s1", status=VariantStatus.READY)],
        approved_variant_ids=["v1"], approved_variant_id="v1",
    )
    job = Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p1.png",
              scene_ids=["s1", "s2"], scene_image_paths=["/p1.png", "/p2.png"],
              images_per_character=2, prompt="JOB PROMPT", characters={"cA": jc})
    return job, jc


def _stub_runner(monkeypatch, job):
    class _S:
        def get_job(self, jid):
            return job if jid == job.job_id else None
        def update_job(self, j):
            pass
    monkeypatch.setattr(runner, "store", lambda: _S())
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    async def _noop_emit(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop_emit)
    gen_calls = []
    async def fake_gen(job_, jc_, target, sem):
        gen_calls.append(target)
        target.status = VariantStatus.READY
    monkeypatch.setattr(runner, "_generate_one_variant", fake_gen)
    return gen_calls


def test_regen_adds_n_variants_for_target_scene_only(monkeypatch):
    job, jc = _job_two_scenes_one_deleted()
    gen_calls = _stub_runner(monkeypatch, job)

    asyncio.run(runner.regen_scene_variants("j1", "cA", "s2"))

    # Two new variants (images_per_character=2) for s2 were generated...
    assert len(gen_calls) == 2
    assert all(v.scene_id == "s2" for v in gen_calls)
    # ...and the original s1 variant is untouched, approvals preserved.
    s1 = [v for v in jc.images if v.scene_id == "s1"]
    s2 = [v for v in jc.images if v.scene_id == "s2"]
    assert len(s1) == 1 and s1[0].variant_id == "v1"
    assert len(s2) == 2
    assert jc.approved_variant_ids == ["v1"]


def test_regen_uses_job_prompt_when_no_override(monkeypatch):
    job, jc = _job_two_scenes_one_deleted()
    gen_calls = _stub_runner(monkeypatch, job)
    asyncio.run(runner.regen_scene_variants("j1", "cA", "s2"))
    assert all(v.prompt == "JOB PROMPT" for v in gen_calls)


def test_regen_prompt_override_wins(monkeypatch):
    job, jc = _job_two_scenes_one_deleted()
    gen_calls = _stub_runner(monkeypatch, job)
    asyncio.run(runner.regen_scene_variants("j1", "cA", "s2", prompt="CUSTOM"))
    assert all(v.prompt == "CUSTOM" for v in gen_calls)


def test_regen_refuses_after_movement_submitted(monkeypatch):
    job, jc = _job_two_scenes_one_deleted()
    job.movement_prompt = "pan left"   # gen flow locked
    gen_calls = _stub_runner(monkeypatch, job)
    asyncio.run(runner.regen_scene_variants("j1", "cA", "s2"))
    assert gen_calls == []                       # nothing generated
    assert all(v.scene_id == "s1" for v in jc.images)  # no new s2 variants


def test_regen_ignores_unknown_scene(monkeypatch):
    job, jc = _job_two_scenes_one_deleted()
    gen_calls = _stub_runner(monkeypatch, job)
    asyncio.run(runner.regen_scene_variants("j1", "cA", "does-not-exist"))
    assert gen_calls == []
    assert len(jc.images) == 1                   # untouched
