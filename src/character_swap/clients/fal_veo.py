"""fal.ai Veo 3.1 Fast image-to-video client.

Routes the `veo-3.1-fast` model through fal.ai's Veo endpoint, mirroring
clients/fal_kling.py (same FAL_API_KEY auth, same upload → submit → poll →
download two-phase shape the swap runner expects, same shared account-level
circuit breaker so a balance/locked error fails siblings fast).

Why fal and not Gemini: the app already routes Kling 3.0 through fal, so a
second fal-hosted video model needs no new credentials, and fal exposes Veo
3.1 Fast (which the Gemini path doesn't carry — it only has Veo 3 / Veo 3
Fast).

API: https://fal.ai/models/fal-ai/veo3.1/fast/image-to-video
  image_url       (required)  URL or data URI — we upload the local frame first
  prompt          (required)  motion prompt
  duration        (enum str)  "4s" | "6s" | "8s"  (default "8s")
  aspect_ratio    (enum)      "auto" | "16:9" | "9:16"  (default "auto")
  resolution      (enum)      "720p" | "1080p" | "4k"  (default "720p")
  generate_audio  (bool)      native audio (default true)
  negative_prompt (string)    optional
Response: {video: {url, ...}, ...}  — identical shape to fal Kling v3.

NOTE: this endpoint has NO end-frame input (fal exposes start→end only via a
separate `first-last-frame-to-video` endpoint), so a scene overridden to this
model ignores any end pose — matching the per-scene soft-degrade in the runner.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from character_swap import call_log
from character_swap.clients import ProviderNotConfigured
# Share the fal account-level circuit breaker: a balance/locked rejection is
# account-wide, so one tripped breaker should pause Kling AND Veo submits alike.
from character_swap.clients.fal_kling import (
    FalAccountError,
    _check_account_block,
    _is_account_error,
    _trip_account_block,
)
from character_swap.config import settings

_ENDPOINT = "fal-ai/veo3.1/fast/image-to-video"

# fal Veo 3.1 Fast duration enum (whole seconds, sent as "<n>s").
_ALLOWED_DURATIONS = (4, 6, 8)
_ALLOWED_RESOLUTIONS = ("720p", "1080p", "4k")
_ALLOWED_ASPECTS = ("auto", "16:9", "9:16")


def _client():
    """Lazy import + auth check (mirrors fal_kling). Raises
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
    """Snap a requested duration to Veo's nearest accepted bucket {4,6,8}."""
    try:
        d = int(duration_secs) if duration_secs else 8
    except (TypeError, ValueError):
        d = 8
    return min(_ALLOWED_DURATIONS, key=lambda v: abs(v - d))


def _resolution(dur: int | None = None) -> str:
    """Effective render resolution. fal's Veo 3.1 Fast only accepts 1080p (and
    4k) at duration=8s — for 4s/6s clips it rejects anything above 720p with a
    `value_error` ("1080p resolution is only supported with a duration of 8s").
    Reengineer clip length is dictated by the scene, so we can't force 8s;
    instead we downgrade to 720p for sub-8s clips so the clip RENDERS at 720p
    instead of failing outright (Hugo 2026-06-19). 8s clips keep the configured
    VEO_FAL_RESOLUTION (1080p by default)."""
    r = (settings.veo_fal_resolution or "1080p").strip().lower()
    if r not in _ALLOWED_RESOLUTIONS:
        r = "1080p"
    if dur is not None and dur != 8 and r in ("1080p", "4k"):
        return "720p"
    return r


def _aspect_ratio(aspect_ratio: str | None) -> str:
    """Veo accepts auto/16:9/9:16. Pass a portrait/landscape request straight
    through; anything else (1:1, None, …) → 'auto' (Veo derives it from the
    input frame)."""
    ar = (aspect_ratio or "").strip()
    return ar if ar in _ALLOWED_ASPECTS else "auto"


def submit_image_to_video(
    *,
    image: Path,
    prompt: str,
    duration_secs: int | None = 8,
    aspect_ratio: str | None = None,
    generate_audio: bool = True,
    app_job_id: str | None = None,
) -> str:
    """Upload the start frame, submit a Veo 3.1 Fast i2v job, return the fal
    `request_id` for polling in `wait_for_video`."""
    _check_account_block()
    fal = _client()
    dur = clamp_duration(duration_secs)
    with call_log.record(
        phase="veo_fal_submit", model=_ENDPOINT, character="veo-3.1-fast",
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
            "duration": f"{dur}s",          # fal expects the enum as "<n>s"
            "aspect_ratio": _aspect_ratio(aspect_ratio),
            "resolution": _resolution(dur),
            "generate_audio": generate_audio,
        }
        # Reuse the shared talking-head negative set (empty → field omitted).
        neg = (settings.kling_negative_prompt or "").strip()
        if neg:
            arguments["negative_prompt"] = neg[:2500]
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
        phase="veo_fal_wait", model=_ENDPOINT, character="veo-3.1-fast",
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
                f"fal Veo 3.1 Fast job {request_id} timed out after {timeout}s"
            )
        result = fal.result(_ENDPOINT, request_id)

    video = result.get("video") if isinstance(result, dict) else None
    if not video or not isinstance(video, dict) or not video.get("url"):
        raise RuntimeError(
            f"fal Veo 3.1 Fast response missing video.url; got {result!r}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    _download(video["url"], dest)
    return dest


def _download(url: str, dest: Path, *, attempts: int = 3) -> None:
    """Download with transient-error retries (mirrors fal_kling._download): a
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
