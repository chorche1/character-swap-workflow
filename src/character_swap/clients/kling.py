"""
Kling video API client — stub for now.

Kling auth uses JWT signed with access-key + secret-key. Add both as
KLING_ACCESS_KEY / KLING_SECRET_KEY in .env. Today this module only signals
"not configured" so the UI can lock the model selector.
"""
from __future__ import annotations

from pathlib import Path

from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


def _require_kling() -> None:
    if not (settings.kling_access_key and settings.kling_secret_key):
        raise ProviderNotConfigured(
            "Kling",
            "Add KLING_ACCESS_KEY and KLING_SECRET_KEY to .env "
            "(get them at https://app.klingai.com/).",
        )


def submit_kling(
    *,
    image: Path,
    prompt: str,
    aspect_ratio: str | None = None,
    duration_secs: int | None = None,
    app_job_id: str | None = None,
) -> str:
    """Stub. Will sign a JWT and POST to Kling's image-to-video endpoint."""
    _require_kling()
    raise NotImplementedError("Kling wiring is part of the next phase.")


def wait_for_kling(*, task_id: str, dest: Path) -> Path:
    """Stub. Will poll the task and download the mp4."""
    _require_kling()
    raise NotImplementedError("Kling wiring is part of the next phase.")
