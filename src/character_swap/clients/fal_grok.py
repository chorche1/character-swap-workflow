"""fal.ai Grok Imagine Video 1.5 image-to-video client.

Routes the `grok-imagine-1.5` model through fal.ai's xAI Grok Imagine 1.5
endpoint — the newest/best Grok video model — mirroring clients/fal_veo.py
(same FAL_API_KEY auth, same upload → submit → poll → download two-phase shape
the swap runner expects, same shared account-level circuit breaker so a
balance/locked error fails siblings fast).

Why fal and not xAI direct: the app already routes Kling 3.0 + Veo 3.1 Fast
through fal, so a third fal-hosted video model needs no new credentials, and
fal exposes Grok Imagine 1.5 with native synchronized audio + lip-sync — the
exact image_url + motion-prompt → talking clip shape Swap/Reengineer need.

API: https://fal.ai/models/xai/grok-imagine-video/v1.5/image-to-video
  image_url   (required)  URL or data URI — we upload the local frame first
  prompt      (required)  motion prompt
  duration    (integer)   seconds, 1–15 (default 6). NOTE: a PLAIN INT, not the
                          "<n>s" enum string Veo/Kling use.
  resolution  (enum)      "480p" | "720p" | "1080p"  (default "720p")
Response: {video: {url, ...}, ...}  — identical shape to fal Kling/Veo.

DIFFERENCES vs fal_veo (deliberate): Grok 1.5 i2v has NO documented
aspect_ratio field (aspect is inferred from the input frame, which is already
9:16 in our pipeline), NO generate_audio toggle (native audio is ALWAYS on),
and NO negative_prompt — so the shared KLING_NEGATIVE_PROMPT is not sent here.
Like fal_veo, this endpoint has NO end-frame input, so a scene overridden to
this model ignores any end pose — matching the per-scene soft-degrade in the
runner (runner._resolve_end_image only resolves an end frame for kling-v3).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from character_swap import call_log
from character_swap.clients import ProviderNotConfigured
# Share the fal account-level circuit breaker: a balance/locked rejection is
# account-wide, so one tripped breaker pauses Kling, Veo AND Grok submits alike.
from character_swap.clients.fal_kling import (
    FalAccountError,
    _check_account_block,
    _is_account_error,
    _trip_account_block,
)
from character_swap.config import settings

_ENDPOINT = "xai/grok-imagine-video/v1.5/image-to-video"

# Grok Imagine 1.5 accepts an integer duration in seconds. The model's true
# range is 1–15; we floor at 3 (sub-3s clips have little value and match the
# Kling v3 floor) so per-scene auto-lengths land in a sensible band.
MIN_DURATION = 3
MAX_DURATION = 15
_ALLOWED_RESOLUTIONS = ("480p", "720p", "1080p")


def _client():
    """Lazy import + auth check (mirrors fal_veo/fal_kling). Raises
    ProviderNotConfigured when FAL_API_KEY is missing."""
    if not settings.fal_api_key:
        raise ProviderNotConfigured(
            "FAL_API_KEY not set — sign up at https://fal.ai/dashboard/keys "
            "and add `FAL_API_KEY=fal_...` to your .env"
        )
    try:
        import fal_client  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "fal-client package not installed. Run `uv add fal-client` and retry."
        ) from e
    os.environ["FAL_KEY"] = settings.fal_api_key
    return fal_client


def clamp_duration(duration_secs: int | None) -> int:
    """Snap a requested duration into Grok Imagine 1.5's 3–15s integer range."""
    try:
        d = int(duration_secs) if duration_secs else 5
    except (TypeError, ValueError):
        d = 5
    return max(MIN_DURATION, min(MAX_DURATION, d))


def _resolution() -> str:
    """Effective render resolution. Configurable via GROK_FAL_RESOLUTION
    (480p/720p/1080p), default 720p. Unlike Veo, Grok 1.5 accepts every
    resolution at every duration, so there is no per-duration downgrade."""
    r = (settings.grok_fal_resolution or "720p").strip().lower()
    if r not in _ALLOWED_RESOLUTIONS:
        r = "720p"
    return r


def submit_image_to_video(
    *,
    image: Path,
    prompt: str,
    duration_secs: int | None = 6,
    app_job_id: str | None = None,
) -> str:
    """Upload the start frame, submit a Grok Imagine 1.5 i2v job, return the fal
    `request_id` for polling in `wait_for_video`."""
    _check_account_block()
    fal = _client()
    dur = clamp_duration(duration_secs)
    with call_log.record(
        phase="grok_fal_submit", model=_ENDPOINT, character="grok-imagine-1.5",
        job_id=app_job_id, duration_secs=dur,
    ) as payload:
        try:
            start_url = fal.upload_file(str(image))
        except Exception as e:
            if _is_account_error(e):
                _trip_account_block(e)
                raise FalAccountError(
                    f"fal account cannot accept work: {e}") from e
            raise RuntimeError(f"fal.upload_file failed: {e}") from e
        payload["upload_url"] = start_url

        arguments = {
            "image_url": start_url,
            "prompt": (prompt or "")[:2500],
            "duration": dur,                # Grok expects a plain INTEGER
            "resolution": _resolution(),
        }
        try:
            handler = fal.submit(_ENDPOINT, arguments=arguments)
        except Exception as e:
            if _is_account_error(e):
                _trip_account_block(e)
                raise FalAccountError(
                    f"fal account cannot accept work: {e}") from e
            raise RuntimeError(f"fal {_ENDPOINT} submit failed: {e}") from e
        request_id = handler.request_id
        payload["request_id"] = request_id
        return request_id


def wait_for_video(
    *,
    request_id: str,
    dest: Path,
    app_job_id: str | None = None,
    timeout_secs: int | None = None,
    poll_secs: int | None = None,
) -> Path:
    """Poll the fal queue until the job completes, then download the MP4 to
    `dest`. Raises RuntimeError on timeout / missing output."""
    fal = _client()
    import fal_client  # type: ignore
    timeout = timeout_secs or settings.video_timeout_secs
    interval = poll_secs or max(5, settings.video_poll_interval_secs)

    with call_log.record(
        phase="grok_fal_wait", model=_ENDPOINT, character="grok-imagine-1.5",
        job_id=app_job_id,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = fal.status(_ENDPOINT, request_id, with_logs=False)
            if isinstance(status, fal_client.Completed):
                break
            time.sleep(interval)
        else:
            raise RuntimeError(
                f"fal Grok Imagine 1.5 job {request_id} timed out after {timeout}s"
            )
        result = fal.result(_ENDPOINT, request_id)

    video = result.get("video") if isinstance(result, dict) else None
    if not video or not isinstance(video, dict) or not video.get("url"):
        raise RuntimeError(
            f"fal Grok Imagine 1.5 response missing video.url; got {result!r}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    _download(video["url"], dest)
    return dest


def _download(url: str, dest: Path, *, attempts: int = 3) -> None:
    """Download with transient-error retries (mirrors fal_veo._download): a
    connection reset / SSL hiccup on the FINISHED clip used to fail the whole
    already-billed generation."""
    import ssl
    last: Exception | None = None
    for i in range(attempts):
        try:
            with httpx.stream("GET", url, timeout=180,
                              follow_redirects=True) as r:
                r.raise_for_status()
                with dest.open("wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)
            return
        except (httpx.TransportError, ssl.SSLError) as e:
            last = e
            dest.unlink(missing_ok=True)
            if i < attempts - 1:
                time.sleep(2.0 * (i + 1))
    raise RuntimeError(
        f"download failed after {attempts} attempts: {last}") from last
