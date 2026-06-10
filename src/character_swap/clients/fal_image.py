"""fal.ai image-edit client — powers the scene-preserving Swap engines hosted
on fal (Qwen Image Edit+, FLUX Kontext Max, Seedream Edit, ...).

These are INSTRUCTION-EDIT models: they take the scene image + the character
image together with an imperative prompt ("replace the person in image 1 with
the person from image 2, keep everything else identical") and edit in place —
which is exactly the strict scene-preservation behavior the Swap flow needs
(unlike reinterpretation engines such as Higgsfield Soul).

Transport: fal queue API.
  POST https://queue.fal.run/{model_id}   (Authorization: Key <FAL_API_KEY>)
  -> {status_url, response_url}; poll status_url until COMPLETED; GET
  response_url -> {images: [{url}]}. Local images are first uploaded to fal's
  CDN via fal_client.upload_file (cached per content sha256) — multi-MB base64
  data URIs in the JSON body intermittently broke TLS during the overnight
  eval (SSLV3_ALERT_BAD_RECORD_MAC), so URLs are the only transport.

Per-model payload quirks are confined to _payload_for(). Sync; called from the
runner via asyncio.to_thread (same pattern as grok.py / higgsfield.py).
"""
from __future__ import annotations

import base64
import os
import threading
import time
from pathlib import Path

import httpx

from character_swap.call_log import record
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings
from character_swap.images import sha256_file

_QUEUE_BASE = "https://queue.fal.run"
_TIMEOUT = 120.0
_POLL_INTERVAL_SECS = 3.0
_POLL_TIMEOUT_SECS = 420.0

# Swap-engine slugs (as exposed in the app's model picker) -> fal model ids.
# Set chosen by the 2026-06-10 overnight bake-off (56 judged generations across
# 2 real scene pairs; see eval_out/gallery.html):
#   nbp-swap      7.2-7.6 composite, zero fatals, handled the moderation-
#                 sensitive pair GPT Image refused. The clear winner.
#   nb2-swap      6.3-6.8, same interface at about half the price.
#   seedream-...  6.5-6.75, cheapest credible tier; weaker identity match.
# Eliminated by the same bake-off: qwen-image-edit-plus (ignored the scene,
# fidelity 1-2/10), flux kontext multi (identity 1/10 + all-black censorship
# frames), higgsfield soul (regenerates an unrelated scene), easel (deprecated
# + wrong identity), ideogram mask-inpaint (mask swallowed key props).
SWAP_MODELS: dict[str, str] = {
    "nbp-swap":           "fal-ai/nano-banana-pro/edit",
    "nb2-swap":           "fal-ai/nano-banana-2/edit",
    "seedream-edit-swap": "fal-ai/bytedance/seedream/v4.5/edit",
    # Back-compat: jobs created while these were selectable keep working,
    # but the slugs are no longer offered in the picker.
    "qwen-edit-swap":     "fal-ai/qwen-image-edit-plus",
    "kontext-max-swap":   "fal-ai/flux-pro/kontext/max/multi",
}


class FalError(Exception):
    pass


def _headers() -> dict[str, str]:
    if not settings.fal_api_key:
        raise ProviderNotConfigured("fal.ai", "Set FAL_API_KEY in .env.")
    return {"Authorization": f"Key {settings.fal_api_key}",
            "Content-Type": "application/json"}


# Uploaded-URL cache: file sha256 -> fal CDN URL (per process).
_UPLOAD_CACHE: dict[str, str] = {}
_UPLOAD_LOCK = threading.Lock()


def _hosted_url(path: Path) -> str:
    """Upload a local image to fal's CDN (cached by content hash)."""
    sha = sha256_file(path)
    with _UPLOAD_LOCK:
        cached = _UPLOAD_CACHE.get(sha)
    if cached:
        return cached
    if not settings.fal_api_key:
        raise ProviderNotConfigured("fal.ai", "Set FAL_API_KEY in .env.")
    os.environ.setdefault("FAL_KEY", settings.fal_api_key)
    import fal_client
    url = fal_client.upload_file(str(path))
    with _UPLOAD_LOCK:
        _UPLOAD_CACHE[sha] = url
    return url


def _payload_for(model_id: str, *, prompt: str, scene_image: Path,
                 character_image: Path, aspect_ratio: str,
                 extra_reference_image: Path | None = None) -> dict:
    """Two-image edit payload (+ optional 3rd reference, e.g. a replacement
    background), with per-model sizing quirks."""
    images = [_hosted_url(scene_image), _hosted_url(character_image)]
    if extra_reference_image is not None:
        images.append(_hosted_url(extra_reference_image))
    payload: dict = {"prompt": prompt, "image_urls": images, "num_images": 1}
    if "nano-banana" in model_id:
        payload["aspect_ratio"] = aspect_ratio        # e.g. "9:16"
        payload["resolution"] = "1K"
    elif "kontext" in model_id:
        payload["aspect_ratio"] = aspect_ratio
    elif "seedream" in model_id:
        w, h = (1152, 2048) if aspect_ratio == "9:16" else (2048, 1152) if aspect_ratio == "16:9" else (1536, 1536)
        payload["image_size"] = {"width": w, "height": h}
    return payload


def _poll(submit: dict, model_id: str, client: httpx.Client) -> dict:
    status_url = submit.get("status_url")
    response_url = submit.get("response_url")
    if not status_url:                      # synchronous response
        return submit
    deadline = time.monotonic() + _POLL_TIMEOUT_SECS
    while True:
        if time.monotonic() > deadline:
            raise FalError(f"{model_id}: timed out after {int(_POLL_TIMEOUT_SECS)}s")
        sr = client.get(status_url)
        if sr.status_code >= 400:
            raise FalError(f"{model_id} status: HTTP {sr.status_code} {sr.text[:300]}")
        st = (sr.json().get("status") or "").upper()
        if st == "COMPLETED":
            break
        if st in {"FAILED", "ERROR"}:
            raise FalError(f"{model_id}: job failed — {sr.text[:300]}")
        time.sleep(_POLL_INTERVAL_SECS)
    rr = client.get(response_url)
    if rr.status_code >= 400:
        raise FalError(f"{model_id} result: HTTP {rr.status_code} {rr.text[:300]}")
    return rr.json()


def _first_image_bytes(resp: dict, model_id: str) -> bytes:
    imgs = resp.get("images") or ([resp["image"]] if resp.get("image") else [])
    if not imgs:
        raise FalError(f"{model_id}: no images in response (keys={list(resp.keys())})")
    url = imgs[0]["url"] if isinstance(imgs[0], dict) else imgs[0]
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    with httpx.Client(timeout=_TIMEOUT) as raw:
        d = raw.get(url)
        if d.status_code >= 400:
            raise FalError(f"{model_id} download: HTTP {d.status_code}")
        return d.content


def swap_image(
    *,
    model_slug: str,
    scene_image: Path,
    character_image: Path,
    prompt: str,
    aspect_ratio: str = "9:16",
    app_job_id: str | None = None,
    extra_reference_image: Path | None = None,
) -> bytes:
    """Run a scene-preserving person swap on a fal-hosted instruction-edit
    model. `model_slug` is one of SWAP_MODELS' keys. An optional third
    reference (Image 3 in the prompt — e.g. a replacement background) is
    appended after scene + character. Returns image bytes."""
    model_id = SWAP_MODELS.get(model_slug)
    if model_id is None:
        raise FalError(f"Unknown fal swap model slug: {model_slug}")
    payload = _payload_for(model_id, prompt=prompt, scene_image=scene_image,
                           character_image=character_image, aspect_ratio=aspect_ratio,
                           extra_reference_image=extra_reference_image)
    with record(phase="fal_swap", model=model_slug, job_id=app_job_id) as entry:
        with httpx.Client(timeout=_TIMEOUT, headers=_headers()) as client:
            r = client.post(f"{_QUEUE_BASE}/{model_id}", json=payload)
            if r.status_code >= 400:
                raise FalError(f"{model_id} submit: HTTP {r.status_code} {r.text[:300]}")
            sub = r.json()
            entry["request_id"] = sub.get("request_id", "")
            resp = _poll(sub, model_id, client)
        return _first_image_bytes(resp, model_id)
