"""
ElevenLabs client — voice library + Text-to-Speech + Voice Changer (Speech-to-Speech).

Auth: single API key in the `xi-api-key` header. Same key powers all three
surfaces. Endpoints used:

  GET  /v1/voices                          → returns the user's voice library
  POST /v1/text-to-speech/{voice_id}       → JSON body → audio bytes (mp3)
  POST /v1/speech-to-speech/{voice_id}     → multipart audio file → audio bytes (mp3)
"""
from __future__ import annotations

import json
import ssl
import time
from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from character_swap.call_log import record
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings

_BASE_URL = "https://api.elevenlabs.io/v1"
# TransportError is the base of the whole connect/read/write/timeout family;
# the old explicit list missed ReadError + ssl.SSLError (backlog #34).
_RETRY_EXCS = (
    httpx.TransportError,
    ssl.SSLError,
)


class ElevenLabsError(Exception):
    pass


class ElevenLabsAccountError(ElevenLabsError):
    """Non-retryable ACCOUNT-level error: the subscription lacks the
    feature, the key is unauthorized, or quota/payment is exhausted.
    Backlog #26 (2026-06-12): 15/17 lifetime voice-changer calls failed
    with the SAME subscription error, repeated for every character in a
    compile batch — each one a wasted upload."""


_ACCOUNT_ERROR_MARKERS = (
    "subscription", "unauthorized", "permission", "payment",
    "quota_exceeded", "missing_permissions",
)
# Process-wide breaker: after one account-level rejection, sibling calls in
# the same batch fail FAST with the same actionable message. Account fixes
# are human-speed — 30 min or a restart clears it.
_ACCOUNT_BLOCK_SECS = 1800.0
_account_block: dict = {"until": 0.0, "reason": ""}


def _classify_http_error(status_code: int, body: str) -> type[ElevenLabsError]:
    low = (body or "").lower()
    if status_code in {401, 402, 403} or any(
            m in low for m in _ACCOUNT_ERROR_MARKERS):
        return ElevenLabsAccountError
    return ElevenLabsError


def _check_account_block(feature: str) -> None:
    remaining = _account_block["until"] - time.monotonic()
    if remaining > 0:
        raise ElevenLabsAccountError(
            f"ElevenLabs {feature} paused ({int(remaining)}s left): "
            f"{_account_block['reason']} — fix the subscription/key at "
            "elevenlabs.io, then retry")


def _trip_account_block(reason: str) -> None:
    _account_block["until"] = time.monotonic() + _ACCOUNT_BLOCK_SECS
    _account_block["reason"] = reason[:300]


def _require_elevenlabs() -> None:
    if not settings.elevenlabs_api_key:
        raise ProviderNotConfigured(
            "ElevenLabs",
            "Add ELEVENLABS_API_KEY to .env (get one at https://elevenlabs.io/app/settings/api-keys).",
        )


def _headers(json_content: bool = False) -> dict[str, str]:
    h = {"xi-api-key": settings.elevenlabs_api_key, "accept": "audio/mpeg"}
    if json_content:
        h["content-type"] = "application/json"
    return h


@retry(
    retry=retry_if_exception_type(_RETRY_EXCS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=30),
    reraise=True,
)
def list_voices() -> list[dict]:
    """GET /v1/voices — returns every voice on the user's account
    (cloned + favourites + premades). Normalised to a frontend-friendly shape."""
    _require_elevenlabs()
    with record(phase="elevenlabs_list_voices", model="elevenlabs",
                character="—") as entry:
        with httpx.Client(timeout=30) as c:
            r = c.get(
                f"{_BASE_URL}/voices",
                headers={"xi-api-key": settings.elevenlabs_api_key, "accept": "application/json"},
            )
            if r.status_code >= 400:
                raise ElevenLabsError(f"list_voices failed ({r.status_code}): {r.text[:300]}")
            entry["request_id"] = r.headers.get("x-request-id")
            data = r.json()
    voices = []
    for v in data.get("voices", []):
        voices.append({
            "voice_id":    v.get("voice_id"),
            "name":        v.get("name"),
            "category":    v.get("category"),                   # "cloned" / "premade" / "generated" / "professional"
            "description": v.get("description"),
            "preview_url": v.get("preview_url"),
            "labels":      v.get("labels") or {},
        })
    return voices


@retry(
    retry=retry_if_exception_type(_RETRY_EXCS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def text_to_speech(
    *,
    voice_id: str,
    text: str,
    model_id: str = "eleven_multilingual_v2",
    app_job_id: str | None = None,
) -> bytes:
    """POST /v1/text-to-speech/{voice_id} — returns mp3 bytes."""
    _require_elevenlabs()
    body = {"text": text, "model_id": model_id}
    with record(phase="elevenlabs_tts", model=model_id,
                character="—", job_id=app_job_id, n_chars=len(text)) as entry:
        with httpx.Client(timeout=120) as c:
            r = c.post(
                f"{_BASE_URL}/text-to-speech/{voice_id}",
                headers=_headers(json_content=True),
                content=json.dumps(body),
            )
            if r.status_code >= 400:
                raise ElevenLabsError(f"TTS failed ({r.status_code}): {r.text[:300]}")
            entry["request_id"] = r.headers.get("x-request-id")
            return r.content


@retry(
    retry=retry_if_exception_type(_RETRY_EXCS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def voice_changer(
    *,
    voice_id: str,
    source_audio: Path,
    model_id: str = "eleven_multilingual_sts_v2",
    app_job_id: str | None = None,
) -> bytes:
    """POST /v1/speech-to-speech/{voice_id} — multipart upload.

    Re-renders the source audio as the target voice. Emotion + intonation +
    timing of the source are preserved. Returns mp3 bytes."""
    _require_elevenlabs()
    _check_account_block("voice changer")
    with record(phase="elevenlabs_vc", model=model_id,
                character="—", job_id=app_job_id) as entry:
        with source_audio.open("rb") as f, httpx.Client(timeout=180) as c:
            files = {"audio": (source_audio.name, f, "audio/mpeg")}
            data = {"model_id": model_id}
            r = c.post(
                f"{_BASE_URL}/speech-to-speech/{voice_id}",
                headers={"xi-api-key": settings.elevenlabs_api_key,
                         "accept": "audio/mpeg"},
                files=files, data=data,
            )
            if r.status_code >= 400:
                exc = _classify_http_error(r.status_code, r.text)
                msg = f"Voice changer failed ({r.status_code}): {r.text[:300]}"
                if exc is ElevenLabsAccountError:
                    _trip_account_block(msg)
                raise exc(msg)
            entry["request_id"] = r.headers.get("x-request-id")
            return r.content
