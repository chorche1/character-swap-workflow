"""
Background job runner — orchestrates per-character multi-variant image generation,
edit, and multi-video animation. Emits events on every state change so the
WebSocket layer can broadcast them.
"""
from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from pathlib import Path

from character_swap import events, pipeline
from character_swap.clients import grok
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)
from character_swap.state import store


def _output_dir(job_id: str, char_id: str) -> Path:
    return settings.output_dir / job_id / char_id


def _short(prefix: str = "") -> str:
    return prefix + secrets.token_hex(3)


def _persist(job: Job, jc: JobCharacter, *, status: CharStatus | None = None,
             **fields) -> JobCharacter:
    if status is not None:
        jc.status = status
    for k, v in fields.items():
        setattr(jc, k, v)
    jc.updated_at = datetime.utcnow()
    job.characters[jc.char_id] = jc
    store().update_job(job)
    return jc


async def _emit(job_id: str, kind: str, char_id: str | None = None, **data) -> None:
    payload = {"kind": kind, "job_id": job_id, "ts": datetime.utcnow().isoformat() + "Z"}
    if char_id is not None:
        payload["char_id"] = char_id
    payload.update(data)
    await events.publish(job_id, payload)


# --- image generation -----------------------------------------------------------------

def _scene_path_for_variant(job: Job, variant: GeneratedImage) -> Path:
    """Look up the scene image path for this variant. Variants carry
    their own `scene_id` (when generated under multi-scene support);
    fall back to the job's single scene_image_path for legacy variants
    without scene_id."""
    if variant.scene_id and job.scene_ids and job.scene_image_paths:
        try:
            i = list(job.scene_ids).index(variant.scene_id)
            return Path(job.scene_image_paths[i])
        except ValueError:
            pass
    return Path(job.scene_image_path)


async def _generate_one_variant(
    job: Job, jc: JobCharacter, variant: GeneratedImage,
    sem: asyncio.Semaphore,
) -> None:
    async with sem:
        # Promote char status the first time we actually start work.
        if jc.status == CharStatus.QUEUED:
            _persist(job, jc, status=CharStatus.GENERATING)
            await _emit(job.job_id, "char.generating", char_id=jc.char_id)
        await _emit(job.job_id, "variant.started",
                    char_id=jc.char_id, variant_id=variant.variant_id)
        dest = Path(variant.path)
        try:
            await asyncio.to_thread(
                pipeline.generate_variant,
                model=job.image_model,
                scene_image=_scene_path_for_variant(job, variant),
                character_image=Path(jc.source_image_path),
                character_name=jc.name,
                prompt=variant.prompt,
                dest=dest,
                job_id=job.job_id,
            )
        except Exception as e:
            variant.status = VariantStatus.FAILED
            variant.error = f"{type(e).__name__}: {e}"
            _replace_variant(job, jc, variant)
            await _emit(job.job_id, "variant.failed",
                        char_id=jc.char_id, variant_id=variant.variant_id,
                        error=variant.error)
            # If all variants failed, mark char failed.
            if all(v.status == VariantStatus.FAILED for v in jc.images):
                _persist(job, jc, status=CharStatus.FAILED,
                         error="all variants failed")
            return

        variant.status = VariantStatus.READY
        _replace_variant(job, jc, variant)
        # First successful variant flips char to AWAITING_APPROVAL so user can act early.
        if jc.status != CharStatus.AWAITING_APPROVAL:
            _persist(job, jc, status=CharStatus.AWAITING_APPROVAL)
        await _emit(job.job_id, "variant.ready",
                    char_id=jc.char_id, variant_id=variant.variant_id,
                    path=variant.path)


def _replace_variant(job: Job, jc: JobCharacter, variant: GeneratedImage) -> None:
    """Replace a variant in jc.images by variant_id and persist."""
    for i, v in enumerate(jc.images):
        if v.variant_id == variant.variant_id:
            jc.images[i] = variant
            break
    else:
        jc.images.append(variant)
    _persist(job, jc)


async def _kick_char(job: Job, jc: JobCharacter, n: int, sem: asyncio.Semaphore) -> None:
    """Reset a character and start N fresh variants per scene.

    When the job has multiple scene_ids, we generate `n` variants for
    each scene — so the total per-character variant count is `n × len(scene_ids)`.
    Each placeholder records which scene it belongs to via `scene_id`, so
    the runner picks the right reference image and the UI can group
    results per scene.
    """
    jc.images = []
    jc.videos = []
    jc.approved_variant_id = None
    jc.error = None
    _persist(job, jc, status=CharStatus.QUEUED)

    # Effective scene list — multi-scene jobs use `scene_ids`; legacy
    # single-scene jobs fall back to a 1-item list.
    scene_ids = list(job.scene_ids) if job.scene_ids else [job.scene_id]

    # Create N×scenes placeholder variants in `generating` state up front
    # so the UI can show all skeleton cards immediately.
    placeholders: list[GeneratedImage] = []
    for sid in scene_ids:
        for _ in range(n):
            variant_id = _short("v_")
            path = _output_dir(job.job_id, jc.char_id) / f"variant_{variant_id}.png"
            v = GeneratedImage(
                variant_id=variant_id,
                path=str(path),
                prompt=job.prompt or pipeline.GENERATION_PROMPT,
                scene_id=sid,
                status=VariantStatus.GENERATING,
            )
            placeholders.append(v)
            jc.images.append(v)
    _persist(job, jc)
    await _emit(job.job_id, "char.queued", char_id=jc.char_id,
                images_per_character=n,
                n_scenes=len(scene_ids))

    await asyncio.gather(
        *[_generate_one_variant(job, jc, v, sem) for v in placeholders]
    )


async def retry_single_variant(job_id: str, char_id: str, variant_id: str) -> None:
    """Re-run image gen for ONE specific (already-failed) variant slot.

    Unlike `run_image_generation` which wipes all variants for the
    character, this keeps the other (possibly successful) variants intact
    and only re-attempts the failed slot. The variant_id is preserved so
    the UI swaps it in place without losing scroll position.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None or job.movement_prompt:
        return
    jc = job.characters.get(char_id)
    if jc is None:
        return
    target = next((v for v in jc.images if v.variant_id == variant_id), None)
    if target is None:
        return
    # Reset the slot to GENERATING + clear any prior error
    target.status = VariantStatus.GENERATING
    target.error = None
    _replace_variant(job, jc, target)
    if jc.status in {CharStatus.FAILED, CharStatus.AWAITING_APPROVAL}:
        _persist(job, jc, status=CharStatus.GENERATING, error=None)
    await _emit(job_id, "variant.started",
                char_id=char_id, variant_id=variant_id)
    sem = asyncio.Semaphore(max(1, settings.image_concurrency))
    await _generate_one_variant(job, jc, target, sem)


async def run_image_generation(job_id: str, char_ids: list[str] | None = None) -> None:
    """Kick off N variants for the listed characters (or every non-progressing char)."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return
    if job.movement_prompt:
        # Approvals locked after movement submission; refuse to disrupt.
        return

    n = max(1, min(4, job.images_per_character))
    targets: list[JobCharacter] = []
    for cid, jc in job.characters.items():
        if char_ids is not None and cid not in char_ids:
            continue
        if jc.status in {CharStatus.ANIMATING, CharStatus.DONE}:
            continue
        targets.append(jc)
    if not targets:
        return

    sem = asyncio.Semaphore(max(1, settings.image_concurrency))
    await asyncio.gather(*[_kick_char(job, jc, n, sem) for jc in targets])


# --- edit ---------------------------------------------------------------------------

async def run_edit_variant(
    job_id: str, char_id: str, parent_variant_id: str, custom_prompt: str,
) -> None:
    s = store()
    job = s.get_job(job_id)
    if job is None or job.movement_prompt:
        return
    jc = job.characters.get(char_id)
    if jc is None:
        return
    parent = next((v for v in jc.images if v.variant_id == parent_variant_id), None)
    if parent is None or not Path(parent.path).exists():
        return

    variant_id = _short("v_")
    dest = _output_dir(job.job_id, jc.char_id) / f"edit_{variant_id}.png"
    variant = GeneratedImage(
        variant_id=variant_id,
        path=str(dest),
        prompt=custom_prompt,
        parent_variant_id=parent_variant_id,
        # Inherit the parent's scene anchor so the edit groups under the
        # same scene in the UI gallery.
        scene_id=parent.scene_id,
        status=VariantStatus.GENERATING,
    )
    jc.images.append(variant)
    _persist(job, jc)
    await _emit(job_id, "variant.started",
                char_id=char_id, variant_id=variant_id,
                parent_variant_id=parent_variant_id)

    try:
        await asyncio.to_thread(
            pipeline.edit_image,
            source_image=Path(parent.path),
            custom_prompt=custom_prompt,
            character_name=jc.name,
            dest=dest,
            job_id=job_id,
        )
    except Exception as e:
        variant.status = VariantStatus.FAILED
        variant.error = f"{type(e).__name__}: {e}"
        _replace_variant(job, jc, variant)
        await _emit(job_id, "variant.failed",
                    char_id=char_id, variant_id=variant_id, error=variant.error)
        return

    variant.status = VariantStatus.READY
    _replace_variant(job, jc, variant)
    await _emit(job_id, "variant.ready",
                char_id=char_id, variant_id=variant_id, path=str(dest))


# --- video synthesis ----------------------------------------------------------------

async def _animate_one_video(
    job: Job, jc: JobCharacter, video: VideoVariant, movement_prompt: str,
) -> None:
    await _emit(job.job_id, "video.started",
                char_id=jc.char_id, video_id=video.video_id)

    # Submit
    try:
        approved = next(
            (v for v in jc.images if v.variant_id == jc.approved_variant_id), None
        )
        if approved is None:
            raise grok.GrokError("approved variant missing on disk")
        grok_job_id = await asyncio.to_thread(
            pipeline.submit_video,
            image=Path(approved.path),
            movement_prompt=movement_prompt,
            character_name=jc.name,
            job_id=job.job_id,
        )
    except grok.GrokError as e:
        video.status = VideoStatus.ERROR
        video.error = f"submit: {e}"
        _replace_video(job, jc, video)
        await _emit(job.job_id, "video.failed",
                    char_id=jc.char_id, video_id=video.video_id, error=str(e))
        _maybe_complete_char(job, jc)
        return

    video.grok_job_id = grok_job_id
    _replace_video(job, jc, video)
    await _emit(job.job_id, "video.submitted",
                char_id=jc.char_id, video_id=video.video_id, grok_job_id=grok_job_id)

    loop = asyncio.get_running_loop()
    dest = _output_dir(job.job_id, jc.char_id) / f"video_{video.video_id}.mp4"

    def _progress(status: str, url: str | None) -> None:
        # Grok may send intermediate states we don't enumerate (e.g. "queued",
        # "running"). Bucket those into PROCESSING so video.status stays a valid
        # VideoStatus.
        try:
            new_status = VideoStatus(status)
        except ValueError:
            new_status = VideoStatus.PROCESSING
        if video.status != new_status:
            video.status = new_status
            video.download_url = url
            _replace_video(job, jc, video)
        events.publish_threadsafe(
            loop, job.job_id,
            {"kind": "video.progress", "job_id": job.job_id, "char_id": jc.char_id,
             "video_id": video.video_id, "status": new_status.value,
             "ts": datetime.utcnow().isoformat() + "Z"},
        )

    try:
        await asyncio.to_thread(
            pipeline.wait_for_video,
            job_id=grok_job_id,
            character_name=jc.name,
            dest=dest,
            on_progress=_progress,
            app_job_id=job.job_id,
        )
    except grok.GrokError as e:
        video.status = VideoStatus.ERROR
        video.error = str(e)
        _replace_video(job, jc, video)
        await _emit(job.job_id, "video.failed",
                    char_id=jc.char_id, video_id=video.video_id, error=str(e))
        _maybe_complete_char(job, jc)
        return

    video.status = VideoStatus.DONE
    video.completed_at = datetime.utcnow()
    video.final_video_path = str(dest)
    _replace_video(job, jc, video)
    await _emit(job.job_id, "video.ready",
                char_id=jc.char_id, video_id=video.video_id, path=str(dest))
    _maybe_complete_char(job, jc)


def _replace_video(job: Job, jc: JobCharacter, video: VideoVariant) -> None:
    for i, v in enumerate(jc.videos):
        if v.video_id == video.video_id:
            jc.videos[i] = video
            break
    else:
        jc.videos.append(video)
    _persist(job, jc)


_VIDEO_TERMINAL = {VideoStatus.DONE, VideoStatus.FAILED, VideoStatus.ERROR}


def _maybe_complete_char(job: Job, jc: JobCharacter) -> None:
    if not jc.videos:
        return
    if all(v.status in _VIDEO_TERMINAL for v in jc.videos):
        any_ok = any(v.status == VideoStatus.DONE for v in jc.videos)
        _persist(
            job, jc,
            status=CharStatus.DONE if any_ok else CharStatus.FAILED,
        )


async def _animate_character(
    job: Job, jc: JobCharacter, movement_prompt: str, m_videos: int,
) -> None:
    if jc.approved_variant_id is None:
        return
    _persist(job, jc, status=CharStatus.ANIMATING)

    placeholders: list[VideoVariant] = []
    for _ in range(m_videos):
        vid = _short("vd_")
        v = VideoVariant(
            video_id=vid,
            grok_job_id="",
            status=VideoStatus.PENDING,
            source_variant_id=jc.approved_variant_id,
        )
        jc.videos.append(v)
        placeholders.append(v)
    _persist(job, jc)

    await asyncio.gather(
        *[_animate_one_video(job, jc, v, movement_prompt) for v in placeholders]
    )


async def retry_one_video(job_id: str, char_id: str, video_id: str) -> None:
    """Re-submit a single failed video. Replaces the entry in-place with a fresh
    `VideoVariant` and re-runs `_animate_one_video`."""
    s = store()
    job = s.get_job(job_id)
    if job is None or not job.movement_prompt:
        return
    jc = job.characters.get(char_id)
    if jc is None or jc.approved_variant_id is None:
        return
    idx = next((i for i, v in enumerate(jc.videos) if v.video_id == video_id), None)
    if idx is None:
        return
    if jc.videos[idx].status not in {VideoStatus.FAILED, VideoStatus.ERROR}:
        return

    fresh = VideoVariant(
        video_id=_short("vd_"),
        grok_job_id="",
        status=VideoStatus.PENDING,
        source_variant_id=jc.approved_variant_id,
    )
    jc.videos[idx] = fresh
    _persist(job, jc, status=CharStatus.ANIMATING)
    await _animate_one_video(job, jc, fresh, job.movement_prompt)


async def run_video_synthesis(job_id: str) -> None:
    s = store()
    job = s.get_job(job_id)
    if job is None or not job.movement_prompt:
        return
    m = max(1, min(4, job.videos_per_character))
    targets = [jc for jc in job.characters.values()
               if jc.status == CharStatus.APPROVED and jc.approved_variant_id]
    if not targets:
        return
    await asyncio.gather(
        *[_animate_character(job, jc, job.movement_prompt, m) for jc in targets]
    )


# --- resumption -----------------------------------------------------------------------

async def resume_pending(job_id: str) -> None:
    """
    After a server restart:
      - Stale `generating` image variants: mark failed (interrupted).
      - In-flight videos with a grok_job_id: re-poll & download.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return

    dirty = False
    for jc in job.characters.values():
        for v in jc.images:
            if v.status == VariantStatus.GENERATING:
                v.status = VariantStatus.FAILED
                v.error = "interrupted (server restart)"
                dirty = True
        if all(v.status == VariantStatus.FAILED for v in jc.images) and jc.images:
            jc.status = CharStatus.FAILED
            jc.error = "all variants failed"
    if dirty:
        s.update_job(job)

    for jc in job.characters.values():
        for v in jc.videos:
            if v.grok_job_id and v.status not in _VIDEO_TERMINAL:
                asyncio.create_task(_resume_video(job, jc, v))


async def _resume_video(job: Job, jc: JobCharacter, video: VideoVariant) -> None:
    loop = asyncio.get_running_loop()
    dest = _output_dir(job.job_id, jc.char_id) / f"video_{video.video_id}.mp4"
    await _emit(job.job_id, "video.resumed",
                char_id=jc.char_id, video_id=video.video_id,
                grok_job_id=video.grok_job_id)

    def _progress(status: str, url: str | None) -> None:
        events.publish_threadsafe(
            loop, job.job_id,
            {"kind": "video.progress", "job_id": job.job_id, "char_id": jc.char_id,
             "video_id": video.video_id, "status": status,
             "ts": datetime.utcnow().isoformat() + "Z"},
        )

    try:
        await asyncio.to_thread(
            pipeline.wait_for_video,
            job_id=video.grok_job_id,
            character_name=jc.name,
            dest=dest,
            on_progress=_progress,
            app_job_id=job.job_id,
        )
    except grok.GrokError as e:
        video.status = VideoStatus.ERROR
        video.error = str(e)
        _replace_video(job, jc, video)
        await _emit(job.job_id, "video.failed",
                    char_id=jc.char_id, video_id=video.video_id, error=str(e))
        _maybe_complete_char(job, jc)
        return

    video.status = VideoStatus.DONE
    video.completed_at = datetime.utcnow()
    video.final_video_path = str(dest)
    _replace_video(job, jc, video)
    await _emit(job.job_id, "video.ready",
                char_id=jc.char_id, video_id=video.video_id, path=str(dest))
    _maybe_complete_char(job, jc)
