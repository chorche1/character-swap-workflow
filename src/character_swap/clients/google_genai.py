"""
Google Gen AI / Vertex AI clients.

Nano Banana (gemini-2.5-flash-image-preview) and Nano Banana Pro
(gemini-2.5-pro-image-preview) are reachable via the REST
`generateContent` endpoint — we use httpx directly so we don't need
a new SDK dependency.

Veo 3 is still a stub; will come in a later phase.
"""
from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

import httpx

from character_swap.call_log import record
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


GEMINI_REST_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Map our model slugs → Google's full model names.
_NANO_BANANA_MODELS = {
    "nano-banana": "gemini-2.5-flash-image-preview",
    "nano-banana-pro": "gemini-2.5-pro-image-preview",
}


def _require_gemini() -> None:
    if not settings.gemini_api_key:
        raise ProviderNotConfigured(
            "Nano Banana / Veo",
            "Add GEMINI_API_KEY to .env (get one at https://aistudio.google.com/apikey).",
        )


def _to_inline_part(path: Path) -> dict:
    """Pack a local image file as an inline_data part for the Gemini API."""
    data = path.read_bytes()
    mime, _ = mimetypes.guess_type(str(path))
    if not mime or not mime.startswith("image/"):
        mime = "image/png"
    return {
        "inline_data": {
            "mime_type": mime,
            "data": base64.b64encode(data).decode("ascii"),
        }
    }


def generate_nano_banana(
    *,
    prompt: str,
    reference_images: list[Path] | None = None,
    aspect_ratio: str | None = None,
    app_job_id: str | None = None,
    model: str | None = None,
) -> bytes:
    """Call Gemini's image-generation endpoint via REST. Returns raw image
    bytes. Accepts an arbitrary number of reference images — Gemini's
    multi-reference is the whole reason we have this path; the reel runner
    uses it to pass anchor + all input frames at once.

    `model` may be a slug ('nano-banana', 'nano-banana-pro') or a full
    Google model name. Defaults to flash (nano-banana).
    """
    _require_gemini()
    refs = reference_images or []
    slug = (model or "nano-banana").strip()
    google_model = _NANO_BANANA_MODELS.get(slug, slug)

    parts: list[dict] = [{"text": prompt}]
    for p in refs:
        parts.append(_to_inline_part(p))

    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
        },
    }

    url = f"{GEMINI_REST_BASE}/{google_model}:generateContent"
    headers = {
        "x-goog-api-key": settings.gemini_api_key,
        "Content-Type": "application/json",
    }

    with record(phase="reel_render", model=google_model,
                character="reel", job_id=app_job_id,
                n_references=len(refs)):
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            raise RuntimeError(
                f"Gemini API error {resp.status_code}: "
                f"{resp.text[:500]}"
            )
        payload = resp.json()

    # Walk the response, find the first inline_data image part.
    candidates = payload.get("candidates") or []
    for cand in candidates:
        for part in (cand.get("content", {}).get("parts") or []):
            blob = part.get("inline_data") or part.get("inlineData")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])
    raise RuntimeError(
        f"Gemini returned no image data for model={google_model}. "
        f"Response shape: {list(payload.keys())}"
    )


def submit_veo(
    *,
    image: Path,
    prompt: str,
    aspect_ratio: str | None = None,
    duration_secs: int | None = None,
    app_job_id: str | None = None,
) -> str:
    """Stub. Will submit a Veo 3 job and return the long-running-op id."""
    _require_gemini()
    raise NotImplementedError("Veo wiring is part of the next phase.")


def wait_for_veo(*, op_id: str, dest: Path) -> Path:
    """Stub. Will poll the LRO and download the mp4."""
    _require_gemini()
    raise NotImplementedError("Veo wiring is part of the next phase.")
