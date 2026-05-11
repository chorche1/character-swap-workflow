from __future__ import annotations

import contextlib
import hashlib
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

from character_swap import events, runner
from character_swap.config import settings
from character_swap.models import (
    CharacterAsset,
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    SceneAsset,
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


async def _save_upload(upload: UploadFile, dest: Path) -> bytes:
    data = await upload.read()
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

app.mount(
    "/files",
    StaticFiles(directory=str(settings.project_root)),
    name="files",
)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(settings.web_dir / "index.html")


@app.get("/app.js")
async def app_js() -> FileResponse:
    return FileResponse(settings.web_dir / "app.js")


# --- scenes --------------------------------------------------------------------------

@app.post("/api/scenes")
async def upload_scene(file: UploadFile) -> dict:
    ext = _safe_ext(file.filename or "")
    scene_id = _short_id("sc_")
    dest = settings.scenes_dir / f"{scene_id}{ext}"
    await _save_upload(file, dest)
    scene = SceneAsset(
        scene_id=scene_id,
        filename=dest.name,
        original_name=file.filename or dest.name,
    )
    store().add_scene(scene)
    return {
        "scene_id": scene_id,
        "filename": dest.name,
        "url": _file_url(dest),
        "original_name": scene.original_name,
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
    data = await file.read()
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
    asset = store().remove_character(char_id)
    if asset is None:
        raise HTTPException(404, "Character not found")
    store().save()
    with contextlib.suppress(OSError):
        (settings.characters_dir / asset.filename).unlink(missing_ok=True)
    return {"ok": True}


# --- jobs ----------------------------------------------------------------------------

class CreateJobBody(BaseModel):
    scene_id: str
    character_ids: list[str]
    images_per_character: int = Field(default=1, ge=1, le=4)
    title: str | None = None


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


class RenameJobBody(BaseModel):
    title: str


@app.patch("/api/jobs/{job_id}")
async def rename_job(job_id: str, body: RenameJobBody) -> dict:
    s = store()
    job = s.get_job(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(400, "Empty title")
    job.title = new_title
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
        if match.status != "ready":
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
