"""Tests for swap-variant generation: the cross-provider NSFW fallback has been
intentionally REMOVED. `generate_variant` now runs ONLY on the chosen model —
if it refuses on content-policy grounds, that refusal propagates and the variant
fails; the app never silently switches to a different model/provider than the
one the user selected.

We monkeypatch `_dispatch_variant` (the per-model dispatch) so no real API is
hit; the no-fallback behavior is what's under test.
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


def test_content_rejection_propagates_no_cross_provider_switch(monkeypatch):
    """GPT Image refusing on policy grounds must NOT fall back to Gemini/NBP —
    the real refusal surfaces and the variant fails on the chosen model."""
    calls = []
    def fake(*, model, **kw):
        calls.append(model)
        raise _PolicyError()
    monkeypatch.setattr(pipeline, "_dispatch_variant", fake)
    # Even with Gemini configured, there must be no model switch.
    monkeypatch.setattr(type(pipeline.settings), "has_provider", lambda self, p: True)

    with pytest.raises(_PolicyError):
        pipeline.generate_variant(**_args("gpt-image"))
    assert calls == ["gpt-image"]   # chosen model only — never nano-banana-pro


def test_non_content_error_propagates(monkeypatch):
    calls = []
    def fake(*, model, **kw):
        calls.append(model)
        raise RuntimeError("connection reset by peer")
    monkeypatch.setattr(pipeline, "_dispatch_variant", fake)
    monkeypatch.setattr(type(pipeline.settings), "has_provider", lambda self, p: True)

    with pytest.raises(RuntimeError, match="connection reset"):
        pipeline.generate_variant(**_args("gpt-image"))
    assert calls == ["gpt-image"]


def test_happy_path_uses_chosen_model_only(monkeypatch):
    calls = []
    def fake(*, model, **kw):
        calls.append(model)
        return Path("/out.png")
    monkeypatch.setattr(pipeline, "_dispatch_variant", fake)
    monkeypatch.setattr(type(pipeline.settings), "has_provider", lambda self, p: True)

    out = pipeline.generate_variant(**_args("gpt-image"))
    assert out == Path("/out.png")
    assert calls == ["gpt-image"]
