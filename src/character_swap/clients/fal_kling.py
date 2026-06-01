"""fal.ai Kling Video v3 image-to-video client.

The OFFICIAL Kling API (clients/kling.py) only generates 5s or 10s clips — its
`duration` field is an enum of exactly {"5","10"}. fal.ai's Kling v3 endpoint
instead accepts any duration 3–15s, so we route the `kling-v3` model through
here to give per-second clip lengths (the thing the official API can't do).

Auth + upload mirror clients/fal_veed.py: FAL_API_KEY → FAL_KEY env, fal_client
for upload + submit. Submit returns the fal `request_id`; `wait_for_video`
polls the queue and downloads the finished MP4 — matching the
submit_video / wait_for_video two-phase shape the swap runner expects.

API: https://fal.ai/models/fal-ai/kling-video/v3/standard/image-to-video
  start_image_url  (required)  URL or data URI — we upload the local frame first
  prompt           (string)    motion prompt
  duration         (enum str)  "3".."15" seconds (default "5")
  generate_audio   (bool)      native audio (default true)
Response: {video: {url, ...}, ...}
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

from character_swap import call_log
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


ENDPOINT = "fal-ai/kling-video/v3/standard/image-to-video"

# fal Kling v3 duration is an enum of whole seconds 3..15.
MIN_DURATION = 3
MAX_DURATION = 15


def _client():
    """Lazy import + auth check (mirrors fal_veed). Raises ProviderNotConfigured
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


def clamp_duration(duration_secs: int | None) -> int:
    """Snap a requested duration into Kling v3's accepted 3–15s range."""
    try:
        d = int(duration_secs) if duration_secs else 5
    except (TypeError, ValueError):
        d = 5
    return max(MIN_DURATION, min(MAX_DURATION, d))


def submit_image_to_video(
    *,
    image: Path,
    prompt: str,
    duration_secs: int | None = 5,
    generate_audio: bool = True,
    app_job_id: str | None = None,
) -> str:
    """Upload the start frame, submit a Kling v3 i2v job, return the fal
    `request_id` for polling in `wait_for_video`."""
    fal = _client()
    dur = clamp_duration(duration_secs)
    with call_log.record(
        phase="kling_fal_submit", model=ENDPOINT, character="kling-v3",
        job_id=app_job_id, duration_secs=dur,
    ) as payload:
        try:
            start_url = fal.upload_file(str(image))
        except Exception as e:
            raise RuntimeError(f"fal.upload_file failed: {e}") from e
        payload["upload_url"] = start_url

        arguments = {
            "start_image_url": start_url,
            "prompt": (prompt or "")[:2500],
            "duration": str(dur),           # fal expects the enum as a string
            "generate_audio": generate_audio,
        }
        try:
            handler = fal.submit(ENDPOINT, arguments=arguments)
        except Exception as e:
            raise RuntimeError(f"fal {ENDPOINT} submit failed: {e}") from e
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
        phase="kling_fal_wait", model=ENDPOINT, character="kling-v3",
        job_id=app_job_id,
    ):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = fal.status(ENDPOINT, request_id, with_logs=False)
            if isinstance(status, fal_client.Completed):
                break
            time.sleep(interval)
        else:
            raise RuntimeError(
                f"fal Kling v3 job {request_id} timed out after {timeout}s"
            )
        result = fal.result(ENDPOINT, request_id)

    video = result.get("video") if isinstance(result, dict) else None
    if not video or not isinstance(video, dict) or not video.get("url"):
        raise RuntimeError(f"fal Kling v3 response missing video.url; got {result!r}")

    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", video["url"], timeout=180, follow_redirects=True) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
    return dest
