"""
ElevenLabs client — voice library + Text-to-Speech + Voice Changer (Speech-to-Speech).

Auth is a single API key in the `xi-api-key` header. Same key powers all
three surfaces (voices listing, TTS, STS). Endpoints:

  GET  /v1/voices                          → returns the user's voice library
  POST /v1/text-to-speech/{voice_id}       → audio bytes (mp3/wav based on accept header)
  POST /v1/speech-to-speech/{voice_id}     → multipart: audio file + model_id → audio bytes

This file is a stub today: all four functions raise ProviderNotConfigured
until ELEVENLABS_API_KEY is set, then NotImplementedError until the real
adapter is wired.
"""
from __future__ import annotations

from pathlib import Path

from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


def _require_elevenlabs() -> None:
    if not settings.elevenlabs_api_key:
        raise ProviderNotConfigured(
            "ElevenLabs",
            "Add ELEVENLABS_API_KEY to .env (sign up at https://elevenlabs.io/).",
        )


def list_voices() -> list[dict]:
    """Returns a list of {voice_id, name, category, description, preview_url,
    labels: {accent, gender, ...}}. Once wired, calls GET /v1/voices."""
    _require_elevenlabs()
    raise NotImplementedError("ElevenLabs list_voices wiring pending.")


def text_to_speech(
    *,
    voice_id: str,
    text: str,
    model_id: str = "eleven_multilingual_v2",
    app_job_id: str | None = None,
) -> bytes:
    """Generate speech. Returns mp3 bytes."""
    _require_elevenlabs()
    raise NotImplementedError("ElevenLabs text_to_speech wiring pending.")


def voice_changer(
    *,
    voice_id: str,
    source_audio: Path,
    model_id: str = "eleven_multilingual_sts_v2",
    app_job_id: str | None = None,
) -> bytes:
    """Speech-to-Speech: re-render the source audio as the target voice. Returns
    mp3 bytes. Emotion + intonation + timing of the source are preserved."""
    _require_elevenlabs()
    raise NotImplementedError("ElevenLabs voice_changer wiring pending.")
