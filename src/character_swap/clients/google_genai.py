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
import time
from pathlib import Path

import httpx

from character_swap import content_policy
from character_swap.call_log import record
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


GEMINI_REST_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

# Map our model slugs → Google's full model names. Verified live against
# /v1beta/models?key=... on 2026-05-16. Google has been renaming these in
# preview, so if a call 404s, re-run that ListModels call to see what's
# current and update this table.
_NANO_BANANA_MODELS = {
    "nano-banana": "gemini-2.5-flash-image",      # the flash variant
    "nano-banana-pro": "nano-banana-pro-preview", # marketing name == model name
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


def generate_nano_banana(*, prompt: str, **kwargs) -> bytes:
    """Generate via Gemini, auto-recovering from content-policy / safety
    rejections by retrying with a minimally softened prompt (see
    `content_policy`). Thin wrapper around `_generate_nano_banana_once`."""
    return content_policy.generate_with_softening(
        _generate_nano_banana_once, prompt=prompt, **kwargs
    )


def _generate_nano_banana_once(
    *,
    prompt: str,
    reference_images: list[Path] | None = None,
    aspect_ratio: str | None = None,
    app_job_id: str | None = None,
    model: str | None = None,
) -> bytes:
    """Call Gemini's image-generation endpoint via REST. Returns raw image
    bytes. Accepts an arbitrary number of reference images — Gemini's
    multi-reference is the whole reason we have this path.

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

    # Retry with exponential backoff on transient errors (429 quota,
    # 5xx server). Preview image models have low per-minute caps that
    # parallel follower rendering can spike — retrying after a beat
    # nearly always recovers without the user seeing a "failed" frame.
    RETRYABLE = {429, 500, 502, 503, 504}
    MAX_ATTEMPTS = 5
    BACKOFFS = [2.0, 5.0, 12.0, 30.0]  # seconds between attempt 1→2, 2→3, ...

    with record(phase="nano_banana", model=google_model,
                character="freeform", job_id=app_job_id,
                n_references=len(refs)):
        last_error = None
        for attempt in range(MAX_ATTEMPTS):
            with httpx.Client(timeout=180.0) as client:
                resp = client.post(url, headers=headers, json=body)
            if resp.status_code == 200:
                break
            last_error = f"{resp.status_code}: {resp.text[:400]}"
            if resp.status_code not in RETRYABLE or attempt == MAX_ATTEMPTS - 1:
                raise RuntimeError(f"Gemini API error {last_error}")
            time.sleep(BACKOFFS[min(attempt, len(BACKOFFS) - 1)])
        payload = resp.json()

    # Walk the response, find the first inline_data image part.
    candidates = payload.get("candidates") or []
    for cand in candidates:
        for part in (cand.get("content", {}).get("parts") or []):
            blob = part.get("inline_data") or part.get("inlineData")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])

    # No image part. Gemini signals a moderation/safety block as HTTP 200 with
    # either a top-level promptFeedback.blockReason or a per-candidate
    # finishReason of SAFETY / IMAGE_SAFETY / PROHIBITED_CONTENT. Surface that
    # as a clearly-labeled "content policy" error so the softening retry kicks
    # in (vs a generic empty-response failure).
    pf = payload.get("promptFeedback") or payload.get("prompt_feedback") or {}
    block_reason = str(pf.get("blockReason") or pf.get("block_reason") or "")
    if not block_reason:
        for cand in candidates:
            fr = str(cand.get("finishReason") or cand.get("finish_reason") or "")
            if fr and fr.upper() not in ("STOP", "MAX_TOKENS"):
                block_reason = fr
                break
    if block_reason:
        raise RuntimeError(
            f"Gemini blocked this prompt (content policy / safety, "
            f"reason: {block_reason}, model={google_model})."
        )
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
