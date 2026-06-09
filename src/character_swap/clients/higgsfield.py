"""Higgsfield official REST API client — powers the Swap "Higgsfield Character
Swap" model.

This wraps Higgsfield's documented platform API (https://platform.higgsfield.ai),
authenticated with a STATIC key+secret pair (Authorization: Key {key}:{secret})
created at cloud.higgsfield.ai/api-keys. It is the headless equivalent of the
"Character Swap" feature in the Higgsfield web app: a saved character reference
(custom_reference_id) + a scene image are passed together into Soul.

Character-swap flow (one variant):
  1. Upload scene + character bytes  -> public CDN URLs
     POST /files/generate-upload-url {content_type} -> {upload_url, public_url}
     PUT  {upload_url} <bytes>
  2. Ensure a character reference (created once per character image, cached)
     POST /v1/custom-references {name, input_images:[{type:image_url,image_url}]}
     poll GET /v1/custom-references/{id} until completed
  3. Generate the swap
     POST /v1/text2image/soul {params:{prompt, custom_reference_id,
          custom_reference_strength, <scene_field>:{type:image_url,image_url},
          width_and_height, quality, batch_size}}
     poll the returned job until completed
  4. Download the result image bytes.

Endpoints + auth + upload flow are taken verbatim from Higgsfield's official
SDKs (higgsfield-ai/higgsfield-client, higgsfield-ai/higgsfield-js). The only
field confirmed at runtime is which key carries the scene image on the soul
call (settings.higgsfield_scene_field, default "image_reference").

Returns raw PNG/JPEG bytes; sync (matches grok.py / google_genai.py), called
from pipeline via asyncio.to_thread.
"""
from __future__ import annotations

import json
import mimetypes
import threading
import time
from pathlib import Path

import httpx

from character_swap.call_log import record
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings
from character_swap.images import sha256_file

_TIMEOUT = 90.0
_POLL_INTERVAL_SECS = 3.0
_POLL_TIMEOUT_SECS = 300.0

# Soul accepts a fixed set of width_and_height strings; map our aspect ratios to
# the nearest. Swap is 9:16 portrait by default.
_SIZE_BY_ASPECT = {
    "9:16": "1152x2048",
    "16:9": "2048x1152",
    "1:1": "1536x1536",
    "3:4": "1536x2048",
    "4:3": "2048x1536",
}
_DEFAULT_SIZE = "1152x2048"

# On-disk cache: character-image sha256 -> reference id (persists across restarts
# so a job with N scenes/variants reuses ONE Higgsfield reference per character).
_REF_CACHE_LOCK = threading.Lock()
# In-memory cache: file sha256 -> uploaded public URL (avoids re-uploading the
# same scene/character across variants within a process).
_UPLOAD_CACHE: dict[str, str] = {}
_UPLOAD_CACHE_LOCK = threading.Lock()


class HiggsfieldError(Exception):
    pass


def _credential() -> str:
    key = settings.higgsfield_api_key
    secret = settings.higgsfield_api_secret
    if not key or not secret:
        raise ProviderNotConfigured(
            "Higgsfield",
            "Set HIGGSFIELD_API_KEY and HIGGSFIELD_API_SECRET (create a key+secret "
            "at cloud.higgsfield.ai/api-keys).",
        )
    return f"{key}:{secret}"


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.higgsfield_base_url,
        timeout=_TIMEOUT,
        headers={
            "Authorization": f"Key {_credential()}",
            "Content-Type": "application/json",
            "User-Agent": "character-swap-studio/1.0",
        },
    )


def _raise_for_status(r: httpx.Response, what: str) -> None:
    if r.status_code == 401:
        raise HiggsfieldError(f"{what}: invalid Higgsfield API credentials (401)")
    if r.status_code == 403:
        raise HiggsfieldError(f"{what}: not enough Higgsfield credits (403)")
    if r.status_code >= 400:
        raise HiggsfieldError(f"{what}: HTTP {r.status_code} {r.text[:300]}")


# --- reference-id cache (on disk) ------------------------------------------------

def _ref_cache_path() -> Path:
    return settings.state_dir / "higgsfield_refs.json"


def _load_ref_cache() -> dict[str, str]:
    p = _ref_cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_ref(sha: str, reference_id: str) -> None:
    with _REF_CACHE_LOCK:
        cache = _load_ref_cache()
        cache[sha] = reference_id
        p = _ref_cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, indent=2), encoding="utf-8")
        tmp.replace(p)


# --- upload ----------------------------------------------------------------------

def _content_type(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "application/octet-stream"


def _upload(path: Path, client: httpx.Client) -> str:
    """Upload a local image and return its public CDN URL (cached by sha256)."""
    sha = sha256_file(path)
    with _UPLOAD_CACHE_LOCK:
        if sha in _UPLOAD_CACHE:
            return _UPLOAD_CACHE[sha]
    ctype = _content_type(path)
    r = client.post("/files/generate-upload-url", json={"content_type": ctype})
    _raise_for_status(r, "generate-upload-url")
    body = r.json()
    upload_url, public_url = body["upload_url"], body["public_url"]
    # The presigned PUT must NOT carry the API auth header — use a bare client.
    with httpx.Client(timeout=_TIMEOUT) as raw:
        put = raw.put(upload_url, content=path.read_bytes(),
                      headers={"Content-Type": ctype})
        _raise_for_status(put, "upload PUT")
    with _UPLOAD_CACHE_LOCK:
        _UPLOAD_CACHE[sha] = public_url
    return public_url


# --- character reference ---------------------------------------------------------

def _ensure_reference(character_image: Path, client: httpx.Client) -> str:
    """Return a Higgsfield custom-reference id for this character, creating it
    once and caching by the character image's sha256."""
    sha = sha256_file(character_image)
    cached = _load_ref_cache().get(sha)
    if cached:
        return cached

    char_url = _upload(character_image, client)
    r = client.post(
        "/v1/custom-references",
        json={"name": f"cs_{sha[:12]}",
              "input_images": [{"type": "image_url", "image_url": char_url}]},
    )
    _raise_for_status(r, "create custom-reference")
    data = r.json()
    ref_id = data.get("id") or data.get("reference_id")
    if not ref_id:
        raise HiggsfieldError(f"create custom-reference: no id in response {data}")

    # Poll until the reference finishes training (status completed/failed).
    deadline = time.monotonic() + _POLL_TIMEOUT_SECS
    while True:
        status = (data.get("status") or "").lower()
        if status in {"completed", "ready", "succeeded"}:
            break
        if status in {"failed", "error"}:
            raise HiggsfieldError(f"custom-reference {ref_id} failed: {data}")
        if time.monotonic() > deadline:
            raise HiggsfieldError(f"custom-reference {ref_id} timed out")
        time.sleep(_POLL_INTERVAL_SECS)
        pr = client.get(f"/v1/custom-references/{ref_id}")
        _raise_for_status(pr, "poll custom-reference")
        data = pr.json()

    _save_ref(sha, ref_id)
    return ref_id


# --- result extraction (robust to job-set vs generic shapes) ---------------------

def _extract_status(data: dict) -> str:
    """Normalize a poll payload to one of: completed | failed | nsfw | pending."""
    top = (data.get("status") or "").lower()
    if top in {"completed", "failed", "nsfw", "canceled", "cancelled"}:
        return "failed" if top in {"canceled", "cancelled"} else top
    jobs = data.get("jobs") or []
    if jobs:
        sts = [(j.get("status") or "").lower() for j in jobs]
        if any(s in {"failed", "error"} for s in sts):
            return "failed"
        if any(s == "nsfw" for s in sts):
            return "nsfw"
        if all(s in {"completed", "succeeded"} for s in sts):
            return "completed"
        return "pending"
    return top or "pending"


def _extract_result_url(data: dict) -> str | None:
    # generic completed-status shape
    imgs = data.get("images")
    if isinstance(imgs, list) and imgs and isinstance(imgs[0], dict) and imgs[0].get("url"):
        return imgs[0]["url"]
    # job-set shape
    for j in (data.get("jobs") or []):
        results = j.get("results") or {}
        for key in ("raw", "min"):
            node = results.get(key) or {}
            if node.get("url"):
                return node["url"]
    return None


def _job_set_id(data: dict) -> str | None:
    return data.get("id") or data.get("job_set_id") or data.get("request_id")


def _status_url(data: dict, job_set_id: str) -> str:
    # Generic API returns an explicit status_url; v1 job-sets use /v1/job-sets/{id}.
    return data.get("status_url") or f"/v1/job-sets/{job_set_id}"


# --- public API ------------------------------------------------------------------

def generate_swap(
    *,
    scene_image: Path,
    character_image: Path,
    prompt: str,
    aspect_ratio: str | None = None,
    app_job_id: str | None = None,
) -> bytes:
    """Run Higgsfield Character Swap: put `character_image`'s person into
    `scene_image`, preserving the scene. Returns the result image bytes.

    Raises HiggsfieldError on failure/nsfw, ProviderNotConfigured if creds are
    missing."""
    size = _SIZE_BY_ASPECT.get((aspect_ratio or "9:16"), _DEFAULT_SIZE)
    with record(phase="higgsfield_swap", model="higgsfield-swap",
                job_id=app_job_id) as entry, _client() as client:
        scene_url = _upload(scene_image, client)
        ref_id = _ensure_reference(character_image, client)
        entry["reference_id"] = ref_id

        params = {
            "prompt": prompt,
            "custom_reference_id": ref_id,
            "custom_reference_strength": 1.0,
            settings.higgsfield_scene_field: {"type": "image_url", "image_url": scene_url},
            "width_and_height": size,
            "quality": "1080p",
            "batch_size": 1,
        }
        r = client.post("/v1/text2image/soul", json={"params": params})
        _raise_for_status(r, "submit soul")
        data = r.json()

        # The submit response may already be terminal; only poll if pending
        # (which requires a job id to build the status URL).
        status = _extract_status(data)
        if status == "pending":
            job_set_id = _job_set_id(data)
            if not job_set_id:
                raise HiggsfieldError(f"submit soul: no job id in response {data}")
            entry["job_set_id"] = job_set_id
            status_url = _status_url(data, job_set_id)
            deadline = time.monotonic() + _POLL_TIMEOUT_SECS
            while status == "pending":
                if time.monotonic() > deadline:
                    raise HiggsfieldError(f"soul job {job_set_id} timed out")
                time.sleep(_POLL_INTERVAL_SECS)
                pr = client.get(status_url)
                _raise_for_status(pr, "poll soul")
                data = pr.json()
                status = _extract_status(data)
        else:
            entry["job_set_id"] = _job_set_id(data) or ""

        if status == "nsfw":
            raise HiggsfieldError("Higgsfield rejected the swap as NSFW (credits refunded)")
        if status != "completed":
            raise HiggsfieldError(f"Higgsfield soul job {job_set_id} {status}: {data}")

        url = _extract_result_url(data)
        if not url:
            raise HiggsfieldError(f"soul job {job_set_id} completed but no result URL: {data}")

        with httpx.Client(timeout=_TIMEOUT) as raw:
            dl = raw.get(url)
            _raise_for_status(dl, "download result")
            return dl.content
