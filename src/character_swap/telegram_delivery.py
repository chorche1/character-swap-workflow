"""Shared Telegram routing and receipt helpers for finished videos."""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from character_swap.clients import telegram
from character_swap.config import settings


# In-flight registry (Hugo 2026-08-03): ONE upload at a time per delivery
# target — (run_id, target_id, variant). An upload takes minutes (34 MB finals,
# 600 s timeout × 3 attempts), so the automatic post-build delivery and a
# manual ➤ Telegram click can easily overlap and post the SAME video twice in
# the channel. Everything blocked here is per TARGET: sending another
# character (or another run) while one upload runs is fine and must stay
# possible — the old blunt "whole run is busy" guard was exactly the bug.
# In-process only, mutated from the event loop thread; a restart clears it,
# which is correct (no upload survives the process).
_SENDING: set[tuple[str, str, str]] = set()


class AlreadySending(RuntimeError):
    """Raised when this exact video is already being uploaded right now."""


def send_key(run_id: str, target_id: str, variant: str) -> tuple[str, str, str]:
    return (str(run_id), str(target_id), str(variant or "final"))


def is_sending(run_id: str, target_id: str, variant: str) -> bool:
    return send_key(run_id, target_id, variant) in _SENDING


@contextmanager
def sending(run_id: str, target_id: str, variant: str):
    """Claim a delivery target for the duration of one upload.

    Raises AlreadySending if another sender holds it — callers turn that into
    an honest message ("skickas redan just nu") instead of a build-status lie.
    """
    key = send_key(run_id, target_id, variant)
    if key in _SENDING:
        raise AlreadySending(
            "Den här videon skickas redan till Telegram just nu — "
            "vänta tills den är klar.")
    _SENDING.add(key)
    try:
        yield
    finally:
        _SENDING.discard(key)


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
