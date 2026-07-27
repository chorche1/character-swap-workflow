"""Lossless Telegram Bot API transport for finished videos.

Final MP4s are always sent with ``sendDocument``. Telegram therefore stores
the uploaded file itself instead of treating it as a video to transcode, and a
downloaded copy is byte-identical to the local final.

The official cloud Bot API accepts multipart uploads up to 50 MB. Files above
that limit are rejected loudly — never re-encoded — unless TELEGRAM_API_BASE
points at a local Bot API server, whose documented upload limit is 2000 MB.
"""
from __future__ import annotations

import contextlib
import time
from pathlib import Path

import httpx

# Small headroom below the published limits prevents a multipart boundary or
# an implementation-specific MB/MiB interpretation from turning a valid local
# final into a mysterious remote 413. This only gates delivery; it never alters
# the source file.
_CLOUD_LIMIT = 49 * 1024 * 1024
_LOCAL_LIMIT = 1990 * 1024 * 1024


class TelegramNotConfigured(RuntimeError):
    """Raised when a required bot token or destination is missing."""


def _cloud_api(api_base: str) -> bool:
    return api_base.rstrip("/") == "https://api.telegram.org"


def _post_with_retry(url: str, *, data: dict, files: dict,
                     timeout: float, max_attempts: int = 3) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        for value in files.values():
            if isinstance(value, tuple) and len(value) >= 2:
                handle = value[1]
                if hasattr(handle, "seek"):
                    handle.seek(0)
        try:
            response = httpx.post(
                url, data=data, files=files, timeout=timeout)
            # 429 and 5xx are transient; honor Telegram's retry_after when
            # present, otherwise use a short exponential backoff.
            if response.status_code != 429 and response.status_code < 500:
                return response
            if attempt == max_attempts - 1:
                return response
            retry_after = 0
            with contextlib.suppress(ValueError, TypeError):
                retry_after = int(
                    response.json().get("parameters", {}).get("retry_after", 0))
            time.sleep(max(retry_after, 2 ** attempt))
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt == max_attempts - 1:
                raise RuntimeError(
                    f"Telegram request failed after {max_attempts} attempts: "
                    f"{exc}") from exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Telegram request failed: {last_error}")


def send_document(file_path: Path, *, bot_token: str, chat_id: str,
                  caption: str = "", filename: str | None = None,
                  api_base: str = "https://api.telegram.org",
                  timeout: float = 600.0) -> dict:
    """Upload the exact source bytes as a Telegram document."""
    token = (bot_token or "").strip()
    target = (chat_id or "").strip()
    if not token:
        raise TelegramNotConfigured("Telegram bot-token saknas.")
    if not target:
        raise TelegramNotConfigured("Telegram-kanal saknas.")
    source = Path(file_path)
    if not source.is_file():
        raise FileNotFoundError(f"Video file not found: {source.name}")

    api_root = api_base.rstrip("/")
    cloud = _cloud_api(api_root)
    limit = _CLOUD_LIMIT if cloud else _LOCAL_LIMIT
    size = source.stat().st_size
    if size > limit:
        mode = "vanliga Telegram Bot API" if cloud else "lokala Telegram Bot API"
        raise RuntimeError(
            f"Originalvideon är {size / 1024 / 1024:.1f} MB och överskrider "
            f"gränsen för {mode} ({limit / 1024 / 1024:.0f} MB). "
            "Videon skickades inte — den komprimeras aldrig. "
            "Anslut den lokala Telegram Bot API-servern för lossless leverans."
        )

    url = f"{api_root}/bot{token}/sendDocument"
    upload_name = filename or source.name
    with source.open("rb") as handle:
        response = _post_with_retry(
            url,
            data={
                "chat_id": target,
                "caption": caption[:1024],
                # Force document semantics even though the filename ends in
                # .mp4, so Telegram never treats this upload as a video encode.
                "disable_content_type_detection": "true",
            },
            files={
                "document": (
                    upload_name, handle, "application/octet-stream")
            },
            timeout=timeout,
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Telegram returned HTTP {response.status_code} without JSON: "
            f"{response.text[:500]}") from exc
    if response.status_code != 200 or not body.get("ok"):
        raise RuntimeError(
            f"Telegram sendDocument failed ({response.status_code}): "
            f"{body.get('description') or response.text[:500]}")
    return body["result"]
