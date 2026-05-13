"""
Google Gen AI / Vertex AI clients — stubs for now.

Nano Banana (Gemini 2.5 Flash Image) and Veo 3 are both reachable via the
google-genai SDK (`pip install google-genai`). Today this module only checks
whether GEMINI_API_KEY is configured and raises ProviderNotConfigured when
not — so the UI can show the model as locked.

Once the user adds GEMINI_API_KEY, the real implementations go here.
"""
from __future__ import annotations

from pathlib import Path

from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


def _require_gemini() -> None:
    if not settings.gemini_api_key:
        raise ProviderNotConfigured(
            "Nano Banana / Veo",
            "Add GEMINI_API_KEY to .env (get one at https://aistudio.google.com/apikey).",
        )


def generate_nano_banana(
    *,
    prompt: str,
    reference_images: list[Path] | None = None,
    aspect_ratio: str | None = None,
    app_job_id: str | None = None,
    model: str | None = None,
) -> bytes:
    """Stub. Will use `google-genai` to call gemini-2.5-flash-image-preview
    (or `gemini-2.5-pro-image-preview` for Nano Banana Pro). The same stub
    serves both — caller picks via the `model` kwarg."""
    _require_gemini()
    raise NotImplementedError(
        f"Gemini image client wiring pending (model={model or 'flash'})."
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
