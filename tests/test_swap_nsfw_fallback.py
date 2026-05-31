"""Tests for the swap variant's third-stage NSFW recovery: when the chosen
image model keeps refusing a prompt on content-policy grounds (after the
client's own prompt-softening), `generate_variant` re-runs the variant on
Nano Banana Pro — a different moderation backend — provided Gemini is
configured and we're not already on it.

We monkeypatch `_dispatch_variant` (the per-model dispatch) so no real API is
hit; the fallback orchestration is what's under test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap import pipeline


class _PolicyError(Exception):
    """Looks like a provider content-policy refusal."""
    def __init__(self):
        super().__init__("Your request was rejected by the safety system (content policy).")


def _args(model: str) -> dict:
    return dict(
        model=model, scene_image=Path("/s.png"), character_image=Path("/c.png"),
        character_name="X", prompt="a man on a beach", dest=Path("/out.png"),
        job_id="j1",
    )


def test_falls_back_to_nano_banana_pro_on_persistent_rejection(monkeypatch):
    calls = []
    def fake(*, model, **kw):
        calls.append(model)
        if model == "gpt-image":
            raise _PolicyError()
        return Path("/out.png")   # NBP succeeds
    monkeypatch.setattr(pipeline, "_dispatch_variant", fake)
    monkeypatch.setattr(type(pipeline.settings), "has_provider", lambda self, p: p == "gemini")

    out = pipeline.generate_variant(**_args("gpt-image"))
    assert out == Path("/out.png")
    assert calls == ["gpt-image", "nano-banana-pro"]   # tried original, then fell back


def test_no_fallback_when_already_nano_banana_pro(monkeypatch):
    calls = []
    def fake(*, model, **kw):
        calls.append(model)
        raise _PolicyError()
    monkeypatch.setattr(pipeline, "_dispatch_variant", fake)
    monkeypatch.setattr(type(pipeline.settings), "has_provider", lambda self, p: True)

    with pytest.raises(_PolicyError):
        pipeline.generate_variant(**_args("nano-banana-pro"))
    assert calls == ["nano-banana-pro"]   # no recursion onto itself


def test_no_fallback_when_gemini_not_configured(monkeypatch):
    calls = []
    def fake(*, model, **kw):
        calls.append(model)
        raise _PolicyError()
    monkeypatch.setattr(pipeline, "_dispatch_variant", fake)
    monkeypatch.setattr(type(pipeline.settings), "has_provider", lambda self, p: False)

    with pytest.raises(_PolicyError):
        pipeline.generate_variant(**_args("gpt-image"))
    assert calls == ["gpt-image"]   # couldn't fall back — Gemini key missing


def test_non_content_error_propagates_without_fallback(monkeypatch):
    calls = []
    def fake(*, model, **kw):
        calls.append(model)
        raise RuntimeError("connection reset by peer")
    monkeypatch.setattr(pipeline, "_dispatch_variant", fake)
    monkeypatch.setattr(type(pipeline.settings), "has_provider", lambda self, p: True)

    with pytest.raises(RuntimeError, match="connection reset"):
        pipeline.generate_variant(**_args("gpt-image"))
    assert calls == ["gpt-image"]   # network error ≠ content block → no model switch


def test_happy_path_no_rejection_uses_chosen_model_only(monkeypatch):
    calls = []
    def fake(*, model, **kw):
        calls.append(model)
        return Path("/out.png")
    monkeypatch.setattr(pipeline, "_dispatch_variant", fake)
    monkeypatch.setattr(type(pipeline.settings), "has_provider", lambda self, p: True)

    out = pipeline.generate_variant(**_args("gpt-image"))
    assert out == Path("/out.png")
    assert calls == ["gpt-image"]   # accepted first try — no fallback
