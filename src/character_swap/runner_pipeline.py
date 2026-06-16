"""Phase 4: 'Run full pipeline' orchestrator.

The user clicks one button in Step 6 and we chain:

    compile (no captions)
       ↓
    package into a temp folder (video + SRT + automate.py + credentials)
       ↓
    spawn `python automate.py` subprocess  →  Resolve renders MP4
                                              automate.py uploads to Drive

Each character runs on its own task — failures are isolated.

Status flows on JobCharacter.pipeline_status:
    None → "compiling" → "packaging" → "rendering" → "uploading" → "done"
                                                                 ↘ "failed"

The "rendering" + "uploading" transitions come from parsing the subprocess
stdout for marker lines that automate.py prints. See `_STATUS_MARKERS`.

The temp folder lives at:
    ~/character-swap-data/pipeline-runs/<job_id>/<char_id>/

We don't auto-clean it — the user might want to re-run, inspect logs, or
manually re-render in Resolve. Old runs accumulate; clean periodically.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from character_swap import events, exporter
from character_swap.config import settings
from character_swap.models import Job, JobCharacter
from character_swap.runner_compile import _eligible_for_compile, compile_job_videos
from character_swap.state import store


# Marker lines automate.py prints that map to user-visible status transitions.
# Order matters: matched in order until one fires for each line.
_STATUS_MARKERS: list[tuple[re.Pattern, str, str | None]] = [
    # (pattern, new_status, optional named-capture group that becomes a detail)
    (re.compile(r"^Project ready:"),                  "rendering",  None),
    (re.compile(r"^Rendering →"),                     "rendering",  None),
    (re.compile(r"^\s+render OK\s+:\s+(?P<name>\S+)"), "uploading", "name"),
    (re.compile(r"^\s+drive(?:\s+:)?\s+uploading"),   "uploading",  None),
    (re.compile(r"^\s+drive OK\s+:\s+(?P<link>\S+)"), "done",       "link"),
    (re.compile(r"^\s+drive SKIP"),                   "done",       None),
    (re.compile(r"^\s+drive ERROR\s*:\s*(?P<err>.+)"), "done",      None),
    (re.compile(r"^Done\."),                          "done",       None),
]

# Subprocess output line is a hard failure if it contains one of these.
_FATAL_MARKERS: list[re.Pattern] = [
    re.compile(r"^Open DaVinci Resolve first"),
    re.compile(r"^Could not import DaVinciResolveScript"),
    re.compile(r"^Could not create/open project"),
]


async def _emit(job_id: str, char_id: str, **data) -> None:
    payload = {
        "kind": "char.pipeline_status",
        "job_id": job_id,
        "char_id": char_id,
        "ts": datetime.utcnow().isoformat() + "Z",
        **data,
    }
    await events.publish(job_id, payload)


def _persist_pipeline(job: Job, jc: JobCharacter, **fields) -> None:
    for k, v in fields.items():
        setattr(jc, k, v)
    jc.updated_at = datetime.utcnow()
    job.characters[jc.char_id] = jc
    store().update_job(job)


def _pipeline_root() -> Path:
    """Per-machine root for pipeline temp dirs. Lives alongside the shared
    data store so it survives `git worktree remove` like everything else."""
    root = (settings.state_dir.parent / "pipeline-runs").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _shared_credentials() -> Path | None:
    """Single Google OAuth credentials file shared across all pipeline runs.
    Looked up at <data-root>/credentials.json. None = no Drive upload (the
    subprocess prints 'drive SKIP' and the pipeline still finishes 'done')."""
    candidate = (settings.state_dir.parent / "credentials.json").resolve()
    return candidate if candidate.exists() else None


def _shared_drive_token() -> Path | None:
    """Reusable Drive OAuth token across all pipeline runs — avoids needing
    a browser-consent flow per character. Looked up + stored at
    <data-root>/token.json. Returned even if it doesn't exist yet (subprocess
    will create it on first run)."""
    return (settings.state_dir.parent / "token.json").resolve()


async def _run_char_pipeline(job_id: str, char_id: str) -> None:
    """Compile-then-render-then-upload for one character. Wraps everything in
    a single asyncio task so we can fan out via asyncio.gather and have failures
    stay isolated per-char."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return
    jc = job.characters.get(char_id)
    if jc is None:
        return

    # === STAGE 1: compile (no captions — Resolve will handle them) ===
    _persist_pipeline(job, jc,
                      pipeline_status="compiling", pipeline_error=None,
                      pipeline_drive_link=None)
    await _emit(job_id, char_id, status="compiling")
    try:
        await compile_job_videos(
            job_id, char_ids=[char_id],
            enable_captions=False,            # Resolve burns captions in its render
            enable_transcribe=True,           # always — Resolve export needs SRT
        )
    except Exception as e:
        _persist_pipeline(job, jc,
                          pipeline_status="failed",
                          pipeline_error=f"compile: {type(e).__name__}: {e}")
        await _emit(job_id, char_id, status="failed",
                    error=f"compile: {e}")
        return

    # Re-fetch — compile_job_videos mutated the job via its own store.
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return
    jc = job.characters.get(char_id)
    if jc is None or jc.compile_status != "done" or not jc.compiled_video_path:
        _persist_pipeline(job, jc or JobCharacter(
            char_id=char_id, name="?", source_image_path=""),
                          pipeline_status="failed",
                          pipeline_error="compile did not finish")
        await _emit(job_id, char_id, status="failed",
                    error="compile did not finish")
        return

    # === STAGE 2: package into temp dir ===
    _persist_pipeline(job, jc, pipeline_status="packaging")
    await _emit(job_id, char_id, status="packaging")
    try:
        temp_dir = _pipeline_root() / job_id / char_id
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

        # Build the same zip the GET endpoint produces, then unpack into the
        # temp dir so automate.py finds the expected sibling files.
        final_video = Path(jc.compiled_video_path)
        pre_caption: Path | None = None
        words: list[dict] | None = None
        if jc.compile_edit_id:
            edit_dir = settings.output_dir / "editor" / jc.compile_edit_id
            pre_candidate = edit_dir / "pre_caption.txt"
            if pre_candidate.exists():
                try:
                    recorded = Path(pre_candidate.read_text(encoding="utf-8").strip())
                    if recorded.exists() and recorded != final_video:
                        pre_caption = recorded
                except OSError:
                    pre_caption = None
            words_path = edit_dir / "words.json"
            if words_path.exists():
                try:
                    words = json.loads(words_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    words = None

        zip_bytes = exporter.build_export_zip(
            final_video=final_video,
            pre_caption_video=pre_caption,
            words=words,
            project_name=char_id,
        )
        # zip_bytes nests everything under char_id/; we want it flat in temp_dir.
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for member in zf.namelist():
                # Strip the leading <char_id>/ prefix.
                rel = member[len(char_id) + 1:] if member.startswith(char_id + "/") else member
                if not rel:
                    continue
                dest = temp_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

        # If the user has Drive set up centrally, copy credentials + reuse
        # the cached OAuth token so every char doesn't trigger a browser.
        creds = _shared_credentials()
        if creds:
            shutil.copyfile(creds, temp_dir / "credentials.json")
        token = _shared_drive_token()
        if token and token.exists():
            shutil.copyfile(token, temp_dir / "token.json")

        _persist_pipeline(job, jc, pipeline_temp_dir=str(temp_dir))
    except Exception as e:
        _persist_pipeline(job, jc,
                          pipeline_status="failed",
                          pipeline_error=f"package: {type(e).__name__}: {e}")
        await _emit(job_id, char_id, status="failed",
                    error=f"package: {e}")
        return

    # === STAGE 3: spawn automate.py and parse its stdout for status ===
    _persist_pipeline(job, jc, pipeline_status="rendering")
    await _emit(job_id, char_id, status="rendering")

    automate = temp_dir / "automate.py"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(automate),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(temp_dir),
        )
    except Exception as e:
        _persist_pipeline(job, jc,
                          pipeline_status="failed",
                          pipeline_error=f"spawn: {type(e).__name__}: {e}")
        await _emit(job_id, char_id, status="failed",
                    error=f"spawn: {e}")
        return

    log_path = temp_dir / "automate.log"
    drive_link: str | None = None
    fatal_seen: str | None = None
    last_status = "rendering"

    with log_path.open("w", encoding="utf-8") as log_fh:
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            log_fh.write(line + "\n")
            log_fh.flush()

            # Detect fatal lines so we surface a clear error rather than
            # waiting for the subprocess to exit with a generic non-zero.
            for fatal in _FATAL_MARKERS:
                if fatal.search(line):
                    fatal_seen = line.strip()
                    break

            # Map line to a status transition.
            for pat, new_status, capture_name in _STATUS_MARKERS:
                m = pat.search(line)
                if not m:
                    continue
                detail = m.group(capture_name) if capture_name else None
                if capture_name == "link" and detail:
                    drive_link = detail
                if new_status != last_status:
                    last_status = new_status
                    _persist_pipeline(job, jc, pipeline_status=new_status)
                    await _emit(job_id, char_id, status=new_status,
                                line=line.strip(),
                                drive_link=drive_link)
                break

    rc = await proc.wait()

    # Re-fetch in case mutations happened concurrently.
    s = store()
    job = s.get_job(job_id) or job
    jc = job.characters.get(char_id) or jc

    if fatal_seen:
        _persist_pipeline(job, jc,
                          pipeline_status="failed",
                          pipeline_error=fatal_seen)
        await _emit(job_id, char_id, status="failed", error=fatal_seen)
        return
    if rc != 0:
        _persist_pipeline(job, jc,
                          pipeline_status="failed",
                          pipeline_error=f"automate.py exited {rc}; see {log_path}")
        await _emit(job_id, char_id, status="failed",
                    error=f"exit {rc} — log: {log_path}")
        return

    # Success path: also cache the Drive token back to the shared location so
    # the next run for any character is consent-free.
    token = _shared_drive_token()
    if token:
        candidate = temp_dir / "token.json"
        if candidate.exists():
            shutil.copyfile(candidate, token)

    _persist_pipeline(job, jc,
                      pipeline_status="done",
                      pipeline_error=None,
                      pipeline_drive_link=drive_link)
    await _emit(job_id, char_id, status="done", drive_link=drive_link)


async def run_full_pipeline(job_id: str, *,
                            char_ids: list[str] | None = None) -> None:
    """Fan out the per-character pipeline. Each char runs on its own task so
    one failure (e.g. Drive credentials missing) doesn't block the rest.

    Selection: same eligibility rules as compile_job_videos — at least one
    approved variant + at least one DONE video. Skips REJECTED and unfinished
    chars silently.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return

    targets: list[str] = []
    for cid, jc in job.characters.items():
        if char_ids is not None and cid not in char_ids:
            continue
        # Same eligibility as Step-6 compile — shared so the two can't drift.
        if not _eligible_for_compile(jc):
            continue
        targets.append(cid)
        _persist_pipeline(job, jc,
                          pipeline_status="queued", pipeline_error=None,
                          pipeline_drive_link=None)

    if not targets:
        return

    await asyncio.gather(*[
        _run_char_pipeline(job_id, cid) for cid in targets
    ])


# --- Editor-side pipeline (same Resolve render+upload, no JobCharacter) ----

def _editor_pipeline_state_path(edit_id: str) -> Path:
    """Path to the per-edit pipeline-state JSON. Polled by the UI for status
    transitions since editor edits don't have WebSocket subscribers like jobs."""
    return settings.output_dir / "editor" / edit_id / "pipeline_state.json"


def _persist_editor_pipeline(edit_id: str, **fields) -> dict:
    """Read-modify-write the editor pipeline state JSON. Each transition merges
    the new fields and bumps updated_at."""
    path = _editor_pipeline_state_path(edit_id)
    state: dict = {}
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            state = {}
    state.update(fields)
    state["updated_at"] = datetime.utcnow().isoformat() + "Z"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError:
        pass
    return state


def _editor_locate_videos(edit_dir: Path) -> tuple[Path | None, Path | None]:
    """Pick the final + pre-caption MP4s for an edit. Same priority order as
    api._find_editor_videos (duplicated here to avoid the cross-module import)."""
    if not edit_dir.is_dir():
        return None, None

    final: Path | None = None
    for name in ("04-final.mp4", "captioned.mp4"):
        candidate = edit_dir / name
        if candidate.exists():
            final = candidate
            break
    if final is None:
        rerenders = sorted(edit_dir.glob("rerender-*.mp4"),
                           key=lambda p: p.stat().st_mtime)
        if rerenders:
            final = rerenders[-1]
    if final is None:
        all_mp4 = sorted(edit_dir.glob("*.mp4"),
                         key=lambda p: p.stat().st_mtime)
        if all_mp4:
            final = all_mp4[-1]

    pre: Path | None = None
    marker = edit_dir / "pre_caption.txt"
    if marker.exists():
        try:
            recorded = Path(marker.read_text(encoding="utf-8").strip())
            if recorded.exists() and recorded != final:
                pre = recorded
        except OSError:
            pre = None
    if pre is None:
        for name in ("03-stretched.mp4", "02-swapped.mp4",
                     "01-trimmed.mp4", "trimmed.mp4", "00-concat.mp4"):
            candidate = edit_dir / name
            if candidate.exists() and candidate != final:
                pre = candidate
                break

    return final, pre


async def run_editor_pipeline(edit_id: str) -> None:
    """Package an editor edit_dir's rendered MP4 as a DaVinci Resolve project
    and run automate.py to render + (optionally) upload to Drive.

    Mirrors `_run_char_pipeline` STAGES 2+3 but reads from edit_dir instead of
    a JobCharacter and writes status to `pipeline_state.json` in the edit_dir
    instead of mutating DB rows. The UI polls
    `GET /api/editor/{edit_id}/pipeline_state` for updates.
    """
    edit_dir = settings.output_dir / "editor" / edit_id
    if not edit_dir.is_dir():
        _persist_editor_pipeline(edit_id,
                                 status="failed",
                                 error=f"edit_dir not found: {edit_dir}")
        return

    final_video, pre_caption = _editor_locate_videos(edit_dir)
    if final_video is None:
        _persist_editor_pipeline(edit_id,
                                 status="failed",
                                 error="no rendered video in edit_dir")
        return

    words: list[dict] | None = None
    words_path = edit_dir / "words.json"
    if words_path.exists():
        try:
            words = json.loads(words_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            words = None

    # === STAGE 2: package into temp dir ===
    _persist_editor_pipeline(edit_id, status="packaging",
                             error=None, drive_link=None)
    try:
        temp_dir = _pipeline_root() / "editor" / edit_id
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        temp_dir.mkdir(parents=True, exist_ok=True)

        zip_bytes = exporter.build_export_zip(
            final_video=final_video,
            pre_caption_video=pre_caption,
            words=words,
            project_name=edit_id,
        )
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for member in zf.namelist():
                rel = member[len(edit_id) + 1:] if member.startswith(edit_id + "/") else member
                if not rel:
                    continue
                dest = temp_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

        creds = _shared_credentials()
        if creds:
            shutil.copyfile(creds, temp_dir / "credentials.json")
        token = _shared_drive_token()
        if token and token.exists():
            shutil.copyfile(token, temp_dir / "token.json")

        _persist_editor_pipeline(edit_id, temp_dir=str(temp_dir))
    except Exception as e:
        _persist_editor_pipeline(edit_id, status="failed",
                                 error=f"package: {type(e).__name__}: {e}")
        return

    # === STAGE 3: spawn automate.py and parse its stdout for status ===
    _persist_editor_pipeline(edit_id, status="rendering")

    automate = temp_dir / "automate.py"
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(automate),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(temp_dir),
        )
    except Exception as e:
        _persist_editor_pipeline(edit_id, status="failed",
                                 error=f"spawn: {type(e).__name__}: {e}")
        return

    log_path = temp_dir / "automate.log"
    drive_link: str | None = None
    fatal_seen: str | None = None
    last_status = "rendering"

    with log_path.open("w", encoding="utf-8") as log_fh:
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8", errors="replace").rstrip()
            log_fh.write(line + "\n")
            log_fh.flush()

            for fatal in _FATAL_MARKERS:
                if fatal.search(line):
                    fatal_seen = line.strip()
                    break

            for pat, new_status, capture_name in _STATUS_MARKERS:
                m = pat.search(line)
                if not m:
                    continue
                detail = m.group(capture_name) if capture_name else None
                if capture_name == "link" and detail:
                    drive_link = detail
                if new_status != last_status:
                    last_status = new_status
                    _persist_editor_pipeline(edit_id, status=new_status,
                                             drive_link=drive_link,
                                             last_line=line.strip())
                break

    rc = await proc.wait()

    if fatal_seen:
        _persist_editor_pipeline(edit_id, status="failed", error=fatal_seen)
        return
    if rc != 0:
        _persist_editor_pipeline(
            edit_id, status="failed",
            error=f"automate.py exited {rc}; see {log_path}",
        )
        return

    # Cache the Drive token back so the next run is consent-free.
    token = _shared_drive_token()
    if token:
        candidate = temp_dir / "token.json"
        if candidate.exists():
            shutil.copyfile(candidate, token)

    _persist_editor_pipeline(edit_id, status="done",
                             error=None, drive_link=drive_link)
