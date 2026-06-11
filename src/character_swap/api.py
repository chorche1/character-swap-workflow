from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from character_swap import call_log, events, runner, runner_media
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings
from character_swap.models import (
    CharacterAsset,
    CharacterImage,
    CharStatus,
    GeneratedImage,
    GenKind,
    GenStatus,
    Job,
    JobCharacter,
    MediaGeneration,
    ProjectAsset,
    SceneAsset,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)
from character_swap.state import store

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".ogg", ".webm", ".flac"}
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


# --- helpers --------------------------------------------------------------------------

def _ensure_dirs() -> None:
    for d in (
        settings.scenes_dir,
        settings.characters_dir,
        settings.output_dir,
        settings.state_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)


def _short_id(prefix: str = "") -> str:
    return prefix + secrets.token_hex(4)


def _safe_ext(filename: str, *, allow_audio: bool = False, allow_video: bool = False) -> str:
    ext = Path(filename).suffix.lower()
    allowed = set(ALLOWED_IMAGE_EXTS)
    if allow_audio:
        allowed |= ALLOWED_AUDIO_EXTS
    if allow_video:
        # Voice-Changer accepts videos too — we extract the audio + re-mux on output.
        allowed |= {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
    if ext not in allowed:
        raise HTTPException(400, f"Unsupported file type '{ext}'. Allowed: {sorted(allowed)}")
    return ext


def _safe_filename_stem(name: str) -> str:
    """Make a filesystem-safe stem from a display name (no extension)."""
    s = _SAFE_NAME_RE.sub("-", (name or "").strip()).strip("-_.")
    return s or "image"


async def _read_capped(upload: UploadFile) -> bytes:
    """Read an UploadFile but reject if it exceeds settings.max_upload_bytes."""
    cap = settings.max_upload_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(1 << 16)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(
                413,
                f"File too large (>{cap // (1024 * 1024)} MB). "
                f"Adjust MAX_UPLOAD_BYTES in .env to allow bigger uploads.",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _save_upload(upload: UploadFile, dest: Path) -> bytes:
    data = await _read_capped(upload)
    if not data:
        raise HTTPException(400, "Empty upload")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return data


def _file_url(path: Path | str | None) -> str | None:
    """Map a filesystem path to a /files/... URL served by one of the mounts.

    Three explicit mount prefixes (characters/, input/scenes/, output/)
    handle the canonical data dirs — checked first so paths in those dirs
    resolve correctly even when they're OUTSIDE project_root (the shared
    data store at ~/character-swap-data/). Falls back to project_root for
    web assets and anything else that happens to live inside the repo.
    """
    if path is None:
        return None
    p = Path(path).resolve()
    for prefix, base in (
        ("characters", settings.characters_dir.resolve()),
        ("input/scenes", settings.scenes_dir.resolve()),
        ("input/extra_refs", (settings.input_dir / "extra_refs").resolve()),
        ("output", settings.output_dir.resolve()),
    ):
        try:
            rel = p.relative_to(base)
            return f"/files/{prefix}/{rel.as_posix()}"
        except ValueError:
            continue
    try:
        rel = p.relative_to(settings.project_root.resolve())
        return f"/files/{rel.as_posix()}"
    except ValueError:
        return None


def _variant_download_name(jc: JobCharacter, variant: GeneratedImage) -> str:
    """1-based index among same-kind variants for this character."""
    stem = _safe_filename_stem(jc.name)
    is_edit = variant.parent_variant_id is not None
    same_kind = [
        v for v in jc.images
        if (v.parent_variant_id is not None) == is_edit
    ]
    idx = same_kind.index(variant) + 1 if variant in same_kind else 1
    ext = Path(variant.path).suffix or ".png"
    kind = "edit" if is_edit else "variant"
    return f"{stem}-{kind}-{idx}{ext}"


def _video_download_name(jc: JobCharacter, video: VideoVariant) -> str:
    """Step-5 clip download name. ALL of a character's clips share ONE name —
    just the character name — so they group together when organizing files;
    different characters get different names. (The browser de-dupes repeats on
    download: chang.mp4, chang (1).mp4, ….)"""
    stem = _safe_filename_stem(jc.name)
    ext = ".mp4"
    if video.final_video_path:
        ext = Path(video.final_video_path).suffix or ".mp4"
    return f"{stem}{ext}"


def _auto_title(char_names: list[str]) -> str:
    when = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    if not char_names:
        return when
    head = ", ".join(char_names[:3])
    suffix = " …" if len(char_names) > 3 else ""
    out = f"{when} — {head}{suffix}"
    # Cap to ~80 chars to keep sidebar tidy.
    return out[:80]


def _effective_scene_ids(job: Job) -> list[str]:
    """Resolve the canonical scene list for a job. Old jobs from before
    multi-scene support only have `scene_id` set; treat them as a 1-item
    list."""
    if job.scene_ids:
        return list(job.scene_ids)
    return [job.scene_id]


def _effective_scene_paths(job: Job) -> list[str]:
    if job.scene_image_paths:
        return list(job.scene_image_paths)
    return [job.scene_image_path]


def _director_plan_summary(plan_json: str | None) -> dict | None:
    """Parse the cached SwapDirectorPlan JSON down to a compact UI summary:
    `{present: bool, intent: str, n_chars, n_scenes, n_prompts}`. Returns
    None if no plan was cached on the Job."""
    if not plan_json:
        return None
    try:
        import json as _json
        data = _json.loads(plan_json)
    except (ValueError, TypeError):
        return {"present": True, "intent": "(unparseable)",
                "n_chars": 0, "n_scenes": 0, "n_prompts": 0}
    chars = data.get("characters") or []
    n_chars = len(chars)
    seen_scenes: set[str] = set()
    n_prompts = 0
    for c in chars:
        for sc in c.get("scenes") or []:
            sid = sc.get("scene_id")
            if sid:
                seen_scenes.add(sid)
            n_prompts += len(sc.get("variants") or [])
    return {
        "present": True,
        "intent": data.get("intent") or "",
        "n_chars": n_chars,
        "n_scenes": len(seen_scenes),
        "n_prompts": n_prompts,
    }


def _job_to_dict(job: Job) -> dict:
    eff_ids = _effective_scene_ids(job)
    eff_paths = _effective_scene_paths(job)
    return {
        "job_id": job.job_id,
        "title": job.title or job.job_id,
        "project_id": job.project_id,
        # Legacy single-scene fields preserved.
        "scene_id": job.scene_id,
        "scene_image_url": _file_url(job.scene_image_path),
        # New multi-scene fields: parallel lists [{scene_id, url}] for the
        # frontend.
        "scenes": [
            {"scene_id": sid, "url": _file_url(p),
             "end_frame_url": _file_url((job.end_frames_by_scene or {}).get(sid))}
            for sid, p in zip(eff_ids, eff_paths)
        ],
        "prompt": job.prompt,
        "image_model": job.image_model,
        "video_model": job.video_model,
        # Legacy single + new per-scene dict. The UI prefers the dict;
        # the singular is the "first" scene's value for old code paths.
        "movement_prompt": job.movement_prompt,
        "movement_prompts": dict(job.movement_prompts or {}),
        # Per-approved-image overrides (Step 4 per-image rows). Empty in
        # per-scene / legacy mode.
        "movement_prompts_by_variant": dict(job.movement_prompts_by_variant or {}),
        "durations_by_variant": dict(job.durations_by_variant or {}),
        # AI Director: opt-in toggle + a small summary parsed from the
        # cached plan so the UI can show a 🎬 badge ("12 tailored prompts").
        # The plan JSON itself stays on the server (too large for the UI).
        "use_director": bool(job.use_director),
        "director_plan_summary": _director_plan_summary(job.director_prompts_json),
        "images_per_character": job.images_per_character,
        "videos_per_character": job.videos_per_character,
        "duration_secs": job.duration_secs,
        "compacted": job.compacted,
        "created_at": job.created_at.isoformat() + "Z",
        "updated_at": job.updated_at.isoformat() + "Z",
        "characters": {
            cid: {
                "char_id": jc.char_id,
                "name": jc.name,
                "source_image_url": _file_url(jc.source_image_path),
                "status": jc.status.value,
                # Legacy single-pick + the canonical multi-pick list. The UI
                # keys all green-ring / approved-badge logic off
                # `approved_variant_ids`; `approved_variant_id` is kept in
                # sync as the first element for old code paths.
                "approved_variant_id": jc.approved_variant_id,
                "approved_variant_ids": list(jc.approved_variant_ids or []),
                "error": jc.error,
                # Step 6 (Compile) state — surfaces the compiled per-character
                # video so the UI can show a preview + download. `compile_status`
                # transitions null → "compiling" → "done" | "failed".
                "compiled_video_url": (
                    _file_url(jc.compiled_video_path)
                    if jc.compiled_video_path else None
                ),
                "compile_status": jc.compile_status,
                "compile_edit_id": jc.compile_edit_id,
                "compile_error": jc.compile_error,
                "pipeline_status": jc.pipeline_status,
                "pipeline_error": jc.pipeline_error,
                "pipeline_drive_link": jc.pipeline_drive_link,
                # Generated end frames per scene (this character swapped into
                # the scene's end pose) — the Kling 3.0 interpolation target.
                # scene_id → URL. Empty when no end poses are set.
                "end_frame_urls": {
                    sid: _file_url(p)
                    for sid, p in (jc.end_frame_paths or {}).items()
                },
                # Per-scene end-frame generation errors, surfaced so a failed
                # swap is visible in Step 3 instead of silently swallowed.
                "end_frame_errors": dict(jc.end_frame_errors or {}),
                "images": [
                    {
                        "variant_id": v.variant_id,
                        "url": _file_url(v.path),
                        "prompt": v.prompt,
                        "parent_variant_id": v.parent_variant_id,
                        "scene_id": v.scene_id,
                        "status": v.status,
                        "error": v.error,
                        "imported": v.imported,
                        "qc_status": v.qc_status,
                        "qc_reason": v.qc_reason,
                        "qc_attempts": v.qc_attempts,
                        "fallback_model": v.fallback_model,
                        "download_name": _variant_download_name(jc, v),
                    }
                    for v in jc.images
                ],
                "videos": [
                    {
                        "video_id": vv.video_id,
                        "grok_job_id": vv.grok_job_id,
                        "status": vv.status,
                        "url": _file_url(vv.final_video_path),
                        "source_variant_id": vv.source_variant_id,
                        "error": vv.error,
                        "download_name": _video_download_name(jc, vv),
                        # Per-video override + the fallback per-scene prompt,
                        # so the Step 5 regen modal can pre-fill correctly
                        # without re-fetching the job.
                        "movement_prompt_override": vv.movement_prompt_override,
                        "effective_movement_prompt": (
                            vv.movement_prompt_override
                            or (job.movement_prompts or {}).get(
                                next((iv.scene_id for iv in jc.images
                                      if iv.variant_id == vv.source_variant_id),
                                     None) or job.scene_id)
                            or job.movement_prompt
                        ),
                    }
                    for vv in jc.videos
                ],
            }
            for cid, jc in job.characters.items()
        },
    }


def _job_summary(job: Job) -> dict:
    n_chars = len(job.characters)
    n_done = sum(1 for c in job.characters.values() if c.status == CharStatus.DONE)
    n_failed = sum(1 for c in job.characters.values() if c.status == CharStatus.FAILED)
    n_approved = sum(
        1 for c in job.characters.values()
        if c.status in {CharStatus.APPROVED, CharStatus.ANIMATING, CharStatus.DONE}
    )
    return {
        "job_id": job.job_id,
        "title": job.title or job.job_id,
        "project_id": job.project_id,
        "scene_image_url": _file_url(job.scene_image_path),
        "n_characters": n_chars,
        "n_done": n_done,
        "n_failed": n_failed,
        "n_approved": n_approved,
        "movement_set": bool(job.movement_prompt),
        "created_at": job.created_at.isoformat() + "Z",
        "updated_at": job.updated_at.isoformat() + "Z",
    }


# --- lifespan ------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_dirs()
    for job in store().list_jobs():
        await runner.resume_pending(job.job_id)
    # Re-attach reengineer runs orphaned by the restart (their phase watchers
    # are in-process tasks) + auto-retry restart-interrupted swap slots. Must
    # run AFTER resume_pending, which marks those slots failed. Fire-and-
    # forget: watchers run for as long as the runs do.
    from character_swap import runner_reengineer
    runner_reengineer._spawn(runner_reengineer.resume_all(), "reengineer-resume")
    # Higgsfield→Drive watcher: poll the user's configured Drive folder
    # for new Supercomputer outputs and stage them in the Editor inbox.
    # Inert if Drive OAuth isn't configured yet — the loop logs "not set
    # up" and tries again next cycle, so the server still boots fine.
    from character_swap import runner_drive_watcher
    _drive_stop = asyncio.Event()
    _drive_task = asyncio.create_task(
        runner_drive_watcher.watcher_loop(stop_event=_drive_stop),
        name="higgsfield-drive-watcher",
    )
    try:
        yield
    finally:
        _drive_stop.set()
        try:
            await asyncio.wait_for(_drive_task, timeout=5)
        except (asyncio.TimeoutError, Exception):
            _drive_task.cancel()


app = FastAPI(title="Character Swap Studio", lifespan=lifespan)

# Ensure mount directories exist *before* StaticFiles validates them. Otherwise
# a fresh checkout (no uploads yet) crashes the server at import time because
# `lifespan` (which runs _ensure_dirs) hasn't fired yet.
_ensure_dirs()

# Narrow static mounts — only the directories that should be web-reachable.
# Keeps state/, .env, and project source off the wire even if HOST is changed.
app.mount("/files/output",
          StaticFiles(directory=str(settings.output_dir)),
          name="files-output")
app.mount("/files/input/scenes",
          StaticFiles(directory=str(settings.scenes_dir)),
          name="files-scenes")
# Lazily ensure extra_refs/ exists so StaticFiles doesn't fail at startup on
# fresh installs (the dir is created on first upload, but the mount is
# constructed at module import).
(settings.input_dir / "extra_refs").mkdir(parents=True, exist_ok=True)
app.mount("/files/input/extra_refs",
          StaticFiles(directory=str(settings.input_dir / "extra_refs")),
          name="files-extra-refs")
app.mount("/files/characters",
          StaticFiles(directory=str(settings.characters_dir)),
          name="files-characters")
# Frontend static assets (the esbuild Remotion preview bundle). index.html
# references /files/web/static/remotion-preview.js — without this mount the
# in-browser @remotion/player preview 404s silently and the visual caption
# editor's live preview never mounts.
(settings.web_dir / "static").mkdir(parents=True, exist_ok=True)
app.mount("/files/web/static",
          StaticFiles(directory=str(settings.web_dir / "static")),
          name="files-web-static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(settings.web_dir / "index.html")


@app.get("/app.js")
async def app_js() -> FileResponse:
    return FileResponse(settings.web_dir / "app.js")


# SPA deep-link: a reload on /j/<job_id> should serve the same index.html.
# Defined separately (not catch-all) so we don't accidentally swallow real API
# routes when adding new ones later.
@app.get("/j/{job_id}")
async def job_spa(job_id: str) -> FileResponse:
    return FileResponse(settings.web_dir / "index.html")


# --- scenes --------------------------------------------------------------------------

@app.post("/api/jobs/extra_ref")
async def upload_extra_reference(file: UploadFile) -> dict:
    """Upload the optional 3rd reference image used by the swap-image model.

    Saved to `input/extra_refs/xr_<sha256[:10]><ext>` (content-addressed so
    re-uploading the same file deduplicates). Returns the basename which
    the client sends back in `POST /api/jobs` as `extra_reference_filename`.

    These files are job-context, not reusable library assets — no DB row,
    just a path. The basename-only return value lets the create-job handler
    validate against `..` traversal cheaply.
    """
    ext = _safe_ext(file.filename or "")
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    digest = hashlib.sha256(data).hexdigest()[:10]
    filename = f"xr_{digest}{ext}"
    dest_dir = settings.input_dir / "extra_refs"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / filename
    if not dest.exists():
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
    return {
        "filename": filename,
        "url": _file_url(dest),
        "original_name": file.filename or filename,
    }


@app.post("/api/scenes")
async def upload_scene(file: UploadFile) -> dict:
    ext = _safe_ext(file.filename or "")
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    # Content-addressed scene ids: same image twice → same scene.
    scene_id = "sc_" + hashlib.sha256(data).hexdigest()[:10]
    s = store()
    existing = s.get_scene(scene_id)
    if existing is not None:
        # File should already be on disk, but write if missing (e.g. user
        # cleared input/scenes without clearing state).
        existing_path = settings.scenes_dir / existing.filename
        if not existing_path.exists():
            existing_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = existing_path.with_suffix(existing_path.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(existing_path)
        return {
            "scene_id": existing.scene_id,
            "filename": existing.filename,
            "url": _file_url(existing_path),
            "original_name": existing.original_name,
            "deduped": True,
        }

    dest = settings.scenes_dir / f"{scene_id}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    scene = SceneAsset(
        scene_id=scene_id,
        filename=dest.name,
        original_name=file.filename or dest.name,
    )
    s.add_scene(scene)
    return {
        "scene_id": scene_id,
        "filename": dest.name,
        "url": _file_url(dest),
        "original_name": scene.original_name,
        "deduped": False,
    }


@app.get("/api/scenes/{scene_id}")
async def get_scene(scene_id: str) -> dict:
    scene = store().get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "Scene not found")
    return {
        "scene_id": scene.scene_id,
        "filename": scene.filename,
        "url": _file_url(settings.scenes_dir / scene.filename),
        "original_name": scene.original_name,
    }


# --- characters ----------------------------------------------------------------------

def _char_to_dict(ch: CharacterAsset) -> dict:
    """Serialize a character with its image list. The legacy `url`/`filename`
    fields point to the primary image so older frontend code paths keep working."""
    primary = next((i for i in ch.images if i.image_id == ch.primary_image_id),
                   ch.images[0] if ch.images else None)
    primary_filename = primary.filename if primary else ch.filename
    return {
        "char_id": ch.char_id,
        "name": ch.name,
        "filename": primary_filename,
        "url": _file_url(settings.characters_dir / primary_filename) if primary_filename else None,
        "primary_image_id": ch.primary_image_id,
        # Preset ElevenLabs voice — auto-applied when generating a video for
        # this character in the Editor tab (via Character dropdown) or via
        # the Swap Step 6 compile feature.
        "voice_id": ch.voice_id,
        "voice_provider": ch.voice_provider,
        "images": [
            {
                "image_id": img.image_id,
                "filename": img.filename,
                "url": _file_url(settings.characters_dir / img.filename),
                "created_at": img.created_at.isoformat() + "Z",
            }
            for img in ch.images
        ],
    }


@app.post("/api/characters")
async def upload_character(
    file: UploadFile,
    character_id: str | None = Form(None),
    name: str | None = Form(None),
) -> dict:
    """Upload a character image. Two modes:

    - `character_id` provided + character exists → append a new image to that
      character. The character's name is unchanged.
    - `character_id` missing or unknown → create a brand-new character.
      Name comes from the `name` form field, or falls back to the file's stem.
    """
    ext = _safe_ext(file.filename or "")
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    s = store()

    image_id = "im_" + hashlib.sha256(data).hexdigest()[:10]
    # Files are content-addressed by hash → same image uploaded twice
    # never doubles disk usage. The filename is reused.
    image_filename = f"{image_id}{ext}"
    dest = settings.characters_dir / image_filename
    if not dest.exists():
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)

    target = s.get_character(character_id) if character_id else None
    if target is None:
        # Brand-new character. char_id stays content-addressed off the image so
        # repeated uploads of the same source don't create duplicate characters.
        char_id = "ch_" + image_id[3:]
        existing = s.get_character(char_id)
        if existing is not None:
            # Same image already exists somewhere — fold into that character.
            target = existing
        else:
            display_name = (name or "").strip() or Path(file.filename or char_id).stem
            target = CharacterAsset(char_id=char_id, filename=image_filename,
                                    name=display_name)
            s.add_character(target)

    # Append the image if it isn't already in this character's list.
    if not any(img.image_id == image_id for img in target.images):
        target.images.append(CharacterImage(image_id=image_id, filename=image_filename))
        if target.primary_image_id is None:
            target.primary_image_id = image_id
            target.filename = image_filename
        s.add_character(target)  # upsert
    return _char_to_dict(target)


@app.delete("/api/characters/{char_id}/images/{image_id}")
async def delete_character_image(char_id: str, image_id: str) -> dict:
    """Remove one image from a character. If it was the primary, repoint to
    another. If it was the last image, the character itself is deleted."""
    s = store()
    asset = s.get_character(char_id)
    if asset is None:
        raise HTTPException(404, "Character not found")
    img = next((i for i in asset.images if i.image_id == image_id), None)
    if img is None:
        raise HTTPException(404, "Image not found on this character")

    asset.images = [i for i in asset.images if i.image_id != image_id]
    # Best-effort file removal (other characters might share the same hash-named
    # file; only unlink if no other character references it).
    still_referenced = any(
        any(i.filename == img.filename for i in c.images)
        for c in s.state.characters.values() if c.char_id != char_id
    )
    if not still_referenced:
        with contextlib.suppress(OSError):
            (settings.characters_dir / img.filename).unlink(missing_ok=True)

    if not asset.images:
        # Last image gone → delete the character entirely.
        s.remove_character(char_id)
        for project in s.state.projects.values():
            if char_id in project.character_ids:
                project.character_ids = [c for c in project.character_ids if c != char_id]
                s.update_project(project)
        return {"ok": True, "character_deleted": True}

    if asset.primary_image_id == image_id:
        asset.primary_image_id = asset.images[0].image_id
        asset.filename = asset.images[0].filename
    s.add_character(asset)
    return {"ok": True, "character_deleted": False}


@app.get("/api/characters/{char_id}/gallery")
async def character_gallery(char_id: str) -> dict:
    """Every `ready` variant from every job that referenced this character.

    Used by the right-side library panel. Single-user scale, no caching.
    """
    s = store()
    asset = s.get_character(char_id)
    if asset is None:
        raise HTTPException(404, "Character not found")
    appearances: list[dict] = []
    for job in s.list_jobs():
        jc = job.characters.get(char_id)
        if jc is None:
            continue
        for v in jc.images:
            if v.status != VariantStatus.READY:
                continue
            appearances.append({
                "variant_id": v.variant_id,
                "url": _file_url(Path(v.path)),
                "job_id": job.job_id,
                "job_title": job.title or job.job_id,
                # Approved in EITHER the multi-pick list or the legacy
                # single field (the latter for jobs created before the
                # multi-approve migration ran).
                "is_approved": (v.variant_id in (jc.approved_variant_ids or [])
                                or v.variant_id == jc.approved_variant_id),
                "is_edit": v.parent_variant_id is not None,
                "created_at": v.created_at.isoformat() + "Z",
            })
    appearances.sort(key=lambda x: x["created_at"], reverse=True)
    return {
        "char_id": asset.char_id,
        "name": asset.name,
        "source_url": _file_url(settings.characters_dir / asset.filename),
        "appearances": appearances,
    }


@app.get("/api/characters")
async def list_characters() -> list[dict]:
    return [_char_to_dict(ch) for ch in store().list_characters()]


class RenameCharacterBody(BaseModel):
    """PATCH body for /api/characters/{char_id}. All fields optional —
    only sends what's actually changing. Empty string on voice_id clears the
    preset."""
    name: str | None = None
    voice_id: str | None = None
    voice_provider: str | None = None


@app.patch("/api/characters/{char_id}")
async def rename_character(char_id: str, body: RenameCharacterBody) -> dict:
    """Despite the name, this endpoint updates ANY character attribute that
    the client cares to send: display name, preset voice_id, voice_provider.
    Renaming is also retroactive — every past job's snapshot name updates."""
    s = store()
    asset = s.get_character(char_id)
    if asset is None:
        raise HTTPException(404, "Character not found")
    affected_jobs: list = []
    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(400, "Empty name")
        asset.name = new_name
        # Retroactive: walk every job and update snapshot names where char_id matches.
        for job in s.state.jobs.values():
            if char_id in job.characters:
                job.characters[char_id].name = new_name
                affected_jobs.append(job)
    if body.voice_id is not None:
        # Empty string clears the preset voice (user picked "— none —").
        new_voice = body.voice_id.strip()
        asset.voice_id = new_voice or None
        # Pick a sensible provider default when a voice is set.
        if asset.voice_id:
            asset.voice_provider = (body.voice_provider or "elevenlabs").strip() or "elevenlabs"
        else:
            asset.voice_provider = None
    elif body.voice_provider is not None:
        # Allow swapping provider without changing voice_id (rare).
        asset.voice_provider = body.voice_provider.strip() or None
    s.update_character(asset)
    for job in affected_jobs:
        s.update_job(job)
    return _char_to_dict(asset)


@app.delete("/api/characters/{char_id}")
async def delete_character(char_id: str) -> dict:
    s = store()
    asset = s.remove_character(char_id)
    if asset is None:
        raise HTTPException(404, "Character not found")
    # Prune any project presets that referenced this character.
    for project in s.state.projects.values():
        if char_id in project.character_ids:
            project.character_ids = [c for c in project.character_ids if c != char_id]
            s.update_project(project)
    with contextlib.suppress(OSError):
        (settings.characters_dir / asset.filename).unlink(missing_ok=True)
    return {"ok": True}


# --- projects ------------------------------------------------------------------------

class CreateProjectBody(BaseModel):
    name: str
    character_ids: list[str] | None = None


def _project_to_dict(project: ProjectAsset, n_jobs: int) -> dict:
    return {
        "project_id": project.project_id,
        "name": project.name,
        "character_ids": project.character_ids,
        "default_prompt": project.default_prompt,
        "n_jobs": n_jobs,
        "created_at": project.created_at.isoformat() + "Z",
        "updated_at": project.updated_at.isoformat() + "Z",
    }


def _validate_character_ids(s, ids: list[str]) -> list[str]:
    """Strip duplicates, reject ids that aren't in the library, preserve order."""
    seen: set[str] = set()
    out: list[str] = []
    for cid in ids:
        if cid in seen:
            continue
        if s.get_character(cid) is None:
            raise HTTPException(404, f"Character not found: {cid}")
        seen.add(cid)
        out.append(cid)
    return out


def _project_job_counts(s) -> dict[str, int]:
    counts: dict[str, int] = {}
    for j in s.state.jobs.values():
        if j.project_id is not None:
            counts[j.project_id] = counts.get(j.project_id, 0) + 1
    return counts


@app.post("/api/projects")
async def create_project(body: CreateProjectBody) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Empty project name")
    s = store()
    char_ids = _validate_character_ids(s, body.character_ids or [])
    project = ProjectAsset(
        project_id="pr_" + secrets.token_hex(5),
        name=name,
        character_ids=char_ids,
    )
    s.add_project(project)
    return _project_to_dict(project, n_jobs=0)


@app.get("/api/projects")
async def list_projects() -> list[dict]:
    s = store()
    counts = _project_job_counts(s)
    projects = sorted(s.list_projects(), key=lambda p: p.created_at)
    return [_project_to_dict(p, counts.get(p.project_id, 0)) for p in projects]


@app.patch("/api/projects/{project_id}")
async def patch_project(project_id: str, body: dict) -> dict:
    s = store()
    project = s.get_project(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")

    changed = False
    if "name" in body:
        new_name = (body.get("name") or "").strip()
        if not new_name:
            raise HTTPException(400, "Empty project name")
        project.name = new_name
        changed = True
    if "character_ids" in body:
        raw = body.get("character_ids")
        if not isinstance(raw, list):
            raise HTTPException(400, "character_ids must be a list of strings")
        project.character_ids = _validate_character_ids(s, raw)
        changed = True
    if "default_prompt" in body:
        raw = body.get("default_prompt")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            project.default_prompt = None    # clear → fall back to global default
        elif isinstance(raw, str):
            project.default_prompt = raw.strip()
        else:
            raise HTTPException(400, "default_prompt must be a string or null")
        changed = True

    if not changed:
        raise HTTPException(400, "No supported fields to update")

    s.update_project(project)
    return _project_to_dict(project, _project_job_counts(s).get(project_id, 0))


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str) -> dict:
    s = store()
    project = s.get_project(project_id)
    if project is None:
        raise HTTPException(404, "Project not found")
    deleted_jobs = s.delete_project(project_id)
    for jid in deleted_jobs:
        target = settings.output_dir / jid
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    return {"ok": True, "deleted_jobs": deleted_jobs}


# --- jobs ----------------------------------------------------------------------------

class CreateJobBody(BaseModel):
    # scene_id stays for back-compat with the existing single-scene UX;
    # scene_ids is the new canonical list. If both supplied, scene_ids wins.
    # If only scene_id, we treat the job as having a single scene = [scene_id].
    scene_id: str | None = None
    scene_ids: list[str] | None = None
    character_ids: list[str]
    images_per_character: int = Field(default=1, ge=1, le=4)
    title: str | None = None
    project_id: str | None = None
    prompt: str | None = None
    image_model: str | None = None
    # Optional per-character source-image override at create time. Maps
    # char_id → image_id (from CharacterAsset.images[].image_id). When set,
    # the job snapshots that image instead of the character's primary.
    # Lets users pick a non-primary reference in Step 2 BEFORE generation,
    # without needing to start the job and then PATCH source_image.
    character_source_image_ids: dict[str, str] | None = None
    # When True, expand the custom `prompt` (and later the movement_prompt
    # when submitted) through GPT-4o into a cinematic spec before sending
    # to the image / video model. See `prompt_enrich.py`.
    enrich_prompt: bool = False
    # When True, route the job through the AI Director: one Claude Opus
    # call with vision + tool-use writes a tailored prompt per (character,
    # scene, variant). Slower (~15-25s) and higher cost (~$0.05) than
    # `enrich_prompt`, but produces character-specific prompts that
    # reference visible features instead of "the second picture".
    # See `prompt_director.py`. Director takes precedence over enrich;
    # both can be enabled simultaneously (Director wins where it succeeds,
    # enrich is the fallback).
    use_director: bool = False
    # Optional third reference image for the image model: scene + character +
    # this one. Path is relative to `settings.input_dir / 'extra_refs'`, as
    # returned by `POST /api/jobs/extra_ref`. None when the user didn't
    # upload one.
    extra_reference_filename: str | None = None
    # Optional per-scene END-POSE reference: owner scene_id → scene_id of an
    # uploaded pose image (uploaded via POST /api/scenes like any scene). In
    # Step 3 the runner swaps each character into the pose so the scene's Kling
    # 3.0 end frame features the same character (start→end interpolation).
    # Resolved to file paths → Job.end_frames_by_scene at creation.
    end_poses: dict[str, str] | None = None


async def _run_async(coro_fn, *args, **kwargs) -> None:
    await coro_fn(*args, **kwargs)


@app.post("/api/jobs")
async def create_job(body: CreateJobBody, background: BackgroundTasks) -> dict:
    settings.require_keys("openai")
    s = store()
    # Resolve the scene list: prefer `scene_ids` (multi-scene), fall back
    # to `scene_id` (legacy single). At least one must be supplied.
    raw_scene_ids: list[str] = []
    if body.scene_ids:
        raw_scene_ids = [sid for sid in body.scene_ids if sid]
    if body.scene_id and body.scene_id not in raw_scene_ids:
        raw_scene_ids.insert(0, body.scene_id)
    if not raw_scene_ids:
        raise HTTPException(400, "Provide scene_id or scene_ids")

    scene_paths: list[Path] = []
    for sid in raw_scene_ids:
        scene = s.get_scene(sid)
        if scene is None:
            raise HTTPException(404, f"Scene not found: {sid}")
        path = settings.scenes_dir / scene.filename
        if not path.exists():
            raise HTTPException(500, f"Scene file missing on disk: {path}")
        scene_paths.append(path)

    # Optional per-scene end-pose references: scene_id → pose scene_id. Each
    # pose was uploaded via POST /api/scenes (its own scene_id). Resolve to a
    # file path keyed by the OWNING scene_id; unknown poses are skipped.
    end_frames_by_scene: dict[str, str] = {}
    for owner_sid, pose_sid in (body.end_poses or {}).items():
        if owner_sid not in raw_scene_ids or not pose_sid:
            continue
        pose_scene = s.get_scene(pose_sid)
        if pose_scene is None:
            continue
        pose_path = settings.scenes_dir / pose_scene.filename
        if pose_path.exists():
            end_frames_by_scene[owner_sid] = str(pose_path)

    if not body.character_ids:
        raise HTTPException(400, "At least one character_id required")

    if body.project_id is not None and s.get_project(body.project_id) is None:
        raise HTTPException(404, f"Project not found: {body.project_id}")

    job_id = "j_" + secrets.token_hex(5)
    chars: dict[str, JobCharacter] = {}
    char_names: list[str] = []
    overrides = body.character_source_image_ids or {}
    for cid in body.character_ids:
        ch = s.get_character(cid)
        if ch is None:
            raise HTTPException(404, f"Character not found: {cid}")
        src = settings.characters_dir / ch.resolve_source_filename(overrides.get(cid))
        if not src.exists():
            raise HTTPException(500, f"Character file missing on disk: {src}")
        chars[cid] = JobCharacter(
            char_id=cid,
            name=ch.name,
            source_image_path=str(src),
            status=CharStatus.QUEUED,
        )
        char_names.append(ch.name)

    title = (body.title or "").strip() or _auto_title(char_names)

    image_model = (body.image_model or "gpt-image").strip()
    if image_model not in runner_media.IMAGE_MODELS:
        raise HTTPException(400, f"Unknown image_model '{image_model}'")
    if not settings.has_provider(runner_media.IMAGE_MODELS[image_model]["provider"]):
        raise HTTPException(
            503,
            f"{runner_media.IMAGE_MODELS[image_model]['label']} is not configured. "
            f"Add the right API key to .env.",
        )
    custom_prompt = (body.prompt or "").strip() or None
    # If no explicit prompt was supplied and the job's project has a
    # custom default_prompt, inherit it. This lets the user say "every
    # job in project X uses this prompt" without having to retype it.
    if not custom_prompt and body.project_id:
        proj = s.get_project(body.project_id)
        if proj and proj.default_prompt:
            custom_prompt = proj.default_prompt

    # Resolve optional extra reference image (uploaded via /api/jobs/extra_ref).
    extra_ref_abs: str | None = None
    if body.extra_reference_filename:
        candidate = (settings.input_dir / "extra_refs" / body.extra_reference_filename).resolve()
        extra_refs_root = (settings.input_dir / "extra_refs").resolve()
        # Defend against `..` traversal — must live under extra_refs/.
        try:
            candidate.relative_to(extra_refs_root)
        except ValueError:
            raise HTTPException(400, "extra_reference_filename must be a basename")
        if not candidate.exists():
            raise HTTPException(404, f"Extra reference file not found: {body.extra_reference_filename}")
        extra_ref_abs = str(candidate)

    job = Job(
        job_id=job_id,
        title=title,
        project_id=body.project_id,
        # Legacy single-scene fields point at the FIRST scene so older
        # code paths (download names, summaries) keep working.
        scene_id=raw_scene_ids[0],
        scene_image_path=str(scene_paths[0]),
        # New canonical fields — runner reads these.
        scene_ids=raw_scene_ids,
        scene_image_paths=[str(p) for p in scene_paths],
        characters=chars,
        images_per_character=body.images_per_character,
        prompt=custom_prompt,
        image_model=image_model,
        enrich_prompt=body.enrich_prompt,
        use_director=body.use_director,
        extra_reference_path=extra_ref_abs,
        end_frames_by_scene=end_frames_by_scene,
    )
    s.add_job(job)
    background.add_task(_run_async, runner.run_image_generation, job_id)
    return _job_to_dict(job)


# How many images one Animate-tab sequence may contain. Generous — a long
# reel is ~10-15 scenes; the cap just guards against pathological uploads.
_MAX_SEQUENCE_IMAGES = 50


@app.post("/api/jobs/from_images")
async def create_job_from_images(
    files: list[UploadFile] = File(...),
    title: str | None = Form(None),
    video_model: str = Form("kling-v2-6"),
) -> dict:
    """Create a job straight from finished images — powers the Animate tab.

    Unlike `POST /api/jobs` (the Swap flow), this skips Steps 1-3 entirely
    (no scene upload, no character pick, no AI image generation, no manual
    approval). Each uploaded image becomes one scene slot in upload order,
    carried by a single synthetic character whose variants are pre-marked
    READY and pre-approved. The job lands in APPROVED status ready for
    Step 4 (movement) immediately, so the existing video-synthesis +
    compile pipeline runs unchanged.
    """
    if not files:
        raise HTTPException(400, "Upload at least one image")
    if len(files) > _MAX_SEQUENCE_IMAGES:
        raise HTTPException(
            400,
            f"Too many images ({len(files)}); cap is {_MAX_SEQUENCE_IMAGES} per sequence.",
        )

    # Validate the requested video model against the registry. Fall back to
    # the default if the slug is unknown — the user re-picks in Step 4 anyway,
    # so this is just the picker's initial value.
    model = (video_model or "").strip()
    if model not in runner_media.VIDEO_MODELS:
        model = "kling-v2-6" if "kling-v2-6" in runner_media.VIDEO_MODELS else "grok-imagine"

    job_id = "j_" + secrets.token_hex(5)
    char_id = "seq_" + secrets.token_hex(4)
    out_dir = settings.output_dir / job_id / char_id
    out_dir.mkdir(parents=True, exist_ok=True)

    images: list[GeneratedImage] = []
    approved_ids: list[str] = []
    scene_ids: list[str] = []
    scene_paths: list[str] = []
    for idx, file in enumerate(files):
        ext = _safe_ext(file.filename or "") or ".png"
        data = await _read_capped(file)
        if not data:
            raise HTTPException(400, f"Empty upload: {file.filename or f'image {idx + 1}'}")
        variant_id = "v_" + secrets.token_hex(5)
        scene_id = f"seq_{idx}"
        dest = out_dir / f"variant_{variant_id}{ext}"
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        images.append(GeneratedImage(
            variant_id=variant_id,
            path=str(dest),
            prompt="(uploaded image)",
            scene_id=scene_id,
            status=VariantStatus.READY,
        ))
        approved_ids.append(variant_id)
        scene_ids.append(scene_id)
        scene_paths.append(str(dest))

    name = (title or "").strip() or "Sequence"
    jc = JobCharacter(
        char_id=char_id,
        name=name,
        # The first uploaded image stands in as the character's reference
        # thumbnail; it's never used to generate anything.
        source_image_path=scene_paths[0],
        status=CharStatus.APPROVED,
        images=images,
        approved_variant_ids=list(approved_ids),
        approved_variant_id=approved_ids[0],
    )
    job = Job(
        job_id=job_id,
        title=(title or "").strip() or _auto_title([name]),
        # Legacy single-scene fields point at the first slot.
        scene_id=scene_ids[0],
        scene_image_path=scene_paths[0],
        # Canonical ordered scene slots — one per uploaded image.
        scene_ids=scene_ids,
        scene_image_paths=scene_paths,
        characters={char_id: jc},
        images_per_character=1,
        video_model=model,
    )
    store().add_job(job)
    return _job_to_dict(job)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = store().get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return _job_to_dict(job)


@app.get("/api/jobs")
async def list_jobs(summary: int = 0) -> list[dict]:
    jobs = store().list_jobs()
    if summary:
        return [_job_summary(j) for j in jobs]
    return [_job_to_dict(j) for j in jobs]


@app.patch("/api/jobs/{job_id}")
async def patch_job(job_id: str, body: dict) -> dict:
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")

    # Editing the swap prompt or image_model is allowed up until movement is
    # submitted (i.e. before the user has committed to specific videos).
    locked_after_movement = bool(job.movement_prompt)

    changed = False
    if "title" in body:
        new_title = (body.get("title") or "").strip()
        if not new_title:
            raise HTTPException(400, "Empty title")
        job.title = new_title
        changed = True
    if "project_id" in body:
        pid = body.get("project_id")
        if pid is not None:
            if not isinstance(pid, str):
                raise HTTPException(400, "project_id must be a string or null")
            if s.get_project(pid) is None:
                raise HTTPException(404, f"Project not found: {pid}")
        job.project_id = pid
        changed = True
    if "prompt" in body:
        if locked_after_movement:
            raise HTTPException(409, "Movement prompt already submitted; swap prompt is locked")
        raw = body.get("prompt")
        if raw is not None and not isinstance(raw, str):
            raise HTTPException(400, "prompt must be a string or null")
        cleaned = (raw or "").strip() or None
        job.prompt = cleaned
        changed = True
    if "image_model" in body:
        if locked_after_movement:
            raise HTTPException(409, "Movement prompt already submitted; image model is locked")
        new_model = (body.get("image_model") or "").strip()
        if new_model not in runner_media.IMAGE_MODELS:
            raise HTTPException(400, f"Unknown image_model '{new_model}'")
        if not settings.has_provider(runner_media.IMAGE_MODELS[new_model]["provider"]):
            raise HTTPException(
                503,
                f"{runner_media.IMAGE_MODELS[new_model]['label']} is not configured.",
            )
        job.image_model = new_model
        changed = True

    # scene_ids: allow extending the scene list pre-generation. The check
    # against variant existence prevents mid-generation mutation that would
    # race the runner's per-(char, scene) variant scheduling. The user
    # then clicks "↻ regenerate all" (or, equivalently, this PATCH triggers
    # a fresh `run_image_generation` call which `_kick_char`-wipes + re-fans).
    if "scene_ids" in body:
        raw_ids = body.get("scene_ids")
        if not isinstance(raw_ids, list) or not raw_ids:
            raise HTTPException(400, "scene_ids must be a non-empty list")
        any_variants = any(len(jc.images) > 0 for jc in job.characters.values())
        if any_variants:
            raise HTTPException(
                409,
                "Cannot edit scene_ids after variant generation has started. "
                "Click `duplicate` in the job header to fork this job with the same chars "
                "and add scenes there.",
            )
        new_paths: list[str] = []
        for sid in raw_ids:
            scene = s.get_scene(sid)
            if scene is None:
                raise HTTPException(404, f"Scene not found: {sid}")
            path = settings.scenes_dir / scene.filename
            if not path.exists():
                raise HTTPException(500, f"Scene file missing on disk: {path}")
            new_paths.append(str(path))
        job.scene_ids = list(raw_ids)
        job.scene_image_paths = new_paths
        # Keep legacy single-scene fields pointing at the first entry.
        job.scene_id = raw_ids[0]
        job.scene_image_path = new_paths[0]
        changed = True

    if not changed:
        raise HTTPException(400, "No supported fields to update")

    s.update_job(job)
    return _job_to_dict(job)


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    s.remove_job(job_id)
    # rmtree the job's output dir best-effort
    target = settings.output_dir / job_id
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    return {"ok": True}


class ApproveBody(BaseModel):
    char_id: str
    action: str
    variant_id: str | None = None


@app.post("/api/jobs/{job_id}/approve")
async def approve(job_id: str, body: ApproveBody, background: BackgroundTasks) -> dict:
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    # Reengineer edit mode approves NEW-scene variants after videos exist —
    # its own approval flow gates the expensive work, so the lock is relaxed
    # for reengineer-origin jobs only.
    if job.movement_prompt and not job.from_reengineer:
        raise HTTPException(409, "Movement prompt already submitted; approvals are locked")
    jc = job.characters.get(body.char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")

    if body.action == "approve":
        if not body.variant_id:
            raise HTTPException(400, "variant_id required for approve")
        match = next((v for v in jc.images if v.variant_id == body.variant_id), None)
        if match is None:
            raise HTTPException(404, "Variant not found on this character")
        if match.status != VariantStatus.READY:
            raise HTTPException(409, f"Variant is '{match.status}', cannot approve")

        # Multi-variant approval — TOGGLE the variant. Clicking ✓ on an
        # already-approved variant un-approves it; clicking on a fresh one
        # adds it to the list so multiple (typically one per scene) can be
        # animated in parallel in Step 4.
        approved_ids = list(jc.approved_variant_ids or [])
        if not approved_ids and jc.approved_variant_id:
            approved_ids = [jc.approved_variant_id]
        if body.variant_id in approved_ids:
            approved_ids = [vid for vid in approved_ids if vid != body.variant_id]
            event_kind = "char.unapproved"
        else:
            approved_ids.append(body.variant_id)
            event_kind = "char.approved"
        # Keep ordering aligned with jc.images so the UI shows approvals in
        # a stable order across scene groups.
        order = {v.variant_id: i for i, v in enumerate(jc.images)}
        approved_ids.sort(key=lambda x: order.get(x, len(jc.images)))

        jc.approved_variant_ids = approved_ids
        jc.approved_variant_id = approved_ids[0] if approved_ids else None
        jc.status = (CharStatus.APPROVED if approved_ids
                     else CharStatus.AWAITING_APPROVAL)
        jc.updated_at = datetime.utcnow()
        job.characters[body.char_id] = jc
        s.update_job(job)
        await events.publish(job_id, {"kind": event_kind, "job_id": job_id,
                                      "char_id": body.char_id,
                                      "variant_id": body.variant_id,
                                      "approved_variant_ids": approved_ids})
    elif body.action == "reject":
        jc.status = CharStatus.REJECTED
        jc.approved_variant_id = None
        jc.approved_variant_ids = []
        jc.updated_at = datetime.utcnow()
        job.characters[body.char_id] = jc
        s.update_job(job)
        await events.publish(job_id, {"kind": "char.rejected", "job_id": job_id,
                                      "char_id": body.char_id})
    elif body.action == "regenerate":
        background.add_task(_run_async, runner.run_image_generation, job_id, [body.char_id])
    else:
        raise HTTPException(400, f"Unknown action '{body.action}'")
    return _job_to_dict(job)


@app.post("/api/jobs/{job_id}/approve_all")
async def approve_all(job_id: str) -> dict:
    """Bulk-approve one READY variant per (character, scene) pair.

    For multi-scene jobs this fills in a default pick for every scene that
    doesn't yet have an approved variant — typically the FIRST ready
    variant (by position). Single-scene jobs collapse to "approve one per
    character" (same behavior as before). Skips characters that are
    rejected / animating / done. Idempotent.

    Used by the "✓ Approve all" button in Step 3. Each approval emits a
    `char.approved` event so the UI updates in place.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.movement_prompt:
        raise HTTPException(409, "Movement prompt already submitted; approvals are locked")

    scene_ids = _effective_scene_ids(job) or [job.scene_id]
    # Legacy single-scene variants have scene_id = None; treat them as
    # belonging to the first (only) scene so the per-scene check still works.
    primary_scene = scene_ids[0] if scene_ids else None

    def variant_scene(v) -> str | None:
        return v.scene_id or primary_scene

    picked: list[dict] = []
    for cid, jc in job.characters.items():
        if jc.status in {CharStatus.REJECTED, CharStatus.ANIMATING, CharStatus.DONE}:
            continue
        approved_ids = list(jc.approved_variant_ids or [])
        if not approved_ids and jc.approved_variant_id:
            approved_ids = [jc.approved_variant_id]
        approved_set = set(approved_ids)

        # Which scenes already have at least one approval?
        scenes_covered = {
            variant_scene(v)
            for v in jc.images
            if v.variant_id in approved_set
        }

        char_changed = False
        for sid in scene_ids:
            if sid in scenes_covered:
                continue
            # First READY variant for THIS scene (by position).
            ready = next(
                (v for v in jc.images
                 if variant_scene(v) == sid and v.status == VariantStatus.READY),
                None,
            )
            if ready is None:
                continue
            approved_ids.append(ready.variant_id)
            approved_set.add(ready.variant_id)
            scenes_covered.add(sid)
            picked.append({"char_id": cid, "variant_id": ready.variant_id,
                           "scene_id": sid})
            char_changed = True

        if char_changed:
            # Keep variant order aligned with jc.images for stable UI.
            order = {v.variant_id: i for i, v in enumerate(jc.images)}
            approved_ids.sort(key=lambda x: order.get(x, len(jc.images)))
            jc.approved_variant_ids = approved_ids
            jc.approved_variant_id = approved_ids[0] if approved_ids else None
            jc.status = CharStatus.APPROVED
            jc.updated_at = datetime.utcnow()
            job.characters[cid] = jc

    if picked:
        s.update_job(job)
        for p in picked:
            await events.publish(
                job_id,
                {"kind": "char.approved", "job_id": job_id,
                 "char_id": p["char_id"], "variant_id": p["variant_id"],
                 "scene_id": p["scene_id"]},
            )

    return {"job": _job_to_dict(job), "approved": picked}


class SetSourceImageBody(BaseModel):
    image_id: str


@app.patch("/api/jobs/{job_id}/characters/{char_id}/source_image")
async def set_character_source_image(job_id: str, char_id: str,
                                      body: SetSourceImageBody) -> dict:
    """Swap which image (from the character's library gallery) is used as
    the reference for THIS character on THIS job. Useful when a character
    has multiple reference photos and the user wants different photos for
    different scenes.

    Refused when:
    - the job's movement_prompt is set (the whole approval/gen flow is locked)
    - the character is currently mid-generation or mid-animation (race condition)
    Existing already-generated variants on the character are NOT regenerated
    automatically — the user can hit ↻ regenerate after swapping if they want.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.movement_prompt:
        raise HTTPException(409,
            "Movement prompt already submitted; source image is locked")
    jc = job.characters.get(char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    if jc.status in {CharStatus.GENERATING, CharStatus.ANIMATING}:
        raise HTTPException(409,
            f"Character is '{jc.status}', wait until it settles before swapping the source")

    asset = s.get_character(char_id)
    if asset is None:
        raise HTTPException(404, "Character not in library")
    image = next((i for i in (asset.images or []) if i.image_id == body.image_id), None)
    if image is None:
        raise HTTPException(404, "image_id not found on this character")

    jc.source_image_path = str(settings.characters_dir / image.filename)
    jc.updated_at = datetime.utcnow()
    job.characters[char_id] = jc
    s.update_job(job)
    await events.publish(job_id, {"kind": "char.source_image_changed",
                                   "job_id": job_id, "char_id": char_id,
                                   "image_id": body.image_id})
    return _job_to_dict(job)


class RetryVariantBody(BaseModel):
    # Optional edited prompt — regenerate the failed slot with a NEW prompt
    # (the UI pre-fills it with the prompt that failed so the user can tweak it).
    # None / empty → retry with the slot's existing prompt.
    prompt: str | None = None


@app.post("/api/jobs/{job_id}/characters/{char_id}/variants/{variant_id}/retry")
async def retry_variant(job_id: str, char_id: str, variant_id: str,
                        background: BackgroundTasks,
                        body: RetryVariantBody | None = None) -> dict:
    """Re-run image gen for one specific variant slot — keeps the other
    variants on this character intact and only re-attempts this one.

    Two flavors, same endpoint:
    - FAILED slot → classic retry.
    - READY slot → "reject & regenerate": the user judged the image wrong
      (wrong character/clothes/background) and wants a fresh take in place.
      Any approval of the rejected image is withdrawn first — the new image
      is a different picture and must be re-approved.

    Refuses if movement_prompt is set (gen flow is locked — relaxed for
    Reengineer edit mode, see `approve`) or the slot is still GENERATING
    (already in flight)."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.movement_prompt and not job.from_reengineer:
        raise HTTPException(409, "Movement prompt already submitted; variants are locked")
    jc = job.characters.get(char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    target = next((v for v in jc.images if v.variant_id == variant_id), None)
    if target is None:
        raise HTTPException(404, "Variant not found on this character")
    if target.status not in {VariantStatus.FAILED, VariantStatus.READY}:
        raise HTTPException(409,
            f"Variant status is '{target.status}' — only failed or ready "
            f"slots can be regenerated")
    # Withdraw any approval of the old image (mirrors the approve toggle's
    # bookkeeping: list + legacy field + char status).
    approved_ids = list(jc.approved_variant_ids or [])
    if not approved_ids and jc.approved_variant_id:
        approved_ids = [jc.approved_variant_id]
    if variant_id in approved_ids:
        approved_ids = [vid for vid in approved_ids if vid != variant_id]
        jc.approved_variant_ids = approved_ids
        jc.approved_variant_id = approved_ids[0] if approved_ids else None
        if jc.status == CharStatus.APPROVED and not approved_ids:
            jc.status = CharStatus.AWAITING_APPROVAL
        jc.updated_at = datetime.utcnow()
        job.characters[char_id] = jc
        s.update_job(job)
        await events.publish(job_id, {"kind": "char.unapproved", "job_id": job_id,
                                      "char_id": char_id,
                                      "variant_id": variant_id,
                                      "approved_variant_ids": approved_ids})
    background.add_task(_run_async, runner.retry_single_variant,
                        job_id, char_id, variant_id,
                        (body.prompt if body else None))
    return _job_to_dict(job)


class RegenSceneBody(BaseModel):
    # Optional prompt override for this scene's fresh variants. None / empty →
    # use the same precedence as initial generation (Director → enriched →
    # job.prompt → GENERATION_PROMPT).
    prompt: str | None = None


@app.post("/api/jobs/{job_id}/characters/{char_id}/scenes/{scene_id}/regenerate")
async def regenerate_scene(job_id: str, char_id: str, scene_id: str,
                           background: BackgroundTasks,
                           body: RegenSceneBody | None = None) -> dict:
    """Generate fresh variants for ONE (character, scene) pair — additively,
    without wiping the character's other scenes or approvals.

    Primary use: rebuild a scene whose variants were all deleted (it shows
    "0 variants" in the UI) so the user can recover it with one click instead
    of regenerating every scene. Refuses once the movement prompt is set."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.movement_prompt:
        raise HTTPException(409, "Movement prompt already submitted; variants are locked")
    jc = job.characters.get(char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    scene_ids = list(job.scene_ids) if job.scene_ids else [job.scene_id]
    if scene_id not in scene_ids:
        raise HTTPException(404, "Scene not in job")
    background.add_task(_run_async, runner.regen_scene_variants,
                        job_id, char_id, scene_id,
                        (body.prompt if body else None))
    return _job_to_dict(job)


@app.post("/api/jobs/{job_id}/characters/{char_id}/variants/{variant_id}/replace")
async def replace_variant(job_id: str, char_id: str, variant_id: str,
                          file: UploadFile = File(...)) -> dict:
    """Replace a variant's image with an UPLOADED one (not generated here) — e.g.
    when the app can't produce it (content-policy block). The slot becomes READY
    + `imported` so it can be approved like any variant. Locked after movement."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if _movement_locked(job):
        raise HTTPException(409, "Movement already submitted; variants are locked")
    jc = job.characters.get(char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    target = next((v for v in jc.images if v.variant_id == variant_id), None)
    if target is None:
        raise HTTPException(404, "Variant not found on this character")
    ext = _safe_ext(file.filename or "")
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    out_dir = settings.output_dir / job_id / char_id
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"imported_{variant_id}{ext}"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    target.path = str(dest)
    target.status = VariantStatus.READY
    target.error = None
    target.imported = True
    # A previously failed / still-generating character becomes approvable again.
    if jc.status in {CharStatus.FAILED, CharStatus.GENERATING}:
        jc.status = CharStatus.AWAITING_APPROVAL
    jc.updated_at = datetime.utcnow()
    s.update_job(job)
    await events.publish(job_id, {"kind": "variant.ready", "job_id": job_id,
                                  "char_id": char_id, "variant_id": variant_id})
    return _job_to_dict(job)


@app.delete("/api/jobs/{job_id}/characters/{char_id}/variants/{variant_id}")
async def delete_variant(job_id: str, char_id: str, variant_id: str) -> dict:
    """Remove a single generated variant from a character.

    - Locked once movement_prompt is set (videos may reference the approved one).
    - Deletes the file from disk if it exists.
    - If it was the approved variant, the character drops back to AWAITING_APPROVAL.
    - If it was the last variant, the character flips to FAILED with a hint —
      user can click '↻ regenerate all' to redo.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.movement_prompt:
        raise HTTPException(409, "Movement prompt already submitted; variants are locked")
    jc = job.characters.get(char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    target = next((v for v in jc.images if v.variant_id == variant_id), None)
    if target is None:
        raise HTTPException(404, "Variant not found on this character")

    with contextlib.suppress(OSError):
        p = Path(target.path)
        if p.exists():
            p.unlink()

    jc.images = [v for v in jc.images if v.variant_id != variant_id]
    # Drop the deleted variant from BOTH the legacy field and the multi-pick
    # list, then keep them in sync (legacy = first entry of list, or None).
    jc.approved_variant_ids = [
        vid for vid in (jc.approved_variant_ids or []) if vid != variant_id
    ]
    if jc.approved_variant_id == variant_id:
        jc.approved_variant_id = (jc.approved_variant_ids[0]
                                  if jc.approved_variant_ids else None)
    if not jc.images:
        jc.status = CharStatus.FAILED
        jc.error = "all variants deleted; click regenerate to re-run"
    elif not jc.approved_variant_ids:
        jc.status = CharStatus.AWAITING_APPROVAL
        jc.error = None

    jc.updated_at = datetime.utcnow()
    job.characters[char_id] = jc
    s.update_job(job)
    await events.publish(job_id, {"kind": "variant.deleted", "job_id": job_id,
                                   "char_id": char_id, "variant_id": variant_id})
    return _job_to_dict(job)


class EditVariantBody(BaseModel):
    char_id: str
    variant_id: str
    prompt: str


@app.post("/api/jobs/{job_id}/edit_variant")
async def edit_variant(job_id: str, body: EditVariantBody,
                       background: BackgroundTasks) -> dict:
    settings.require_keys("openai")
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.movement_prompt:
        raise HTTPException(409, "Movement prompt already submitted; edits are locked")
    jc = job.characters.get(body.char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    parent = next((v for v in jc.images if v.variant_id == body.variant_id), None)
    if parent is None:
        raise HTTPException(404, "Variant not found on this character")
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "Edit prompt is empty")

    background.add_task(
        _run_async, runner.run_edit_variant,
        job_id, body.char_id, body.variant_id, prompt,
    )
    return _job_to_dict(job)


class MovementBody(BaseModel):
    # Legacy single-prompt path — applied to every scene that has approvals.
    # Kept for back-compat with older client builds; new UI sends
    # `movement_prompts` instead.
    prompt: str | None = None
    # Per-scene prompts: scene_id → prompt. Every scene with at least one
    # approved variant must have a non-empty entry. Scenes without approvals
    # are skipped (no videos to render for them).
    movement_prompts: dict[str, str] | None = None
    # Per-approved-IMAGE prompts: variant_id → prompt. The granular path used
    # by Step 4's per-image rows — each approved image animates with its own
    # motion. When provided, takes precedence over `movement_prompts`; the
    # server derives a per-scene `movement_prompts` from it for back-compat +
    # the Step 6 compile.
    movement_prompts_by_variant: dict[str, str] | None = None
    # Per-approved-image duration override: variant_id → seconds.
    durations_by_variant: dict[str, int] | None = None
    # Per-scene duration: scene_id → seconds. The granularity the Step 4 UI
    # uses (one duration per scene, shared by that scene's images).
    durations_by_scene: dict[str, int] | None = None
    videos_per_character: int = Field(default=1, ge=1, le=10)
    # Which video provider to use. Defaults to grok-imagine (legacy behavior);
    # the Step-4 picker in web/index.html sends this field so the user can pick
    # Kling / Veo / Runway / Luma / Pika / etc. for the swap flow too.
    video_model: str = "grok-imagine"
    # Per-job duration override (seconds). When None, runner falls back to
    # `settings.video_duration_secs`. The UI's duration dropdown is gated by
    # each model's `duration_options` registry — so any value here that
    # reaches the runner has already been validated against the picker, but
    # we re-validate against the registry server-side to defend against
    # hand-crafted requests.
    duration_secs: int | None = Field(default=None, ge=1, le=120)


# Pre-check map for video providers — refuses to start a job if the user
# picked a model whose API key isn't configured. Mirrors runner_media's
# implicit checks but surfaces the error UPFRONT, before kicking N parallel
# submits that would all fail with the same auth error.
_VIDEO_MODEL_KEYS: dict[str, str] = {
    "grok-imagine": "xai",
    "veo": "gemini",
    "veo-3-fast": "gemini",
    "kling": "kling",
    "kling-2.1-pro": "kling",
    "kling-1.6": "kling",
    "kling-v3": "fal",   # Kling 3.0 routes through fal.ai, not the official Kling API
}


@app.post("/api/jobs/{job_id}/movement")
async def set_movement(job_id: str, body: MovementBody,
                       background: BackgroundTasks) -> dict:
    """Lock in a per-scene movement direction and kick off the video phase.

    Each scene with at least one approved variant needs its own non-empty
    prompt — the runner uses scene-S's prompt for every approved variant
    that belongs to scene S, across all characters. Legacy clients still
    sending a single `prompt` get it broadcast to every scene-with-approvals
    (1-scene jobs collapse to the historical behavior).
    """
    key_name = _VIDEO_MODEL_KEYS.get(body.video_model)
    if key_name:
        settings.require_keys(key_name)
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.movement_prompt or job.movement_prompts:
        raise HTTPException(409, "Movement prompt already set")
    approved_chars = [jc for jc in job.characters.values()
                      if jc.status == CharStatus.APPROVED]
    if not approved_chars:
        raise HTTPException(409, "No approved characters to animate")

    # Which scenes actually need a prompt? Any scene with ≥1 approved variant
    # across all characters. Legacy variants with scene_id=None map to the
    # job's primary scene_id.
    scene_ids = _effective_scene_ids(job) or [job.scene_id]
    primary_scene = scene_ids[0] if scene_ids else job.scene_id
    scenes_with_approvals: set[str] = set()
    approved_variant_ids: set[str] = set()
    variant_to_scene: dict[str, str] = {}
    for jc in approved_chars:
        approved_set = set(jc.approved_variant_ids or [])
        if jc.approved_variant_id:
            approved_set.add(jc.approved_variant_id)
        for v in jc.images:
            if v.variant_id in approved_set:
                sid = v.scene_id or primary_scene
                scenes_with_approvals.add(sid)
                approved_variant_ids.add(v.variant_id)
                variant_to_scene[v.variant_id] = sid

    # Per-variant durations are resolved against the chosen model's options.
    by_variant: dict[str, str] = {}
    durations_by_variant: dict[str, int] = {}

    # Build the canonical per-scene dict from whichever field the client sent.
    # Precedence: per-image (most granular) → per-scene → legacy single.
    if body.movement_prompts_by_variant:
        # Per-IMAGE mode: each approved image carries its own prompt.
        by_variant = {vid: (p or "").strip()
                      for vid, p in body.movement_prompts_by_variant.items()
                      if (p or "").strip()}
        missing_v = approved_variant_ids - set(by_variant.keys())
        if missing_v:
            raise HTTPException(
                400,
                f"Missing movement prompt for {len(missing_v)} approved "
                f"image(s) — every approved image needs a motion prompt.",
            )
        # Derive a per-scene dict (first approved image per scene) so the
        # legacy lock fields + Step 6 compile + per-scene resolver still work.
        prompts = {}
        for vid in approved_variant_ids:
            prompts.setdefault(variant_to_scene[vid], by_variant[vid])
        # Optional per-image durations, validated against the model's options.
        if body.durations_by_variant:
            spec = runner_media.video_duration_spec(body.video_model or "grok-imagine")
            for vid, d in body.durations_by_variant.items():
                if vid in approved_variant_ids and int(d) in spec["options"]:
                    durations_by_variant[vid] = int(d)
    elif body.movement_prompts:
        # Per-scene mode. Trim whitespace, drop empty entries.
        prompts = {sid: (p or "").strip()
                   for sid, p in body.movement_prompts.items()
                   if (p or "").strip()}
    elif body.prompt and body.prompt.strip():
        # Legacy single-prompt: broadcast to every scene-with-approvals.
        single = body.prompt.strip()
        prompts = {sid: single for sid in scenes_with_approvals}
    else:
        raise HTTPException(400, "Movement prompt is empty")

    # Validate: every scene with approvals must have a non-empty prompt.
    missing = scenes_with_approvals - set(prompts.keys())
    if missing:
        raise HTTPException(
            400,
            f"Missing movement prompt for {len(missing)} scene(s) "
            f"that have approved images: {sorted(missing)}",
        )

    # Per-scene durations (scene_id → secs), validated against the model's
    # options. Scenes without approvals are ignored.
    durations_by_scene: dict[str, int] = {}
    if body.durations_by_scene:
        spec = runner_media.video_duration_spec(body.video_model or "grok-imagine")
        for sid, d in body.durations_by_scene.items():
            if sid in scenes_with_approvals and int(d) in spec["options"]:
                durations_by_scene[sid] = int(d)

    job.movement_prompts = prompts
    # Per-image overrides (empty in per-scene / legacy mode). The runner
    # resolves these first, then falls back to the per-scene prompt/duration.
    job.movement_prompts_by_variant = by_variant
    job.durations_by_variant = durations_by_variant
    job.durations_by_scene = durations_by_scene
    # Keep singular field in sync (first scene with a prompt) so legacy
    # `if job.movement_prompt:` lock checks stay truthy.
    job.movement_prompt = (
        prompts.get(primary_scene)
        or next(iter(prompts.values()), None)
    )
    # Reset any stale enriched cache so the runner re-enriches per-scene.
    job.enriched_movement_prompts = {}
    job.enriched_movement_prompt = None
    job.videos_per_character = body.videos_per_character
    job.video_model = body.video_model or "grok-imagine"
    # Validate duration against the chosen model's registry options.
    # Unknown / out-of-range values silently fall back to the model's
    # default (or env default for unregistered models) rather than 400 —
    # the picker shouldn't ever produce a bad value, but defenders gonna
    # defend.
    if body.duration_secs is not None:
        spec = runner_media.video_duration_spec(job.video_model)
        if int(body.duration_secs) in spec["options"]:
            job.duration_secs = int(body.duration_secs)
        else:
            job.duration_secs = spec["default"]
    job.updated_at = datetime.utcnow()
    s.update_job(job)
    await events.publish(job_id, {"kind": "movement.set", "job_id": job_id,
                                  "prompt": job.movement_prompt,
                                  "movement_prompts": prompts,
                                  "video_model": job.video_model,
                                  "videos_per_character": body.videos_per_character})
    background.add_task(_run_async, runner.run_video_synthesis, job_id)
    return _job_to_dict(job)


# --- Scene sequencing (between Step 3 and Step 4) -----------------------------
# Duplicate / reorder / delete scene "slots" so the user can build a video
# sequence from approved images — multiple clips from one image (duplicate),
# custom order (reorder), or drop a slot (delete). All are locked once the
# movement prompt is submitted (videos may already reference the slots).

def _movement_locked(job: Job) -> bool:
    return bool(job.movement_prompt or job.movement_prompts)


def _belongs_to_scene(variant: GeneratedImage, scene_id: str, primary: str) -> bool:
    """A variant belongs to `scene_id` if its scene_id matches, OR it's a
    legacy variant (scene_id=None) and `scene_id` is the job's primary scene."""
    return (variant.scene_id or primary) == scene_id


def _apply_scene_duplicate(job: Job, scene_id: str) -> str:
    """Lock-free core of scene duplication (shared by the Swap endpoint and
    Reengineer edit mode): insert a new scene slot right after the source,
    reuse the same background image, clone every character's APPROVED
    variant (same file on disk, new variant_id) pre-approved under the new
    scene, carry end frames. Returns the new scene_id. Caller persists."""
    scene_ids = _effective_scene_ids(job)
    scene_paths = _effective_scene_paths(job)
    if scene_id not in scene_ids:
        raise HTTPException(404, f"Scene not in job: {scene_id}")
    primary = scene_ids[0] if scene_ids else job.scene_id
    idx = scene_ids.index(scene_id)
    src_path = scene_paths[idx] if idx < len(scene_paths) else (scene_paths[0] if scene_paths else job.scene_image_path)

    new_sid = f"{scene_id}__dup{secrets.token_hex(3)}"
    scene_ids.insert(idx + 1, new_sid)
    scene_paths.insert(idx + 1, src_path)

    # Clone each character's approved variant(s) for the source scene.
    for jc in job.characters.values():
        approved = set(jc.approved_variant_ids or [])
        if jc.approved_variant_id:
            approved.add(jc.approved_variant_id)
        clones: list[GeneratedImage] = []
        for v in jc.images:
            if v.variant_id in approved and _belongs_to_scene(v, scene_id, primary):
                clones.append(GeneratedImage(
                    variant_id="v_" + secrets.token_hex(5),
                    path=v.path,                 # same file on disk
                    prompt=v.prompt,
                    parent_variant_id=v.variant_id,
                    scene_id=new_sid,
                    status=VariantStatus.READY,
                ))
        for c in clones:
            jc.images.append(c)
            jc.approved_variant_ids = list(jc.approved_variant_ids or []) + [c.variant_id]
        if clones:
            jc.updated_at = datetime.utcnow()
        # Carry the already-generated end frame to the duplicated scene so the
        # copy starts with the same end pose (the user can still change it).
        if (jc.end_frame_paths or {}).get(scene_id):
            jc.end_frame_paths[new_sid] = jc.end_frame_paths[scene_id]

    # Carry the scene's end-pose reference to the duplicate too.
    if (job.end_frames_by_scene or {}).get(scene_id):
        job.end_frames_by_scene[new_sid] = job.end_frames_by_scene[scene_id]

    job.scene_ids = scene_ids
    job.scene_image_paths = scene_paths
    job.scene_id = scene_ids[0]
    job.scene_image_path = scene_paths[0]
    job.updated_at = datetime.utcnow()
    return new_sid


@app.post("/api/jobs/{job_id}/scenes/{scene_id}/duplicate")
async def duplicate_scene(job_id: str, scene_id: str) -> dict:
    """Clone a scene slot: a new scene_id is inserted right after the source,
    reusing the same background image, and every character's APPROVED variant
    for that scene is cloned (same file on disk, new variant_id) and
    pre-approved under the new scene. The duplicate gets its own movement
    prompt + duration in Step 4 — the Higgsfield "duplicate slot" model."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if _movement_locked(job):
        raise HTTPException(409, "Movement already submitted; scenes are locked")

    new_sid = _apply_scene_duplicate(job, scene_id)
    s.update_job(job)
    await events.publish(job_id, {"kind": "scene.duplicated", "job_id": job_id,
                                  "scene_id": scene_id, "new_scene_id": new_sid})
    return _job_to_dict(job)


class SceneOrderBody(BaseModel):
    scene_ids: list[str]


@app.patch("/api/jobs/{job_id}/scene_order")
async def reorder_scenes(job_id: str, body: SceneOrderBody) -> dict:
    """Reorder the job's scene slots. Body `scene_ids` must be a permutation of
    the current scene list. scene_image_paths are reordered in lockstep so the
    Step 6 compile concatenates in the new order."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if _movement_locked(job):
        raise HTTPException(409, "Movement already submitted; scene order is locked")

    _apply_scene_reorder(job, body.scene_ids)
    s.update_job(job)
    await events.publish(job_id, {"kind": "scene.reordered", "job_id": job_id,
                                  "scene_ids": job.scene_ids})
    return _job_to_dict(job)


def _apply_scene_reorder(job: Job, new_order: list[str]) -> None:
    """Lock-free core of scene reordering (shared by the Swap endpoint and
    Reengineer edit mode). `new_order` must be a permutation. Caller persists."""
    cur_ids = _effective_scene_ids(job)
    cur_paths = _effective_scene_paths(job)
    if sorted(new_order) != sorted(cur_ids):
        raise HTTPException(400, "scene_ids must be a permutation of the job's scenes")

    path_by_id = {sid: (cur_paths[i] if i < len(cur_paths) else cur_paths[0])
                  for i, sid in enumerate(cur_ids)}
    job.scene_ids = list(new_order)
    job.scene_image_paths = [path_by_id[sid] for sid in new_order]
    job.scene_id = job.scene_ids[0]
    job.scene_image_path = job.scene_image_paths[0]
    job.updated_at = datetime.utcnow()


@app.delete("/api/jobs/{job_id}/scenes/{scene_id}")
async def delete_scene(job_id: str, scene_id: str) -> dict:
    """Drop a scene slot and every variant that belongs to it. Blocked when
    it's the last remaining scene. Files on disk are left in place (a
    duplicate may share the same path)."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if _movement_locked(job):
        raise HTTPException(409, "Movement already submitted; scenes are locked")

    _apply_scene_delete(job, scene_id)
    s.update_job(job)
    await events.publish(job_id, {"kind": "scene.deleted", "job_id": job_id,
                                  "scene_id": scene_id})
    return _job_to_dict(job)


def _apply_scene_delete(job: Job, scene_id: str) -> None:
    """Lock-free core of scene deletion (shared by the Swap endpoint and
    Reengineer edit mode): drop the slot + every variant that belongs to it.
    Files on disk are left in place (a duplicate may share the same path).
    Caller persists."""
    scene_ids = _effective_scene_ids(job)
    scene_paths = _effective_scene_paths(job)
    if scene_id not in scene_ids:
        raise HTTPException(404, f"Scene not in job: {scene_id}")
    if len(scene_ids) <= 1:
        raise HTTPException(409, "Can't delete the only scene")
    primary = scene_ids[0]
    idx = scene_ids.index(scene_id)

    # Drop variants belonging to this scene from every character.
    for jc in job.characters.values():
        keep = [v for v in jc.images if not _belongs_to_scene(v, scene_id, primary)]
        removed_ids = {v.variant_id for v in jc.images} - {v.variant_id for v in keep}
        if removed_ids:
            jc.images = keep
            jc.approved_variant_ids = [vid for vid in (jc.approved_variant_ids or [])
                                       if vid not in removed_ids]
            if jc.approved_variant_id in removed_ids:
                jc.approved_variant_id = (jc.approved_variant_ids[0]
                                          if jc.approved_variant_ids else None)
            jc.updated_at = datetime.utcnow()

    del scene_ids[idx]
    if idx < len(scene_paths):
        del scene_paths[idx]
    job.scene_ids = scene_ids
    job.scene_image_paths = scene_paths
    job.scene_id = scene_ids[0]
    job.scene_image_path = scene_paths[0] if scene_paths else job.scene_image_path
    job.updated_at = datetime.utcnow()


@app.post("/api/jobs/{job_id}/scenes/{scene_id}/end_frame")
async def set_scene_end_frame(job_id: str, scene_id: str,
                              background: BackgroundTasks,
                              file: UploadFile = File(...)) -> dict:
    """Attach an optional END POSE to a scene. Each character is swapped into
    the pose so that scene's Kling 3.0 clip interpolates from the approved image
    (start) to the swapped end frame (end). Only Kling 3.0 honors it — other
    models ignore the end frame. Locked once movement is submitted.

    If Step-3 variants already exist, kicks a background regeneration so the
    preview end frame matches the new pose (errors surfaced on
    `end_frame_errors`, never swallowed)."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if _movement_locked(job):
        raise HTTPException(409, "Movement already submitted; end frames are locked")
    if scene_id not in _effective_scene_ids(job):
        raise HTTPException(404, f"Scene not in job: {scene_id}")
    ext = _safe_ext(file.filename or "")
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    dest = settings.output_dir / job_id / "end_frames" / f"{scene_id}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    job.end_frames_by_scene = {**(job.end_frames_by_scene or {}), scene_id: str(dest)}
    # Replacing the pose invalidates any previously-generated swap for this
    # scene — drop the stale frame + error so a fresh one is produced.
    for jc in job.characters.values():
        jc.end_frame_paths.pop(scene_id, None)
        jc.end_frame_errors.pop(scene_id, None)
    job.updated_at = datetime.utcnow()
    s.update_job(job)
    # If Step-3 variants already exist, regenerate now so the user previews the
    # end frame before animating (matches the Step-3 generation path).
    if any(jc.images for jc in job.characters.values()):
        background.add_task(_run_async, runner.regen_scene_end_frames, job_id, scene_id)
    await events.publish(job_id, {"kind": "scene.end_frame_set", "job_id": job_id,
                                  "scene_id": scene_id})
    return _job_to_dict(job)


@app.delete("/api/jobs/{job_id}/scenes/{scene_id}/end_frame")
async def clear_scene_end_frame(job_id: str, scene_id: str) -> dict:
    """Remove a scene's end pose (revert to start-frame-only animation)."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if _movement_locked(job):
        raise HTTPException(409, "Movement already submitted; end frames are locked")
    frames = dict(job.end_frames_by_scene or {})
    old = frames.pop(scene_id, None)
    if old:
        with contextlib.suppress(OSError):
            Path(old).unlink(missing_ok=True)
    job.end_frames_by_scene = frames
    # Drop the generated swaps + errors for this scene too.
    for jc in job.characters.values():
        jc.end_frame_paths.pop(scene_id, None)
        jc.end_frame_errors.pop(scene_id, None)
    job.updated_at = datetime.utcnow()
    s.update_job(job)
    await events.publish(job_id, {"kind": "scene.end_frame_cleared", "job_id": job_id,
                                  "scene_id": scene_id})
    return _job_to_dict(job)


@app.post("/api/jobs/{job_id}/scenes/{scene_id}/regen_end_frame")
async def regen_scene_end_frame(job_id: str, scene_id: str,
                                background: BackgroundTasks) -> dict:
    """Re-run the end-frame swap for ONE scene using its EXISTING end pose
    (e.g. after a content-policy block) — without re-running the variants.
    Clears the prior error so the UI shows it retrying. Requires an end pose to
    be set; locked once movement is submitted."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if _movement_locked(job):
        raise HTTPException(409, "Movement already submitted; end frames are locked")
    if not (job.end_frames_by_scene or {}).get(scene_id):
        raise HTTPException(409, "No end pose set for this scene")
    # Clear the stale error so the UI reflects the in-flight retry.
    for jc in job.characters.values():
        jc.end_frame_errors.pop(scene_id, None)
    job.updated_at = datetime.utcnow()
    s.update_job(job)
    background.add_task(_run_async, runner.regen_scene_end_frames, job_id, scene_id)
    await events.publish(job_id, {"kind": "scene.end_frame_regen", "job_id": job_id,
                                  "scene_id": scene_id})
    return _job_to_dict(job)


@app.post("/api/jobs/{job_id}/compact")
async def compact_job(job_id: str) -> dict:
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.compacted:
        return {"already_compacted": True, "bytes_freed": 0}
    in_flight = {CharStatus.QUEUED, CharStatus.GENERATING, CharStatus.ANIMATING}
    if any(jc.status in in_flight for jc in job.characters.values()):
        raise HTTPException(409, "Job has in-flight work; wait for it to finish first")

    bytes_freed = 0
    for jc in job.characters.values():
        # Keep EVERY approved variant — multi-scene jobs may have several.
        keep_variants = set(jc.approved_variant_ids or [])
        if jc.approved_variant_id:
            keep_variants.add(jc.approved_variant_id)
        remaining_images: list[GeneratedImage] = []
        for v in jc.images:
            if v.variant_id in keep_variants:
                remaining_images.append(v)
                continue
            with contextlib.suppress(OSError):
                p = Path(v.path)
                if p.exists():
                    bytes_freed += p.stat().st_size
                    p.unlink()
        jc.images = remaining_images

        remaining_videos: list[VideoVariant] = []
        for vv in jc.videos:
            if vv.status == VideoStatus.DONE and vv.final_video_path:
                remaining_videos.append(vv)
                continue
            if vv.final_video_path:
                with contextlib.suppress(OSError):
                    p = Path(vv.final_video_path)
                    if p.exists():
                        bytes_freed += p.stat().st_size
                        p.unlink()
        jc.videos = remaining_videos

    job.compacted = True
    job.updated_at = datetime.utcnow()
    s.update_job(job)
    # Invalidate disk cache so the footer reflects the change immediately.
    _disk_cache["data"] = None
    return {"ok": True, "bytes_freed": bytes_freed}


@app.post("/api/jobs/{job_id}/duplicate")
async def duplicate_job(job_id: str, background: BackgroundTasks) -> dict:
    settings.require_keys("openai")
    s = store()
    src = s.get_job(job_id)
    if src is None:
        raise HTTPException(404, "Job not found")
    # Copy ALL scenes (multi-scene aware). Falls back to single scene_id
    # for legacy jobs.
    src_scene_ids = _effective_scene_ids(src)
    scene_paths: list[Path] = []
    for sid in src_scene_ids:
        scene = s.get_scene(sid)
        if scene is None:
            raise HTTPException(409, f"Source scene {sid} no longer exists; cannot duplicate")
        path = settings.scenes_dir / scene.filename
        if not path.exists():
            raise HTTPException(500, f"Scene file missing on disk: {path}")
        scene_paths.append(path)

    # Snapshot characters from the source job. Reject if any have been deleted
    # from the library since (we need their file on disk to seed the new job).
    new_chars: dict[str, JobCharacter] = {}
    for cid, src_jc in src.characters.items():
        ch = s.get_character(cid)
        if ch is None:
            raise HTTPException(409,
                                f"Character '{src_jc.name}' was removed from library; "
                                "cannot duplicate")
        char_path = settings.characters_dir / ch.filename
        if not char_path.exists():
            raise HTTPException(500, f"Character file missing on disk: {char_path}")
        new_chars[cid] = JobCharacter(
            char_id=cid,
            name=ch.name,
            source_image_path=str(char_path),
            status=CharStatus.QUEUED,
        )
    if not new_chars:
        raise HTTPException(409, "Source job has no characters")

    new_id = "j_" + secrets.token_hex(5)
    base_title = src.title or src.job_id
    new_title = f"{base_title} (copy)"
    new_job = Job(
        job_id=new_id,
        title=new_title,
        project_id=src.project_id,
        scene_id=src_scene_ids[0],
        scene_image_path=str(scene_paths[0]),
        scene_ids=src_scene_ids,
        scene_image_paths=[str(p) for p in scene_paths],
        characters=new_chars,
        prompt=src.prompt,
        image_model=src.image_model,
        video_model=src.video_model,
        images_per_character=src.images_per_character,
        videos_per_character=src.videos_per_character,
        enrich_prompt=src.enrich_prompt,
        use_director=src.use_director,
        # Do NOT copy movement_prompts NOR director_prompts_json — the
        # duplicate is meant to re-run generation (Director re-plans against
        # the new variants), not skip straight to videos or reuse stale
        # Director output. User submits a new movement direction in Step 4.
    )
    s.add_job(new_job)
    background.add_task(_run_async, runner.run_image_generation, new_id)
    return _job_to_dict(new_job)


class RetryVideoBody(BaseModel):
    char_id: str
    video_id: str
    # Optional per-video prompt override. When None, retry uses the job's
    # per-scene movement prompt (or any existing override on this video).
    # When set (even to empty string), persists on the new VideoVariant.
    prompt_override: str | None = None


class GenerateMoreVideosBody(BaseModel):
    char_id: str
    n: int = Field(default=1, ge=1, le=10)
    source_variant_id: str | None = None
    prompt_override: str | None = None


@app.post("/api/jobs/{job_id}/generate_more_videos")
async def generate_more_videos(job_id: str, body: GenerateMoreVideosBody,
                               background: BackgroundTasks) -> dict:
    """Append N more videos to a character that already finished its initial
    batch. Strictly additive — doesn't wipe existing videos, doesn't require
    re-submitting the movement prompt. Use cases:

    - "Generate 3 more takes of the same scene because I want options."
    - "Generate 1 more take for THIS specific approved variant (source_variant_id
       passed) with a tweaked prompt_override."

    Defaults: applies to ALL approved variants of the char if source_variant_id
    is omitted, mirroring how the initial batch fans out.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    key_name = _VIDEO_MODEL_KEYS.get(job.video_model or "grok-imagine")
    if key_name:
        settings.require_keys(key_name)
    if not job.movement_prompt:
        raise HTTPException(409, "Job has no movement prompt yet — submit Step 4 first")
    jc = job.characters.get(body.char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    if not (jc.approved_variant_ids or jc.approved_variant_id):
        raise HTTPException(409, "Character has no approved variant to animate")
    if body.source_variant_id is not None:
        approved = set(jc.approved_variant_ids or [])
        if jc.approved_variant_id:
            approved.add(jc.approved_variant_id)
        if body.source_variant_id not in approved:
            raise HTTPException(404, "source_variant_id is not an approved variant")
    background.add_task(
        _run_async, runner.generate_more_videos,
        job_id, body.char_id, body.n,
        source_variant_id=body.source_variant_id,
        prompt_override=body.prompt_override,
    )
    return _job_to_dict(job)


@app.post("/api/jobs/{job_id}/retry_video")
async def retry_video(job_id: str, body: RetryVideoBody,
                      background: BackgroundTasks) -> dict:
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    # Whichever provider the job is configured for must have its key set —
    # otherwise the retry would silently 401 on the worker thread.
    key_name = _VIDEO_MODEL_KEYS.get(job.video_model or "grok-imagine")
    if key_name:
        settings.require_keys(key_name)
    if not job.movement_prompt:
        raise HTTPException(409, "Job has no movement prompt yet")
    jc = job.characters.get(body.char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    target = next((v for v in jc.videos if v.video_id == body.video_id), None)
    if target is None:
        raise HTTPException(404, "Video not found on this character")
    # Allow retry on FAILED/ERROR (classic) AND DONE (Step 5 regen flow —
    # user wants a different take on a successful clip). Block PROCESSING
    # because we'd leak a running provider job.
    if target.status == VideoStatus.PROCESSING:
        raise HTTPException(409, "Video is still processing; wait for it to finish")
    background.add_task(
        _run_async, runner.retry_one_video, job_id, body.char_id, body.video_id,
        body.prompt_override,
    )
    return _job_to_dict(job)


class RetryFailedVideosBody(BaseModel):
    """Optional filter — when set, only this character's failed clips retry."""
    char_id: str | None = None


@app.post("/api/jobs/{job_id}/retry_failed_videos")
async def retry_failed_videos(job_id: str, background: BackgroundTasks,
                              body: RetryFailedVideosBody | None = None) -> dict:
    """Re-submit EVERY failed/error video on the job in one click — the
    recovery path for restart-stranded clips (resume_pending marks them
    failed; this button re-bills them only when Hugo asks). Each clip goes
    through the same `retry_one_video` as the per-clip ↻ button."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    key_name = _VIDEO_MODEL_KEYS.get(job.video_model or "grok-imagine")
    if key_name:
        settings.require_keys(key_name)
    if not job.movement_prompt:
        raise HTTPException(409, "Job has no movement prompt yet")
    char_id = body.char_id if body else None
    failed = [
        v for cid, jc in job.characters.items()
        if char_id is None or cid == char_id
        for v in jc.videos
        if v.status in {VideoStatus.FAILED, VideoStatus.ERROR}
    ]
    if not failed:
        raise HTTPException(409, "No failed videos to retry")
    background.add_task(_run_async, runner.retry_failed_videos, job_id, char_id)
    return _job_to_dict(job)


class CompileVideosBody(BaseModel):
    """POST /api/jobs/{job_id}/compile_videos — per-character compile of all
    scene videos into ONE final MP4 per character. Settings apply uniformly
    across every character in the job (same template, same WPM target, etc.).
    `voice_override` if set wins over each character's preset voice_id; if
    null, falls back to the character's preset.
    """
    template: str = "capcut-purple-pill"   # matches the UI default; was submagic-pro
    overrides: dict | None = None
    enable_trim: bool = True
    enable_captions: bool = True
    enable_wpm_normalize: bool = True
    target_wpm: float = Field(default=190.0, ge=80, le=400)
    threshold_db: float = -30.0
    min_silence_secs: float = Field(default=0.30, ge=0.05, le=5.0)
    pad_secs: float = Field(default=0.03, ge=0.0, le=1.0)
    voice_override: str | None = None
    # When False, keep the original generated/Kling audio — skip the ElevenLabs
    # voice swap entirely, ignoring both `voice_override` and each character's
    # library preset voice. The Step-6 "Voice swap" checkbox drives this.
    enable_voice_swap: bool = True
    # Optional filter — when present, only compile these char_ids. Used by
    # the per-character retry button when ONE character's compile failed.
    char_ids: list[str] | None = None


@app.post("/api/jobs/{job_id}/compile_videos")
async def compile_job_videos(job_id: str, body: CompileVideosBody,
                              background: BackgroundTasks) -> dict:
    """Kick off the per-character compile. Returns the job dict immediately
    so the UI can show every selected character flipping to
    `compile_status="compiling"`. Real progress comes via WS events
    `char.compile_started` / `char.compile_done` / `char.compile_failed`.

    Requires `OPENAI_API_KEY` (Whisper) when `enable_captions` or
    `enable_wpm_normalize` is on; `ELEVENLABS_API_KEY` when any character's
    preset voice (or `voice_override`) is set AND voice swap actually runs."""
    settings.require_keys("openai")
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    # Sanity check: at least one char must have approved variants + done video.
    eligible = [
        cid for cid, jc in job.characters.items()
        if (jc.approved_variant_ids or jc.approved_variant_id)
        and any(v.status == VideoStatus.DONE and v.final_video_path
                for v in jc.videos)
        and (body.char_ids is None or cid in body.char_ids)
    ]
    if not eligible:
        raise HTTPException(
            409,
            "No characters with both an approved variant AND a done video — "
            "finish Step 5 before compiling.",
        )
    # Flip eligible chars to compiling state immediately (so the UI
    # spinner shows even before the runner actually starts).
    for cid in eligible:
        jc = job.characters[cid]
        jc.compile_status = "compiling"
        jc.compile_error = None
        jc.updated_at = datetime.utcnow()
        job.characters[cid] = jc
    s.update_job(job)

    from character_swap import runner_compile
    background.add_task(
        _run_async, runner_compile.compile_job_videos, job_id,
        template=body.template, overrides=body.overrides,
        enable_trim=body.enable_trim, enable_captions=body.enable_captions,
        enable_wpm_normalize=body.enable_wpm_normalize,
        target_wpm=body.target_wpm, threshold_db=body.threshold_db,
        min_silence_secs=body.min_silence_secs, pad_secs=body.pad_secs,
        voice_override=body.voice_override,
        enable_voice_swap=body.enable_voice_swap, char_ids=body.char_ids,
    )
    return _job_to_dict(job)


@app.post("/api/jobs/{job_id}/unlock_movement")
async def unlock_movement(job_id: str) -> dict:
    """Clear the movement prompt + reset videos for re-prompting. Refuses if any
    video has already completed (downloaded mp4) — protects the contract that
    completed videos came from a specific image+prompt pair."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if not job.movement_prompt:
        raise HTTPException(409, "Job is not in movement state")
    any_done = any(
        v.status == VideoStatus.DONE
        for jc in job.characters.values() for v in jc.videos
    )
    if any_done:
        raise HTTPException(409,
                            "At least one video has already completed; cannot unlock")
    job.movement_prompt = None
    job.movement_prompts = {}
    job.enriched_movement_prompt = None
    job.enriched_movement_prompts = {}
    for jc in job.characters.values():
        jc.videos = []
        # Re-arm any char that has at least one approved variant.
        has_approved = bool(jc.approved_variant_ids) or bool(jc.approved_variant_id)
        if jc.status in {CharStatus.ANIMATING, CharStatus.DONE, CharStatus.FAILED} \
                and has_approved:
            jc.status = CharStatus.APPROVED
            jc.error = None
        jc.updated_at = datetime.utcnow()
    job.updated_at = datetime.utcnow()
    s.update_job(job)
    await events.publish(job_id, {"kind": "movement.unlocked", "job_id": job_id})
    return _job_to_dict(job)


# --- websocket -----------------------------------------------------------------------

@app.websocket("/ws/jobs/{job_id}")
async def ws_job(ws: WebSocket, job_id: str) -> None:
    await ws.accept()
    queue = await events.subscribe(job_id)
    job = store().get_job(job_id)
    if job is not None:
        await ws.send_json({"kind": "snapshot", "job": _job_to_dict(job)})
    try:
        while True:
            evt = await queue.get()
            await ws.send_json(evt)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await events.unsubscribe(job_id, queue)


# --- health --------------------------------------------------------------------------

_disk_cache: dict = {"at": 0.0, "data": None}


def _disk_usage() -> dict:
    import time
    now = time.monotonic()
    if _disk_cache["data"] is not None and now - _disk_cache["at"] < 30:
        return _disk_cache["data"]

    by_job: dict[str, int] = {}
    total = 0
    out_root = settings.output_dir
    if out_root.exists():
        for entry in out_root.iterdir():
            if not entry.is_dir():
                continue
            job_bytes = 0
            for root, _dirs, files in os.walk(entry):
                for fname in files:
                    fp = Path(root) / fname
                    try:
                        job_bytes += fp.stat().st_size
                    except OSError:
                        continue
            by_job[entry.name] = job_bytes
            total += job_bytes

    s = store()
    rows = []
    for jid, bytes_ in by_job.items():
        job = s.get_job(jid)
        rows.append({
            "job_id": jid,
            "title": (job.title if job else None) or jid,
            "bytes": bytes_,
        })
    rows.sort(key=lambda r: r["bytes"], reverse=True)

    data = {"output_bytes": total, "by_job": rows}
    _disk_cache["at"] = now
    _disk_cache["data"] = data
    return data


@app.get("/api/disk")
async def disk_usage() -> dict:
    return _disk_usage()


@app.get("/api/jobs/{job_id}/cost")
async def get_job_cost(job_id: str) -> dict:
    job = store().get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return {"usd": call_log.read_costs(job_id=job_id)}


@app.get("/api/costs")
async def get_costs(days: float = 1.0) -> dict:
    if days <= 0 or days > 365:
        raise HTTPException(400, "days must be in (0, 365]")
    return {"usd": call_log.costs_since(days), "days": days}


# --- generations (Image / Video tabs) -----------------------------------------------

def _gen_to_dict(gen: MediaGeneration) -> dict:
    return {
        "gen_id": gen.gen_id,
        "kind": gen.kind.value,
        "model": gen.model,
        "prompt": gen.prompt,
        "reference_urls": [_file_url(Path(p)) for p in gen.reference_paths],
        "aspect_ratio": gen.aspect_ratio,
        "duration_secs": gen.duration_secs,
        "avatar_id": gen.avatar_id,
        "voice_id": gen.voice_id,
        "voice_provider": gen.voice_provider,
        "enrich_prompt": bool(gen.enrich_prompt),
        "enriched_prompt": gen.enriched_prompt,
        "use_director": bool(gen.use_director),
        "director_prompt": gen.director_prompt,
        "status": gen.status.value,
        "output_url": _file_url(Path(gen.output_path)) if gen.output_path else None,
        "provider_job_id": gen.provider_job_id,
        "cost_usd": gen.cost_usd,
        "error": gen.error,
        "created_at": gen.created_at.isoformat() + "Z",
        "completed_at": (gen.completed_at.isoformat() + "Z") if gen.completed_at else None,
    }


def _models_payload() -> dict:
    """Tell the frontend which models exist + whether their keys are configured."""
    def _entry(slug: str, info: dict) -> dict:
        provider = info["provider"]
        row = {
            "slug": slug,
            "label": info["label"],
            "provider": provider,
            "available": settings.has_provider(provider),
        }
        # Surface per-video-model duration specs so the Step-4 picker can
        # render the dropdown gated to each provider's accepted values.
        # Image/audio/avatar models don't carry this — leave the key out
        # entirely rather than send null so the frontend can `'duration_options' in m`.
        if "duration_options" in info:
            row["duration_options"] = list(info["duration_options"])
            row["duration_default"] = info.get("duration_default",
                                               info["duration_options"][0])
        return row
    return {
        "image":  [_entry(s, i) for s, i in runner_media.IMAGE_MODELS.items()],
        "video":  [_entry(s, i) for s, i in runner_media.VIDEO_MODELS.items()],
        "avatar": [_entry(s, i) for s, i in runner_media.AVATAR_MODELS.items()],
        "audio":  [_entry(s, i) for s, i in runner_media.AUDIO_MODELS.items()],
    }


@app.get("/api/generations/models")
async def get_gen_models() -> dict:
    return _models_payload()


@app.get("/api/swap/defaults")
async def get_swap_defaults(project_id: str | None = None) -> dict:
    """Defaults for the Swap-tab Step-2 form (prompt + model).

    If `project_id` is given AND that project has a custom `default_prompt`,
    we return that instead of the global default. The frontend uses this
    to drive both the textarea's initial value and the "↺ reset to default"
    button. `global_prompt` is always returned alongside so the frontend
    can offer a "reset to global default" too.
    """
    from character_swap import pipeline
    global_prompt = pipeline.GENERATION_PROMPT
    project_prompt: str | None = None
    if project_id:
        s = store()
        project = s.get_project(project_id)
        if project and project.default_prompt:
            project_prompt = project.default_prompt
    return {
        "prompt": project_prompt or global_prompt,
        "global_prompt": global_prompt,
        "project_prompt": project_prompt,
        "image_model": "gpt-image",
        "image_models": _models_payload()["image"],
    }


@app.post("/api/generations")
async def create_generation(
    background: BackgroundTasks,
    kind: str = Form(...),
    model: str = Form(...),
    prompt: str = Form(...),
    aspect_ratio: str | None = Form(None),
    duration_secs: int | None = Form(None),
    avatar_id: str | None = Form(None),
    voice_id: str | None = Form(None),
    voice_provider: str | None = Form(None),
    enrich_prompt: bool = Form(False),
    use_director: bool = Form(False),
    files: list[UploadFile] = File(default=[]),
) -> dict:
    try:
        kind_enum = GenKind(kind)
    except ValueError:
        raise HTTPException(400, f"Invalid kind '{kind}' (use image, video, avatar, or audio)")
    if not prompt.strip():
        raise HTTPException(400, "Empty prompt")

    info = runner_media.model_info(model)
    if info is None:
        raise HTTPException(400, f"Unknown model '{model}'")
    if kind_enum is GenKind.IMAGE and model not in runner_media.IMAGE_MODELS:
        raise HTTPException(400, f"Model '{model}' is not an image model")
    if kind_enum is GenKind.VIDEO and model not in runner_media.VIDEO_MODELS:
        raise HTTPException(400, f"Model '{model}' is not a video model")
    if kind_enum is GenKind.AVATAR and model not in runner_media.AVATAR_MODELS:
        raise HTTPException(400, f"Model '{model}' is not an avatar model")
    if kind_enum is GenKind.AUDIO and model not in runner_media.AUDIO_MODELS:
        raise HTTPException(400, f"Model '{model}' is not an audio model")
    if not settings.has_provider(info["provider"]):
        raise HTTPException(
            503,
            f"{info['label']} is not configured. Add the right API key to .env.",
        )
    if kind_enum is GenKind.VIDEO and not files:
        raise HTTPException(400, "Video generation requires a reference image")
    if kind_enum is GenKind.AVATAR:
        if not voice_id:
            raise HTTPException(400, "Avatar generation requires voice_id")
        if model == "heygen-avatar-5" and not avatar_id:
            raise HTTPException(400, "heygen-avatar-5 requires avatar_id")
        if model == "heygen-photo-avatar" and not files:
            raise HTTPException(400, "heygen-photo-avatar requires a reference image")
        # Validate voice_provider when explicitly set; default behaviour is HeyGen.
        if voice_provider and voice_provider not in {"heygen", "elevenlabs"}:
            raise HTTPException(400, f"Invalid voice_provider '{voice_provider}'")
        if voice_provider == "elevenlabs" and not settings.has_provider("elevenlabs"):
            raise HTTPException(503, "ElevenLabs not configured. Add ELEVENLABS_API_KEY to .env.")
    if kind_enum is GenKind.AUDIO:
        if not voice_id:
            raise HTTPException(400, "Audio generation requires voice_id")
        if model == "elevenlabs-vc" and not files:
            raise HTTPException(400, "Voice Changer requires a source audio file")

    # Allow audio (and video, for VC) uploads for the Voice Changer flow;
    # everything else stays image-only.
    allow_audio_upload = (kind_enum is GenKind.AUDIO and model == "elevenlabs-vc")
    allow_video_upload = (kind_enum is GenKind.AUDIO and model == "elevenlabs-vc")

    gen_id = "g_" + secrets.token_hex(5)
    gen_dir = settings.output_dir / "generations" / gen_id
    gen_dir.mkdir(parents=True, exist_ok=True)
    ref_paths: list[str] = []
    for i, f in enumerate(files):
        if not f.filename:
            continue
        ext = _safe_ext(f.filename, allow_audio=allow_audio_upload, allow_video=allow_video_upload)
        data = await _read_capped(f)
        if not data:
            continue
        dest = gen_dir / f"ref_{i}{ext}"
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
        ref_paths.append(str(dest))

    gen = MediaGeneration(
        gen_id=gen_id,
        kind=kind_enum,
        model=model,
        prompt=prompt.strip(),
        reference_paths=ref_paths,
        aspect_ratio=aspect_ratio,
        duration_secs=duration_secs,
        avatar_id=avatar_id,
        voice_id=voice_id,
        voice_provider=voice_provider or ("heygen" if kind_enum is GenKind.AVATAR else None),
        # Only enrich image + video — avatar/audio prompts are literal scripts,
        # NOT creative descriptions, so enrichment would corrupt them.
        enrich_prompt=enrich_prompt and kind_enum in (GenKind.IMAGE, GenKind.VIDEO),
        # AI Director also limited to image + video (and requires a ref image
        # since Director relies on vision input). Runner_media handles the
        # no-ref case by silently skipping the Director call.
        use_director=use_director and kind_enum in (GenKind.IMAGE, GenKind.VIDEO),
    )
    store().add_generation(gen)

    if kind_enum is GenKind.IMAGE:
        runner_fn = runner_media.run_image_gen
    elif kind_enum is GenKind.VIDEO:
        runner_fn = runner_media.run_video_gen
    elif kind_enum is GenKind.AVATAR:
        runner_fn = runner_media.run_avatar_gen
    else:  # AUDIO
        runner_fn = runner_media.run_audio_gen
    background.add_task(_run_async, runner_fn, gen_id)
    return _gen_to_dict(gen)


@app.get("/api/heygen/avatars")
async def heygen_avatars() -> list[dict]:
    from character_swap.clients import heygen as _heygen
    try:
        return _heygen.list_avatars()
    except ProviderNotConfigured as e:
        raise HTTPException(503, str(e))
    except NotImplementedError as e:
        raise HTTPException(501, str(e))


@app.get("/api/heygen/voices")
async def heygen_voices() -> list[dict]:
    from character_swap.clients import heygen as _heygen
    try:
        return _heygen.list_voices()
    except ProviderNotConfigured as e:
        raise HTTPException(503, str(e))
    except NotImplementedError as e:
        raise HTTPException(501, str(e))


@app.get("/api/elevenlabs/voices")
async def elevenlabs_voices() -> list[dict]:
    from character_swap.clients import elevenlabs as _eleven
    try:
        return _eleven.list_voices()
    except ProviderNotConfigured as e:
        raise HTTPException(503, str(e))
    except NotImplementedError as e:
        raise HTTPException(501, str(e))


@app.get("/api/generations")
async def list_generations(kind: str | None = None) -> list[dict]:
    gens = store().list_generations()
    if kind:
        try:
            kind_enum = GenKind(kind)
        except ValueError:
            raise HTTPException(400, f"Invalid kind '{kind}'")
        gens = [g for g in gens if g.kind == kind_enum]
    gens.sort(key=lambda g: g.created_at, reverse=True)
    return [_gen_to_dict(g) for g in gens]


@app.get("/api/generations/{gen_id}")
async def get_generation(gen_id: str) -> dict:
    gen = store().get_generation(gen_id)
    if gen is None:
        raise HTTPException(404, "Generation not found")
    return _gen_to_dict(gen)


@app.delete("/api/generations/{gen_id}")
async def delete_generation(gen_id: str) -> dict:
    s = store()
    gen = s.get_generation(gen_id)
    if gen is None:
        raise HTTPException(404, "Generation not found")
    if gen.status in {GenStatus.PENDING, GenStatus.RUNNING}:
        raise HTTPException(409, "Generation is in flight; wait for it to finish")
    s.delete_generation(gen_id)
    gen_dir = settings.output_dir / "generations" / gen_id
    if gen_dir.exists():
        shutil.rmtree(gen_dir, ignore_errors=True)
    return {"ok": True}


@app.post("/api/generations/{gen_id}/retry")
async def retry_generation(gen_id: str, background: BackgroundTasks) -> dict:
    s = store()
    gen = s.get_generation(gen_id)
    if gen is None:
        raise HTTPException(404, "Generation not found")
    if gen.status not in {GenStatus.FAILED}:
        raise HTTPException(409, f"Generation is '{gen.status}', only failed can retry")
    gen.status = GenStatus.PENDING
    gen.error = None
    gen.completed_at = None
    s.update_generation(gen)
    runner_fn = runner_media.run_image_gen if gen.kind is GenKind.IMAGE \
        else runner_media.run_video_gen
    background.add_task(_run_async, runner_fn, gen_id)
    return _gen_to_dict(gen)


# --- Video Editor: silence-trim + auto-captions -------------------------------------

ALLOWED_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


def _safe_video_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported video type '{ext}'. Allowed: {sorted(ALLOWED_VIDEO_EXTS)}")
    return ext


def _remotion_available() -> bool:
    """Cheap cache check: does the Remotion project look installed?

    True when (a) `node` is on PATH and (b) `remotion/node_modules/` exists.
    Frontend skips Remotion templates from the picker when this is false.
    The cache is module-level so we don't shell out on every templates call.
    """
    cached = getattr(_remotion_available, "_v", None)
    if cached is not None:
        return cached
    import shutil as _shutil
    have_node = _shutil.which("node") is not None
    nm = settings.project_root / "remotion" / "node_modules"
    have_nm = nm.is_dir()
    _remotion_available._v = bool(have_node and have_nm)  # type: ignore[attr-defined]
    return _remotion_available._v  # type: ignore[attr-defined]


@app.get("/api/editor/templates")
async def editor_templates() -> list[dict]:
    from character_swap import video_edit
    remotion_ok = _remotion_available()
    fal_ok = settings.has_provider("fal")
    out = []
    for slug, style in video_edit.TEMPLATES.items():
        if style.engine == "remotion" and not remotion_ok:
            continue
        # VEED templates stay in the picker even when no key is configured —
        # we surface them as `locked` so the UI can show a 🔒 chip with the
        # signup instruction tooltip rather than silently hiding them.
        locked_reason: str | None = None
        if style.engine == "veed" and not fal_ok:
            locked_reason = "Requires FAL_API_KEY — sign up at fal.ai/dashboard/keys"
        row = {
            "slug": slug,
            "label": slug.title(),
            "font": style.font,
            "size": style.size,
            "primary_color": style.primary_color,
            "outline_color": style.outline_color,
            "back_color": style.back_color,
            "bold": style.bold,
            "outline": style.outline,
            "box": style.box,
            "words_per_card": style.words_per_card,
            "highlight_color": style.highlight_color,
            "margin_v": style.margin_v,
            "margin_h": style.margin_h,
            "all_caps": style.all_caps,
            "shadow": style.shadow,
            "alignment": style.alignment,
            "engine": style.engine,
            "composition_id": style.composition_id,
            "locked": locked_reason is not None,
            "locked_reason": locked_reason,
        }
        if style.engine == "remotion":
            # Pack the exact props the Remotion Player will consume so the
            # frontend can mirror the rendered output in its preview.
            row["remotion_props"] = style.to_remotion_props()
        out.append(row)
    return out


@app.post("/api/editor/trim_silences")
async def editor_trim_silences(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    threshold_db: float = Form(-25.0),
    min_silence_secs: float = Form(0.30),
    pad_secs: float = Form(0.07),
) -> dict:
    """Synchronous silence-trim. Saves the trimmed video under
    `output/editor/<edit_id>/trimmed.mp4` and returns a MediaGeneration-shaped
    dict so the frontend can drop it into the same history grid as videos."""
    from character_swap import video_edit
    ext = _safe_video_ext(file.filename or "video.mp4")
    edit_id = "ed_" + secrets.token_hex(5)
    edit_dir = settings.output_dir / "editor" / edit_id
    edit_dir.mkdir(parents=True, exist_ok=True)
    src = edit_dir / f"source{ext}"
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    src.write_bytes(data)
    out_path = edit_dir / "trimmed.mp4"
    try:
        summary = await asyncio.to_thread(
            video_edit.trim_silences,
            src, out_path,
            threshold_db=threshold_db,
            min_silence_secs=min_silence_secs,
            pad_secs=pad_secs,
            job_id=edit_id,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {
        "edit_id": edit_id,
        "output_url": _file_url(out_path),
        "source_url": _file_url(src),
        **summary,
    }


@app.post("/api/editor/captions")
async def editor_captions(
    file: UploadFile = File(...),
    template: str = Form("tiktok"),
    overrides: str | None = Form(None),
) -> dict:
    """Transcribe with Whisper, then burn captions into the video using the
    chosen template (plus optional JSON overrides for font/size/color)."""
    from character_swap import video_edit
    settings.require_keys("openai")
    ext = _safe_video_ext(file.filename or "video.mp4")
    edit_id = "ed_" + secrets.token_hex(5)
    edit_dir = settings.output_dir / "editor" / edit_id
    edit_dir.mkdir(parents=True, exist_ok=True)
    src = edit_dir / f"source{ext}"
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    src.write_bytes(data)
    out_path = edit_dir / "captioned.mp4"

    overrides_dict: dict | None = None
    if overrides:
        try:
            overrides_dict = json.loads(overrides)
        except json.JSONDecodeError:
            raise HTTPException(400, "overrides must be valid JSON")

    style = video_edit.style_from_params(template, overrides_dict)
    try:
        words = await asyncio.to_thread(video_edit.transcribe_words, src, job_id=edit_id)
        if not words:
            raise HTTPException(422, "No speech detected — nothing to caption")
        summary = await asyncio.to_thread(
            video_edit.render_captions,
            src, out_path,
            words=words, style=style, job_id=edit_id,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))
    return {
        "edit_id": edit_id,
        "output_url": _file_url(out_path),
        "source_url": _file_url(src),
        "template": template,
        "n_words": len(words),
        **summary,
    }


@app.post("/api/editor/auto_edit")
async def editor_auto_edit(
    file: UploadFile = File(...),
    threshold_db: float = Form(-25.0),
    min_silence_secs: float = Form(0.30),
    pad_secs: float = Form(0.07),
    voice_id: str | None = Form(None),     # ElevenLabs voice_id for voice swap (optional)
    template: str = Form("capcut-purple-pill"),
    overrides: str | None = Form(None),
    enable_trim: bool = Form(True),        # opt-out of auto silence-trim
    enable_captions: bool = Form(True),    # opt-out of caption burn-in
    enable_wpm_normalize: bool = Form(True),  # time-stretch to hit target_wpm
    target_wpm: float = Form(190.0),
) -> dict:
    """One-shot pipeline. Each step is opt-out:
      - trim silences (enable_trim)
      - voice swap (only if voice_id is set)
      - captions (enable_captions)
    Returns the final mp4 + per-step summaries."""
    from character_swap import video_edit
    from character_swap.clients import elevenlabs as _eleven
    settings.require_keys("openai")        # Whisper

    ext = _safe_video_ext(file.filename or "video.mp4")
    edit_id = "ed_" + secrets.token_hex(5)
    edit_dir = settings.output_dir / "editor" / edit_id
    edit_dir.mkdir(parents=True, exist_ok=True)
    src = edit_dir / f"source{ext}"
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    src.write_bytes(data)

    # Whisper is only required if captions are enabled.
    if enable_captions:
        settings.require_keys("openai")

    # Step 0: ALWAYS cut to audio onset — every clip entering the Editor
    # starts exactly when there's enough sound (silencedetect energy vs
    # threshold_db), regardless of the enable_trim toggle, which governs
    # interior pauses only. Failure → keep the original, never block.
    current = src
    try:
        no_lead = edit_dir / "00-noLead.mp4"
        await asyncio.to_thread(
            video_edit.trim_leading_silence, src, no_lead,
            threshold_db=threshold_db,
            min_silence_secs=0.05,  # very aggressive — exact start
            job_id=edit_id,
        )
        current = no_lead
    except (RuntimeError, ValueError):
        pass

    # Step 1: trim silences (optional — interior pauses)
    trim_summary: dict | None = None
    if enable_trim:
        trimmed = edit_dir / "01-trimmed.mp4"
        try:
            trim_summary = await asyncio.to_thread(
                video_edit.trim_silences, current, trimmed,
                threshold_db=threshold_db, min_silence_secs=min_silence_secs,
                pad_secs=pad_secs, job_id=edit_id,
            )
        except RuntimeError as e:
            raise HTTPException(500, f"Trim failed: {e}")
        current = trimmed

    swap_summary: dict | None = None

    # Step 2: optional ElevenLabs voice swap
    if voice_id:
        if not settings.has_provider("elevenlabs"):
            raise HTTPException(503, "ELEVENLABS_API_KEY not set — cannot swap voice")
        try:
            tmp_audio_in = edit_dir / "02-original.wav"
            await asyncio.to_thread(
                video_edit._run,
                [video_edit._ffmpeg(), "-y", "-i", str(current),
                 "-vn", "-ac", "1", "-ar", "44100", str(tmp_audio_in)],
            )
            new_audio_bytes = await asyncio.to_thread(
                _eleven.voice_changer,
                voice_id=voice_id, source_audio=tmp_audio_in, app_job_id=edit_id,
            )
            new_audio = edit_dir / "02-swapped.mp3"
            new_audio.write_bytes(new_audio_bytes)
            swapped = edit_dir / "02-swapped.mp4"
            await asyncio.to_thread(
                video_edit.replace_audio, current, new_audio, swapped,
            )
            current = swapped
            swap_summary = {"voice_id": voice_id, "audio_path": str(new_audio)}
            tmp_audio_in.unlink(missing_ok=True)
        except NotImplementedError as e:
            raise HTTPException(501, f"ElevenLabs wiring pending: {e}")
        except Exception as e:
            raise HTTPException(500, f"Voice swap failed: {type(e).__name__}: {e}")

    # Step 3a: transcribe (required if captions OR wpm-normalize is on)
    words: list = []
    if enable_captions or enable_wpm_normalize:
        settings.require_keys("openai")
        words = await asyncio.to_thread(video_edit.transcribe_words, current, job_id=edit_id)
        if enable_captions and not words:
            raise HTTPException(422, "No speech detected — nothing to caption")

    # (The old Step 3a.5 Whisper-first-word recut was removed 2026-06-11:
    # Hugo chose AUDIO energy as the start marker — the unconditional
    # audio-onset trim at Step 0 is the contract now, and sub-threshold
    # ambient before speech is intentional content.)

    # Step 3b: WPM normalization (time-stretch so spoken pace ≈ target_wpm)
    wpm_info: dict | None = None
    if enable_wpm_normalize and words:
        original_wpm = video_edit.compute_wpm(words)
        speed = video_edit.compute_speed_factor(words, target_wpm=target_wpm)
        if abs(speed - 1.0) > 1e-3:
            stretched = edit_dir / "03-stretched.mp4"
            try:
                await asyncio.to_thread(
                    video_edit.time_stretch, current, stretched,
                    speed_factor=speed, job_id=edit_id,
                )
            except (RuntimeError, ValueError) as e:
                raise HTTPException(500, f"Time-stretch failed: {e}")
            words = video_edit.scale_word_timestamps(words, speed)
            current = stretched
        wpm_info = {
            "original_wpm": round(original_wpm, 1),
            "target_wpm": target_wpm,
            "speed_factor": round(speed, 3),
            "stretched": abs(speed - 1.0) > 1e-3,
        }

    # Step 3c: captions (optional)
    cap_info: dict | None = None
    if enable_captions:
        overrides_dict: dict | None = None
        if overrides:
            try:
                overrides_dict = json.loads(overrides)
            except json.JSONDecodeError:
                raise HTTPException(400, "overrides must be valid JSON")
        style = video_edit.style_from_params(template, overrides_dict)

        try:
            # `words` already populated (and possibly time-scaled) above.
            (edit_dir / "words.json").write_text(video_edit.words_to_json(words), encoding="utf-8")
            (edit_dir / "pre_caption.txt").write_text(str(current), encoding="utf-8")
            final_out = edit_dir / "04-final.mp4"
            cap_summary = await asyncio.to_thread(
                video_edit.render_captions, current, final_out,
                words=words, style=style, job_id=edit_id,
            )
            cap_info = {"n_words": len(words), "template": template, **cap_summary}
            current = final_out
        except RuntimeError as e:
            raise HTTPException(500, f"Caption rendering failed: {e}")

    return {
        "edit_id": edit_id,
        "output_url": _file_url(current),
        "source_url": _file_url(src),
        "trim": trim_summary,
        "voice_swap": swap_summary,
        "wpm_normalize": wpm_info,
        "captions": cap_info,
        "rerender_available": bool(enable_captions),
    }


@app.post("/api/editor/multi_auto_edit")
async def editor_multi_auto_edit(
    files: list[UploadFile] = File(...),
    script: str = Form(...),
    threshold_db: float = Form(-25.0),
    min_silence_secs: float = Form(0.30),
    pad_secs: float = Form(0.07),
    voice_id: str | None = Form(None),
    template: str = Form("capcut-purple-pill"),
    overrides: str | None = Form(None),
    enable_trim: bool = Form(True),
    enable_captions: bool = Form(True),
    enable_wpm_normalize: bool = Form(True),
    target_wpm: float = Form(190.0),
    # Global playback-speed multiplier applied to the FINAL stitched video
    # (pitch-preserving). 1.0 = no change, 1.5 = 50% faster, etc. Distinct
    # from WPM normalize (which equalizes per-clip pace) — this is a
    # deliberate overall speed-up of the whole reel. Clamped to [0.5, 2.0].
    playback_speed: float = Form(1.0),
) -> dict:
    """Multi-clip auto-edit:
      1. Save each uploaded clip.
      2. Transcribe each with Whisper.
      3. Match each transcript to a position in the supplied script and
         reorder accordingly.
      4. (Optional) Time-stretch each ordered clip so its spoken pace
         hits target_wpm independently — uniform pacing across takes.
      5. Concat the clips in script order.
      6. Continue with the standard auto-edit pipeline:
         trim silences → (optional) voice swap → captions.
    """
    from character_swap import video_edit
    from character_swap.clients import elevenlabs as _eleven
    settings.require_keys("openai")
    if not script.strip():
        raise HTTPException(400, "Script text is empty")
    if not files or len(files) < 1:
        raise HTTPException(400, "Upload at least one clip")

    edit_id = "ed_" + secrets.token_hex(5)
    edit_dir = settings.output_dir / "editor" / edit_id
    edit_dir.mkdir(parents=True, exist_ok=True)
    (edit_dir / "script.txt").write_text(script, encoding="utf-8")

    # 1. Save each upload to disk.
    clip_paths: list[Path] = []
    for i, f in enumerate(files):
        if not f.filename:
            continue
        ext = _safe_video_ext(f.filename)
        dest = edit_dir / f"clip-{i:02d}{ext}"
        data = await _read_capped(f)
        if not data:
            continue
        dest.write_bytes(data)
        clip_paths.append(dest)
    if not clip_paths:
        raise HTTPException(400, "No valid clips uploaded")

    # 1.5. ALWAYS cut every clip to audio onset at ENTRY — before
    # transcription, so word timestamps, fuzzy matching, and WPM scaling all
    # operate on the trimmed timeline (no shifting needed downstream).
    # Independent of enable_trim (which governs interior pauses only).
    # Failure on a clip → keep that clip untrimmed, never block.
    async def _entry_trim(i: int, p: Path) -> Path:
        cut = edit_dir / f"clip-{i:02d}-noLead.mp4"
        try:
            await asyncio.to_thread(
                video_edit.trim_leading_silence, p, cut,
                threshold_db=threshold_db,
                min_silence_secs=0.05,  # very aggressive — exact start
                job_id=edit_id,
            )
            return cut
        except (RuntimeError, ValueError):
            return p

    clip_paths = list(await asyncio.gather(*[
        _entry_trim(i, p) for i, p in enumerate(clip_paths)
    ]))

    # 2. Transcribe each clip in parallel via to_thread (Whisper is sync).
    try:
        transcripts_per_clip = await asyncio.gather(*[
            asyncio.to_thread(video_edit.transcribe_words, p, job_id=edit_id)
            for p in clip_paths
        ])
    except RuntimeError as e:
        raise HTTPException(500, f"Transcription failed: {e}")

    plain_transcripts = [" ".join(w.text for w in words) for words in transcripts_per_clip]

    # 3. Match each clip to a position in the script + sort.
    placements = video_edit.match_clips_by_transcript(plain_transcripts, script)
    ordered_paths = [clip_paths[p["idx"]] for p in placements]
    ordered_transcripts = [transcripts_per_clip[p["idx"]] for p in placements]
    matching_summary = [{
        "clip_index": p["idx"],
        "score": p["score"],
        "unmatched": p["unmatched"],
        "transcript_preview": plain_transcripts[p["idx"]][:120],
    } for p in placements]

    # 4. (Optional) Per-clip WPM normalization. Each clip is time-stretched
    # independently so every take in the final concat plays at the same
    # spoken pace. Audio pitch is preserved (ffmpeg atempo).
    wpm_decisions: list[dict] | None = None
    if enable_wpm_normalize:
        stretched_paths: list[Path] = []
        wpm_decisions = []
        for i, (p, words) in enumerate(zip(ordered_paths, ordered_transcripts)):
            original_wpm = video_edit.compute_wpm(words)
            speed = video_edit.compute_speed_factor(words, target_wpm=target_wpm)
            stretched = edit_dir / f"clip-{i:02d}-stretched.mp4"
            if abs(speed - 1.0) < 1e-3:
                # Within dead zone — passthrough, but still re-encode so the
                # concat sees consistent codecs across all inputs.
                try:
                    await asyncio.to_thread(
                        video_edit.time_stretch, p, stretched,
                        speed_factor=1.0, job_id=edit_id,
                    )
                except (RuntimeError, ValueError):
                    # If passthrough re-encode fails for any reason, fall
                    # back to the original input — concat will handle it.
                    stretched = p
            else:
                try:
                    await asyncio.to_thread(
                        video_edit.time_stretch, p, stretched,
                        speed_factor=speed, job_id=edit_id,
                    )
                except (RuntimeError, ValueError):
                    # Stretch failed (unusual) — fall back to original.
                    stretched = p
                    speed = 1.0
            stretched_paths.append(stretched)
            wpm_decisions.append({
                "ordered_idx": i,
                "source_clip_idx": placements[i]["idx"],
                "original_wpm": round(original_wpm, 1),
                "speed_factor": round(speed, 3),
                "stretched": abs(speed - 1.0) > 1e-3,
            })
        ordered_paths = stretched_paths
        (edit_dir / "wpm_decisions.json").write_text(
            json.dumps({"target_wpm": target_wpm, "clips": wpm_decisions}, indent=2),
            encoding="utf-8",
        )

    # (The old step 4.5 per-clip leading trim was moved to step 1.5 — every
    # clip is now cut to AUDIO onset at entry, before transcription, so no
    # word-timestamp scaling/shifting is needed here. Word-based
    # trim_to_first_word was retired 2026-06-11: audio energy is the marker.)

    # 5. Concat in script order.
    concat_out = edit_dir / "01-concat.mp4"
    try:
        await asyncio.to_thread(video_edit.concat_videos, ordered_paths, concat_out)
    except RuntimeError as e:
        raise HTTPException(500, f"Concat failed: {e}")

    # 5. Trim silences on the concatenated video (optional)
    current = concat_out
    trim_summary: dict | None = None
    if enable_trim:
        trimmed = edit_dir / "02-trimmed.mp4"
        try:
            trim_summary = await asyncio.to_thread(
                video_edit.trim_silences, concat_out, trimmed,
                threshold_db=threshold_db, min_silence_secs=min_silence_secs,
                pad_secs=pad_secs, job_id=edit_id,
            )
        except RuntimeError as e:
            raise HTTPException(500, f"Trim failed: {e}")
        current = trimmed

    swap_summary: dict | None = None

    # 6. Optional voice swap.
    if voice_id:
        if not settings.has_provider("elevenlabs"):
            raise HTTPException(503, "ELEVENLABS_API_KEY not set — cannot swap voice")
        try:
            tmp_audio_in = edit_dir / "03-original.wav"
            await asyncio.to_thread(
                video_edit._run,
                [video_edit._ffmpeg(), "-y", "-i", str(current),
                 "-vn", "-ac", "1", "-ar", "44100", str(tmp_audio_in)],
            )
            new_audio_bytes = await asyncio.to_thread(
                _eleven.voice_changer,
                voice_id=voice_id, source_audio=tmp_audio_in, app_job_id=edit_id,
            )
            new_audio = edit_dir / "03-swapped.mp3"
            new_audio.write_bytes(new_audio_bytes)
            swapped = edit_dir / "03-swapped.mp4"
            await asyncio.to_thread(
                video_edit.replace_audio, current, new_audio, swapped,
            )
            current = swapped
            swap_summary = {"voice_id": voice_id}
            tmp_audio_in.unlink(missing_ok=True)
        except NotImplementedError as e:
            raise HTTPException(501, f"ElevenLabs wiring pending: {e}")
        except Exception as e:
            raise HTTPException(500, f"Voice swap failed: {type(e).__name__}: {e}")

    # 6.5. Global speed-up (pitch-preserving). Applied to the stitched result
    # BEFORE captions so the caption transcription below runs on the sped-up
    # audio and stays perfectly in sync — and the cached pre-caption video
    # (used by rerender) is already at the target speed.
    speed_info: dict | None = None
    speed = max(0.5, min(2.0, float(playback_speed or 1.0)))
    if abs(speed - 1.0) > 1e-3:
        sped = edit_dir / "035-speed.mp4"
        try:
            await asyncio.to_thread(
                video_edit.time_stretch, current, sped,
                speed_factor=speed, job_id=edit_id,
            )
            current = sped
            speed_info = {"playback_speed": round(speed, 3)}
        except (RuntimeError, ValueError) as e:
            raise HTTPException(500, f"Speed-up failed: {e}")

    # 7. Captions (optional)
    cap_info: dict | None = None
    if enable_captions:
        overrides_dict: dict | None = None
        if overrides:
            try:
                overrides_dict = json.loads(overrides)
            except json.JSONDecodeError:
                raise HTTPException(400, "overrides must be valid JSON")
        style = video_edit.style_from_params(template, overrides_dict)

        try:
            words = await asyncio.to_thread(video_edit.transcribe_words, current, job_id=edit_id)
            if not words:
                raise HTTPException(422, "No speech detected after processing")
            (edit_dir / "words.json").write_text(video_edit.words_to_json(words), encoding="utf-8")
            (edit_dir / "pre_caption.txt").write_text(str(current), encoding="utf-8")
            final_out = edit_dir / "04-final.mp4"
            cap_summary = await asyncio.to_thread(
                video_edit.render_captions, current, final_out,
                words=words, style=style, job_id=edit_id,
            )
            cap_info = {"n_words": len(words), "template": template, **cap_summary}
            current = final_out
        except RuntimeError as e:
            raise HTTPException(500, f"Caption rendering failed: {e}")

    return {
        "edit_id": edit_id,
        "output_url": _file_url(current),
        "n_clips": len(ordered_paths),
        "matching": matching_summary,
        "wpm_normalize": ({"target_wpm": target_wpm, "clips": wpm_decisions}
                          if wpm_decisions else None),
        "trim": trim_summary,
        "voice_swap": swap_summary,
        "speed": speed_info,
        "captions": cap_info,
        "rerender_available": bool(enable_captions),
    }


@app.post("/api/editor/rerender")
async def editor_rerender(
    edit_id: str = Form(...),
    template: str = Form("capcut-purple-pill"),
    overrides: str | None = Form(None),
    trim_start_secs: float = Form(0.0),
    trim_end_secs: float = Form(0.0),   # 0 = until end
    # Optional per-word edits from the CapCut-style caption editor. JSON list
    # of `{text, start, end}` that REPLACES the cached `words.json` for this
    # render only. We also persist the edits back to words.json so subsequent
    # rerenders pick them up automatically.
    words_json: str | None = Form(None),
) -> dict:
    """Re-render captions (and optionally manually-trim) the pre-caption
    video produced by a previous /api/editor/auto_edit run. Reuses the cached
    transcript so no Whisper call is needed.

    When `words_json` is provided, it overrides the cached transcript with
    user-edited text/timings (from the visual caption editor) and is
    persisted back to words.json so future rerenders use the edits."""
    from character_swap import video_edit
    edit_dir = settings.output_dir / "editor" / edit_id
    words_path = edit_dir / "words.json"
    pre_caption_path_file = edit_dir / "pre_caption.txt"
    if not words_path.exists() or not pre_caption_path_file.exists():
        raise HTTPException(404, "Cached transcript missing — original auto-edit no longer re-renderable")

    pre_caption = Path(pre_caption_path_file.read_text(encoding="utf-8").strip())
    if not pre_caption.exists():
        raise HTTPException(404, "Cached pre-caption video missing on disk")

    # User-edited transcript takes precedence and gets persisted so future
    # rerenders inherit the edits.
    if words_json:
        try:
            words = video_edit.words_from_json(words_json)
        except (ValueError, TypeError, json.JSONDecodeError):
            raise HTTPException(400, "words_json must be a JSON list of {text, start, end}")
        if words:
            # Persist atomically. Keep a `.original.json` backup the first
            # time so users can recover if they regret edits.
            backup = edit_dir / "words.original.json"
            if not backup.exists():
                try:
                    backup.write_text(words_path.read_text(encoding="utf-8"),
                                      encoding="utf-8")
                except OSError:
                    pass
            tmp = words_path.with_suffix(".json.tmp")
            tmp.write_text(words_json, encoding="utf-8")
            tmp.replace(words_path)
    else:
        words = video_edit.words_from_json(words_path.read_text(encoding="utf-8"))
    if not words:
        raise HTTPException(422, "Cached transcript is empty")

    overrides_dict: dict | None = None
    if overrides:
        try:
            overrides_dict = json.loads(overrides)
        except json.JSONDecodeError:
            raise HTTPException(400, "overrides must be valid JSON")
    style = video_edit.style_from_params(template, overrides_dict)

    # Build a unique output filename so old re-renders don't get overwritten
    # (lets the user A/B compare).
    existing = sorted(edit_dir.glob("rerender-*.mp4"))
    version = len(existing) + 1
    rerender_out = edit_dir / f"rerender-{version:02d}.mp4"

    try:
        # If a trim range is given, cut the pre-caption video first AND shift
        # the cached word timestamps so captions still line up.
        if trim_start_secs > 0 or (trim_end_secs and trim_end_secs > 0):
            trimmed = edit_dir / f"rerender-{version:02d}-trimmed.mp4"
            await asyncio.to_thread(
                video_edit.trim_range, pre_caption, trimmed,
                start_secs=trim_start_secs, end_secs=trim_end_secs,
            )
            adj_words = video_edit.filter_and_shift_words(
                words, start=trim_start_secs, end=trim_end_secs,
            )
            await asyncio.to_thread(
                video_edit.render_captions, trimmed, rerender_out,
                words=adj_words, style=style, job_id=edit_id,
            )
            trimmed.unlink(missing_ok=True)
            used_words = adj_words
        else:
            await asyncio.to_thread(
                video_edit.render_captions, pre_caption, rerender_out,
                words=words, style=style, job_id=edit_id,
            )
            used_words = words
    except RuntimeError as e:
        raise HTTPException(500, f"Rerender failed: {e}")

    return {
        "edit_id": edit_id,
        "version": version,
        "output_url": _file_url(rerender_out),
        "template": template,
        "n_words": len(used_words),
        "trim_start_secs": trim_start_secs,
        "trim_end_secs": trim_end_secs,
    }


@app.post("/api/editor/timeline_render")
async def editor_timeline_render(
    edit_id: str = Form(...),
    segments_json: str = Form(...),
    source_filename: str | None = Form(None),
) -> dict:
    """CapCut-style timeline apply: take a list of `{start, end}` segments
    (in seconds, relative to the source video) and emit a new clip with each
    segment trimmed and concatenated in the supplied order.

    `source_filename` lets the client pick which video to slice — typically
    the most recent `rerender-NN.mp4` or `captioned.mp4` under the edit
    directory. Defaults to the newest mp4 in the edit folder.
    """
    from character_swap import video_edit
    edit_dir = settings.output_dir / "editor" / edit_id
    if not edit_dir.exists():
        raise HTTPException(404, "edit_id not found")

    # Pick the source file. Either the explicit filename (basename-only,
    # constrained to edit_dir to block path traversal) or the most recent
    # mp4 in the directory.
    src: Path | None = None
    if source_filename:
        safe = Path(source_filename).name  # basename only
        candidate = edit_dir / safe
        if candidate.exists() and candidate.is_file():
            src = candidate
        else:
            raise HTTPException(404, f"source video not found: {safe}")
    else:
        mp4s = sorted(edit_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not mp4s:
            raise HTTPException(404, "no rendered videos found for this edit_id")
        src = mp4s[0]

    try:
        raw = json.loads(segments_json)
    except json.JSONDecodeError:
        raise HTTPException(400, "segments_json must be valid JSON")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "segments_json must be a non-empty list")

    segments: list[tuple[float, float]] = []
    for i, seg in enumerate(raw):
        if not isinstance(seg, dict) or "start" not in seg or "end" not in seg:
            raise HTTPException(400, f"segment[{i}] must be {{start, end}}")
        try:
            s = float(seg["start"])
            e = float(seg["end"])
        except (TypeError, ValueError):
            raise HTTPException(400, f"segment[{i}] has non-numeric start/end")
        if e - s <= 0.02:
            continue  # skip degenerate ranges (UI sometimes ships ~0-length)
        segments.append((s, e))
    if not segments:
        raise HTTPException(400, "no valid segments after filtering")

    # Numbered output so old timeline renders are kept for A/B compare.
    existing = sorted(edit_dir.glob("timeline-*.mp4"))
    version = len(existing) + 1
    out_path = edit_dir / f"timeline-{version:02d}.mp4"

    try:
        summary = await asyncio.to_thread(
            video_edit.apply_timeline, src, out_path,
            segments=segments, job_id=edit_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(500, f"Timeline render failed: {e}")

    return {
        "edit_id": edit_id,
        "version": version,
        "output_url": _file_url(out_path),
        "source_filename": src.name,
        **summary,
    }


# --- Full auto-pipeline (compile → Resolve → Drive in one click) -----------------

class RunFullPipelineBody(BaseModel):
    """POST /api/jobs/{job_id}/run_full_pipeline body. Empty = run every
    eligible character. Pass `char_ids` to limit to specific characters
    (handy for retrying one failed pipeline run)."""
    char_ids: list[str] | None = None


@app.post("/api/jobs/{job_id}/run_full_pipeline")
async def run_full_pipeline(job_id: str, body: RunFullPipelineBody,
                            background: BackgroundTasks) -> dict:
    """Chain compile-no-captions → package → spawn automate.py per character.

    Each character's pipeline runs in its own asyncio task — failures stay
    isolated. UI tracks progress via JobCharacter.pipeline_status + the WS
    `char.pipeline_status` events emitted by runner_pipeline.

    Prerequisites the user must have set up (otherwise rendered MP4 appears
    but no Drive upload):
      - DaVinci Resolve installed + RUNNING (Mac: Privacy → Automation perm
        granted to whichever process runs the server)
      - ~/character-swap-data/credentials.json with OAuth Desktop client
        (optional — Drive step skips gracefully if missing)
      - pip install google-api-python-client google-auth-httplib2
        google-auth-oauthlib (optional, same)
    """
    from character_swap import runner_pipeline

    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    background.add_task(_run_async,
                        runner_pipeline.run_full_pipeline,
                        job_id, char_ids=body.char_ids)
    return _job_to_dict(job)


# --- Export to DaVinci Resolve project (download zip) -----------------------------

def _find_editor_videos(edit_dir: Path) -> tuple[Path | None, Path | None]:
    """Pick the rendered (post-caption) and pre-caption MP4s for an edit.

    Returns (final_video, pre_caption_video). Either can be None if the
    pipeline didn't produce that step (e.g. captions disabled → no
    post-caption, only pre-caption).

    Priority for FINAL: 04-final.mp4 → captioned.mp4 → rerender-NN.mp4
    (highest version) → newest .mp4 in the dir.
    Priority for PRE-CAPTION: pre_caption.txt (records the exact path the
    auto-edit pipeline used) → 03-stretched.mp4 → 02-swapped.mp4 →
    01-trimmed.mp4 → trimmed.mp4 → 00-concat.mp4. None if none of those
    exist or they're the same as the chosen final.
    """
    if not edit_dir.is_dir():
        return None, None

    # FINAL — preferred captioned outputs first.
    final: Path | None = None
    for name in ("04-final.mp4", "captioned.mp4"):
        candidate = edit_dir / name
        if candidate.exists():
            final = candidate
            break
    if final is None:
        rerenders = sorted(
            edit_dir.glob("rerender-*.mp4"),
            key=lambda p: p.stat().st_mtime,
        )
        if rerenders:
            final = rerenders[-1]
    if final is None:
        all_mp4 = sorted(
            edit_dir.glob("*.mp4"),
            key=lambda p: p.stat().st_mtime,
        )
        if all_mp4:
            final = all_mp4[-1]

    # PRE-CAPTION — prefer the pipeline's own recorded path.
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


@app.get("/api/jobs/{job_id}/characters/{char_id}/export_resolve")
async def job_char_export_resolve(job_id: str, char_id: str):
    """Download one compiled per-character video as a Resolve project zip.

    Mirrors `editor_export_resolve` but takes the (job_id, char_id) of a
    Step 6 compile output instead of an arbitrary edit_id. The final MP4
    is `jc.compiled_video_path`; pre-caption + words.json live in the
    underlying edit_dir (jc.compile_edit_id) so we can still emit SRT
    even when the compile was run with captions disabled.
    """
    from fastapi.responses import Response
    from character_swap import exporter

    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    jc = job.characters.get(char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    if jc.compile_status != "done" or not jc.compiled_video_path:
        raise HTTPException(409,
            f"Character {char_id!r} has no compiled video "
            f"(compile_status={jc.compile_status!r})")

    final_video = Path(jc.compiled_video_path)
    if not final_video.exists():
        raise HTTPException(404, f"Compiled video missing on disk: {final_video}")

    pre_caption: Path | None = None
    words: list[dict] | None = None
    if jc.compile_edit_id:
        edit_dir = settings.output_dir / "editor" / jc.compile_edit_id
        if edit_dir.is_dir():
            _, pre_caption = _find_editor_videos(edit_dir)
            words_path = edit_dir / "words.json"
            if words_path.exists():
                try:
                    words = json.loads(words_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    words = None

    char_slug = _safe_filename_stem(jc.name) or char_id
    project_name = f"{job_id}-{char_slug}"
    zip_bytes = exporter.build_export_zip(
        final_video=final_video,
        pre_caption_video=pre_caption,
        words=words,
        project_name=project_name,
    )
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{project_name}-resolve.zip"',
            "Content-Length": str(len(zip_bytes)),
        },
    )


class RunEditorPipelineBody(BaseModel):
    """POST /api/editor/run_full_pipeline body."""
    edit_id: str


@app.post("/api/editor/run_full_pipeline")
async def editor_run_full_pipeline(body: RunEditorPipelineBody,
                                   background: BackgroundTasks) -> dict:
    """Package an editor edit_id as a DaVinci Resolve project, then spawn
    automate.py to render it in Resolve and (optionally) upload to Drive.

    Same prerequisites as `/api/jobs/{job_id}/run_full_pipeline`:
      - DaVinci Resolve installed + RUNNING (Mac: Privacy → Automation perm
        granted to whichever process runs the server)
      - ~/character-swap-data/credentials.json with OAuth Desktop client
        (optional — Drive step skips gracefully if missing)

    Returns immediately with the initial pipeline state. Poll
    `GET /api/editor/{edit_id}/pipeline_state` for transitions.
    """
    from character_swap import runner_pipeline

    edit_dir = settings.output_dir / "editor" / body.edit_id
    if not edit_dir.is_dir():
        raise HTTPException(404, f"Edit {body.edit_id!r} not found")
    final, _ = runner_pipeline._editor_locate_videos(edit_dir)
    if final is None:
        raise HTTPException(409, f"No rendered video in {body.edit_id!r}")

    state = runner_pipeline._persist_editor_pipeline(
        body.edit_id, status="queued", error=None, drive_link=None,
    )
    background.add_task(_run_async,
                        runner_pipeline.run_editor_pipeline, body.edit_id)
    return state


@app.get("/api/editor/{edit_id}/pipeline_state")
async def editor_pipeline_state(edit_id: str) -> dict:
    """Return the current pipeline state for one editor edit. Empty dict
    `{}` when no pipeline has been kicked off for this edit_id."""
    from character_swap import runner_pipeline

    edit_dir = settings.output_dir / "editor" / edit_id
    if not edit_dir.is_dir():
        raise HTTPException(404, f"Edit {edit_id!r} not found")
    path = runner_pipeline._editor_pipeline_state_path(edit_id)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


@app.get("/api/editor/export_resolve/{edit_id}")
async def editor_export_resolve(edit_id: str):
    """Download the edit as a DaVinci Resolve-ready project bundle (.zip).

    Contains: final MP4 + pre-caption MP4 (if available) + captions.srt
    (from words.json) + raw words.json + starter Python script driving
    Resolve's scripting API + README. See `exporter.build_export_zip`.
    """
    from fastapi.responses import Response
    from character_swap import exporter

    edit_dir = settings.output_dir / "editor" / edit_id
    if not edit_dir.is_dir():
        raise HTTPException(404, f"Edit {edit_id!r} not found")

    final_video, pre_caption = _find_editor_videos(edit_dir)
    if final_video is None:
        raise HTTPException(404, f"No rendered video in {edit_id!r}")

    words: list[dict] | None = None
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
        project_name=edit_id,
    )
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{edit_id}-resolve.zip"',
            "Content-Length": str(len(zip_bytes)),
        },
    )


# --- B-roll generation (audio → cinematic medical-realism clips → final mp4) -----

@app.post("/api/broll/generate")
async def broll_generate(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    video_model: str = Form("grok-imagine"),
    aspect_ratio: str = Form("9:16"),
) -> dict:
    """Kick off a full B-roll generation from a narration audio file.
    Returns a `broll_id` immediately; the actual work happens in a
    background task. Poll `GET /api/broll/{broll_id}` for progress."""
    from character_swap import broll as broll_mod, runner_broll

    if not settings.openai_api_key:
        raise HTTPException(503, "OpenAI API key required for transcription + planning")

    if aspect_ratio not in {"9:16", "1:1", "16:9"}:
        raise HTTPException(400,
            f"aspect_ratio must be one of 9:16 / 1:1 / 16:9 (got {aspect_ratio!r})")

    ext = Path(file.filename or "").suffix.lower()
    allowed = ALLOWED_AUDIO_EXTS | {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
    if ext not in allowed:
        raise HTTPException(400,
            f"Unsupported source type '{ext}'. Allowed: {sorted(allowed)}")

    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")

    broll_id = "br_" + secrets.token_hex(5)
    work = broll_mod.broll_dir(broll_id)
    source_path = work / f"source{ext}"
    tmp = source_path.with_suffix(source_path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(source_path)

    # If source is a video, extract the audio so Whisper has something
    # straightforward to chew on. We still keep the original file for the
    # caller to reference.
    audio_for_pipeline = source_path
    if ext in {".mp4", ".mov", ".webm", ".mkv", ".m4v"}:
        from character_swap import video_edit
        extracted = work / "source.audio.wav"
        await asyncio.to_thread(
            video_edit._run,
            [video_edit._ffmpeg(), "-y", "-i", str(source_path),
             "-vn", "-ac", "1", "-ar", "16000", str(extracted)],
        )
        audio_for_pipeline = extracted

    initial_state = {
        "broll_id": broll_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "status": "queued",
        "error": None,
        "source_path": str(source_path),
        "audio_path": str(audio_for_pipeline),
        "source_url": _file_url(source_path),
        "video_model": video_model,
        "aspect_ratio": aspect_ratio,
        "transcript": "",
        "clips": [],
        "final_video_path": None,
        "final_video_url": None,
    }
    broll_mod.save_state(initial_state)

    background_tasks.add_task(runner_broll.run_broll, broll_id)
    return initial_state


@app.get("/api/broll")
async def broll_list() -> list[dict]:
    from character_swap import broll as broll_mod
    return broll_mod.list_states()


@app.get("/api/broll/{broll_id}")
async def broll_get(broll_id: str) -> dict:
    from character_swap import broll as broll_mod
    state = broll_mod.load_state(broll_id)
    if not state:
        raise HTTPException(404, "broll_id not found")
    return state


@app.delete("/api/broll/{broll_id}")
async def broll_delete(broll_id: str) -> dict:
    from character_swap import broll as broll_mod
    work = broll_mod.broll_dir(broll_id)
    if not work.exists():
        raise HTTPException(404, "broll_id not found")
    shutil.rmtree(work, ignore_errors=True)
    return {"ok": True, "broll_id": broll_id}


# ---------------------------------------------------------------------------
# Reengineer — rebuild an uploaded reference video with different characters.
# Pipeline: scene detection → frame per scene → vision agent writes motion+
# speech prompts → underlying Swap job (variants → approval → Kling v3 clips
# with native audio) → trim to original scene durations → concat per character.
# ---------------------------------------------------------------------------

_REENGINEER_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}


def _reengineer_view(state: dict, *, slim: bool = False) -> dict:
    """State + URL fields the frontend can render directly. Embeds a full
    job dict (variants for the approval strip, videos for progress) when the
    underlying Swap job exists.

    `slim=True` drops each variant's `prompt` — the Reengineer strip never
    renders it, and at ~3-3.8KB × 45 variants the prompts alone were ~70% of
    the payload the tab re-downloaded every 5s poll."""
    out = dict(state)
    if state.get("source_path"):
        out["source_url"] = _file_url(Path(state["source_path"]))
    for e in out.get("scenes", []):
        sid = e.get("scene_id")
        if sid:
            scene = store().get_scene(sid)
            if scene:
                e["frame_url"] = _file_url(settings.scenes_dir / scene.filename)
    finals = out.get("finals") or {}
    for cid, f in finals.items():
        if f.get("final_path"):
            f["final_url"] = _file_url(Path(f["final_path"]))
    if state.get("job_id"):
        job = store().get_job(state["job_id"])
        if job is not None:
            out["job"] = _job_to_dict(job)
            if slim:
                for jc in out["job"]["characters"].values():
                    for img in jc["images"]:
                        img.pop("prompt", None)
    return out


@app.post("/api/reengineer")
async def reengineer_create(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    character_ids: str = Form(...),          # JSON array of char ids
    image_model: str = Form("gpt2-id-swap"),
    video_model: str = Form("kling-v3"),
    auto_mode: bool = Form(False),
    # Outfit ("Kläder"): scene = wear the original video person's clothes;
    # character = the character reference's own clothes; custom = outfit_text.
    outfit_mode: str = Form("scene"),
    outfit_text: str = Form(""),
    # Cut sensitivity: normal/high/max -> ffmpeg scene-score thresholds.
    scene_sensitivity: str = Form("high"),
    # 🎬 AI Director: one Claude call looks at every scene frame and writes a
    # tailored compact swap prompt per scene (props named with position/size,
    # camera distance anchored). Off by default; needs ANTHROPIC_API_KEY.
    use_director: bool = Form(False),
    # Optional replacement background: applied to EVERY scene's swap image;
    # the character + kept props are relit to match its light.
    background_file: UploadFile | None = File(None),
    # Optional JSON dict {char_id: image_id}: which of the character's gallery
    # images to use as the identity reference (e.g. a specific outfit). Falls
    # back to the character's primary image.
    character_source_image_ids: str = Form(""),
) -> dict:
    """Upload a reference video and start the Reengineer pipeline. Returns the
    initial state immediately; poll GET /api/reengineer/{re_id}."""
    from character_swap import reengineer as reengineer_mod, runner_reengineer

    if not settings.openai_api_key:
        raise HTTPException(503, "OpenAI API key required (Whisper transcription)")
    try:
        char_ids = [c for c in json.loads(character_ids) if c]
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(400, "character_ids must be a JSON array of ids")
    if not char_ids:
        raise HTTPException(400, "Pick at least one character")
    for cid in char_ids:
        if store().get_character(cid) is None:
            raise HTTPException(404, f"Character not found: {cid}")
    info = runner_media.IMAGE_MODELS.get(image_model)
    if info is None:
        raise HTTPException(400, f"Unknown image_model '{image_model}'")
    if not settings.has_provider(info["provider"]):
        raise HTTPException(503, f"{info['label']} is not configured. Add the API key to .env.")
    if video_model not in runner_media.VIDEO_MODELS:
        raise HTTPException(400, f"Unknown video_model '{video_model}'")
    if outfit_mode not in ("scene", "character", "custom"):
        raise HTTPException(400, f"Unknown outfit_mode '{outfit_mode}'")
    if outfit_mode == "custom" and not outfit_text.strip():
        raise HTTPException(400, "outfit_mode 'custom' requires a clothing description")
    if scene_sensitivity not in reengineer_mod.SENSITIVITY_THRESHOLDS:
        raise HTTPException(400, f"Unknown scene_sensitivity '{scene_sensitivity}'")
    source_overrides: dict[str, str] = {}
    if character_source_image_ids.strip():
        try:
            parsed = json.loads(character_source_image_ids)
            if isinstance(parsed, dict):
                source_overrides = {str(k): str(v) for k, v in parsed.items() if v}
        except json.JSONDecodeError:
            raise HTTPException(400, "character_source_image_ids must be a JSON dict")

    ext = Path(file.filename or "").suffix.lower()
    if ext not in _REENGINEER_VIDEO_EXTS:
        raise HTTPException(400, f"Unsupported video type '{ext}'. "
                                 f"Allowed: {sorted(_REENGINEER_VIDEO_EXTS)}")
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")

    re_id = "re_" + secrets.token_hex(5)
    work = reengineer_mod.reengineer_dir(re_id)
    source_path = work / f"source{ext}"
    tmp = source_path.with_suffix(source_path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(source_path)

    background_path: str | None = None
    if background_file is not None and (background_file.filename or "").strip():
        bg_ext = _safe_ext(background_file.filename or "")
        if not bg_ext:
            raise HTTPException(400, "Background must be an image "
                                     f"({sorted(ALLOWED_IMAGE_EXTS)})")
        bg_data = await _read_capped(background_file)
        if not bg_data:
            raise HTTPException(400, "Empty background upload")
        bg_dest = work / f"background{bg_ext}"
        bg_tmp = bg_dest.with_suffix(bg_dest.suffix + ".tmp")
        bg_tmp.write_bytes(bg_data)
        bg_tmp.replace(bg_dest)
        background_path = str(bg_dest)

    initial_state = {
        "re_id": re_id,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "updated_at": datetime.utcnow().isoformat() + "Z",
        "status": "queued",
        "error": None,
        "source_path": str(source_path),
        "source_name": file.filename or source_path.name,
        "character_ids": char_ids,
        "image_model": image_model,
        "video_model": video_model,
        "auto_mode": bool(auto_mode),
        "outfit_mode": outfit_mode,
        "outfit_text": outfit_text.strip(),
        "scene_sensitivity": scene_sensitivity,
        "use_director": bool(use_director) and bool(settings.anthropic_api_key),
        "background_path": background_path,
        "character_source_image_ids": source_overrides,
        "scenes": [],
        "job_id": None,
        "finals": {},
    }
    reengineer_mod.save_state(initial_state)
    background_tasks.add_task(_run_async, runner_reengineer.run_reengineer, re_id)
    return _reengineer_view(initial_state)


@app.get("/api/reengineer")
async def reengineer_list() -> list[dict]:
    from character_swap import reengineer as reengineer_mod
    # List view stays light: no embedded job dicts (the detail view has them).
    out = []
    for state in reengineer_mod.list_states():
        row = dict(state)
        row.pop("scenes", None)
        out.append(row)
    return out


@app.get("/api/reengineer/{re_id}")
async def reengineer_get(re_id: str, slim: bool = False) -> dict:
    """`?slim=1` omits each variant's prompt text from the embedded job —
    the polling/WS-refresh path uses it (the strip never shows prompts)."""
    from character_swap import reengineer as reengineer_mod
    state = reengineer_mod.load_state(re_id)
    if not state:
        raise HTTPException(404, "re_id not found")
    return _reengineer_view(state, slim=slim)


class ReAssembleSettingsBody(BaseModel):
    """Editor finishing settings for the Reengineer final build (the ⚙ panel).

    Optional on both the animate endpoint (persisted BEFORE the video phase so
    the auto-assemble that follows uses them) and the assemble endpoint
    (re-builds). Field set mirrors Swap Step 6's CompileVideosBody; the
    DEFAULTS differ — voice swap + WPM normalize are OFF so Kling's own
    lip-synced voice and pacing survive (Hugo 2026-06-12). All fields default
    to None = "keep whatever is already stored / the runner default"."""
    template: str | None = None
    overrides: dict | None = None
    enable_trim: bool | None = None
    enable_captions: bool | None = None
    enable_wpm_normalize: bool | None = None
    target_wpm: float | None = Field(default=None, ge=80, le=400)
    threshold_db: float | None = Field(default=None, ge=-60, le=0)
    min_silence_secs: float | None = Field(default=None, ge=0.05, le=5)
    pad_secs: float | None = Field(default=None, ge=0, le=1)
    enable_voice_swap: bool | None = None
    voice_override: str | None = None


def _store_assemble_settings(state: dict,
                             body: ReAssembleSettingsBody | None) -> bool:
    """Merge the panel's explicit (non-None) fields into the run state.
    Returns True when something changed (caller persists). No body /
    all-None → False, so settings-less calls keep the stored values."""
    if body is None:
        return False
    sent = {k: v for k, v in body.model_dump().items() if v is not None}
    # voice_override="" means "clear the override" — store it as None.
    if body.voice_override is not None:
        sent["voice_override"] = body.voice_override.strip() or None
    if not sent:
        return False
    merged = dict(state.get("assemble_settings") or {})
    merged.update(sent)
    if merged == (state.get("assemble_settings") or {}):
        return False
    state["assemble_settings"] = merged
    return True


@app.post("/api/reengineer/{re_id}/animate")
async def reengineer_animate(re_id: str, background_tasks: BackgroundTasks,
                             body: ReAssembleSettingsBody | None = None) -> dict:
    """Continue after manual image approval: submit movement (agent prompts +
    matched durations) and generate the Kling clips. The optional body is the
    ⚙ final-build panel — persisted NOW so the automatic assemble at the end
    of the video phase (and any crash-resume) uses the user's choices."""
    from character_swap import reengineer as reengineer_mod, runner_reengineer
    state = reengineer_mod.load_state(re_id)
    if not state:
        raise HTTPException(404, "re_id not found")
    if not state.get("job_id"):
        raise HTTPException(409, "run has no underlying job yet")
    if state.get("status") not in {"awaiting_approval", "failed", "animating"}:
        raise HTTPException(409, f"cannot animate from status '{state.get('status')}'")
    if _store_assemble_settings(state, body):
        _save_reengineer_state(state)
    background_tasks.add_task(_run_async, runner_reengineer.animate, re_id)
    return {"ok": True, "re_id": re_id}


@app.post("/api/reengineer/{re_id}/assemble")
async def reengineer_assemble(re_id: str, background_tasks: BackgroundTasks,
                              body: ReAssembleSettingsBody | None = None) -> dict:
    """(Re-)run final assembly — e.g. after retrying a failed video in the
    underlying job, after a server restart mid-assembly, or to re-build with
    new ⚙ panel settings (passed in the optional body)."""
    from character_swap import reengineer as reengineer_mod, runner_reengineer
    state = reengineer_mod.load_state(re_id)
    if not state:
        raise HTTPException(404, "re_id not found")
    if not state.get("job_id"):
        raise HTTPException(409, "run has no underlying job yet")
    # Assembly now bills Whisper (+ optional Remotion/ElevenLabs) per
    # character — refuse overlap instead of double-building the same finals.
    if state.get("status") == "assembling" or re_id in runner_reengineer._ASSEMBLING:
        raise HTTPException(409, "assembly already running for this run")
    if _store_assemble_settings(state, body):
        _save_reengineer_state(state)
    background_tasks.add_task(_run_async, runner_reengineer.assemble, re_id)
    return {"ok": True, "re_id": re_id}


@app.delete("/api/reengineer/{re_id}")
async def reengineer_delete(re_id: str) -> dict:
    from character_swap import reengineer as reengineer_mod
    work = settings.output_dir / "reengineer" / re_id
    if not work.exists():
        raise HTTPException(404, "re_id not found")
    shutil.rmtree(work, ignore_errors=True)
    return {"ok": True, "re_id": re_id}


# ------------------------------------------------------------- reengineer EDIT MODE
#
# Opt-in iteration on a run at the approval gate or after it finished. All
# endpoints key scenes by their LIST INDEX (`idx`) — scene_id is NOT unique
# within state.scenes (static videos legitimately repeat one id). The default
# pipeline is untouched: these endpoints only run when the user acts.

def _editable_reengineer_state(re_id: str, *, statuses: set[str] | None = None) -> dict:
    from character_swap import reengineer as reengineer_mod, runner_reengineer
    state = reengineer_mod.load_state(re_id)
    if not state:
        raise HTTPException(404, "re_id not found")
    allowed = statuses if statuses is not None else runner_reengineer._EDITABLE_RUN_STATES
    if state.get("status") not in allowed:
        raise HTTPException(409,
            f"cannot edit while run status is '{state.get('status')}'")
    return state


def _reengineer_entry(state: dict, idx: int) -> dict:
    entries = state.get("scenes") or []
    if idx < 0 or idx >= len(entries):
        raise HTTPException(404, f"scene idx {idx} out of range (0..{len(entries) - 1})")
    return entries[idx]


def _renumber_scenes(state: dict) -> None:
    entries = state.get("scenes") or []
    for i, e in enumerate(entries):
        e["idx"] = i
    state["n_scenes"] = len(entries)


def _save_reengineer_state(state: dict) -> None:
    from character_swap import reengineer as reengineer_mod
    state["updated_at"] = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    reengineer_mod.save_state(state)


def _mark_finals_stale(state: dict) -> None:
    if state.get("finals"):
        state["finals_stale"] = True


class ReSceneEditBody(BaseModel):
    motion_prompt: str | None = None
    duration: float | None = None
    # Explicit dirty mark — the frontend sets it after approve-swaps /
    # variant-regens on an already-animated scene (new image ≠ old clip).
    dirty: bool | None = None


@app.patch("/api/reengineer/{re_id}/scenes/{idx}")
async def reengineer_edit_scene(re_id: str, idx: int, body: ReSceneEditBody) -> dict:
    """Edit one scene entry's motion prompt (incl. dialogue) and/or duration.
    At the gate this is free — _do_animate reads the state fresh. After the
    videos exist the entry is marked dirty AND the edit is synced onto the
    job so single-clip redos already use the new text."""
    from character_swap import runner_reengineer
    state = _editable_reengineer_state(re_id)
    entry = _reengineer_entry(state, idx)

    changed = False
    if body.motion_prompt is not None and body.motion_prompt.strip():
        if body.motion_prompt.strip() != entry.get("motion_prompt"):
            entry["motion_prompt"] = body.motion_prompt.strip()
            changed = True
    if body.duration is not None:
        dur = max(1.0, min(15.0, float(body.duration)))
        if dur != entry.get("duration"):
            entry["duration"] = dur
            changed = True
    if body.dirty:
        entry["dirty"] = True

    job = store().get_job(state.get("job_id") or "")
    post_gate = bool(job is not None and (job.movement_prompts or job.movement_prompt))
    if changed and post_gate:
        entry["dirty"] = True
        runner_reengineer._sync_movement_from_state(job, state, [idx])
    _save_reengineer_state(state)
    return _reengineer_view(state, slim=True)


@app.post("/api/reengineer/{re_id}/scenes")
async def reengineer_add_scene(
    re_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    motion_prompt: str = Form(""),
    duration: float = Form(0.0),
    whisper: bool = Form(False),
    position: int = Form(-1),
) -> dict:
    """Add a scene from an uploaded IMAGE or VIDEO. Video → mid-frame becomes
    the scene image (+ optional Whisper dialogue prefill into the prompt).
    Swap images for EVERY character generate in the background (normal QC);
    the new variants need manual approval before the scene can animate."""
    from character_swap import reengineer as reengineer_mod, runner_reengineer
    from character_swap import video_edit
    state = _editable_reengineer_state(re_id)
    if not state.get("job_id"):
        raise HTTPException(409, "run has no underlying job yet")
    s = store()
    job = s.get_job(state["job_id"])
    if job is None:
        raise HTTPException(409, "underlying job disappeared")

    ext = _safe_ext(file.filename or "upload.png", allow_video=True)
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    run_dir = reengineer_mod.reengineer_dir(re_id) / "added"
    run_dir.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_hex(4)

    is_video = ext in {".mp4", ".mov", ".webm"}
    whisper_source: str | None = None
    if is_video:
        src = run_dir / f"src_{tok}{ext}"
        src.write_bytes(data)
        vid_dur = await asyncio.to_thread(video_edit._probe_duration, src)
        frame = run_dir / f"frame_{tok}.png"
        await asyncio.to_thread(reengineer_mod.extract_frame, src,
                                max(0.0, vid_dur / 2.0), frame)
        scene_id, _path = runner_reengineer._register_frame_as_scene(frame)
        if duration <= 0:
            duration = max(1.0, min(15.0, vid_dur or 5.0))
        if whisper:
            settings.require_keys("openai")
            whisper_source = str(src)
    else:
        upload = run_dir / f"upload_{tok}{ext}"
        upload.write_bytes(data)
        scene_id, _path = runner_reengineer._register_frame_as_scene(upload)
        if duration <= 0:
            duration = 5.0

    duration = max(1.0, min(15.0, float(duration)))
    entry = {
        "idx": 0,  # renumbered below
        "scene_id": scene_id,
        "start": 0.0,
        "end": round(duration, 3),
        "duration": round(duration, 3),
        "motion_prompt": (motion_prompt.strip()
                          or runner_reengineer.ADDED_SCENE_PROMPT),
        "speech": "",
        "summary": (file.filename or "Egen scen")[:80],
        "dirty": True,
        "source": "video" if is_video else "image",
    }
    if whisper_source:
        entry["transcribing"] = True

    entries = state.get("scenes") or []
    pos = position if 0 <= position <= len(entries) else len(entries)
    entries.insert(pos, entry)
    state["scenes"] = entries
    _renumber_scenes(state)
    _mark_finals_stale(state)

    # Extend the underlying job so variant generation accepts the scene.
    # (Append is fine — assembly order follows state.scenes, not the job.)
    if scene_id not in (job.scene_ids or []):
        job.scene_ids = list(job.scene_ids or [job.scene_id]) + [scene_id]
        job.scene_image_paths = (list(job.scene_image_paths
                                      or [job.scene_image_path])
                                 + [str(_path)])
        job.updated_at = datetime.utcnow()
        s.update_job(job)

    _save_reengineer_state(state)
    background_tasks.add_task(_run_async, runner_reengineer.generate_added_scene,
                              re_id, scene_id, whisper_source=whisper_source)
    return _reengineer_view(state, slim=True)


@app.post("/api/reengineer/{re_id}/scenes/{idx}/duplicate")
async def reengineer_duplicate_scene(re_id: str, idx: int) -> dict:
    """Duplicate a scene: same image, NEW scene_id, every character's approved
    image cloned + auto-approved (zero image generations — only the new Kling
    clip costs). Edit the copy's prompt to e.g. say a different line."""
    from character_swap import runner_reengineer
    state = _editable_reengineer_state(re_id)
    entry = _reengineer_entry(state, idx)
    s = store()
    job = s.get_job(state.get("job_id") or "")
    if job is None:
        raise HTTPException(409, "underlying job disappeared")

    new_sid = _apply_scene_duplicate(job, entry["scene_id"])
    s.update_job(job)
    # The strip resolves thumbnails via store().get_scene — register the new
    # id pointing at the SAME file as the source scene (no copy).
    src_scene = s.get_scene(entry["scene_id"])
    filename = (src_scene.filename if src_scene
                else Path(job.scene_image_paths[
                    job.scene_ids.index(new_sid)]).name)
    if s.get_scene(new_sid) is None:
        s.add_scene(SceneAsset(scene_id=new_sid, filename=filename,
                               original_name=f"{entry.get('summary', new_sid)} (kopia)"))

    copy = dict(entry)
    copy.update({
        "scene_id": new_sid,
        "summary": f"{entry.get('summary', '')} (kopia)".strip(),
        "dirty": True,
        "source": "duplicate",
    })
    copy.pop("transcribing", None)
    entries = state.get("scenes") or []
    entries.insert(idx + 1, copy)
    state["scenes"] = entries
    _renumber_scenes(state)
    _mark_finals_stale(state)
    _save_reengineer_state(state)
    return _reengineer_view(state, slim=True)


@app.delete("/api/reengineer/{re_id}/scenes/{idx}")
async def reengineer_delete_scene(re_id: str, idx: int) -> dict:
    """Remove a scene entry from the run (and its variants from the job when
    no other entry still references the same scene_id)."""
    state = _editable_reengineer_state(re_id)
    entry = _reengineer_entry(state, idx)
    entries = state.get("scenes") or []
    if len(entries) <= 1:
        raise HTTPException(409, "Can't delete the only scene")

    sid = entry["scene_id"]
    s = store()
    job = s.get_job(state.get("job_id") or "")
    if job is not None:
        for jc in job.characters.values():
            scene_variant_ids = {v.variant_id for v in jc.images
                                 if v.scene_id == sid}
            if any(v.scene_id == sid and v.status == VariantStatus.GENERATING
                   for v in jc.images):
                raise HTTPException(409, "Scene images are still generating")
            if any(vv.source_variant_id in scene_variant_ids
                   and vv.status in {VideoStatus.PENDING, VideoStatus.PROCESSING}
                   for vv in jc.videos):
                raise HTTPException(409, "Scene clip is still rendering")

    entries.pop(idx)
    state["scenes"] = entries
    _renumber_scenes(state)
    _mark_finals_stale(state)

    shared = any(e.get("scene_id") == sid for e in entries)
    if job is not None and not shared and sid in (job.scene_ids or []):
        try:
            _apply_scene_delete(job, sid)
            s.update_job(job)
        except HTTPException:
            pass  # e.g. last scene on the job — state already updated
    _save_reengineer_state(state)
    return _reengineer_view(state, slim=True)


class ReSceneOrderBody(BaseModel):
    order: list[int]


@app.patch("/api/reengineer/{re_id}/scene_order")
async def reengineer_scene_order(re_id: str, body: ReSceneOrderBody) -> dict:
    """Reorder scenes — `order` is a permutation of the current indices.
    Finals concatenate in the new order on the next rebuild."""
    state = _editable_reengineer_state(re_id)
    entries = state.get("scenes") or []
    if sorted(body.order) != list(range(len(entries))):
        raise HTTPException(400, "order must be a permutation of 0..N-1")

    state["scenes"] = [entries[i] for i in body.order]
    _renumber_scenes(state)
    _mark_finals_stale(state)

    # Keep the job's scene order in lockstep when the sets still match
    # (cosmetic — assembly follows state.scenes).
    s = store()
    job = s.get_job(state.get("job_id") or "")
    if job is not None:
        deduped: list[str] = []
        for e in state["scenes"]:
            if e["scene_id"] not in deduped:
                deduped.append(e["scene_id"])
        if sorted(deduped) == sorted(job.scene_ids or []):
            _apply_scene_reorder(job, deduped)
            s.update_job(job)
    _save_reengineer_state(state)
    return _reengineer_view(state, slim=True)


class ReRedoBody(BaseModel):
    char_id: str | None = None


@app.post("/api/reengineer/{re_id}/scenes/{idx}/redo")
async def reengineer_redo_scene(re_id: str, idx: int,
                                background_tasks: BackgroundTasks,
                                body: ReRedoBody | None = None) -> dict:
    """New take of a scene's Kling clip(s) — for ONE character (`char_id`)
    or all. Same prompt unless the scene was edited (edits sync onto the
    job). Keeps the dirty flag: a redo isn't a re-animation of an edit."""
    from character_swap import runner_reengineer
    state = _editable_reengineer_state(
        re_id, statuses={"done", "partial_success", "failed"})
    _reengineer_entry(state, idx)
    char_id = body.char_id if body else None
    background_tasks.add_task(_run_async, runner_reengineer.reanimate,
                              re_id, [idx], char_id=char_id, clear_dirty=False)
    return {"ok": True, "re_id": re_id, "idx": idx, "char_id": char_id}


class ReAnimateScenesBody(BaseModel):
    idxs: list[int] | None = None


@app.post("/api/reengineer/{re_id}/animate_scenes")
async def reengineer_animate_scenes(re_id: str,
                                    background_tasks: BackgroundTasks,
                                    body: ReAnimateScenesBody | None = None) -> dict:
    """Re-animate edited (dirty) scenes — or an explicit idx list. Clears the
    dirty flags on completion. Never assembles; use the rebuild button."""
    from character_swap import runner_reengineer
    state = _editable_reengineer_state(
        re_id, statuses={"done", "partial_success", "failed"})
    entries = state.get("scenes") or []
    idxs = (body.idxs if body and body.idxs is not None
            else [i for i, e in enumerate(entries) if e.get("dirty")])
    idxs = [i for i in idxs if 0 <= i < len(entries)]
    if not idxs:
        raise HTTPException(400, "no dirty scenes to re-animate")

    # Surface unapproved (entry × char) pairs so the UI can warn up front.
    skipped: list[dict] = []
    job = store().get_job(state.get("job_id") or "")
    if job is not None:
        from character_swap.runner_reengineer import _approved_variant_for
        for i in idxs:
            sid = entries[i]["scene_id"]
            for cid, jc in job.characters.items():
                if _approved_variant_for(jc, sid) is None:
                    skipped.append({"idx": i, "char_id": cid,
                                    "reason": "no approved variant"})

    background_tasks.add_task(_run_async, runner_reengineer.reanimate,
                              re_id, idxs)
    return {"ok": True, "re_id": re_id, "idxs": idxs, "skipped": skipped}


@app.post("/api/broll/{broll_id}/regenerate_clip")
async def broll_regenerate_clip(
    broll_id: str,
    background_tasks: BackgroundTasks,
    idx: int = Form(...),
) -> dict:
    """Reject a single clip and regenerate it. Reuses the same line + prompt
    + target_duration. Allowed only when the job is in awaiting_approval
    (so it doesn't fight an in-flight pipeline)."""
    from character_swap import broll as broll_mod, runner_broll
    state = broll_mod.load_state(broll_id)
    if not state:
        raise HTTPException(404, "broll_id not found")
    if state.get("status") not in {"awaiting_approval", "partial_success", "done"}:
        raise HTTPException(409,
            f"Can't regenerate while job status is '{state.get('status')}'")
    clips = state.get("clips") or []
    if idx < 0 or idx >= len(clips):
        raise HTTPException(400, f"idx {idx} out of range (0..{len(clips) - 1})")
    # Mark immediately so the UI sees it transition out of 'done'.
    clip = clips[idx]
    clip.update({"status": "image_running", "video_url": None,
                 "video_path": None, "error": None})
    state["clips"] = clips
    # If we're regenerating after finalize, drop the old final video so we
    # don't show stale output that doesn't match the new clip set.
    if state.get("status") in {"done", "partial_success"}:
        state["final_video_path"] = None
        state["final_video_url"] = None
    state["status"] = "awaiting_approval"
    broll_mod.save_state(state)

    background_tasks.add_task(runner_broll.regenerate_clip, broll_id, idx)
    return {"ok": True, "broll_id": broll_id, "idx": idx}


@app.post("/api/broll/{broll_id}/finalize")
async def broll_finalize(
    broll_id: str,
    background_tasks: BackgroundTasks,
) -> dict:
    """Trim + concat + mux. Triggered by the user after reviewing each
    clip in awaiting_approval. Refuses if any clip is mid-generation."""
    from character_swap import broll as broll_mod, runner_broll
    state = broll_mod.load_state(broll_id)
    if not state:
        raise HTTPException(404, "broll_id not found")
    if state.get("status") not in {"awaiting_approval", "partial_success", "done"}:
        raise HTTPException(409,
            f"Can't finalize while job status is '{state.get('status')}'")
    clips = state.get("clips") or []
    in_flight = [c for c in clips
                 if c.get("status") in {"pending", "image_running",
                                        "image_done", "video_running"}]
    if in_flight:
        raise HTTPException(409,
            f"{len(in_flight)} clip(s) still generating — wait or refresh")
    successful = [c for c in clips if c.get("status") == "done"]
    if not successful:
        raise HTTPException(400, "No successful clips to finalize")

    state["status"] = "concatenating"
    broll_mod.save_state(state)
    background_tasks.add_task(runner_broll.finalize_broll, broll_id)
    return {"ok": True, "broll_id": broll_id, "n_clips": len(successful)}


# Mount the editor outputs subtree so frontend can download the rendered files.
_editor_dir = settings.output_dir / "editor"
_editor_dir.mkdir(parents=True, exist_ok=True)

# Same for b-roll outputs — exposes source.mp3, clips/clip-NN.mp4, final.mp4.
_broll_dir = settings.output_dir / "broll"
_broll_dir.mkdir(parents=True, exist_ok=True)



@app.exception_handler(ProviderNotConfigured)
async def _provider_not_configured(request, exc: ProviderNotConfigured):
    return JSONResponse(status_code=503, content={"error": str(exc), "provider": exc.provider})


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": "0.5.0",
        "openai_key": bool(settings.openai_api_key),
        # Drives the 🎬 AI Director toggle's disabled state in the UI.
        "anthropic_key": bool(settings.anthropic_api_key),
        "xai_key": bool(settings.xai_api_key),
        "gemini_key": bool(settings.gemini_api_key),
        "kling_key": bool(settings.kling_access_key and settings.kling_secret_key),
        "heygen_key": bool(settings.heygen_api_key),
        "elevenlabs_key": bool(settings.elevenlabs_api_key),
        # Drives the lock state on `veed-*` caption templates in the Editor.
        "fal_key": bool(settings.fal_api_key),
        "remotion_available": _remotion_available(),
        # Higgsfield → Drive auto-import: whether OAuth is set up enough to
        # let the watcher poll the user's configured folder.
        "higgsfield_drive_ready": __import__(
            "character_swap.clients.google_drive",
            fromlist=["status"],
        ).status()["ready"],
        # Drives the Editor's "☁︎ Export to Drive" button — whether the
        # drive.file (write) OAuth token has been issued.
        "drive_write_ready": __import__(
            "character_swap.clients.google_drive",
            fromlist=["write_status"],
        ).write_status()["ready"],
        # Whether the Higgsfield auto-process pipeline can deliver to
        # Telegram. False = files land in the inbox but don't get pushed.
        "telegram_ready": bool(
            settings.telegram_bot_token and settings.telegram_chat_id
        ),
        "higgsfield_auto_process": settings.higgsfield_auto_process,
    }


# --- Editor: Drive export (upload captioned MP4 to user's Google Drive) ---

class DriveExportBody(BaseModel):
    filename: str
    folder_id: str | None = None


@app.post("/api/editor/drive_export/bootstrap")
async def editor_drive_export_bootstrap() -> dict:
    """One-time OAuth flow for the drive.file (write) scope. Opens a browser
    on the server's machine; user clicks through Google consent. Token is
    persisted at ~/character-swap-data/drive_write_token.json — separate
    from the read-only token the Higgsfield-inbox watcher uses."""
    from character_swap.clients import google_drive
    if not (settings.state_dir.parent / "credentials.json").exists():
        raise HTTPException(
            409,
            "credentials.json not in ~/character-swap-data/. Complete the "
            "Google Cloud OAuth Desktop-client setup first (same flow as the "
            "Higgsfield-inbox auth).",
        )
    result = await asyncio.to_thread(google_drive.bootstrap_write_oauth)
    if not result.get("ok"):
        raise HTTPException(500, f"OAuth flow failed: {result}")
    return result


@app.post("/api/editor/{edit_id}/drive_export")
async def editor_drive_export(edit_id: str, body: DriveExportBody) -> dict:
    """Upload the captioned final MP4 of `edit_id` to the user's Drive.
    Filename comes from the request body — caller picks. `.mp4` is appended
    if no extension is supplied."""
    from character_swap.clients import google_drive

    edit_dir = settings.output_dir / "editor" / edit_id
    if not edit_dir.is_dir():
        raise HTTPException(404, f"Edit {edit_id!r} not found")
    # Pick the same "final video" the export-to-Resolve flow used: prefers
    # 04-final.mp4, falls back to captioned.mp4, rerender-NN, or newest mp4.
    final, _ = _find_editor_videos(edit_dir)
    if final is None:
        raise HTTPException(404, f"No rendered video in {edit_id!r}")

    raw = (body.filename or "").strip()
    if not raw:
        raise HTTPException(400, "filename is required")
    # Strip path separators — Drive treats filenames as flat strings.
    raw = raw.replace("/", "_").replace("\\", "_")
    if "." not in raw:
        raw = raw + ".mp4"

    result = await asyncio.to_thread(
        google_drive.upload_file, final,
        drive_filename=raw, folder_id=body.folder_id,
    )
    if result is None:
        raise HTTPException(
            500,
            "Drive upload failed. Make sure Drive write access is authorized "
            "(POST /api/editor/drive_export/bootstrap) and the file is "
            "under Drive's 5TB single-file size cap.",
        )
    return {
        "ok": True,
        "drive_id": result.get("id"),
        "name": result.get("name"),
        "url": result.get("webViewLink"),
        "size": result.get("size"),
        "mime_type": result.get("mimeType"),
    }


# --- Chat tab (Claude-driven agent over the existing endpoints) ----------------

class ChatTurnBody(BaseModel):
    message: str


def _chat_to_dict(chat) -> dict:
    return {
        "chat_id": chat.chat_id,
        "title": chat.title,
        "created_at": chat.created_at.isoformat() + "Z",
        "updated_at": chat.updated_at.isoformat() + "Z",
        "messages": chat.messages,
        "media": chat.media,
        "n_messages": len(chat.messages),
        "n_media": len(chat.media),
    }


@app.post("/api/chats")
async def chats_create() -> dict:
    from character_swap import chat as chat_mod
    if not settings.has_provider("anthropic"):
        raise HTTPException(503,
            "ANTHROPIC_API_KEY not set — required for the Chat tab. Add it to .env.")
    chat = chat_mod.new_chat()
    return _chat_to_dict(chat)


@app.get("/api/chats")
async def chats_list() -> list[dict]:
    chats = store().list_chats() if hasattr(store(), "list_chats") else []
    return [_chat_to_dict(c) for c in chats]


@app.get("/api/chats/{chat_id}")
async def chats_get(chat_id: str) -> dict:
    chat = store().get_chat(chat_id) if hasattr(store(), "get_chat") else None
    if chat is None:
        raise HTTPException(404, f"Chat {chat_id!r} not found")
    return _chat_to_dict(chat)


@app.delete("/api/chats/{chat_id}")
async def chats_delete(chat_id: str) -> dict:
    if not hasattr(store(), "delete_chat"):
        raise HTTPException(404, "Chat backend not available")
    removed = store().delete_chat(chat_id)
    if removed is None:
        raise HTTPException(404, f"Chat {chat_id!r} not found")
    return {"deleted": chat_id}


@app.patch("/api/chats/{chat_id}")
async def chats_update(chat_id: str, body: dict) -> dict:
    chat = store().get_chat(chat_id) if hasattr(store(), "get_chat") else None
    if chat is None:
        raise HTTPException(404, f"Chat {chat_id!r} not found")
    if "title" in body and isinstance(body["title"], str):
        chat.title = body["title"][:200]
    store().update_chat(chat)
    return _chat_to_dict(chat)


@app.post("/api/chats/{chat_id}/turn")
async def chats_turn(chat_id: str, body: ChatTurnBody) -> dict:
    """Run one agent loop: append the user message, call Claude until it
    stops requesting tools. Blocks until done — can take 30s+ for multi-tool
    turns (image gen alone is ~15-30s). Frontend should show a spinner."""
    from character_swap import chat as chat_mod
    if not settings.has_provider("anthropic"):
        raise HTTPException(503, "ANTHROPIC_API_KEY not set")
    if not body.message.strip():
        raise HTTPException(400, "empty message")
    chat = store().get_chat(chat_id)
    if chat is None:
        raise HTTPException(404, f"Chat {chat_id!r} not found")
    chat = await chat_mod.run_turn(chat_id, body.message)
    return _chat_to_dict(chat)


# --- Higgsfield Drive inbox (auto-import from user's Drive folder) -------------

@app.get("/api/higgsfield/inbox")
async def higgsfield_inbox() -> dict:
    """List clips currently staged in the local Higgsfield inbox + the
    Drive-connection status the UI uses to decide what to render."""
    from character_swap import runner_drive_watcher
    from character_swap.clients import google_drive
    drive_status = google_drive.status()
    return {
        "drive": {
            **drive_status,
            "folder_name": settings.higgsfield_drive_folder_name,
            "folder_id": settings.higgsfield_drive_folder_id,
            "poll_secs": settings.higgsfield_drive_poll_secs,
        },
        "items": runner_drive_watcher.list_inbox(),
    }


@app.post("/api/higgsfield/inbox/poll")
async def higgsfield_inbox_poll() -> dict:
    """Force an immediate poll cycle (otherwise the watcher runs every
    `poll_secs`). Useful when the user just finished a Supercomputer
    render and wants to pull it in NOW instead of waiting up to a minute."""
    from character_swap import runner_drive_watcher
    return await runner_drive_watcher.poll_once()


@app.delete("/api/higgsfield/inbox/{drive_id}")
async def higgsfield_inbox_clear(drive_id: str) -> dict:
    """Remove one inbox item from local disk. The Drive `id` stays in our
    seen set, so we don't re-download it on the next poll."""
    from character_swap import runner_drive_watcher
    removed = runner_drive_watcher.clear_inbox_item(drive_id)
    return {"removed": removed, "drive_id": drive_id}


@app.post("/api/higgsfield/drive/bootstrap")
async def higgsfield_drive_bootstrap() -> dict:
    """Kick off the Google OAuth flow for the watcher's `drive.readonly`
    scope. Opens the user's browser (server-side flow) — must run on the
    same machine the user is on. Idempotent: a no-op when a valid token
    already exists."""
    from character_swap.clients import google_drive
    return google_drive.bootstrap_oauth()


@app.exception_handler(404)
async def not_found(_, exc):
    detail = str(exc.detail) if hasattr(exc, "detail") else "not found"
    return JSONResponse(status_code=404, content={"error": detail})
