"""OpenAI 429 → legible, actionable error (Hugo 2026-07-14).

A Whisper call that returns HTTP 429 raises openai.RateLimitError, which is NOT
a RuntimeError — so the Editor endpoints' `except RuntimeError` blocks miss it
and it used to surface as a bare "500 Internal Server Error". When the account
is out of credits (`insufficient_quota`) that made a billing problem look like a
broken feature ("stitch + auto-edit funkar inte"). The global exception handler
maps the two 429 flavors to clean payloads: out-of-credits → 402, plain rate
limit → 429.
"""
from __future__ import annotations

from character_swap import api


def test_insufficient_quota_maps_to_402_with_billing_message():
    """The real message OpenAI returns when the account has no credits."""
    msg = (
        "Error code: 429 - {'error': {'message': 'You exceeded your current "
        "quota, please check your plan and billing details.', 'type': "
        "'insufficient_quota', 'code': 'insufficient_quota'}}"
    )
    status, body = api._openai_limit_response(msg)
    assert status == 402
    assert body["reason"] == "openai_insufficient_quota"
    # Actionable: names the billing page so the user knows what to do.
    assert "billing" in body["error"].lower()


def test_plain_rate_limit_maps_to_429():
    status, body = api._openai_limit_response("Rate limit reached for whisper-1")
    assert status == 429
    assert body["reason"] == "openai_rate_limit"


def test_empty_message_is_treated_as_plain_rate_limit():
    status, body = api._openai_limit_response("")
    assert status == 429
    assert body["reason"] == "openai_rate_limit"


def test_rate_limit_handler_is_registered():
    """The openai.RateLimitError type is wired into FastAPI's exception map so
    a 429 raised deep inside transcribe_words reaches the clean handler instead
    of falling through to a bare 500."""
    from openai import RateLimitError

    assert RateLimitError in api.app.exception_handlers
