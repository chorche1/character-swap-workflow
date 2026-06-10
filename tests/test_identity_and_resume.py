"""2026-06-11 forensics fixes: identity hardening, the GPT identity-first
engine, and reengineer crash-resume.

Background: a 6-char × 9-scene run combining background replacement + custom
outfit produced wrong-person images at a high rate (no QC existed yet), and a
server restart killed one character's 9/9 slots ("interrupted (server
restart)") while the run's in-process watcher died, leaving it stuck.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import pipeline, runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


# ------------------------------------------------------------- prompt hardening

def test_identity_likeness_clause_in_every_mode():
    for mode, text in [("scene", None), ("character", None), ("custom", "a red hoodie")]:
        for bg in (False, True):
            p = pipeline.build_edit_swap_prompt(mode, text, background=bg)
            assert "recognizable likeness" in p
            assert "single most important requirement" in p


# ------------------------------------------------------------- gpt2-id-swap

def test_gpt_prompt_flipped_roles_and_compact():
    p = pipeline.build_gpt_id_swap_prompt("scene")
    assert p.startswith("Image 1 is only the identity reference")
    assert "Image 2 is the fixed master scene" in p
    assert "recognizable likeness of the person in Image 1" in p
    assert "outfit from Image 2" in p           # outfit comes from the SCENE (now Image 2)
    # Compact: the long constraints block must NOT be present (hurts GPT).
    assert "Constraints — do not violate" not in p
    # Organic anti-produced styling present (Hugo: NBP look, not GPT look).
    assert "no studio lighting" in p and "unedited iPhone photo" in p


def test_gpt_prompt_background_mode():
    p = pipeline.build_gpt_id_swap_prompt("custom", "a white linen shirt",
                                          background=True)
    assert "Image 3 is the NEW ENVIRONMENT" in p
    assert "a white linen shirt" in p
    assert "relight the person" in p


def test_flip_image_roles_swaps_1_and_2_keeps_3():
    src = "Keep Image 1's pose. Identity from Image 2. Image 3 is the environment. Image 1 again."
    out = pipeline._flip_image_roles(src)
    assert out == "Keep Image 2's pose. Identity from Image 1. Image 3 is the environment. Image 2 again."


def test_gpt2_dispatch_flips_refs_and_rebuilds_stock_prompt(monkeypatch, tmp_path):
    seen = {}
    def fake_generate(**kw):
        seen.update(kw)
        return b"png"
    monkeypatch.setattr(pipeline.openai_image, "generate", fake_generate)
    scene = tmp_path / "scene.png"; scene.write_bytes(b"s")
    char = tmp_path / "char.png"; char.write_bytes(b"c")
    bg = tmp_path / "bg.png"; bg.write_bytes(b"b")
    dest = tmp_path / "out.png"

    # Stock prompt (built for [scene, char] roles) → rebuilt in flipped form.
    stock = pipeline.build_edit_swap_prompt("custom", "a white linen shirt",
                                            background=True)
    pipeline._dispatch_variant(
        model="gpt2-id-swap", scene_image=scene, character_image=char,
        character_name="X", prompt=stock, dest=dest, job_id="j1",
        extra_reference_image=bg, outfit_mode="custom",
        outfit_text="a white linen shirt",
    )
    assert seen["reference_images"] == [char, scene, bg]   # FLIPPED + bg last
    assert seen["prompt"].startswith("Image 1 is only the identity reference")
    assert "a white linen shirt" in seen["prompt"]
    assert dest.read_bytes() == b"png"


def test_gpt2_dispatch_mechanically_flips_custom_prompt(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(pipeline.openai_image, "generate",
                        lambda **kw: seen.update(kw) or b"png")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    char = tmp_path / "c.png"; char.write_bytes(b"c")
    pipeline._dispatch_variant(
        model="gpt2-id-swap", scene_image=scene, character_image=char,
        character_name="X", prompt="My rule: pose from Image 1, face from Image 2.",
        dest=tmp_path / "o.png", job_id=None,
    )
    assert seen["prompt"] == "My rule: pose from Image 2, face from Image 1."


# ------------------------------------------------------------- crash resume

def _state(re_id, status, job_id="j1"):
    return {"re_id": re_id, "status": status, "job_id": job_id}


def test_resume_all_dispatches_by_status(monkeypatch):
    spawned: list[str] = []
    monkeypatch.setattr(runner_reengineer, "_spawn",
                        lambda coro, name: (spawned.append(name), coro.close()))
    monkeypatch.setattr(runner_reengineer.reengineer, "list_states", lambda: [
        _state("re_a", "swapping"),
        _state("re_b", "animating"),
        _state("re_c", "assembling"),
        _state("re_d", "awaiting_approval"),   # user gate — untouched
        _state("re_e", "done"),                # terminal — untouched
        _state("re_f", "analyzing"),
    ])
    asyncio.run(runner_reengineer.resume_all())
    assert sorted(spawned) == sorted([
        "reengineer-resume-re_a", "reengineer-resume-re_b",
        "reengineer-resume-re_c", "reengineer-resume-re_f",
    ])


def test_resume_swapping_retries_interrupted_slots(monkeypatch, tmp_path):
    """Slots killed by a restart (error='interrupted (server restart)') are
    auto-retried; other failures are left for the user's ↻."""
    v_int = GeneratedImage(variant_id="v1", path="/x.png", prompt="p",
                           scene_id="s1", status=VariantStatus.FAILED,
                           error="interrupted (server restart)")
    v_real = GeneratedImage(variant_id="v2", path="/y.png", prompt="p",
                            scene_id="s2", status=VariantStatus.FAILED,
                            error="BadRequestError: rejected by safety")
    v_ok = GeneratedImage(variant_id="v3", path="/z.png", prompt="p",
                          scene_id="s3", status=VariantStatus.READY)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/a.png",
                      status=CharStatus.GENERATING, images=[v_int, v_real, v_ok])
    job = Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
              characters={"cA": jc})

    class _S:
        def get_job(self, jid): return job if jid == "j1" else None
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    retried: list[str] = []
    async def fake_retry(job_id, cid, vid, prompt=None):
        retried.append(vid)
    monkeypatch.setattr(runner_reengineer.runner, "retry_single_variant", fake_retry)
    monkeypatch.setattr(runner_reengineer, "_spawn",
                        lambda coro, name: asyncio.get_event_loop().create_task(coro))
    async def fake_watch(re_id, job_id): return None
    monkeypatch.setattr(runner_reengineer, "_watch_swap_phase", fake_watch)

    async def run():
        await runner_reengineer._resume_swapping("re_a", _state("re_a", "swapping"))
        await asyncio.sleep(0)              # let spawned retries run
    asyncio.run(run())

    assert retried == ["v1"]                # interrupted only — not v2/v3


def test_resume_swapping_lost_job_fails_run(monkeypatch):
    class _S:
        def get_job(self, jid): return None
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    updates = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.update(kw))
    asyncio.run(runner_reengineer._resume_swapping("re_x", _state("re_x", "swapping")))
    assert updates["status"] == "failed"
