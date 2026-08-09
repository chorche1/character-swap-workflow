"""Shared Google-Drive push helpers.

Extracted from api.py (2026-07-19) so BOTH the HTTP endpoints AND the
background auto-finalize chain (auto_finalize.py) push finished videos with
IDENTICAL folder layout + filename convention — the "single- and batch-push
paths must never drift apart" rule now also covers the automatic path.

Folder layout on Drive (auto-created under the drive.file scope, so only
app-created folders are ever touched):
    Character Swap/<character name>/   ← Swap Step-6 + Reengineer finals
    Character Swap/Editor/             ← Editor finals + repurposed reels

`push_file_core` uploads a local SNAPSHOT of the source (never the live path,
which a recompile overwrites in place), overwriting the same Drive file when a
`prior_file_id` is given. It raises the raw client errors
(`google_drive.DriveNotAuthorized` / `RuntimeError`) + `FileNotFoundError`;
callers map those to whatever their layer needs (api.py → HTTPException, the
auto chain → a loud toast / push).
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from character_swap import deliverables

DRIVE_ROOT_FOLDER = "Character Swap"


def drive_safe_name(name: str) -> str:
    return (name or "").replace("/", "_").replace("\\", "_").strip() or "video"


def drive_final_name(char_name: str, base: str, variant: str,
                     run_id: str, *, label: str | None = None) -> str:
    """Drive filename for a per-character final, CHARACTER NAME FIRST (Hugo
    2026-07-03 — so the folder listing sorts by character). `base` = job/run
    title; `run_id` keeps different runs' finals distinct and, together with
    the persisted file_id, is the overwrite key. Single-, batch- and
    auto-push paths share this so the three never drift apart.

    The suffix comes from `deliverables.name_suffix`, which guarantees a
    DISTINCT name per variant. That is load-bearing here and nowhere else:
    `google_drive.upload_or_replace` keys on the filename, so a deliverable
    that produced the final's name would overwrite the final itself."""
    suffix = deliverables.name_suffix(variant, label=label)
    return f"{char_name} — {base}{suffix} [{run_id}].mp4"


async def push_file_core(source: Path, subfolders: list[str], filename: str,
                         prior_file_id: str | None = None) -> dict:
    """ensure folders + upload_or_replace, returning a receipt dict.

    Raises `FileNotFoundError` when the source is gone,
    `google_drive.DriveNotAuthorized` when write auth is missing/lost, and
    `RuntimeError` on an upload failure — the caller decides how loud to be.

    Uploads a local SNAPSHOT of `source`, never the live path: every push
    source is a stable path that a recompile/rebuild overwrites IN PLACE
    (shutil.copyfile truncates + rewrites the same file), and the upload
    streams for minutes — pushing the live path would land a torn mix of old
    and new bytes on Drive with an ok receipt. The snapshot pins the bytes
    that existed when the push started."""
    from character_swap.clients import google_drive
    if not source.is_file():
        raise FileNotFoundError(f"Video file not found: {source.name}")
    fd, tmp_name = tempfile.mkstemp(prefix="drive-push-",
                                    suffix=source.suffix or ".mp4")
    os.close(fd)
    snapshot = Path(tmp_name)
    try:
        await asyncio.to_thread(shutil.copyfile, source, snapshot)
        folder_id = await asyncio.to_thread(
            google_drive.ensure_folder_path,
            [DRIVE_ROOT_FOLDER, *subfolders])
        result = await asyncio.to_thread(
            lambda: google_drive.upload_or_replace(
                snapshot, drive_filename=filename, folder_id=folder_id,
                file_id=prior_file_id))
    finally:
        snapshot.unlink(missing_ok=True)
    return {
        "ok": True,
        "file_id": result.get("id"),
        "url": result.get("webViewLink"),
        "name": result.get("name"),
        "at": datetime.utcnow().isoformat() + "Z",
    }
