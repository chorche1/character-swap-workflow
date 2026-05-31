"""Kling AI image-to-video client.

Implements the official Kling API at `https://api-singapore.klingai.com`
(international region). Auth is a short-lived (30 min) HS256 JWT signed
with the user's access_key + secret_key pair from app.klingai.com.

Wired into the Swap-flow video step + the freeform Video tab via the
`kling-*` model strings in `runner_media.IMAGE_MODELS`. See
`KLING_MODELS` below for the exact string → API-name mapping.

Notes on the API shape (May 2026):
- `image` accepts either a public URL or a base64 string (no `data:`
  prefix). We always base64 the local file so we don't have to host
  Hugo's input frames anywhere reachable from Kling's servers.
- `duration` is a STRING — "5" or "10" — not an int.
- Status values: submitted → processing → succeed | failed (note
  "succeed" not "succeeded").
- The output video URL `data.task_result.videos[0].url` expires shortly
  after completion, so the caller downloads immediately.
"""
from __future__ import annotations

import base64
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any

import httpx

from character_swap.call_log import record
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


# International region — use this from US/EU. The mainland-China host
# (api.klingai.com) accepts the same shape but applies different regional
# policies; international tokens may not authenticate against it.
BASE_URL = "https://api-singapore.klingai.com"

# Per-Kling-version strings the API accepts in `model_name`. Drives the
# IMAGE_MODELS registry → frontend dropdown. Anything not in this dict
# silently falls back to "kling-v2-master" in `submit_kling` (defensive —
# unknown slugs shouldn't 400 the user mid-generate).
KLING_MODELS: dict[str, str] = {
    "kling-v1":           "kling-v1",
    "kling-v1-5":         "kling-v1-5",
    "kling-v1-6":         "kling-v1-6",
    "kling-v2-master":    "kling-v2-master",
    "kling-v2-1":         "kling-v2-1",
    "kling-v2-1-master":  "kling-v2-1-master",
    "kling-v2-5-turbo":   "kling-v2-5-turbo",
    "kling-v2-6":         "kling-v2-6",
    # NOTE: Kling 3.0 (`kling-v3`) is NOT here — the official Kling API only
    # generates 5s/10s. `kling-v3` is routed through fal.ai instead
    # (clients/fal_kling.py) because fal's Kling v3 accepts 3–15s durations.
    # See pipeline.submit_video / wait_for_video for the routing.
}

# Legacy slug aliases — keep existing IMAGE_MODELS entries working after
# the rename to canonical Kling API names. Hugo's existing jobs that
# reference these old strings still resolve.
LEGACY_ALIASES: dict[str, str] = {
    "kling":          "kling-v2-master",
    "kling-2.0":      "kling-v2-master",
    "kling-2.1-pro":  "kling-v2-1-master",
    "kling-1.6":      "kling-v1-6",
}


# --- JWT auth ---------------------------------------------------------------

# A signed token is good for 30 min. Refresh every ~25 to leave wall-clock
# slack. Thread-local because submit/poll happen on background workers.
_token_lock = threading.Lock()
_cached_token: str | None = None
_cached_expiry: float = 0.0


def _require_kling() -> None:
    if not (settings.kling_access_key and settings.kling_secret_key):
        raise ProviderNotConfigured(
            "Kling",
            "Add KLING_ACCESS_KEY and KLING_SECRET_KEY to .env "
            "(get them at https://app.klingai.com/ → API console).",
        )


def _build_jwt() -> str:
    """Sign a fresh JWT. PyJWT is a dependency — pyproject.toml pins >=2.8."""
    try:
        import jwt
    except ImportError as e:
        raise RuntimeError(
            "PyJWT not installed — run `uv sync` to pick up the dep."
        ) from e
    now = int(time.time())
    payload = {
        "iss": settings.kling_access_key,
        "exp": now + 1800,   # 30 min
        "nbf": now - 5,      # tolerate 5 s of clock skew
    }
    return jwt.encode(
        payload,
        settings.kling_secret_key,
        algorithm="HS256",
        headers={"alg": "HS256", "typ": "JWT"},
    )


def _get_token() -> str:
    """Return a valid Bearer token, regenerating once per ~25 min."""
    global _cached_token, _cached_expiry
    _require_kling()
    with _token_lock:
        if _cached_token and time.time() < _cached_expiry:
            return _cached_token
        _cached_token = _build_jwt()
        _cached_expiry = time.time() + 25 * 60   # refresh 5 min before exp
        return _cached_token


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_get_token()}",
        "Content-Type": "application/json",
    }


# --- Image encoding ---------------------------------------------------------

def _encode_image(path: Path, *, max_long_edge: int = 1920,
                  max_bytes: int = 9_500_000) -> str:
    """Base64-encode an image file for Kling's `image` field.

    Kling caps inputs at 10 MB and requires each side > 300 px. We resize
    on the long edge to keep base64-encoded payloads under 10 MB; long-edge
    1920 px is plenty for Kling's max output resolution.
    """
    raw = path.read_bytes()
    if len(raw) <= max_bytes:
        # Cheap path: no decode if the file's already small. Kling accepts
        # JPEG/PNG bytes as base64 directly.
        return base64.b64encode(raw).decode("ascii")

    try:
        from PIL import Image
    except ImportError as e:
        raise RuntimeError(
            "Pillow not installed — required to resize >10 MB Kling inputs."
        ) from e
    with Image.open(path) as img:
        w, h = img.size
        long_edge = max(w, h)
        if long_edge > max_long_edge:
            scale = max_long_edge / long_edge
            img = img.resize(
                (max(1, int(w * scale)), max(1, int(h * scale))),
                Image.LANCZOS,
            )
        buf = BytesIO()
        if path.suffix.lower() in (".jpg", ".jpeg"):
            img.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
        else:
            img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# --- API: submit + poll + download -----------------------------------------

def _resolve_model_name(model: str) -> str:
    """Map our internal slug → Kling's API model_name. Falls back to the
    safest current model (v2-master) when the slug isn't recognized so a
    typo doesn't 400 mid-generate."""
    if model in KLING_MODELS:
        return KLING_MODELS[model]
    if model in LEGACY_ALIASES:
        return LEGACY_ALIASES[model]
    return "kling-v2-master"


def submit_kling(
    *,
    image: Path,
    prompt: str,
    model: str = "kling-v2-master",
    aspect_ratio: str | None = None,
    duration_secs: int | None = None,
    mode: str = "pro",
    negative_prompt: str | None = None,
    cfg_scale: float = 0.5,
    app_job_id: str | None = None,
) -> str:
    """Submit an image-to-video job. Returns the Kling `task_id` for polling.

    Wrapped in `call_log.record(phase="kling_submit", ...)` so cost +
    latency land in calls.jsonl alongside every other provider.
    """
    _require_kling()
    model_name = _resolve_model_name(model)
    encoded = _encode_image(image)

    # Kling wants duration as a STRING, default 5. 10 is the only other
    # supported value; clamp anything outside to the nearest valid.
    if duration_secs is None or duration_secs <= 7:
        duration = "5"
    else:
        duration = "10"

    # Kling's i2v aspect_ratio field: 16:9 / 9:16 / 1:1. Drop unknown values
    # so the API picks its model-specific default.
    valid_aspects = {"16:9", "9:16", "1:1"}
    if aspect_ratio not in valid_aspects:
        aspect_ratio = None

    body: dict[str, Any] = {
        "model_name": model_name,
        "image": encoded,
        "prompt": (prompt or "")[:2500],
        "mode": mode,
        "duration": duration,
        "cfg_scale": cfg_scale,
    }
    if aspect_ratio:
        body["aspect_ratio"] = aspect_ratio
    if negative_prompt:
        body["negative_prompt"] = negative_prompt[:2500]

    with record(
        phase="kling_submit", model=model_name,
        character="kling", job_id=app_job_id,
    ):
        with httpx.Client(timeout=60.0) as c:
            r = c.post(
                f"{BASE_URL}/v1/videos/image2video",
                headers=_headers(),
                json=body,
            )
            r.raise_for_status()
            payload = r.json()

    if payload.get("code") not in (0, "0"):
        raise RuntimeError(
            f"Kling submit failed (code={payload.get('code')}): "
            f"{payload.get('message')}"
        )
    data = payload.get("data") or {}
    task_id = data.get("task_id")
    if not task_id:
        raise RuntimeError(f"Kling submit returned no task_id: {payload!r}")
    return str(task_id)


def _poll_status(task_id: str) -> dict[str, Any]:
    """One GET against the task. Returns the raw `data` dict from Kling."""
    with httpx.Client(timeout=30.0) as c:
        r = c.get(
            f"{BASE_URL}/v1/videos/image2video/{task_id}",
            headers=_headers(),
        )
        r.raise_for_status()
        payload = r.json()
    if payload.get("code") not in (0, "0"):
        raise RuntimeError(
            f"Kling poll failed (code={payload.get('code')}): "
            f"{payload.get('message')}"
        )
    return payload.get("data") or {}


def wait_for_kling(
    *,
    task_id: str,
    dest: Path,
    poll_interval: float | None = None,
    timeout_secs: float | None = None,
) -> Path:
    """Poll a Kling task until terminal, then download the resulting MP4
    to `dest`. Raises RuntimeError on `failed` or timeout.

    `poll_interval` + `timeout_secs` default to the existing
    `settings.video_poll_interval_secs` / `settings.video_timeout_secs`
    so Kling behaves the same as Grok / Veo / Higgsfield from the
    runner's perspective.
    """
    _require_kling()
    poll = poll_interval if poll_interval is not None else settings.video_poll_interval_secs
    timeout = timeout_secs if timeout_secs is not None else settings.video_timeout_secs

    deadline = time.time() + timeout
    last_status = ""
    while True:
        data = _poll_status(task_id)
        status = (data.get("task_status") or "").lower()
        last_status = status
        if status in ("succeed", "succeeded"):
            break
        if status in ("failed", "fail", "error"):
            msg = data.get("task_status_msg") or "Kling task failed"
            raise RuntimeError(f"Kling task {task_id} failed: {msg}")
        if time.time() >= deadline:
            raise RuntimeError(
                f"Kling task {task_id} timed out after {timeout}s "
                f"(last status: {status})"
            )
        time.sleep(poll)

    # On success the URL lives at data.task_result.videos[0].url. URLs
    # expire shortly, so download right away to dest.
    task_result = data.get("task_result") or {}
    videos = task_result.get("videos") or []
    if not videos:
        raise RuntimeError(
            f"Kling task {task_id} reported {last_status} but no video URL"
        )
    video_url = videos[0].get("url")
    if not video_url:
        raise RuntimeError(
            f"Kling task {task_id} success response missing videos[0].url: "
            f"{task_result!r}"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    with httpx.stream("GET", video_url, timeout=120,
                      follow_redirects=True) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_bytes(chunk_size=65536):
                f.write(chunk)
    return dest


# --- Smoke test helper ------------------------------------------------------

def test_credentials() -> dict[str, Any]:
    """Quick auth check — signs a token and pings the polling endpoint with
    an obviously-bogus task_id. A 4xx-with-Kling-error-body means auth
    worked (the task just doesn't exist). 401/403 means the JWT was
    rejected. Used by `character-swap kling-test` / a Chat-tab tool.
    """
    _require_kling()
    try:
        with httpx.Client(timeout=10.0) as c:
            r = c.get(
                f"{BASE_URL}/v1/videos/image2video/__doesnotexist__",
                headers=_headers(),
            )
        return {
            "ok": r.status_code != 401 and r.status_code != 403,
            "status_code": r.status_code,
            "body_snippet": r.text[:300],
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
