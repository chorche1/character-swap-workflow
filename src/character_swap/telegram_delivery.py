"""Shared Telegram routing and receipt helpers for finished videos."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from character_swap.clients import telegram
from character_swap.config import settings


def safe_name(name: str) -> str:
    return (name or "").replace("/", "_").replace("\\", "_").strip() or "video"


def character_final_name(char_name: str, base: str, variant: str,
                         run_id: str) -> str:
    suffix = " — repurpose" if variant == "repurpose" else ""
    return f"{safe_name(char_name)} — {safe_name(base)}{suffix} [{run_id}].mp4"


def editor_final_name(base: str, variant: str, edit_id: str) -> str:
    suffix = " — repurpose" if variant == "repurpose" else ""
    return f"{safe_name(base)}{suffix} [{edit_id}].mp4"


def _message_url(chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    target = (chat_id or "").strip()
    if target.startswith("@"):
        return f"https://t.me/{target[1:]}/{message_id}"
    return None


async def send_file_core(source: Path, *, bot_token: str, chat_id: str,
                         filename: str, caption: str,
                         account: str) -> dict:
    """Snapshot a mutable final, send it, and return a durable receipt."""
    if not source.is_file():
        raise FileNotFoundError(f"Video file not found: {source.name}")
    fd, temp_name = tempfile.mkstemp(
        prefix="telegram-send-", suffix=source.suffix or ".mp4")
    os.close(fd)
    snapshot = Path(temp_name)
    try:
        await asyncio.to_thread(shutil.copyfile, source, snapshot)
        message = await asyncio.to_thread(
            telegram.send_document,
            snapshot,
            bot_token=bot_token,
            chat_id=chat_id,
            filename=filename,
            caption=caption,
            api_base=settings.telegram_api_base,
        )
    finally:
        snapshot.unlink(missing_ok=True)
    document = message.get("document") or {}
    message_id = message.get("message_id")
    return {
        "ok": True,
        "account": account,
        "chat_id": str(chat_id),
        "message_id": message_id,
        "file_id": document.get("file_id"),
        "file_unique_id": document.get("file_unique_id"),
        "url": _message_url(str(chat_id), message_id),
        "name": filename,
        "at": datetime.utcnow().isoformat() + "Z",
    }


async def send_character_final(source: Path, *, chat_id: str,
                               char_name: str, base: str, variant: str,
                               run_id: str) -> dict:
    filename = character_final_name(char_name, base, variant, run_id)
    caption = filename.removesuffix(".mp4")
    return await send_file_core(
        source,
        bot_token=settings.telegram_character_bot_token,
        chat_id=chat_id,
        filename=filename,
        caption=caption,
        account="character",
    )


async def send_editor_final(source: Path, *, base: str, variant: str,
                            edit_id: str) -> dict:
    filename = editor_final_name(base, variant, edit_id)
    caption = filename.removesuffix(".mp4")
    return await send_file_core(
        source,
        bot_token=settings.telegram_editor_bot_token,
        chat_id=settings.telegram_editor_chat_id,
        filename=filename,
        caption=caption,
        account="editor",
    )
