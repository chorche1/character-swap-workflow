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
    if path is None:
        return None
    p = Path(path).resolve()
    try:
        rel = p.relative_to(settings.project_root.resolve())
    except ValueError:
        return None
    return f"/files/{rel.as_posix()}"


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
    stem = _safe_filename_stem(jc.name)
    idx = jc.videos.index(video) + 1 if video in jc.videos else 1
    ext = ".mp4"
    if video.final_video_path:
        ext = Path(video.final_video_path).suffix or ".mp4"
    return f"{stem}-video-{idx}{ext}"


def _auto_title(char_names: list[str]) -> str:
    when = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    if not char_names:
        return when
    head = ", ".join(char_names[:3])
    suffix = " …" if len(char_names) > 3 else ""
    out = f"{when} — {head}{suffix}"
    # Cap to ~80 chars to keep sidebar tidy.
    return out[:80]


def _job_to_dict(job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "title": job.title or job.job_id,
        "project_id": job.project_id,
        "scene_id": job.scene_id,
        "scene_image_url": _file_url(job.scene_image_path),
        "prompt": job.prompt,
        "image_model": job.image_model,
        "movement_prompt": job.movement_prompt,
        "images_per_character": job.images_per_character,
        "videos_per_character": job.videos_per_character,
        "compacted": job.compacted,
        "created_at": job.created_at.isoformat() + "Z",
        "updated_at": job.updated_at.isoformat() + "Z",
        "characters": {
            cid: {
                "char_id": jc.char_id,
                "name": jc.name,
                "source_image_url": _file_url(jc.source_image_path),
                "status": jc.status.value,
                "approved_variant_id": jc.approved_variant_id,
                "error": jc.error,
                "images": [
                    {
                        "variant_id": v.variant_id,
                        "url": _file_url(v.path),
                        "prompt": v.prompt,
                        "parent_variant_id": v.parent_variant_id,
                        "status": v.status,
                        "error": v.error,
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
    yield


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
app.mount("/files/characters",
          StaticFiles(directory=str(settings.characters_dir)),
          name="files-characters")


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
        s.save()
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
                "is_approved": v.variant_id == jc.approved_variant_id,
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
    name: str


@app.patch("/api/characters/{char_id}")
async def rename_character(char_id: str, body: RenameCharacterBody) -> dict:
    s = store()
    asset = s.get_character(char_id)
    if asset is None:
        raise HTTPException(404, "Character not found")
    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(400, "Empty name")
    asset.name = new_name
    # Retroactive: walk every job and update snapshot names where char_id matches.
    for job in s.state.jobs.values():
        if char_id in job.characters:
            job.characters[char_id].name = new_name
    s.save()
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
    s.save()
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
    scene_id: str
    character_ids: list[str]
    images_per_character: int = Field(default=1, ge=1, le=4)
    title: str | None = None
    project_id: str | None = None
    prompt: str | None = None
    image_model: str | None = None


async def _run_async(coro_fn, *args, **kwargs) -> None:
    await coro_fn(*args, **kwargs)


@app.post("/api/jobs")
async def create_job(body: CreateJobBody, background: BackgroundTasks) -> dict:
    settings.require_keys("openai")
    s = store()
    scene = s.get_scene(body.scene_id)
    if scene is None:
        raise HTTPException(404, "Scene not found")
    if not body.character_ids:
        raise HTTPException(400, "At least one character_id required")
    scene_path = settings.scenes_dir / scene.filename
    if not scene_path.exists():
        raise HTTPException(500, f"Scene file missing on disk: {scene_path}")

    if body.project_id is not None and s.get_project(body.project_id) is None:
        raise HTTPException(404, f"Project not found: {body.project_id}")

    job_id = "j_" + secrets.token_hex(5)
    chars: dict[str, JobCharacter] = {}
    char_names: list[str] = []
    for cid in body.character_ids:
        ch = s.get_character(cid)
        if ch is None:
            raise HTTPException(404, f"Character not found: {cid}")
        src = settings.characters_dir / ch.filename
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

    job = Job(
        job_id=job_id,
        title=title,
        project_id=body.project_id,
        scene_id=body.scene_id,
        scene_image_path=str(scene_path),
        characters=chars,
        images_per_character=body.images_per_character,
        prompt=custom_prompt,
        image_model=image_model,
    )
    s.add_job(job)
    background.add_task(_run_async, runner.run_image_generation, job_id)
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
    s.state.jobs.pop(job_id, None)
    s.save()
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
    if job.movement_prompt:
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
        jc.approved_variant_id = body.variant_id
        jc.status = CharStatus.APPROVED
        jc.updated_at = datetime.utcnow()
        job.characters[body.char_id] = jc
        s.update_job(job)
        await events.publish(job_id, {"kind": "char.approved", "job_id": job_id,
                                      "char_id": body.char_id,
                                      "variant_id": body.variant_id})
    elif body.action == "reject":
        jc.status = CharStatus.REJECTED
        jc.approved_variant_id = None
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
    if jc.approved_variant_id == variant_id:
        jc.approved_variant_id = None
    if not jc.images:
        jc.status = CharStatus.FAILED
        jc.error = "all variants deleted; click regenerate to re-run"
    elif jc.approved_variant_id is None:
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
    prompt: str
    videos_per_character: int = Field(default=1, ge=1, le=4)


@app.post("/api/jobs/{job_id}/movement")
async def set_movement(job_id: str, body: MovementBody,
                       background: BackgroundTasks) -> dict:
    settings.require_keys("xai")
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if job.movement_prompt:
        raise HTTPException(409, "Movement prompt already set")
    prompt = body.prompt.strip()
    if not prompt:
        raise HTTPException(400, "Movement prompt is empty")
    approved = [jc for jc in job.characters.values() if jc.status == CharStatus.APPROVED]
    if not approved:
        raise HTTPException(409, "No approved characters to animate")
    job.movement_prompt = prompt
    job.videos_per_character = body.videos_per_character
    job.updated_at = datetime.utcnow()
    s.update_job(job)
    await events.publish(job_id, {"kind": "movement.set", "job_id": job_id,
                                  "prompt": prompt,
                                  "videos_per_character": body.videos_per_character})
    background.add_task(_run_async, runner.run_video_synthesis, job_id)
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
        keep_variant = jc.approved_variant_id
        remaining_images: list[GeneratedImage] = []
        for v in jc.images:
            if v.variant_id == keep_variant:
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
    scene = s.get_scene(src.scene_id)
    if scene is None:
        raise HTTPException(409, "Source scene no longer exists; cannot duplicate")
    scene_path = settings.scenes_dir / scene.filename
    if not scene_path.exists():
        raise HTTPException(500, f"Scene file missing on disk: {scene_path}")

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
        scene_id=src.scene_id,
        scene_image_path=str(scene_path),
        characters=new_chars,
        images_per_character=src.images_per_character,
    )
    s.add_job(new_job)
    background.add_task(_run_async, runner.run_image_generation, new_id)
    return _job_to_dict(new_job)


class RetryVideoBody(BaseModel):
    char_id: str
    video_id: str


@app.post("/api/jobs/{job_id}/retry_video")
async def retry_video(job_id: str, body: RetryVideoBody,
                      background: BackgroundTasks) -> dict:
    settings.require_keys("xai")
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    if not job.movement_prompt:
        raise HTTPException(409, "Job has no movement prompt yet")
    jc = job.characters.get(body.char_id)
    if jc is None:
        raise HTTPException(404, "Character not in job")
    target = next((v for v in jc.videos if v.video_id == body.video_id), None)
    if target is None:
        raise HTTPException(404, "Video not found on this character")
    if target.status not in {VideoStatus.FAILED, VideoStatus.ERROR}:
        raise HTTPException(409,
                            f"Video is '{target.status}', only failed/error can retry")
    background.add_task(
        _run_async, runner.retry_one_video, job_id, body.char_id, body.video_id,
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
    for jc in job.characters.values():
        jc.videos = []
        if jc.status in {CharStatus.ANIMATING, CharStatus.DONE, CharStatus.FAILED} \
                and jc.approved_variant_id:
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
        return {
            "slug": slug,
            "label": info["label"],
            "provider": provider,
            "available": settings.has_provider(provider),
        }
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
async def get_swap_defaults() -> dict:
    """Defaults for the Swap-tab Step-2 form (prompt + model)."""
    from character_swap import pipeline
    return {
        "prompt": pipeline.GENERATION_PROMPT,
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


@app.get("/api/editor/templates")
async def editor_templates() -> list[dict]:
    from character_swap import video_edit
    out = []
    for slug, style in video_edit.TEMPLATES.items():
        out.append({
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
        })
    return out


@app.post("/api/editor/trim_silences")
async def editor_trim_silences(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    threshold_db: float = Form(-30.0),
    min_silence_secs: float = Form(0.4),
    pad_secs: float = Form(0.05),
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
    threshold_db: float = Form(-30.0),
    min_silence_secs: float = Form(0.4),
    pad_secs: float = Form(0.05),
    voice_id: str | None = Form(None),     # ElevenLabs voice_id for voice swap (optional)
    template: str = Form("popout-yellow"),
    overrides: str | None = Form(None),
    enable_trim: bool = Form(True),        # opt-out of auto silence-trim
    enable_captions: bool = Form(True),    # opt-out of caption burn-in
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

    # Step 1: trim silences (optional)
    current = src
    trim_summary: dict | None = None
    if enable_trim:
        trimmed = edit_dir / "01-trimmed.mp4"
        try:
            trim_summary = await asyncio.to_thread(
                video_edit.trim_silences, src, trimmed,
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

    # Step 3: captions (optional)
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
                raise HTTPException(422, "No speech detected — nothing to caption")
            (edit_dir / "words.json").write_text(video_edit.words_to_json(words), encoding="utf-8")
            (edit_dir / "pre_caption.txt").write_text(str(current), encoding="utf-8")
            final_out = edit_dir / "03-final.mp4"
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
        "captions": cap_info,
        "rerender_available": bool(enable_captions),
    }


@app.post("/api/editor/multi_auto_edit")
async def editor_multi_auto_edit(
    files: list[UploadFile] = File(...),
    script: str = Form(...),
    threshold_db: float = Form(-30.0),
    min_silence_secs: float = Form(0.4),
    pad_secs: float = Form(0.05),
    voice_id: str | None = Form(None),
    template: str = Form("popout-yellow"),
    overrides: str | None = Form(None),
    enable_trim: bool = Form(True),
    enable_captions: bool = Form(True),
) -> dict:
    """Multi-clip auto-edit:
      1. Save each uploaded clip.
      2. Transcribe each with Whisper.
      3. Match each transcript to a position in the supplied script and
         reorder accordingly.
      4. Concat the clips in script order.
      5. Continue with the standard auto-edit pipeline:
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
    matching_summary = [{
        "clip_index": p["idx"],
        "score": p["score"],
        "unmatched": p["unmatched"],
        "transcript_preview": plain_transcripts[p["idx"]][:120],
    } for p in placements]

    # 4. Concat in script order.
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
        "trim": trim_summary,
        "voice_swap": swap_summary,
        "captions": cap_info,
        "rerender_available": bool(enable_captions),
    }


@app.post("/api/editor/rerender")
async def editor_rerender(
    edit_id: str = Form(...),
    template: str = Form("popout-yellow"),
    overrides: str | None = Form(None),
    trim_start_secs: float = Form(0.0),
    trim_end_secs: float = Form(0.0),   # 0 = until end
) -> dict:
    """Re-render captions (and optionally manually-trim) the pre-caption
    video produced by a previous /api/editor/auto_edit run. Reuses the cached
    transcript so no Whisper call is needed."""
    from character_swap import video_edit
    edit_dir = settings.output_dir / "editor" / edit_id
    words_path = edit_dir / "words.json"
    pre_caption_path_file = edit_dir / "pre_caption.txt"
    if not words_path.exists() or not pre_caption_path_file.exists():
        raise HTTPException(404, "Cached transcript missing — original auto-edit no longer re-renderable")

    pre_caption = Path(pre_caption_path_file.read_text(encoding="utf-8").strip())
    if not pre_caption.exists():
        raise HTTPException(404, "Cached pre-caption video missing on disk")

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


# --- B-roll generation (audio → cinematic medical-realism clips → final mp4) -----

@app.post("/api/broll/generate")
async def broll_generate(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    video_model: str = Form("grok-imagine"),
) -> dict:
    """Kick off a full B-roll generation from a narration audio file.
    Returns a `broll_id` immediately; the actual work happens in a
    background task. Poll `GET /api/broll/{broll_id}` for progress."""
    from character_swap import broll as broll_mod, runner_broll

    if not settings.openai_api_key:
        raise HTTPException(503, "OpenAI API key required for transcription + planning")

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
        "xai_key": bool(settings.xai_api_key),
        "gemini_key": bool(settings.gemini_api_key),
        "kling_key": bool(settings.kling_access_key and settings.kling_secret_key),
        "heygen_key": bool(settings.heygen_api_key),
        "elevenlabs_key": bool(settings.elevenlabs_api_key),
    }


@app.exception_handler(404)
async def not_found(_, exc):
    detail = str(exc.detail) if hasattr(exc, "detail") else "not found"
    return JSONResponse(status_code=404, content={"error": detail})
