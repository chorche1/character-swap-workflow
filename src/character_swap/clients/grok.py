from __future__ import annotations

from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from character_swap.call_log import record
from character_swap.config import settings
from character_swap.images import encode_b64, media_type

# --- xAI Grok Imagine video API (per docs.x.ai/docs/guides/video-generations) -
SUBMIT_PATH = "/videos/generations"
STATUS_PATH = "/videos/{job_id}"
TERMINAL_STATES = {"done", "failed", "error", "cancelled"}
SUCCESS_STATES = {"done"}
# ------------------------------------------------------------------------------


class GrokError(Exception):
    pass


class JobFailed(GrokError):
    pass


class JobTimeout(GrokError):
    pass


_RETRY_EXCS = (
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)


def _headers() -> dict[str, str]:
    settings.require_keys("xai")
    return {
        "Authorization": f"Bearer {settings.xai_api_key}",
        "Content-Type": "application/json",
    }


def _client() -> httpx.Client:
    return httpx.Client(base_url=settings.grok_base_url, timeout=60, headers=_headers())


def _retryable_status(response: httpx.Response) -> bool:
    return response.status_code == 429 or 500 <= response.status_code < 600


@retry(
    retry=retry_if_exception_type(_RETRY_EXCS),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def submit(*, image: Path, prompt: str, character: str,
           app_job_id: str | None = None) -> str:
    # Per xAI docs, image is supplied via {"url": "..."}. We embed our local
    # image as a data URL so we don't need an external host.
    data_url = f"data:{media_type(image)};base64,{encode_b64(image)}"
    body = {
        "model": settings.grok_video_model,
        "prompt": prompt,
        "duration": settings.video_duration_secs,
        "aspect_ratio": settings.video_aspect_ratio,
        "resolution": settings.video_resolution,
        "image": {"url": data_url},
    }
    with record(
        phase="phase4_submit",
        model=settings.grok_video_model,
        character=character,
        job_id=app_job_id,
    ) as entry, _client() as h:
        r = h.post(SUBMIT_PATH, json=body)
        if _retryable_status(r):
            r.raise_for_status()
        if r.status_code >= 400:
            raise GrokError(f"Submit failed ({r.status_code}): {r.text[:500]}")
        data = r.json()
        job_id = data.get("request_id") or data.get("id") or data.get("job_id")
        if not job_id:
            raise GrokError(f"Submit response missing job id. body={data!r}")
        entry["request_id"] = r.headers.get("x-request-id")
        entry["job_id"] = job_id
    return job_id


@retry(
    retry=retry_if_exception_type(_RETRY_EXCS),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def status(*, job_id: str, character: str,
           app_job_id: str | None = None) -> dict:
    with record(
        phase="phase4_poll",
        model=settings.grok_video_model,
        character=character,
        grok_job_id=job_id,
        job_id=app_job_id,
    ) as entry, _client() as h:
        r = h.get(STATUS_PATH.format(job_id=job_id))
        if _retryable_status(r):
            r.raise_for_status()
        if r.status_code >= 400:
            raise GrokError(f"Status failed ({r.status_code}): {r.text[:500]}")
        entry["request_id"] = r.headers.get("x-request-id")
        return r.json()


@retry(
    retry=retry_if_exception_type(_RETRY_EXCS),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=2, min=2, max=60),
    reraise=True,
)
def generate_image(*, prompt: str, character: str = "freeform",
                   aspect_ratio: str | None = None,
                   app_job_id: str | None = None) -> bytes:
    """Free-form image generation via xAI's images endpoint.

    Returns raw PNG bytes. Reference images are not part of the request body
    here — Grok's image gen API is text-only as of writing.
    """
    body: dict = {
        "model": settings.grok_image_model,
        "prompt": prompt,
        "n": 1,
        "response_format": "b64_json",
    }
    if aspect_ratio:
        body["aspect_ratio"] = aspect_ratio
    with record(
        phase="image_grok",
        model=settings.grok_image_model,
        character=character,
        job_id=app_job_id,
    ) as entry, _client() as h:
        r = h.post("/images/generations", json=body)
        if _retryable_status(r):
            r.raise_for_status()
        if r.status_code >= 400:
            raise GrokError(f"Image generation failed ({r.status_code}): {r.text[:500]}")
        data = r.json()
        entry["request_id"] = r.headers.get("x-request-id")

    items = data.get("data") or []
    if not items:
        raise GrokError(f"Image response empty. body={data!r}")
    item = items[0]
    b64 = item.get("b64_json")
    if b64:
        import base64
        return base64.b64decode(b64)
    url = item.get("url")
    if url:
        with httpx.Client(timeout=120) as h:
            rr = h.get(url)
            rr.raise_for_status()
            return rr.content
    raise GrokError(f"Image response had neither b64_json nor url. body={data!r}")


def download_video(*, url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with httpx.stream("GET", url, timeout=300) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=1 << 20):
                f.write(chunk)
    if tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise GrokError(f"Downloaded video is empty: {url}")
    import os

    os.replace(tmp, dest)
