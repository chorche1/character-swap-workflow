"""fal.ai ByteDance Seedance 2.0 image-to-video client.

Routes the `seedance-2.0` model through fal.ai's Seedance 2.0 image-to-video
endpoint, mirroring clients/fal_kling.py (same FAL_API_KEY auth, same
upload → submit → poll → download two-phase shape the swap runner expects,
same shared account-level circuit breaker, AND — like Kling — an optional
END FRAME that interpolates start → end).

Why fal: the app already routes Kling 3.0 / Veo 3.1 Fast / Grok 1.5 through
fal, so a fourth fal-hosted video model needs no new credentials. Seedance 2.0
is the only fal video model in this stack besides Kling 3.0 that supports
start→end-frame interpolation, so a scene overridden to it HONORS its 🎯 end
pose (runner._resolve_end_image gates end frames to the END_FRAME_VIDEO_MODELS
set, which includes both).

API: https://fal.ai/models/bytedance/seedance-2.0/image-to-video
  Tier picked by settings.seedance_fal_tier — "standard" (default; up to
  4k) or "fast" (cheaper, caps at 720p). Same request schema either way.
  image_url       (required)  URL or data URI — we upload the local frame first
  prompt          (required)  motion prompt
  duration        (int)       4–15 seconds (or "auto"; we always send an int)
  resolution      (enum)      "480p" | "720p" | "1080p" | "4k"  (default 720p)
  aspect_ratio    (enum)      auto/21:9/16:9/4:3/1:1/3:4/9:16  (default auto)
  end_image_url   (string)    optional final frame — clip interpolates to it
  generate_audio  (bool)      native synced audio (default true; audio is
                              included in the price regardless of this flag)
Response: {video: {url, ...}, ...}  — identical shape to fal Kling/Veo/Grok.

NOTE vs fal_kling: Seedance's START frame field is `image_url` (Kling uses
`start_image_url`), duration is a PLAIN INT (Kling sends a string), and there
is no negative_prompt field (Seedance has none). The fast tier rejects
resolutions above 720p, so we downgrade 1080p/4k → 720p when fast is active.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from character_swap import call_log
from character_swap.clients import ProviderNotConfigured
# Share the fal account-level circuit breaker: a balance/locked rejection is
# account-wide, so one tripped breaker pauses every fal video submit alike.
from character_swap.clients.fal_kling import (
    FalAccountError,
    _check_account_block,
    _is_account_error,
    _trip_account_block,
)
from character_swap.config import settings

# Seedance 2.0 duration is an integer 4–15s (or "auto"); we always send an int.
MIN_DURATION = 4
MAX_DURATION = 15
_ALLOWED_RESOLUTIONS = ("480p", "720p", "1080p", "4k")
_ALLOWED_ASPECTS = ("auto", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16")


def _client():
    """Lazy import + auth check (mirrors fal_kling). Raises ProviderNotConfigured
    when FAL_API_KEY is missing."""
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


def _tier() -> str:
    """Tier from settings.seedance_fal_tier — "standard" (default) or "fast"."""
    t = (settings.seedance_fal_tier or "standard").strip().lower()
    return t if t in {"standard", "fast"} else "standard"


def _endpoint() -> str:
    """Tier-resolved endpoint id. Submit and poll must use the SAME tier —
    request_ids are endpoint-scoped at fal, so don't flip SEEDANCE_FAL_TIER
    while clips are in flight (a resumed poll on the other tier 404s → ↻ retry)."""
    return ("bytedance/seedance-2.0/fast/image-to-video" if _tier() == "fast"
            else "bytedance/seedance-2.0/image-to-video")


def clamp_duration(duration_secs: int | None) -> int:
    """Snap a requested duration into Seedance 2.0's 4–15s integer range."""
    try:
        d = int(duration_secs) if duration_secs else 5
    except (TypeError, ValueError):
        d = 5
    return max(MIN_DURATION, min(MAX_DURATION, d))


def _resolution() -> str:
    """Effective render resolution from SEEDANCE_FAL_RESOLUTION (default 720p).
    The FAST tier rejects anything above 720p, so 1080p/4k downgrade to 720p
    when fast is active (the clip renders instead of failing)."""
    r = (settings.seedance_fal_resolution or "720p").strip().lower()
    if r not in _ALLOWED_RESOLUTIONS:
        r = "720p"
    if _tier() == "fast" and r in ("1080p", "4k"):
        return "720p"
    return r


def _aspect_ratio(aspect_ratio: str | None) -> str:
    """Pass a supported aspect through; anything else (None, …) → 'auto'
    (Seedance derives it from the input frame)."""
    ar = (aspect_ratio or "").strip()
    return ar if ar in _ALLOWED_ASPECTS else "auto"


def submit_image_to_video(
    *,
    image: Path,
    prompt: str,
    duration_secs: int | None = 5,
    aspect_ratio: str | None = None,
    generate_audio: bool = True,
    end_image: Path | None = None,
    app_job_id: str | None = None,
) -> str:
    """Upload the start frame, submit a Seedance 2.0 i2v job, return the fal
    `request_id` for polling in `wait_for_video`.

    `end_image` (optional) is uploaded as `end_image_url` so the clip
    interpolates from the start frame to this final frame."""
    _check_account_block()
    fal = _client()
    dur = clamp_duration(duration_secs)
    with call_log.record(
        phase="seedance_fal_submit", model=_endpoint(), character="seedance-2.0",
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
            "image_url": start_url,         # Seedance's START frame field
            "prompt": (prompt or "")[:2500],
            "duration": dur,                # plain INTEGER seconds
            "resolution": _resolution(),
            "aspect_ratio": _aspect_ratio(aspect_ratio),
            "generate_audio": generate_audio,
        }
        if end_image is not None:
            try:
                end_url = fal.upload_file(str(end_image))
            except Exception as e:
                raise RuntimeError(f"fal.upload_file (end frame) failed: {e}") from e
            arguments["end_image_url"] = end_url
            payload["end_upload_url"] = end_url
        try:
            handler = fal.submit(_endpoint(), arguments=arguments)
        except Exception as e:
            if _is_account_error(e):
                _trip_account_block(e)
                raise FalAccountError(
                    f"fal account cannot accept work: {e}") from e
            raise RuntimeError(f"fal {_endpoint()} submit failed: {e}") from e
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
        phase="seedance_fal_wait", model=_endpoint(), character="seedance-2.0",
        job_id=app_job_id,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = fal.status(_endpoint(), request_id, with_logs=False)
            if isinstance(status, fal_client.Completed):
                break
            time.sleep(interval)
        else:
            raise RuntimeError(
                f"fal Seedance 2.0 job {request_id} timed out after {timeout}s"
            )
        result = fal.result(_endpoint(), request_id)

    video = result.get("video") if isinstance(result, dict) else None
    if not video or not isinstance(video, dict) or not video.get("url"):
        raise RuntimeError(
            f"fal Seedance 2.0 response missing video.url; got {result!r}")

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
