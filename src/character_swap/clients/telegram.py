"""Telegram Bot API wrapper — used to deliver the auto-processed final
video to Hugo as a Telegram message.

Setup:
1. Talk to @BotFather on Telegram, /newbot, get a token. Set
   `TELEGRAM_BOT_TOKEN=...` in `.env`.
2. Send any message to your new bot from your own Telegram account, then
   `curl https://api.telegram.org/bot<TOKEN>/getUpdates` — note the
   `chat.id` of the message. Set `TELEGRAM_CHAT_ID=...`.

Why this client is thin: Telegram has solid Python SDKs but they all
pull in async libs we don't need. The Bot API is a plain HTTP POST and
fits in ~50 lines.
"""
from __future__ import annotations

from pathlib import Path

import httpx

from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


_API_BASE = "https://api.telegram.org"


def _require_telegram() -> None:
    if not settings.telegram_bot_token:
        raise ProviderNotConfigured(
            "Telegram",
            "Add TELEGRAM_BOT_TOKEN to .env (get one from @BotFather).",
        )
    if not settings.telegram_chat_id:
        raise ProviderNotConfigured(
            "Telegram",
            "Add TELEGRAM_CHAT_ID to .env (curl getUpdates after sending "
            "a message to your bot to find it).",
        )


def configured() -> bool:
    """Soft check used by the watcher to decide whether to attempt
    delivery. Returns False if either env var is missing."""
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def send_video(file_path: Path, *, caption: str = "",
               chat_id: str | None = None, timeout: float = 300.0) -> dict:
    """Upload `file_path` to Telegram as a video message.

    Returns the Telegram API response dict. Raises RuntimeError on
    network or API-level failure so the caller can log + retry.

    Uses `sendVideo` (not `sendDocument`) so Telegram renders an inline
    preview with the duration/thumbnail bar instead of a generic file
    attachment.

    `timeout` defaults to 5 min — Telegram's API can be slow with large
    uploads on patchy connections.
    """
    _require_telegram()
    target = chat_id or settings.telegram_chat_id
    url = f"{_API_BASE}/bot{settings.telegram_bot_token}/sendVideo"

    file_path = Path(file_path)
    if not file_path.exists():
        raise RuntimeError(f"Telegram send_video: file missing: {file_path}")
    if file_path.stat().st_size > 50 * 1024 * 1024:
        # Telegram bots are capped at 50 MB per upload for non-premium
        # bots. Above that, sendDocument with stream-from-URL is needed —
        # but Hugo's final reels rarely exceed this.
        raise RuntimeError(
            f"Telegram send_video: file is {file_path.stat().st_size // 1024 // 1024}MB, "
            "exceeds 50 MB bot upload cap. Use sendDocument with a public "
            "URL for larger files."
        )

    with file_path.open("rb") as fh:
        files = {"video": (file_path.name, fh, "video/mp4")}
        data = {
            "chat_id": target,
            "caption": caption[:1024],   # Telegram cap
            "supports_streaming": "true",
        }
        try:
            r = httpx.post(url, data=data, files=files, timeout=timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"Telegram POST failed: {e}") from e

    if r.status_code != 200:
        raise RuntimeError(
            f"Telegram sendVideo returned {r.status_code}: {r.text[:500]}"
        )
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {body.get('description', body)}"
        )
    return body


def send_document(file_path: Path, *, caption: str = "",
                  chat_id: str | None = None, timeout: float = 600.0) -> dict:
    """Like `send_video` but uses `sendDocument` — survives the 50 MB
    sendVideo cap (raises it to ~2 GB) but loses the inline preview."""
    _require_telegram()
    target = chat_id or settings.telegram_chat_id
    url = f"{_API_BASE}/bot{settings.telegram_bot_token}/sendDocument"

    file_path = Path(file_path)
    if not file_path.exists():
        raise RuntimeError(f"Telegram send_document: file missing: {file_path}")

    with file_path.open("rb") as fh:
        files = {"document": (file_path.name, fh, "video/mp4")}
        data = {"chat_id": target, "caption": caption[:1024]}
        try:
            r = httpx.post(url, data=data, files=files, timeout=timeout)
        except httpx.HTTPError as e:
            raise RuntimeError(f"Telegram POST failed: {e}") from e

    if r.status_code != 200:
        raise RuntimeError(
            f"Telegram sendDocument returned {r.status_code}: {r.text[:500]}"
        )
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {body.get('description', body)}"
        )
    return body


def send_text(text: str, *, chat_id: str | None = None) -> dict:
    """Send a plain text message — used for error notifications when
    the auto-pipeline can't deliver a video."""
    _require_telegram()
    target = chat_id or settings.telegram_chat_id
    url = f"{_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    r = httpx.post(url, data={"chat_id": target, "text": text[:4096]},
                   timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendMessage failed: {r.text[:500]}")
    return r.json()
