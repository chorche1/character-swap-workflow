"""Runner-level moderation fallback: gpt-image → nbp-swap on content rejection.

The PIPELINE layer deliberately has no cross-provider fallback (see
tests/test_swap_nsfw_fallback.py — a refusal on the chosen model propagates).
The RUNNER layer adds the sanctioned, LOUD exception on top: when the chosen
engine rejects on content-policy grounds (after the client's own softening
ladder), `_generate_one_variant` retries the slot ONCE on the fal-hosted
nbp-swap — recorded on `variant.fallback_model`, emitted as a
`variant.fallback` event, ⇄ chip in the UI. Measured rationale: 49% of
gpt-image-2 swap calls were safety rejections burning ~131s each, while
nbp-swap had 0 moderation failures on the same scenes.

OPT-IN since 2026-06-12 (Hugo's "100% GPT Image 2" directive): the rescue is
gated behind SWAP_MODERATION_FALLBACK (default OFF). By default a rejected
slot FAILS with the moderation reason — no engine switch. The tests below
enable the flag explicitly via _wire(); the default-off behavior has its own
regression test at the bottom.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import runner
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
            "Your request was rejected by the safety system (content policy).")


def _job(tmp_path, image_model="gpt-image"):
    dest = tmp_path / "variant_v1.png"
    v = GeneratedImage(variant_id="v1", path=str(dest), prompt="BASE",
                       scene_id="s1", status=VariantStatus.GENERATING)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/char.png",
                      status=CharStatus.GENERATING, images=[v])
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path="/scene.png", scene_ids=["s1"],
              scene_image_paths=["/scene.png"], characters={"cA": jc},
              image_model=image_model)
    return job, jc, v


def _wire(monkeypatch, gen_behavior, *, fal_configured=True,
          fallback_enabled=True):
    """gen_behavior(model, kw) is called per generation attempt."""
    monkeypatch.setattr(settings, "swap_moderation_fallback",
                        fallback_enabled, raising=False)
    models_called: list[str] = []
    events: list[tuple[str, dict]] = []

    def fake_gen(*, model, **kw):
        models_called.append(model)
        gen_behavior(model, kw)
    monkeypatch.setattr(runner.pipeline, "generate_variant", fake_gen)
    monkeypatch.setattr(runner.swap_qc, "inspect_variant",
                        lambda **kw: QCVerdict(True, "", ""))
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_variant", lambda *a, **k: None)

    async def fake_emit(job_id, kind, **kw):
        events.append((kind, kw))
    monkeypatch.setattr(runner, "_emit", fake_emit)
    monkeypatch.setattr(runner, "_scene_path_for_variant",
                        lambda j, v: Path("/scene.png"))
    monkeypatch.setattr(type(settings), "has_provider",
                        lambda self, p: fal_configured if p == "fal" else True)
    return models_called, events


def _run(job, jc, v):
    asyncio.run(runner._generate_one_variant(job, jc, v, asyncio.Semaphore(1)))


def test_content_rejection_falls_back_to_nbp_swap(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        if model == "gpt-image":
            raise _PolicyError()
        Path(kw["dest"]).write_bytes(b"img")
    models, events = _wire(monkeypatch, behavior)

    _run(job, jc, v)
    assert models == ["gpt-image", "nbp-swap"]
    assert v.status == VariantStatus.READY
    assert v.fallback_model == "nbp-swap"
    kinds = [k for k, _ in events]
    assert "variant.fallback" in kinds
    fb = dict(events)[ "variant.fallback"]
    assert fb["fallback_model"] == "nbp-swap"
    assert "safety system" in fb["reason"]


def test_qc_retries_stay_on_fallback_model(monkeypatch, tmp_path):
    """Once fallen back, QC repair/re-roll attempts run on nbp-swap — not
    back on the refusing engine."""
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        if model == "gpt-image":
            raise _PolicyError()
        Path(kw["dest"]).write_bytes(b"img")
    models, _ = _wire(monkeypatch, behavior)
    verdicts = iter([QCVerdict(False, "broken hand", "fix"),
                     QCVerdict(True, "", "")])
    monkeypatch.setattr(runner.swap_qc, "inspect_variant",
                        lambda **kw: next(verdicts))

    _run(job, jc, v)
    assert v.status == VariantStatus.READY
    assert models == ["gpt-image", "nbp-swap", "nbp-swap"]


def test_no_fallback_without_fal_key(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        raise _PolicyError()
    models, events = _wire(monkeypatch, behavior, fal_configured=False)

    _run(job, jc, v)
    assert models == ["gpt-image"]
    assert v.status == VariantStatus.FAILED
    assert v.fallback_model is None
    assert "variant.fallback" not in [k for k, _ in events]


def test_no_fallback_for_non_content_errors(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        raise RuntimeError("429: rate limited")
    models, _ = _wire(monkeypatch, behavior)

    _run(job, jc, v)
    assert models == ["gpt-image"]
    assert v.status == VariantStatus.FAILED
    assert v.fallback_model is None


def test_no_fallback_when_already_on_fal_engine(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path, image_model="nbp-swap")

    def behavior(model, kw):
        raise _PolicyError()
    models, _ = _wire(monkeypatch, behavior)

    _run(job, jc, v)
    assert models == ["nbp-swap"]
    assert v.status == VariantStatus.FAILED
    assert v.fallback_model is None


def test_fallback_failure_error_names_both_engines(monkeypatch, tmp_path):
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        if model == "gpt-image":
            raise _PolicyError()
        raise RuntimeError("nbp also exploded")
    models, _ = _wire(monkeypatch, behavior)

    _run(job, jc, v)
    assert models == ["gpt-image", "nbp-swap"]
    assert v.status == VariantStatus.FAILED
    assert "fallback(nbp-swap)" in (v.error or "")
    assert "nbp also exploded" in (v.error or "")


def test_default_is_no_fallback_100_percent_gpt(monkeypatch, tmp_path):
    """Hugo's 2026-06-12 directive: with SWAP_MODERATION_FALLBACK unset
    (default OFF), a content rejection FAILS the slot with the moderation
    reason — nbp-swap is never called, no fallback event, no engine switch."""
    job, jc, v = _job(tmp_path)

    def behavior(model, kw):
        raise _PolicyError()
    models, events = _wire(monkeypatch, behavior, fallback_enabled=False)

    _run(job, jc, v)
    assert models == ["gpt-image"]          # exactly one attempt, one engine
    assert v.status == VariantStatus.FAILED
    assert v.fallback_model is None
    assert "safety system" in (v.error or "")
    assert "variant.fallback" not in [k for k, _ in events]


def test_default_off_matches_field_default():
    """The pydantic field default itself is False — guards against a future
    .env or refactor silently re-enabling the cross-engine rescue."""
    from character_swap.config import Settings
    assert Settings.model_fields["swap_moderation_fallback"].default is False
