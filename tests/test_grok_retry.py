"""Regression tests: Grok's retry-on-429/5xx must actually fire.

2026-07-01 audit finding (grok.py:107): the client routes 429/5xx into an
exception via `if _retryable_status(r): r.raise_for_status()` — but
raise_for_status() raises httpx.HTTPStatusError, which is NOT a subclass of
httpx.TransportError, so the old `retry_if_exception_type(_RETRY_EXCS)`
decorator never matched it. Every xAI rate-limit or transient 5xx failed the
call on the FIRST hit despite five configured retry attempts — a single 500
mid-way through a 10-minute wait_for_video poll aborted the whole
already-billed generation. The fix retries via a predicate that keeps
transport errors AND adds HTTPStatusError filtered to 429/5xx; permanent 4xx
client errors (e.g. a 404 image-download URL) must still NOT retry.
"""
from __future__ import annotations

import ssl

import httpx
import pytest
import tenacity

from character_swap.clients import grok
from character_swap.config import settings


def _resp(status: int, json_body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.x.ai/v1/test"),
        json=json_body if json_body is not None else {},
    )


class _FakeHTTPClient:
    """Stands in for grok._client(): pops one canned response per request."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = responses
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def _next(self) -> httpx.Response:
        self.calls += 1
        assert self._responses, "fake client ran out of canned responses"
        return self._responses.pop(0)

    def post(self, _path, json=None):
        return self._next()

    def get(self, _path):
        return self._next()


@pytest.fixture()
def grok_env(tmp_path, monkeypatch):
    """API key present + a real image file for submit()'s base64 encoding."""
    monkeypatch.setattr(settings, "xai_api_key", "test-key")
    img = tmp_path / "frame.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    return img


def _install_client(monkeypatch, responses: list[httpx.Response]) -> _FakeHTTPClient:
    fake = _FakeHTTPClient(responses)
    monkeypatch.setattr(grok, "_client", lambda: fake)
    return fake


# No exponential backoff in tests — retry_with() clones the decorated
# function's retry policy (same predicate, same stop) with zero wait.
def _fast(fn):
    return fn.retry_with(wait=tenacity.wait_none())


def test_submit_retries_429_then_succeeds(grok_env, monkeypatch):
    """The exact dead-code path from the audit: a 429 on submit must back
    off and retry, not fail the clip on the first hit."""
    fake = _install_client(monkeypatch, [
        _resp(429, {"error": "rate limited"}),
        _resp(200, {"request_id": "job-after-retry"}),
    ])
    job_id = _fast(grok.submit)(
        image=grok_env, prompt="wave at camera", character="test-char")
    assert job_id == "job-after-retry"
    assert fake.calls == 2, "429 must be retried, got a first-hit failure"


def test_status_poll_retries_transient_500(grok_env, monkeypatch):
    """A single transient 500 mid-poll used to abort a whole already-billed
    video generation. It must re-poll instead."""
    fake = _install_client(monkeypatch, [
        _resp(500, {"error": "internal"}),
        _resp(503, {"error": "overloaded"}),
        _resp(200, {"status": "done"}),
    ])
    data = _fast(grok.status)(job_id="g-1", character="test-char")
    assert data == {"status": "done"}
    assert fake.calls == 3


def test_submit_does_not_retry_permanent_4xx(grok_env, monkeypatch):
    """400 (and any non-429 4xx) is a permanent client error — exactly ONE
    attempt, raised loudly as GrokError."""
    fake = _install_client(monkeypatch, [
        _resp(400, {"error": "bad request"}),
    ])
    with pytest.raises(grok.GrokError, match="Submit failed \\(400\\)"):
        _fast(grok.submit)(
            image=grok_env, prompt="wave", character="test-char")
    assert fake.calls == 1, "non-429 4xx must never be retried"


def test_retry_exhaustion_reraises_last_status_error(grok_env, monkeypatch):
    """Five 429s in a row: all attempts consumed, then the HTTPStatusError
    propagates (reraise=True) so the caller's failure path runs."""
    fake = _install_client(monkeypatch, [_resp(429) for _ in range(5)])
    with pytest.raises(httpx.HTTPStatusError):
        _fast(grok.status)(job_id="g-1", character="test-char")
    assert fake.calls == 5


def test_should_retry_predicate_status_filter():
    """Direct predicate coverage: transport + SSL + 429/5xx retry; other
    4xx HTTPStatusErrors (e.g. a 404 download URL) and app errors don't."""
    req = httpx.Request("GET", "https://api.x.ai/v1/test")

    def status_exc(code: int) -> httpx.HTTPStatusError:
        return httpx.HTTPStatusError(
            "boom", request=req, response=httpx.Response(code, request=req))

    assert grok._should_retry(status_exc(429)) is True
    assert grok._should_retry(status_exc(500)) is True
    assert grok._should_retry(status_exc(503)) is True
    assert grok._should_retry(status_exc(599)) is True
    assert grok._should_retry(status_exc(404)) is False
    assert grok._should_retry(status_exc(403)) is False
    assert grok._should_retry(status_exc(400)) is False
    assert grok._should_retry(httpx.ConnectError("reset", request=req)) is True
    assert grok._should_retry(httpx.ReadTimeout("slow", request=req)) is True
    assert grok._should_retry(ssl.SSLError("bad_record_mac")) is True
    assert grok._should_retry(grok.GrokError("app error")) is False
    assert grok._should_retry(ValueError("unrelated")) is False


def test_image_generation_retries_5xx(grok_env, monkeypatch):
    """_generate_image_once shares the same decorator — a transient 502 on
    the images endpoint must retry too (B-roll seed images)."""
    import base64
    png_b64 = base64.b64encode(b"png-bytes").decode()
    fake = _install_client(monkeypatch, [
        _resp(502, {"error": "bad gateway"}),
        _resp(200, {"data": [{"b64_json": png_b64}]}),
    ])
    out = _fast(grok._generate_image_once)(prompt="a lemon", character="freeform")
    assert out == b"png-bytes"
    assert fake.calls == 2


# --- download_video retry (audit 2026-07-03) --------------------------------
#
# The final download is the largest transfer in the flow and runs AFTER the
# generation is billed — it had zero retry, so a mid-stream connection reset
# or CDN 5xx discarded a finished video and the ↻ retry re-submitted (and
# re-paid) a brand-new generation.


class _FakeStreamResponse:
    """Context-manager stand-in for httpx.stream()'s response."""

    def __init__(self, *, chunks: list[bytes] = (),
                 raise_on_status: Exception | None = None,
                 raise_mid_stream: Exception | None = None):
        self._chunks = list(chunks)
        self._raise_on_status = raise_on_status
        self._raise_mid_stream = raise_mid_stream

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def raise_for_status(self):
        if self._raise_on_status is not None:
            raise self._raise_on_status

    def iter_bytes(self, chunk_size):
        yield from self._chunks
        if self._raise_mid_stream is not None:
            raise self._raise_mid_stream


def _install_stream(monkeypatch, responses: list[_FakeStreamResponse]) -> dict:
    calls = {"n": 0}

    def fake_stream(method, url, timeout=None):
        assert method == "GET"
        calls["n"] += 1
        assert responses, "fake stream ran out of canned responses"
        return responses.pop(0)

    monkeypatch.setattr(grok.httpx, "stream", fake_stream)
    return calls


def test_download_video_retries_mid_stream_reset(tmp_path, monkeypatch):
    """Connection reset at 90% of the transfer: the retry must re-download
    and land the FULL file (the truncated .tmp from attempt 1 is rewritten,
    never promoted to dest)."""
    calls = _install_stream(monkeypatch, [
        _FakeStreamResponse(chunks=[b"partial-"],
                            raise_mid_stream=httpx.ReadError(
                                "connection reset by peer")),
        _FakeStreamResponse(chunks=[b"full-", b"video-bytes"]),
    ])
    dest = tmp_path / "video.mp4"
    _fast(grok.download_video)(url="https://cdn.x.ai/vid.mp4", dest=dest)
    assert dest.read_bytes() == b"full-video-bytes"
    assert calls["n"] == 2, "mid-stream reset must be retried, not discarded"


def test_download_video_retries_cdn_5xx(tmp_path, monkeypatch):
    """A transient 503 from the CDN (raise_for_status → HTTPStatusError)
    goes through the same _should_retry predicate as submit/status."""
    req = httpx.Request("GET", "https://cdn.x.ai/vid.mp4")
    calls = _install_stream(monkeypatch, [
        _FakeStreamResponse(raise_on_status=httpx.HTTPStatusError(
            "503", request=req, response=httpx.Response(503, request=req))),
        _FakeStreamResponse(chunks=[b"video-bytes"]),
    ])
    dest = tmp_path / "video.mp4"
    _fast(grok.download_video)(url="https://cdn.x.ai/vid.mp4", dest=dest)
    assert dest.read_bytes() == b"video-bytes"
    assert calls["n"] == 2


def test_download_video_does_not_retry_404(tmp_path, monkeypatch):
    """A 404 download URL is a permanent client error — exactly ONE attempt
    (retrying an expired URL 5 times just delays the loud failure)."""
    req = httpx.Request("GET", "https://cdn.x.ai/gone.mp4")
    calls = _install_stream(monkeypatch, [
        _FakeStreamResponse(raise_on_status=httpx.HTTPStatusError(
            "404", request=req, response=httpx.Response(404, request=req))),
    ])
    dest = tmp_path / "video.mp4"
    with pytest.raises(httpx.HTTPStatusError):
        _fast(grok.download_video)(url="https://cdn.x.ai/gone.mp4", dest=dest)
    assert calls["n"] == 1
    assert not dest.exists()
