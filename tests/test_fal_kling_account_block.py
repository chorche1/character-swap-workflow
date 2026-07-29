"""Backlog #6 (2026-06-12): fal account-error classification + circuit
breaker. calls.jsonl forensics: 47 doomed Kling submits fired in 6 minutes,
every one failing with 'Exhausted balance / User is locked'. One account-
level rejection now (a) raises the distinct FalAccountError with an
actionable billing message, and (b) trips a process-wide block so sibling
submits in the same batch fail FAST instead of re-burning uploads/submits.
Transient errors (5xx etc.) never trip the block.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from character_swap.clients import fal_kling


@pytest.fixture(autouse=True)
def _reset_block():
    fal_kling._account_block.update(until=0.0, reason="")
    yield
    fal_kling._account_block.update(until=0.0, reason="")


def _wire(monkeypatch, upload_error: Exception | None = None,
          submit_error: Exception | None = None):
    uploads: list = []
    submits: list = []

    def upload_file(p):
        uploads.append(p)
        if upload_error:
            raise upload_error
        return "https://fal.example/img.png"

    def submit(endpoint, arguments):
        submits.append(arguments)
        if submit_error:
            raise submit_error
        return SimpleNamespace(request_id="req_1")

    monkeypatch.setattr(fal_kling, "_client", lambda: SimpleNamespace(
        upload_file=upload_file, submit=submit))
    return uploads, submits


def _submit(tmp_path):
    return fal_kling.submit_image_to_video(
        image=tmp_path / "frame.png", prompt="p", duration_secs=5)


def test_balance_error_raises_account_error_and_trips_block(monkeypatch, tmp_path):
    uploads, _ = _wire(monkeypatch, submit_error=RuntimeError(
        "Exhausted balance: User is locked. Top up your balance"))

    with pytest.raises(fal_kling.FalAccountError, match="cannot accept work"):
        _submit(tmp_path)
    assert len(uploads) == 1

    # Sibling submit in the same batch: fails fast, no upload, no submit.
    with pytest.raises(fal_kling.FalAccountError, match="paused"):
        _submit(tmp_path)
    assert len(uploads) == 1                # nothing new hit the API


def test_block_expires_and_submits_resume(monkeypatch, tmp_path):
    uploads, submits = _wire(monkeypatch)
    fal_kling._account_block.update(
        until=fal_kling.time.monotonic() - 1, reason="old")

    assert _submit(tmp_path) == "req_1"     # expired block → normal flow
    assert len(submits) == 1


def test_transient_error_does_not_trip_block(monkeypatch, tmp_path):
    uploads, _ = _wire(monkeypatch, submit_error=RuntimeError(
        "500 internal server error"))

    with pytest.raises(RuntimeError, match="submit failed"):
        _submit(tmp_path)
    # Next attempt still reaches the API — no block was set.
    with pytest.raises(RuntimeError, match="submit failed"):
        _submit(tmp_path)
    assert len(uploads) == 2


def test_account_error_on_upload_also_trips(monkeypatch, tmp_path):
    uploads, _ = _wire(monkeypatch, upload_error=RuntimeError(
        "403: Insufficient credits"))

    with pytest.raises(fal_kling.FalAccountError):
        _submit(tmp_path)
    with pytest.raises(fal_kling.FalAccountError, match="paused"):
        _submit(tmp_path)
    assert len(uploads) == 1


# --- the reason lives in the HTTP BODY, not str(e) (Hugo 2026-07-29) ---------


def _locked_403() -> Exception:
    """The real shape of a locked-account fal error: httpx's message is ONLY
    status + URL; the actionable reason is in the response body."""
    import httpx
    req = httpx.Request(
        "POST", "https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3")
    resp = httpx.Response(403, request=req, json={
        "detail": "User is locked. Reason: Exhausted balance. "
                  "Top up your balance at fal.ai/dashboard/billing"})
    return httpx.HTTPStatusError(
        "Client error '403 Forbidden' for url "
        "'https://rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3'",
        request=req, response=resp)


def test_locked_account_403_is_classified_from_the_response_body(
        monkeypatch, tmp_path):
    """THE FIX: a locked fal account (balance -$69.86, 2026-07-29) reached the
    runner as a bare '403 Forbidden' — so the circuit breaker never tripped and
    every ↻ re-burned an upload, while the UI showed a cryptic URL instead of
    "top up fal"."""
    exc = _locked_403()
    assert "exhausted balance" not in str(exc).lower()   # invisible before
    assert fal_kling._is_account_error(exc)              # visible now

    uploads, _ = _wire(monkeypatch, upload_error=exc)
    with pytest.raises(fal_kling.FalAccountError) as ei:
        _submit(tmp_path)
    # The clip's error chip now names the cause + the fix.
    assert "Exhausted balance" in str(ei.value)
    assert "fal.ai/dashboard/billing" in str(ei.value)
    # ...and the breaker trips, so siblings fail fast without another upload.
    with pytest.raises(fal_kling.FalAccountError, match="paused"):
        _submit(tmp_path)
    assert len(uploads) == 1
    assert "Exhausted balance" in fal_kling._account_block["reason"]


def test_error_detail_is_safe_on_plain_and_bodyless_errors():
    assert fal_kling.error_detail(RuntimeError("boom")) == "boom"
    assert not fal_kling._is_account_error(RuntimeError("connection reset"))

    class _Unread:                       # httpx raises on .text before read
        @property
        def text(self):
            raise RuntimeError("ResponseNotRead")
    exc = RuntimeError("stream broke")
    exc.response = _Unread()             # type: ignore[attr-defined]
    assert fal_kling.error_detail(exc) == "stream broke"


def test_every_fal_client_shares_the_body_aware_classifier():
    """fal_grok / fal_veo / fal_seedance / fal_veed all funnel their error
    paths through the same helper — a locked account must read the same on
    whichever model a clip happened to run."""
    from character_swap.clients import (
        fal_grok, fal_seedance, fal_veed, fal_veo,
    )
    exc = _locked_403()
    for mod in (fal_grok, fal_veo, fal_seedance):
        assert mod._is_account_error(exc), mod.__name__
        assert mod.error_detail is fal_kling.error_detail, mod.__name__
    assert fal_veed.error_detail is fal_kling.error_detail


# --- transient-error retries on download (backlog #34) -----------------------


def test_download_retries_transient_errors(monkeypatch, tmp_path):
    import httpx

    calls: list = []
    sleeps: list = []
    monkeypatch.setattr(fal_kling.time, "sleep", lambda s: sleeps.append(s))

    class FakeStream:
        def __init__(self, fail: bool):
            self.fail = fail

        def __enter__(self):
            if self.fail:
                raise httpx.ReadError("connection reset")
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_bytes(self, chunk_size):
            yield b"clip"

    def fake_stream(method, url, **kw):
        calls.append(url)
        return FakeStream(fail=len(calls) <= 2)
    monkeypatch.setattr(fal_kling.httpx, "stream", fake_stream)

    dest = tmp_path / "v.mp4"
    fal_kling._download("https://x/clip.mp4", dest)
    assert len(calls) == 3                  # 2 resets ridden out
    assert sleeps == [2.0, 4.0]
    assert dest.read_bytes() == b"clip"


def test_download_gives_up_after_attempts(monkeypatch, tmp_path):
    import httpx
    monkeypatch.setattr(fal_kling.time, "sleep", lambda s: None)

    def always_reset(method, url, **kw):
        raise httpx.ReadError("reset")
    monkeypatch.setattr(fal_kling.httpx, "stream", always_reset)

    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="download failed after 3"):
        fal_kling._download("https://x/clip.mp4", tmp_path / "v.mp4")


def test_retry_excs_cover_read_and_ssl_errors():
    """Backlog #34: 'SSL bad_record_mac' / connection-reset ReadErrors burned
    45+ generations — the explicit exception lists missed both classes."""
    import ssl
    import httpx
    from character_swap.clients import elevenlabs, grok
    for excs in (grok._RETRY_EXCS, elevenlabs._RETRY_EXCS):
        assert any(issubclass(httpx.ReadError, e) for e in excs)
        assert any(issubclass(ssl.SSLError, e) for e in excs)
