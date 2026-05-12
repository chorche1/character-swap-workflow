"""
Pipeline primitives — pure functions that wrap the API clients.

The new flow is:
    generate_image(scene, character)  -> Path     # GPT Image 2 (image-to-image)
    submit_video(image, prompt)       -> job_id   # Grok Imagine
    wait_for_video(job_id, dest)      -> Path     # poll + download mp4

Orchestration (parallelism, approval, retries) lives in `runner.py`. Real-time
fan-out to the web UI lives in `events.py`. This module does no I/O beyond
what the clients require.
"""
from __future__ import annotations

import time
from pathlib import Path

from character_swap.clients import grok, openai_image
from character_swap.config import settings
from character_swap.images import atomic_write_bytes

# Verbatim user-specified prompt. Do not paraphrase.
GENERATION_PROMPT = (
    "The man from the second picture is in the exact same pose in the exact "
    "same position and holding the exact same stuff in the exact same place "
    "as the man in the first picture. Remove any text overlays. 9:16 ratio. "
    "The background looks like it is the same environment as the second picture."
)


def generate_image(
    *,
    scene_image: Path,
    character_image: Path,
    character_name: str,
    dest: Path,
    job_id: str | None = None,
) -> Path:
    """
    Image-to-image generation using GPT Image 2.
    Scene is reference #1, character is reference #2 — matches the verbatim prompt.
    Writes the PNG bytes atomically to `dest` and returns it.
    """
    image_bytes = openai_image.generate(
        prompt=GENERATION_PROMPT,
        reference_images=[scene_image, character_image],
        phase="generate",
        character=character_name,
        job_id=job_id,
    )
    atomic_write_bytes(dest, image_bytes)
    return dest


def edit_image(
    *,
    source_image: Path,
    custom_prompt: str,
    character_name: str,
    dest: Path,
    job_id: str | None = None,
) -> Path:
    """
    Refine an existing variant with a user-supplied prompt.
    Single reference image (the variant being edited) + custom prompt.
    """
    image_bytes = openai_image.generate(
        prompt=custom_prompt,
        reference_images=[source_image],
        phase="edit",
        character=character_name,
        job_id=job_id,
    )
    atomic_write_bytes(dest, image_bytes)
    return dest


def submit_video(
    *,
    image: Path,
    movement_prompt: str,
    character_name: str,
    job_id: str | None = None,
) -> str:
    """Submit a Grok Imagine video job. Returns the (Grok) job_id."""
    return grok.submit(image=image, prompt=movement_prompt,
                       character=character_name, app_job_id=job_id)


def poll_video_once(*, job_id: str, character_name: str,
                    app_job_id: str | None = None) -> tuple[str, str | None]:
    """One poll. Returns (status, download_url_or_none)."""
    payload = grok.status(job_id=job_id, character=character_name, app_job_id=app_job_id)
    return _extract_status(payload)


def wait_for_video(
    *,
    job_id: str,
    character_name: str,
    dest: Path,
    on_progress=None,
    app_job_id: str | None = None,
) -> Path:
    """
    Blocking poll loop. Downloads to `dest` on success. Raises grok.GrokError
    on timeout or terminal failure.

    `on_progress(status: str, url: str | None)` is called once per poll.
    """
    deadline = time.monotonic() + settings.video_timeout_secs
    interval = settings.video_poll_interval_secs
    while time.monotonic() < deadline:
        status, url = poll_video_once(job_id=job_id, character_name=character_name,
                                      app_job_id=app_job_id)
        if on_progress is not None:
            on_progress(status, url)
        if status in grok.SUCCESS_STATES:
            if not url:
                raise grok.GrokError("Video reported done but no download URL")
            grok.download_video(url=url, dest=dest)
            return dest
        if status in grok.TERMINAL_STATES:
            raise grok.GrokError(f"Video job ended in state '{status}'")
        time.sleep(interval)
    raise grok.GrokError(f"Video job {job_id} timed out after {settings.video_timeout_secs}s")


def _extract_status(payload: dict) -> tuple[str, str | None]:
    """
    Read (status, download_url) from a Grok status payload. Handles all observed
    response shapes. Verified shape on completion:
      {"status": "done", "video": {"url": "...", "duration": 10}, "progress": 100}
    """
    status = str(
        payload.get("status")
        or payload.get("state")
        or "unknown"
    ).lower()

    url = None
    video = payload.get("video")
    if isinstance(video, dict):
        url = video.get("url")
    if not url:
        url = payload.get("video_url") or payload.get("url")
    if not url:
        data = payload.get("data") or {}
        if isinstance(data, dict):
            inner = data.get("video")
            if isinstance(inner, dict):
                url = inner.get("url")
            url = url or data.get("url") or data.get("video_url")
    if not url:
        outputs = payload.get("outputs") or []
        if outputs and isinstance(outputs, list):
            first = outputs[0]
            if isinstance(first, dict):
                url = first.get("url") or first.get("video_url")
    return status, url
