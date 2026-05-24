"""Background watcher that pulls new Higgsfield outputs from a Google Drive
folder and stages them in the local Editor inbox.

Lifecycle:
1. FastAPI startup hook spawns one asyncio task (see api.py).
2. The task polls Drive every `settings.higgsfield_drive_poll_secs` seconds
   (default 60).
3. For each video file in the configured folder whose Drive `id` we haven't
   seen before, we stream it to `output/higgsfield-inbox/<drive_id>.<ext>`
   and append the id to `state/higgsfield_drive_seen.json`.
4. The Editor's multi-clip tab reads `/api/higgsfield/inbox` to render a
   "Higgsfield Inbox" section with thumbnails. Clicking "+ add all" pulls
   the files into the multi-clip list via the existing `addMultiClips`
   path (browser fetches the local file and turns it into a File object).

Why polling, not a webhook: Higgsfield's Drive integration writes files
into the user's Drive on their schedule. Google Drive itself supports
push notifications via the changes API but they require a public HTTPS
endpoint and TLS — overkill for a local app. Polling at 60s is good
enough for the human-scale latency of "I made a clip, switch to my
editor".
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from character_swap.clients import google_drive
from character_swap.config import settings


_log = logging.getLogger("higgsfield_drive_watcher")


def _seen_path() -> Path:
    """Persisted set of Drive file IDs we've already downloaded — survives
    server restarts so we don't re-download every file on every boot."""
    return settings.state_dir / "higgsfield_drive_seen.json"


def _inbox_dir() -> Path:
    return settings.output_dir / "higgsfield-inbox"


def _load_seen() -> set[str]:
    p = _seen_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("ids", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_seen(ids: set[str]) -> None:
    p = _seen_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps({"ids": sorted(ids)}, indent=2), encoding="utf-8")
    except OSError:
        pass


def _resolved_folder_id() -> str | None:
    """Honor the explicit-ID env var first; fall back to looking up by name."""
    if settings.higgsfield_drive_folder_id.strip():
        return settings.higgsfield_drive_folder_id.strip()
    name = settings.higgsfield_drive_folder_name.strip()
    if not name:
        return None
    return google_drive.resolve_folder_id(name)


def _ext_from_mime(mime: str) -> str:
    """Map a Drive MIME type to an extension. We use the extension on
    disk so video tags / ffmpeg pick the right demuxer."""
    return {
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
        "video/x-matroska": ".mkv",
    }.get(mime, ".mp4")


def list_inbox() -> list[dict[str, Any]]:
    """Local-disk inventory of already-downloaded inbox files. Each row has
    a `file_url` the browser can render directly via the /files/ mount."""
    inbox = _inbox_dir()
    if not inbox.is_dir():
        return []
    out = []
    for p in sorted(inbox.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".mp4", ".mov", ".webm", ".mkv"):
            continue
        rel = p.relative_to(settings.output_dir)
        out.append({
            "drive_id": p.stem,
            "name": p.name,
            "size_bytes": p.stat().st_size,
            "modified_ts": p.stat().st_mtime,
            "file_url": f"/files/output/{rel.as_posix()}",
            "local_path": str(p),
        })
    return out


def clear_inbox_item(drive_id: str) -> bool:
    """Delete a downloaded inbox file. The drive_id stays in `seen.json` so
    we don't redownload — the user removed it intentionally."""
    inbox = _inbox_dir()
    if not inbox.is_dir():
        return False
    removed = False
    for p in inbox.iterdir():
        if p.is_file() and p.stem == drive_id:
            try:
                p.unlink()
                removed = True
            except OSError:
                pass
    return removed


async def poll_once() -> dict[str, Any]:
    """One pull cycle. Lists the folder, downloads any new videos, updates
    the seen set. Returns a small dict the API endpoint can surface."""
    if not google_drive.status()["ready"]:
        return {"ok": False, "reason": "drive_oauth_not_set_up"}

    folder_id = _resolved_folder_id()
    if not folder_id:
        return {"ok": False, "reason": "folder_not_found",
                "looked_for": settings.higgsfield_drive_folder_name}

    files = await asyncio.to_thread(
        google_drive.list_videos_in_folder, folder_id,
    )
    seen = _load_seen()
    inbox = _inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    new_files: list[dict[str, Any]] = []
    for f in files:
        fid = f["id"]
        if fid in seen:
            continue
        ext = _ext_from_mime(f.get("mimeType", "video/mp4"))
        dest = inbox / f"{fid}{ext}"
        ok = await asyncio.to_thread(google_drive.download_file, fid, dest)
        if ok:
            seen.add(fid)
            new_files.append({
                "drive_id": fid,
                "name": f.get("name", dest.name),
                "size_bytes": int(f.get("size") or 0),
                "modified_time": f.get("modifiedTime"),
                "local_path": str(dest),
            })
        else:
            _log.warning("failed to download Drive file %s (%s)",
                         fid, f.get("name"))

    if new_files:
        _save_seen(seen)
    return {
        "ok": True,
        "folder_id": folder_id,
        "n_seen_total": len(files),
        "n_new": len(new_files),
        "new": new_files,
    }


async def watcher_loop(stop_event: asyncio.Event | None = None) -> None:
    """Long-running task started from FastAPI's lifespan. Runs poll_once()
    every `settings.higgsfield_drive_poll_secs`, swallowing per-cycle
    exceptions so a single failure doesn't kill the loop."""
    interval = max(15, int(settings.higgsfield_drive_poll_secs))
    _log.info("Higgsfield Drive watcher starting — interval=%ds", interval)
    while True:
        if stop_event is not None and stop_event.is_set():
            _log.info("Higgsfield Drive watcher stopping")
            return
        try:
            result = await poll_once()
            if result.get("n_new", 0):
                _log.info("Higgsfield Drive watcher: %d new file(s) pulled",
                          result["n_new"])
        except Exception as e:
            _log.warning("watcher cycle failed: %s: %s", type(e).__name__, e)
        try:
            await asyncio.wait_for(
                stop_event.wait() if stop_event else asyncio.sleep(interval),
                timeout=interval,
            )
        except asyncio.TimeoutError:
            pass
        except Exception:
            await asyncio.sleep(interval)
