from __future__ import annotations

import contextlib
import hashlib
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
    HTTPException,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from character_swap import call_log, events, runner
from character_swap.config import settings
from character_swap.models import (
    CharacterAsset,
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    ProjectAsset,
    SceneAsset,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)
from character_swap.state import store

ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
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


def _safe_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        raise HTTPException(400, f"Unsupported image type '{ext}'. Allowed: {sorted(ALLOWED_IMAGE_EXTS)}")
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
        "movement_prompt": job.movement_prompt,
        "images_per_character": job.images_per_character,
        "videos_per_character": job.videos_per_character,
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

@app.post("/api/characters")
async def upload_character(file: UploadFile) -> dict:
    ext = _safe_ext(file.filename or "")
    data = await _read_capped(file)
    if not data:
        raise HTTPException(400, "Empty upload")
    char_id = "ch_" + hashlib.sha256(data).hexdigest()[:10]
    dest = settings.characters_dir / f"{char_id}{ext}"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(dest)
    name = Path(file.filename or char_id).stem
    asset = CharacterAsset(char_id=char_id, filename=dest.name, name=name)
    store().add_character(asset)
    return {
        "char_id": char_id,
        "filename": dest.name,
        "url": _file_url(dest),
        "name": name,
    }


@app.get("/api/characters")
async def list_characters() -> list[dict]:
    return [
        {
            "char_id": ch.char_id,
            "name": ch.name,
            "filename": ch.filename,
            "url": _file_url(settings.characters_dir / ch.filename),
        }
        for ch in store().list_characters()
    ]


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
    return {
        "char_id": asset.char_id,
        "name": asset.name,
        "filename": asset.filename,
        "url": _file_url(settings.characters_dir / asset.filename),
    }


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
    job = Job(
        job_id=job_id,
        title=title,
        project_id=body.project_id,
        scene_id=body.scene_id,
        scene_image_path=str(scene_path),
        characters=chars,
        images_per_character=body.images_per_character,
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


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "version": "0.4.0",
        "openai_key": bool(settings.openai_api_key),
        "xai_key": bool(settings.xai_api_key),
    }


@app.exception_handler(404)
async def not_found(_, exc):
    detail = str(exc.detail) if hasattr(exc, "detail") else "not found"
    return JSONResponse(status_code=404, content={"error": detail})
