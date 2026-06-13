"""Director moderation-rescue rewrite (Hugo 2026-06-13).

When the engine's safety system blocks a swap even after the client's
append-only softeners, ONE Claude call looks at the scene and rewords the
prompt — same scene, same visual result, neutral phrasing — and the slot
retries on the SAME engine. Hugo verified the approach by hand in ChatGPT:
a reworded prompt generated the exact scene the original couldn't.

Ladder order: client softeners → Director rewrite (RUNG A, default when
ANTHROPIC_API_KEY is set) → nbp-swap engine switch (RUNG B, opt-in via
SWAP_MODERATION_FALLBACK) → loud fail.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import prompt_director, runner
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)
from character_swap.swap_qc import QCVerdict


class _PolicyError(Exception):
    def __init__(self):
        super().__init__(
            "Your request was rejected by the safety system. "
            "safety_violations=[sexual]. moderation_blocked")


def _job(tmp_path, image_model="gpt-image", origin=None):
    dest = tmp_path / "variant_v1.png"
    v = GeneratedImage(variant_id="v1", path=str(dest), prompt="BASE",
                       scene_id="s1", status=VariantStatus.GENERATING)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/char.png",
                      status=CharStatus.GENERATING, images=[v])
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path="/scene.png", scene_ids=["s1"],
              scene_image_paths=["/scene.png"], characters={"cA": jc},
              image_model=image_model, origin=origin)
    return job, jc, v


def _wire(monkeypatch, gen_behavior, *, rewrite_result="SAFE PROMPT",
          fallback_enabled=False, fal_configured=True):
    monkeypatch.setattr(settings, "swap_moderation_fallback",
                        fallback_enabled, raising=False)
    models_called: list[tuple[str, str]] = []   # (model, prompt)
    events: list[tuple[str, dict]] = []
    rewrites: list[dict] = []

    def fake_gen(*, model, **kw):
        models_called.append((model, kw["prompt"]))
        gen_behavior(model, kw)
    monkeypatch.setattr(runner.pipeline, "generate_variant", fake_gen)
    monkeypatch.setattr(runner.swap_qc, "inspect_variant",
                        lambda **kw: QCVerdict(True, "", ""))
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_variant", lambda *a, **k: None)

    def fake_rewrite(**kw):
        rewrites.append(kw)
        return rewrite_result
    monkeypatch.setattr(prompt_director, "direct_moderation_rewrite",
                        fake_rewrite)

    async def fake_emit(job_id, kind, **kw):
        events.append((kind, kw))
    monkeypatch.setattr(runner, "_emit", fake_emit)
    monkeypatch.setattr(runner, "_scene_path_for_variant",
                        lambda j, v: Path("/scene.png"))
    monkeypatch.setattr(type(settings), "has_provider",
                        lambda self, p: fal_configured if p == "fal" else True)
    return models_called, events, rewrites


def _run(job, jc, v):
    asyncio.run(runner._generate_one_variant(job, jc, v, asyncio.Semaphore(1)))


def test_rewrite_rescues_blocked_slot_on_same_engine(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        if kw["prompt"] == "BASE":
            raise _PolicyError()
        Path(kw["dest"]).write_bytes(b"img")
    models, events, rewrites = _wire(monkeypatch, behavior)

    _run(job, jc, v)
    assert [m for m, _ in models] == ["gpt-image", "gpt-image"]   # no switch
    assert models[1][1] == "SAFE PROMPT"
    assert v.status == VariantStatus.READY
    assert v.moderation_rewritten is True
    assert v.prompt == "SAFE PROMPT"             # persists for ✎↻ / retries
    assert v.fallback_model is None
    kinds = [k for k, _ in events]
    assert "variant.moderation_rewrite" in kinds
    assert "safety system" in dict(events)["variant.moderation_rewrite"]["reason"]
    assert len(rewrites) == 1
    assert "safety system" in rewrites[0]["rejection_reason"]


def test_rewrite_gets_engine_effective_prompt(monkeypatch, tmp_path):
    """Slots store stock templates; the Director must reword the prompt the
    engine actually saw (compact gpt2-id prompt), not the stock string."""
    from character_swap import pipeline
    job, jc, v = _job(tmp_path, image_model="gpt2-id-swap")
    v.prompt = pipeline.GENERATION_PROMPT

    calls = {"n": 0}

    def behavior(model, kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _PolicyError()
        Path(kw["dest"]).write_bytes(b"img")
    models, _, rewrites = _wire(monkeypatch, behavior)

    _run(job, jc, v)
    assert "Do NOT zoom out" in rewrites[0]["current_prompt"]
    assert rewrites[0]["current_prompt"] != pipeline.GENERATION_PROMPT


def test_rewrite_tried_only_once_per_slot(monkeypatch, tmp_path):
    """A slot that stays blocked even after the rewrite fails loudly —
    the Director is never billed twice for one slot."""
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        raise _PolicyError()
    models, events, rewrites = _wire(monkeypatch, behavior)

    _run(job, jc, v)
    assert len(rewrites) == 1
    assert [m for m, _ in models] == ["gpt-image", "gpt-image"]
    assert v.status == VariantStatus.FAILED
    assert "safety system" in (v.error or "")


def test_rewrite_none_falls_through_to_loud_fail(monkeypatch, tmp_path):
    """No ANTHROPIC key (or Director failure) → None → same behavior as
    before the feature: loud fail with the moderation reason."""
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        raise _PolicyError()
    models, events, _ = _wire(monkeypatch, behavior, rewrite_result=None)

    _run(job, jc, v)
    assert [m for m, _ in models] == ["gpt-image"]
    assert v.status == VariantStatus.FAILED
    assert v.moderation_rewritten is False
    assert "variant.moderation_rewrite" not in [k for k, _ in events]


def test_still_blocked_rewrite_then_nbp_when_opted_in(monkeypatch, tmp_path):
    """Ladder order: rewrite first (same engine), THEN the opt-in engine
    switch when the rewrite is also blocked."""
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        if model == "gpt-image":
            raise _PolicyError()
        Path(kw["dest"]).write_bytes(b"img")
    models, events, rewrites = _wire(monkeypatch, behavior,
                                     fallback_enabled=True)

    _run(job, jc, v)
    assert [m for m, _ in models] == ["gpt-image", "gpt-image", "nbp-swap"]
    assert v.status == VariantStatus.READY
    assert v.fallback_model == "nbp-swap"
    assert v.moderation_rewritten is True
    assert len(rewrites) == 1


def test_non_content_errors_never_trigger_rewrite(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        raise RuntimeError("429: rate limited")
    models, _, rewrites = _wire(monkeypatch, behavior)

    _run(job, jc, v)
    assert rewrites == []
    assert v.status == VariantStatus.FAILED


def test_reengineer_jobs_get_camera_gaze_in_rewrite(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path, origin="reengineer:re_t")

    def behavior(model, kw):
        if kw["prompt"] == "BASE":
            raise _PolicyError()
        Path(kw["dest"]).write_bytes(b"img")
    _, _, rewrites = _wire(monkeypatch, behavior)

    _run(job, jc, v)
    assert rewrites[0]["camera_gaze"] is True


# ------------------------------------------------ direct_moderation_rewrite

def _stub_claude(monkeypatch, payload, captured):
    def fake_messages(**kw):
        captured.update(kw)
        return object()
    monkeypatch.setattr(prompt_director.anthropic_client,
                        "messages_with_tools", fake_messages)
    monkeypatch.setattr(prompt_director.anthropic_client, "extract_tool_call",
                        lambda resp, name: payload)
    monkeypatch.setattr(prompt_director.anthropic_client,
                        "_file_to_image_block",
                        lambda p: {"type": "text", "text": f"IMG:{p}"})


def test_direct_moderation_rewrite_strips_and_reappends_clauses(monkeypatch,
                                                                tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    captured = {}
    _stub_claude(monkeypatch, {"prompt": "NEUTRAL WORDING"}, captured)
    cur = ("the man pinches the woman's back fat"
           + prompt_director.ORGANIC_STYLE_CLAUSE)
    out = prompt_director.direct_moderation_rewrite(
        scene_path=frame, current_prompt=cur,
        rejection_reason="moderation_blocked: sexual")
    assert out == "NEUTRAL WORDING" + prompt_director.ORGANIC_STYLE_CLAUSE
    texts = " ".join(b.get("text", "")
                     for b in captured["messages"][0]["content"])
    assert "pinches" in texts                       # Director sees the original
    assert prompt_director.ORGANIC_STYLE_CLAUSE.strip() not in texts
    assert "moderation_blocked" in texts
    assert captured["phase"] == "director_rewrite"


def test_direct_moderation_rewrite_camera_gaze_and_none(monkeypatch, tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    captured = {}
    _stub_claude(monkeypatch, {"prompt": "NEUTRAL"}, captured)
    out = prompt_director.direct_moderation_rewrite(
        scene_path=frame, current_prompt="p", rejection_reason="r",
        camera_gaze=True)
    assert prompt_director.CAMERA_GAZE_SENTENCE in out
    _stub_claude(monkeypatch, None, captured)
    assert prompt_director.direct_moderation_rewrite(
        scene_path=frame, current_prompt="p", rejection_reason="r") is None
