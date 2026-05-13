"""
HeyGen client — Avatar talking-head video generation.

HeyGen's video API is fundamentally different from the other video models
(text-to-talking-avatar rather than image-to-video). Auth is a Bearer token
from a per-account API key.

This file is a stub: until HEYGEN_API_KEY is in .env it raises
ProviderNotConfigured. The list/submit/wait shapes match HeyGen's v2 REST
API so they're easy to flesh out when the key arrives:

  GET  /v2/avatars                      → catalogue of avatar talents
  GET  /v2/voices                       → catalogue of voices
  POST /v2/video/generate               → start a video; returns video_id
  GET  /v1/video_status.get?video_id=…  → poll; returns status + (when done) video_url
"""
from __future__ import annotations

from pathlib import Path

from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


def _require_heygen() -> None:
    if not settings.heygen_api_key:
        raise ProviderNotConfigured(
            "HeyGen",
            "Add HEYGEN_API_KEY to .env (sign up at https://app.heygen.com/).",
        )


# --- catalogue listings (called by frontend to populate the pickers) ----------------

def list_avatars() -> list[dict]:
    """Returns a list of {avatar_id, name, gender, preview_image_url, preview_video_url}.
    Stub for now; once wired, calls GET /v2/avatars and returns `data.avatars`."""
    _require_heygen()
    raise NotImplementedError("HeyGen list_avatars wiring pending.")


def list_voices() -> list[dict]:
    """Returns a list of {voice_id, name, language, gender, preview_audio_url}.
    Stub for now; once wired, calls GET /v2/voices and returns `data.voices`."""
    _require_heygen()
    raise NotImplementedError("HeyGen list_voices wiring pending.")


# --- video submission + polling -----------------------------------------------------

def submit_avatar_video(
    *,
    avatar_id: str,
    voice_id: str,
    script: str,
    aspect_ratio: str | None = None,
    app_job_id: str | None = None,
) -> str:
    """Submit a talking-avatar video. Returns the HeyGen `video_id` to poll."""
    _require_heygen()
    raise NotImplementedError("HeyGen submit_avatar_video wiring pending.")


def wait_for_avatar_video(*, video_id: str, dest: Path) -> Path:
    """Poll /v1/video_status.get until status=completed, then download the mp4."""
    _require_heygen()
    raise NotImplementedError("HeyGen wait_for_avatar_video wiring pending.")


def submit_avatar_video_with_audio(
    *,
    avatar_id: str | None,
    image: Path | None,
    audio: Path,
    aspect_ratio: str | None = None,
    app_job_id: str | None = None,
) -> str:
    """Submit a talking-head where the speech track is a user-supplied audio file
    (e.g. ElevenLabs TTS output) instead of HeyGen's own TTS.

    Either `avatar_id` (catalogue avatar) OR `image` (talking-photo) must be set.
    Returns video_id — poll with `wait_for_avatar_video`.
    HeyGen body uses `voice.type='audio'` with an audio_asset_id obtained by
    first uploading the file to /v1/asset.
    """
    _require_heygen()
    raise NotImplementedError("HeyGen submit_avatar_video_with_audio wiring pending.")


def submit_photo_avatar(
    *,
    image: Path,
    voice_id: str,
    script: str,
    aspect_ratio: str | None = None,
    app_job_id: str | None = None,
) -> str:
    """Talking-photo avatar — uploads a user-supplied image to HeyGen as a
    one-shot talking-photo and submits a video using it.

    HeyGen's flow (two calls):
      1. POST /v1/talking_photo  — multipart with the photo. Returns talking_photo_id.
      2. POST /v2/video/generate — body uses character.type='talking_photo' +
                                   talking_photo_id, voice, input_text.
    Returns the video_id from step 2 — poll with `wait_for_avatar_video`.
    """
    _require_heygen()
    raise NotImplementedError("HeyGen submit_photo_avatar wiring pending.")
