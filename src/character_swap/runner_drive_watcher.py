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
import secrets
import shutil
import zipfile
from pathlib import Path
from typing import Any

from character_swap.clients import google_drive
from character_swap.config import settings


_log = logging.getLogger("higgsfield_drive_watcher")

# Tracks Drive file IDs that have been auto-processed (auto-edit ran +
# Telegram delivered or attempted). Separate from `_seen_path()` so we
# can still re-trigger auto-process if it failed mid-pipeline last cycle.
_AUTO_PROCESSED_LOG = "higgsfield_drive_auto_processed.json"

# Module-level lock that serializes the Telegram send step across all
# concurrent auto-process tasks. Hitting Telegram's per-chat rate limit
# (~1 msg/s for media) causes silent 429 failures, so we let one upload
# finish before the next begins. Trim+transcribe+caption stages still
# run fully parallel.
_TELEGRAM_SEND_LOCK = asyncio.Lock()


def _seen_path() -> Path:
    """Persisted set of Drive file IDs we've already downloaded — survives
    server restarts so we don't re-download every file on every boot."""
    return settings.state_dir / "higgsfield_drive_seen.json"


def _auto_processed_path() -> Path:
    """File IDs that have already been pushed through the auto-edit +
    Telegram delivery pipeline. Distinct from `_seen_path()` since a
    download can succeed while delivery fails — those need retry."""
    return settings.state_dir / _AUTO_PROCESSED_LOG


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
        "application/zip": ".zip",
        "application/x-zip-compressed": ".zip",
    }.get(mime, ".mp4")


_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".m4v")


def _load_auto_processed() -> set[str]:
    p = _auto_processed_path()
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return set(data.get("ids", []))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_auto_processed(ids: set[str]) -> None:
    p = _auto_processed_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps({"ids": sorted(ids)}, indent=2), encoding="utf-8")
    except OSError:
        pass


def _extract_video_from_zip(zip_path: Path, target_dir: Path) -> Path | None:
    """Legacy single-video extractor. Kept for the case where a ZIP truly
    only contains one clip; for Hugo's multi-scene Higgsfield exports
    use `_extract_all_videos_from_zip` instead."""
    paths = _extract_all_videos_from_zip(zip_path, target_dir)
    return paths[0] if paths else None


def _extract_all_videos_from_zip(zip_path: Path,
                                  target_dir: Path) -> list[Path]:
    """Extract EVERY video member from `zip_path` into `target_dir`.

    Higgsfield Supercomputer's project ZIPs (`zhang.zip`, `Copper.zip`,
    etc.) bundle N scene clips per ZIP — typically named
    `01_scene-1_<...>.mp4`, `02_scene-2_<...>.mp4` and so on. We pull
    them ALL out, prefix with the ZIP stem to avoid collisions across
    ZIPs, and return the list ordered by their original name (the
    filename prefix encodes intended scene order; the multi-clip
    pipeline will re-order against the script if needed).
    """
    extracted: list[Path] = []
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            members = sorted(
                (i for i in zf.infolist()
                 if not i.is_dir()
                 and any(i.filename.lower().endswith(ext)
                         for ext in _VIDEO_EXTS)),
                key=lambda m: m.filename,
            )
            if not members:
                _log.warning("ZIP %s has no video members; first 5 entries=%s",
                             zip_path.name,
                             [i.filename for i in zf.infolist()[:5]])
                return []
            target_dir.mkdir(parents=True, exist_ok=True)
            for idx, member in enumerate(members):
                # Strip any directory components; preserve extension.
                member_name = Path(member.filename).name
                ext = Path(member_name).suffix
                # Prefix with the ZIP stem + index so 5 ZIPs each with
                # 5 `01_scene-1_*.mp4` files don't clobber each other.
                out_path = target_dir / (
                    f"{zip_path.stem}__{idx:02d}_{Path(member_name).stem}{ext}"
                )
                with zf.open(member) as src, out_path.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                extracted.append(out_path)
        return extracted
    except (zipfile.BadZipFile, OSError) as e:
        _log.warning("failed to extract %s: %s", zip_path.name, e)
        return []


def _fetch_script_text(parent_folder_id: str) -> str | None:
    """Look for a `script.txt` (or any `.txt`) sibling of the ZIPs in the
    given Drive folder and return its contents. Used to drive the
    multi-clip pipeline's `match_clips_by_transcript` so each ZIP's
    scene clips concat in the intended narrative order.

    Returns None if no .txt is found OR if Drive auth is broken — caller
    falls back to filename-order concatenation in that case.
    """
    try:
        svc = google_drive._service()
        if svc is None:
            return None
        r = svc.files().list(
            q=(f"'{parent_folder_id}' in parents "
               f"and mimeType = 'text/plain' "
               f"and trashed = false"),
            fields="files(id, name)",
            pageSize=10,
        ).execute()
        files = r.get("files", [])
        if not files:
            return None
        # Prefer one literally called `script.txt`; else take the first .txt.
        target = next((f for f in files
                       if f["name"].lower() == "script.txt"),
                      files[0])
        from googleapiclient.http import MediaIoBaseDownload
        import io
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, svc.files().get_media(fileId=target["id"]))
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue().decode("utf-8", errors="replace")
    except Exception as e:
        _log.warning("failed to fetch script.txt: %s", e)
        return None


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
    """One pull cycle. Lists the folder, downloads any new videos OR ZIPs
    (Higgsfield Supercomputer wraps each clip in a `.zip`), extracts the
    video member from ZIPs into the inbox, and kicks off the auto-edit +
    Telegram-delivery pipeline when `settings.higgsfield_auto_process`
    is on. Returns a small dict the API endpoint can surface."""
    if not google_drive.status()["ready"]:
        return {"ok": False, "reason": "drive_oauth_not_set_up"}

    folder_id = _resolved_folder_id()
    if not folder_id:
        return {"ok": False, "reason": "folder_not_found",
                "looked_for": settings.higgsfield_drive_folder_name}

    # Higgsfield organizes exports as `AI INF Videos/<project>/<clip>.zip`,
    # so we need to walk subfolders. `recursive=True` flattens everything
    # we care about into one list. We also need the `parents` field so
    # `_auto_process_zip` can pull the sibling `script.txt` for the
    # multi-clip script-matching step.
    def _list_with_parents():
        svc = google_drive._service()
        if svc is None:
            return []
        return google_drive.list_processable_in_folder(
            folder_id, page_size=100, recursive=True,
        )
    # We need parents — re-list directly so we can request that field.
    def _list_with_parents_recursive(root_id: str, depth: int = 0) -> list[dict]:
        svc = google_drive._service()
        if svc is None:
            return []
        if depth > 5:
            return []
        try:
            mime_clauses = [
                "mimeType contains 'video/'",
                "mimeType = 'application/zip'",
                "mimeType = 'application/x-zip-compressed'",
                "mimeType = 'application/octet-stream'",
                "mimeType = 'application/vnd.google-apps.folder'",
            ]
            q = (f"'{root_id}' in parents "
                 f"and ({' or '.join(mime_clauses)}) "
                 f"and trashed = false")
            results = svc.files().list(
                q=q, spaces="drive",
                fields="files(id, name, mimeType, modifiedTime, size, parents)",
                orderBy="modifiedTime desc",
                pageSize=100,
            ).execute()
            out: list[dict] = []
            for f in results.get("files", []):
                mime = (f.get("mimeType") or "").lower()
                name = (f.get("name") or "").lower()
                if mime.startswith("video/"):
                    out.append(f)
                elif mime in ("application/zip", "application/x-zip-compressed"):
                    out.append(f)
                elif mime == "application/octet-stream" and name.endswith(".zip"):
                    out.append(f)
                elif mime == "application/vnd.google-apps.folder":
                    out.extend(_list_with_parents_recursive(f["id"], depth + 1))
            return out
        except Exception:
            return []

    files = await asyncio.to_thread(_list_with_parents_recursive, folder_id)
    seen = _load_seen()
    inbox = _inbox_dir()
    inbox.mkdir(parents=True, exist_ok=True)
    new_files: list[dict[str, Any]] = []
    for f in files:
        fid = f["id"]
        if fid in seen:
            continue
        mime = (f.get("mimeType") or "").lower()
        original_name = f.get("name") or fid
        ext = _ext_from_mime(mime)
        # If it's a ZIP we download to a temp dir, extract EVERY video
        # member, and fan them out as a multi-clip batch (Higgsfield's
        # project ZIPs contain N scene clips that should concat into
        # one stitched video, ordered by the sibling script.txt).
        is_zip = (mime in ("application/zip", "application/x-zip-compressed")
                  or (mime == "application/octet-stream"
                      and original_name.lower().endswith(".zip"))
                  or ext == ".zip")
        if is_zip:
            tmp_dir = inbox / "_zip_tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            zip_path = tmp_dir / f"{fid}.zip"
            ok = await asyncio.to_thread(google_drive.download_file, fid, zip_path)
            if not ok:
                _log.warning("failed to download ZIP %s (%s)", fid, original_name)
                continue
            video_paths = await asyncio.to_thread(
                _extract_all_videos_from_zip, zip_path, inbox,
            )
            try:
                zip_path.unlink()
            except OSError:
                pass
            if not video_paths:
                # Mark seen so we don't keep retrying — broken/empty ZIPs
                # stay broken. User can re-export from Higgsfield.
                seen.add(fid)
                continue
            seen.add(fid)
            new_files.append({
                "drive_id": fid,
                "name": original_name,
                "size_bytes": int(f.get("size") or 0),
                "modified_time": f.get("modifiedTime"),
                "is_zip": True,
                "video_paths": [str(p) for p in video_paths],
                # Parent folder id so we can fetch the sibling script.txt
                # from Drive for the multi-clip ordering step.
                "parent_folder_id": (f.get("parents") or [None])[0],
                # First-extracted path is what the inbox endpoint shows.
                "local_path": str(video_paths[0]),
            })
        else:
            dest = inbox / f"{fid}{ext or '.mp4'}"
            ok = await asyncio.to_thread(google_drive.download_file, fid, dest)
            if not ok:
                _log.warning("failed to download Drive file %s (%s)",
                             fid, original_name)
                continue
            seen.add(fid)
            new_files.append({
                "drive_id": fid,
                "name": original_name,
                "size_bytes": int(f.get("size") or 0),
                "modified_time": f.get("modifiedTime"),
                "is_zip": False,
                "local_path": str(dest),
            })

    if new_files:
        _save_seen(seen)

    # Auto-process step: fire-and-forget background tasks per fresh
    # file. For ZIPs we run the multi-clip pipeline (transcribe each
    # member, match against the parent folder's script.txt, concat,
    # caption). For standalone videos we use the single-clip path.
    if settings.higgsfield_auto_process and new_files:
        for nf in new_files:
            if nf.get("is_zip"):
                asyncio.create_task(_auto_process_zip(
                    [Path(p) for p in nf["video_paths"]],
                    parent_folder_id=nf.get("parent_folder_id"),
                    drive_id=nf["drive_id"],
                    original_name=nf["name"],
                ))
            else:
                asyncio.create_task(_auto_process_one(
                    Path(nf["local_path"]),
                    drive_id=nf["drive_id"],
                    original_name=nf["name"],
                ))

    return {
        "ok": True,
        "folder_id": folder_id,
        "n_seen_total": len(files),
        "n_new": len(new_files),
        "new": new_files,
        "auto_processing": settings.higgsfield_auto_process,
    }


async def _auto_process_one(video_path: Path, *, drive_id: str,
                            original_name: str) -> None:
    """Run the Editor's auto-edit (trim+captions) on `video_path` then
    deliver the result to Telegram. Designed to be fired from a
    `asyncio.create_task` in the watcher — never raises out; all errors
    are logged and (when possible) notified via Telegram text.
    """
    processed = _load_auto_processed()
    if drive_id in processed:
        return
    from character_swap import video_edit
    from character_swap.clients import telegram

    edit_id = "drv_" + secrets.token_hex(5)
    edit_dir = settings.output_dir / "editor" / edit_id
    edit_dir.mkdir(parents=True, exist_ok=True)

    # 1. Trim leading + interior silences. Hugo's preferred defaults:
    # -25 dB threshold, 0.30 s min-silence, 0.07 s padding around speech.
    # Match the values surfaced in the multi-clip Trim tab so the
    # automated path produces the same audio shape as a manual render.
    trimmed = edit_dir / "01-trimmed.mp4"
    try:
        await asyncio.to_thread(
            video_edit.trim_silences, video_path, trimmed,
            threshold_db=-25.0, min_silence_secs=0.30, pad_secs=0.07,
            job_id=edit_id,
        )
        current = trimmed
    except Exception as e:
        _log.warning("auto-process trim failed for %s: %s",
                     original_name, e)
        current = video_path  # render captions on the un-trimmed source

    # 2. Transcribe via Whisper.
    try:
        words = await asyncio.to_thread(
            video_edit.transcribe_words, current, job_id=edit_id,
        )
    except Exception as e:
        _log.warning("auto-process transcribe failed for %s: %s",
                     original_name, e)
        _maybe_telegram_error(
            f"Auto-process: transcribe failed for {original_name}: {e}"
        )
        return

    # 3. Whisper-precise leading-silence recut so the clip opens
    # exactly on speech (matches what /api/editor/auto_edit does).
    if words and words[0].start > 0.1:
        recut = edit_dir / "01b-whisper-recut.mp4"
        try:
            await asyncio.to_thread(
                video_edit.trim_to_first_word, current, recut, words,
                pad_secs=0.0, job_id=edit_id,
            )
            words = video_edit.shift_word_timestamps(words, words[0].start)
            current = recut
        except Exception:
            pass

    # 4. Render capcut-purple-pill captions.
    final_out = edit_dir / "04-final.mp4"
    try:
        style = video_edit.style_from_params("capcut-purple-pill", None)
        (edit_dir / "words.json").write_text(
            video_edit.words_to_json(words), encoding="utf-8",
        )
        (edit_dir / "pre_caption.txt").write_text(str(current), encoding="utf-8")
        await asyncio.to_thread(
            video_edit.render_captions, current, final_out,
            words=words, style=style, job_id=edit_id,
        )
    except Exception as e:
        _log.warning("auto-process caption render failed for %s: %s",
                     original_name, e)
        _maybe_telegram_error(
            f"Auto-process: caption render failed for {original_name}: {e}"
        )
        return

    # 5. Deliver via Telegram. Soft-fail if not configured (keep file
    # around for manual pickup). Serialized via _TELEGRAM_SEND_LOCK so
    # concurrent auto-process tasks don't trip Telegram's per-chat rate
    # limit (~1 msg/s for media → silent 429 otherwise).
    if telegram.configured():
        size = final_out.stat().st_size
        caption = (f"✓ {original_name}\n"
                   f"{size // 1024 // 1024} MB · "
                   f"{len(words)} words · capcut-purple-pill")
        async with _TELEGRAM_SEND_LOCK:
            try:
                if size <= 50 * 1024 * 1024:
                    await asyncio.to_thread(
                        telegram.send_video, final_out, caption=caption,
                    )
                else:
                    # 50 MB sendVideo cap; fall back to document for larger.
                    await asyncio.to_thread(
                        telegram.send_document, final_out, caption=caption,
                    )
                _log.info("auto-process Telegram-delivered: %s", original_name)
                # Small inter-send delay — well under Telegram's 1 msg/s
                # cap but enough to keep them visibly ordered in the chat.
                await asyncio.sleep(1.2)
            except Exception as e:
                _log.warning("Telegram delivery failed for %s: %s",
                             original_name, e)
                # Don't mark as auto-processed → next poll cycle retries.
                return
    else:
        _log.info("Telegram not configured — auto-process skipping delivery "
                  "for %s (file stays in inbox)", original_name)

    processed.add(drive_id)
    _save_auto_processed(processed)


async def _auto_process_zip(video_paths: list[Path], *,
                             parent_folder_id: str | None,
                             drive_id: str,
                             original_name: str) -> None:
    """Multi-clip auto-edit for one Higgsfield ZIP's video members.

    Mirrors `/api/editor/multi_auto_edit`'s happy path with Hugo's
    configuration (trim ON, captions ON capcut-purple-pill, WPM OFF,
    voice swap OFF). Pulls the parent folder's `script.txt` from Drive
    to drive the script-position matching that orders the clips.

    On success: writes drive_id into the auto-processed set and posts
    the final stitched MP4 to Telegram. On any irrecoverable failure:
    logs + Telegram-notifies + returns without marking processed (next
    poll cycle retries).
    """
    processed = _load_auto_processed()
    if drive_id in processed:
        return
    if not video_paths:
        _log.warning("auto-process-zip called with no videos for %s",
                     original_name)
        return

    from character_swap import video_edit
    from character_swap.clients import telegram

    edit_id = "drvzip_" + secrets.token_hex(5)
    edit_dir = settings.output_dir / "editor" / edit_id
    edit_dir.mkdir(parents=True, exist_ok=True)

    # 1. Fetch script.txt from the parent Drive folder. Without it the
    # clips concat in filename order (Higgsfield names them
    # `01_scene-1_…`, `02_scene-2_…` so that's a usable fallback).
    script_text = ""
    if parent_folder_id:
        fetched = await asyncio.to_thread(_fetch_script_text, parent_folder_id)
        if fetched:
            script_text = fetched
    if script_text:
        (edit_dir / "script.txt").write_text(script_text, encoding="utf-8")

    # 2. Transcribe every clip in parallel.
    try:
        transcripts: list[list] = await asyncio.gather(*[
            asyncio.to_thread(video_edit.transcribe_words, p, job_id=edit_id)
            for p in video_paths
        ])
    except Exception as e:
        _log.warning("auto-process-zip transcribe failed for %s: %s",
                     original_name, e)
        _maybe_telegram_error(
            f"Auto-process: transcribe failed for {original_name}: {e}"
        )
        return

    # 3. Match each clip to a script position and reorder. When script
    # is empty, the matcher returns clips in upload order (filename
    # order, which matches Higgsfield's `01_scene-1_…` numbering).
    plain_transcripts = [" ".join(w.text for w in ws) for ws in transcripts]
    placements = video_edit.match_clips_by_transcript(
        plain_transcripts, script_text or " ".join(plain_transcripts),
    )
    ordered_paths = [video_paths[p["idx"]] for p in placements]
    ordered_transcripts = [transcripts[p["idx"]] for p in placements]

    # 4. Per-clip leading-silence trim (always — Hugo's hard rule).
    leading_trimmed: list[Path] = []
    for i, p in enumerate(ordered_paths):
        words_for_clip = list(ordered_transcripts[i])
        cut = edit_dir / f"clip-{i:02d}-noLead.mp4"
        trimmed_ok = False
        if words_for_clip:
            try:
                await asyncio.to_thread(
                    video_edit.trim_to_first_word, p, cut,
                    words_for_clip, pad_secs=0.0, job_id=edit_id,
                )
                leading_trimmed.append(cut)
                trimmed_ok = True
            except (RuntimeError, ValueError):
                pass
        if not trimmed_ok:
            try:
                await asyncio.to_thread(
                    video_edit.trim_leading_silence, p, cut,
                    threshold_db=-25.0, min_silence_secs=0.05,
                    job_id=edit_id,
                )
                leading_trimmed.append(cut)
            except (RuntimeError, ValueError):
                leading_trimmed.append(p)
    ordered_paths = leading_trimmed

    # 5. Concat in script order.
    concat_out = edit_dir / "01-concat.mp4"
    try:
        await asyncio.to_thread(
            video_edit.concat_videos, ordered_paths, concat_out,
        )
    except Exception as e:
        _log.warning("auto-process-zip concat failed for %s: %s",
                     original_name, e)
        _maybe_telegram_error(
            f"Auto-process: concat failed for {original_name}: {e}"
        )
        return
    current = concat_out

    # 6. Interior silence trim on the concat'd output (Hugo's defaults).
    trimmed = edit_dir / "02-trimmed.mp4"
    try:
        await asyncio.to_thread(
            video_edit.trim_silences, current, trimmed,
            threshold_db=-25.0, min_silence_secs=0.30, pad_secs=0.07,
            job_id=edit_id,
        )
        current = trimmed
    except Exception:
        # Skip — concat already produced a usable file.
        pass

    # 7. Re-transcribe the concat'd output for caption rendering.
    # Word timestamps from the per-clip transcripts don't line up with
    # the concat timeline after trim+stitch.
    try:
        words = await asyncio.to_thread(
            video_edit.transcribe_words, current, job_id=edit_id,
        )
    except Exception as e:
        _log.warning("auto-process-zip post-concat transcribe failed for %s: %s",
                     original_name, e)
        _maybe_telegram_error(
            f"Auto-process: post-concat transcribe failed for {original_name}: {e}"
        )
        return

    # 7b. Whisper-precise leading-silence recut on the concat'd output
    # so the final MP4 opens exactly on speech.
    if words and words[0].start > 0.1:
        recut = edit_dir / "02b-whisper-recut.mp4"
        try:
            await asyncio.to_thread(
                video_edit.trim_to_first_word, current, recut, words,
                pad_secs=0.0, job_id=edit_id,
            )
            words = video_edit.shift_word_timestamps(words, words[0].start)
            current = recut
        except Exception:
            pass

    # 8. Render capcut-purple-pill captions.
    final_out = edit_dir / "04-final.mp4"
    try:
        style = video_edit.style_from_params("capcut-purple-pill", None)
        (edit_dir / "words.json").write_text(
            video_edit.words_to_json(words), encoding="utf-8",
        )
        (edit_dir / "pre_caption.txt").write_text(str(current), encoding="utf-8")
        await asyncio.to_thread(
            video_edit.render_captions, current, final_out,
            words=words, style=style, job_id=edit_id,
        )
    except Exception as e:
        _log.warning("auto-process-zip caption render failed for %s: %s",
                     original_name, e)
        _maybe_telegram_error(
            f"Auto-process: caption render failed for {original_name}: {e}"
        )
        return

    # 9. Telegram delivery — serialized via the module-level lock.
    if telegram.configured():
        size = final_out.stat().st_size
        caption = (f"✓ {original_name}\n"
                   f"{size // 1024 // 1024} MB · "
                   f"{len(video_paths)} clips · {len(words)} words · capcut-purple-pill")
        async with _TELEGRAM_SEND_LOCK:
            try:
                if size <= 50 * 1024 * 1024:
                    await asyncio.to_thread(
                        telegram.send_video, final_out, caption=caption,
                    )
                else:
                    await asyncio.to_thread(
                        telegram.send_document, final_out, caption=caption,
                    )
                _log.info("auto-process-zip Telegram-delivered: %s",
                          original_name)
                await asyncio.sleep(1.2)
            except Exception as e:
                _log.warning("Telegram delivery failed for %s: %s",
                             original_name, e)
                return
    else:
        _log.info("Telegram not configured — auto-process-zip skipping "
                  "delivery for %s (file at %s)",
                  original_name, final_out)

    processed.add(drive_id)
    _save_auto_processed(processed)


def _maybe_telegram_error(msg: str) -> None:
    """Best-effort Telegram error notification. Swallows its own errors
    so the watcher loop survives even if Telegram itself is down."""
    try:
        from character_swap.clients import telegram as _tg
        if _tg.configured():
            _tg.send_text(msg)
    except Exception:
        pass


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
