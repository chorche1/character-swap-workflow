"""Backlog #26 (2026-06-12): ElevenLabs account-error classification +
circuit breaker. 15/17 lifetime voice-changer calls failed with the SAME
non-retryable subscription error, repeated for every character in a compile
batch. One account-level rejection now raises ElevenLabsAccountError with
an actionable message AND trips a process-wide breaker so sibling calls
fail fast. Transient 5xx errors never trip it.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from character_swap.clients import elevenlabs
from character_swap.config import settings


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    elevenlabs._account_block.update(until=0.0, reason="")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "k", raising=False)
    yield
    elevenlabs._account_block.update(until=0.0, reason="")


class _FakeResponse(SimpleNamespace):
    pass


def _wire(monkeypatch, status_code: int, body: str):
    posts: list = []

    class FakeClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            posts.append(a)
            return _FakeResponse(status_code=status_code, text=body,
                                 headers={}, content=b"")

    monkeypatch.setattr(elevenlabs.httpx, "Client", FakeClient)
    return posts


def _call(tmp_path):
    src = tmp_path / "a.wav"
    src.write_bytes(b"RIFF")
    return elevenlabs.voice_changer(voice_id="v1", source_audio=src)


def test_subscription_error_classified_and_breaker_trips(monkeypatch, tmp_path):
    posts = _wire(monkeypatch, 401, '{"detail": "subscription does not '
                                    'include voice_changer"}')
    with pytest.raises(elevenlabs.ElevenLabsAccountError,
                       match="Voice changer failed"):
        _call(tmp_path)
    assert len(posts) == 1

    # Sibling call in the same compile batch: fails fast, no upload.
    with pytest.raises(elevenlabs.ElevenLabsAccountError, match="paused"):
        _call(tmp_path)
    assert len(posts) == 1


def test_transient_5xx_does_not_trip_breaker(monkeypatch, tmp_path):
    posts = _wire(monkeypatch, 500, "internal error")
    with pytest.raises(elevenlabs.ElevenLabsError):
        _call(tmp_path)
    assert not isinstance(
        pytest.raises(elevenlabs.ElevenLabsError, _call, tmp_path).value,
        elevenlabs.ElevenLabsAccountError)
    assert len(posts) == 2                  # both reached the API


def test_marker_in_body_classifies_even_on_4xx(monkeypatch, tmp_path):
    _wire(monkeypatch, 422, '{"detail": {"status": "quota_exceeded"}}')
    with pytest.raises(elevenlabs.ElevenLabsAccountError):
        _call(tmp_path)
