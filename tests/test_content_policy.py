"""Tests for the NSFW / content-policy auto-softening retry helper.

When an image provider rejects a prompt on moderation grounds, we retry with a
minimally softened prompt instead of failing the slot. These tests cover the
rejection classifier, the (append-only) softener, and the retry loop —
including that NON-content errors fail fast and that the original prompt text
is never mutated, only appended to.
"""
from __future__ import annotations

import pytest

from character_swap import content_policy as cp


# --- is_content_rejection -------------------------------------------------

class _CodedError(Exception):
    """Mimics openai.BadRequestError carrying a machine-readable .code."""
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


@pytest.mark.parametrize("exc", [
    _CodedError("Your request was rejected.", code="content_policy_violation"),
    _CodedError("blocked", code="moderation_blocked"),
    RuntimeError("Gemini blocked this prompt (content policy / safety, reason: IMAGE_SAFETY, model=x)."),
    RuntimeError("Gemini blocked this prompt (content policy / safety, reason: PROHIBITED_CONTENT, model=x)."),
    Exception("Image generation failed (400): Your prompt was flagged by our moderation system."),
    Exception("400: request violates our usage policies"),
    Exception("The content was filtered for NSFW material."),
])
def test_is_content_rejection_true_for_moderation_signals(exc):
    assert cp.is_content_rejection(exc) is True


@pytest.mark.parametrize("exc", [
    Exception("Connection timed out"),
    Exception("429: rate limit exceeded, slow down"),
    _CodedError("not found", code="model_not_found"),
    RuntimeError("Gemini returned no image data for model=x. Response shape: ['usageMetadata']"),
    Exception("401 Unauthorized: invalid API key"),
    ValueError("Unknown image model for swap variant: foo"),
])
def test_is_content_rejection_false_for_other_errors(exc):
    assert cp.is_content_rejection(exc) is False


# --- soften ---------------------------------------------------------------

def test_soften_appends_minimal_clause_first():
    out = cp.soften("a man on a beach", 1)
    assert out.startswith("a man on a beach")
    assert "hypothetical" in out.lower()
    # Original text preserved verbatim, only appended to.
    assert len(out) > len("a man on a beach")


def test_soften_uses_the_single_hypothetical_clause():
    # Only one softener clause exists; every attempt reuses it.
    a1 = cp.soften("x", 1)
    assert a1.startswith("x")
    assert "hypothetical" in a1.lower()
    assert cp.soften("x", 2) == a1


def test_soften_past_last_reuses_strongest():
    last = cp.soften("x", cp.SOFTEN_ATTEMPTS)
    beyond = cp.soften("x", cp.SOFTEN_ATTEMPTS + 5)
    assert beyond == last


def test_soften_attempt_zero_is_noop():
    assert cp.soften("untouched", 0) == "untouched"


# --- generate_with_softening ---------------------------------------------

def _rejection(n: int = 1):
    return _CodedError("safety system rejected this", code="content_policy_violation")


def test_succeeds_first_try_without_softening():
    seen = []
    def call(*, prompt):
        seen.append(prompt)
        return b"IMG"
    out = cp.generate_with_softening(call, prompt="hello")
    assert out == b"IMG"
    assert seen == ["hello"]   # never softened


def test_retries_softened_then_succeeds():
    seen = []
    def call(*, prompt):
        seen.append(prompt)
        if len(seen) == 1:
            raise _rejection()      # original rejected
        return b"OK"
    out = cp.generate_with_softening(call, prompt="a risky scene")
    assert out == b"OK"
    assert len(seen) == 2
    assert seen[0] == "a risky scene"            # first try = verbatim
    assert seen[1].startswith("a risky scene")   # retry = appended softener
    assert seen[1] != seen[0]


def test_non_content_error_fails_fast():
    seen = []
    def call(*, prompt):
        seen.append(prompt)
        raise RuntimeError("429: rate limited")
    with pytest.raises(RuntimeError, match="rate limited"):
        cp.generate_with_softening(call, prompt="hi")
    assert len(seen) == 1   # no softening retries on a non-content error


def test_gives_up_after_all_softeners_and_reraises():
    seen = []
    def call(*, prompt):
        seen.append(prompt)
        raise _rejection()
    with pytest.raises(_CodedError):
        cp.generate_with_softening(call, prompt="nope")
    # original + SOFTEN_ATTEMPTS retries
    assert len(seen) == cp.SOFTEN_ATTEMPTS + 1


def test_passes_through_other_kwargs():
    captured = {}
    def call(*, prompt, character, size):
        captured.update(prompt=prompt, character=character, size=size)
        return b"X"
    cp.generate_with_softening(call, prompt="p", character="Roger", size="1024x1792")
    assert captured == {"prompt": "p", "character": "Roger", "size": "1024x1792"}
