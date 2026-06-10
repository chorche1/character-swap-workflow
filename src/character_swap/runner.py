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

from character_swap import events, pipeline, swap_qc, video_qc
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


def _ensure_end_frame_swap(job: Job, jc: JobCharacter, scene_id, pose_path: str,
                           *, force: bool = False) -> Path:
    """Swap this character into the uploaded END-POSE reference so a scene's
    Kling end frame features the SAME character. The swapped frame is cached on
    disk per (char, scene) so all of that scene's videos reuse it. Returns the
    Path on success.

    RAISES on failure — the caller records the message on
    `JobCharacter.end_frame_errors[scene_id]` and surfaces it via an event. We
    NEVER swallow end-frame errors here: a bare `except: return None` swallow
    (zero user feedback on a content-policy block) is exactly why the first
    version of this feature was reverted. `pipeline.generate_variant` already
    retries a rejection with a softened prompt on the chosen model, so by the
    time this raises the swap is genuinely unrecoverable."""
    safe_scene = str(scene_id or "scene").replace("/", "_")
    out_dir = _output_dir(job.job_id, jc.char_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"endframe_{safe_scene}.png"
    if dest.exists() and not force:
        return dest
    pipeline.generate_variant(
        model=_swap_image_model(job),
        scene_image=Path(pose_path),
        character_image=Path(jc.source_image_path),
        character_name=jc.name,
        prompt=job.prompt or pipeline.GENERATION_PROMPT,
        dest=dest,
        job_id=job.job_id,
    )
    if not dest.exists():
        raise RuntimeError("end-frame swap produced no output file")
    return dest


# --- image generation -----------------------------------------------------------------

def _is_gemini_image_model(slug: str) -> bool:
    """True if `slug` is a Google/Gemini image model (Nano Banana family).
    Looked up from the model registry so it stays correct if Google adds more.
    Lazy import to avoid an import cycle with runner_media."""
    try:
        from character_swap.runner_media import IMAGE_MODELS
        info = IMAGE_MODELS.get((slug or "").strip())
        return bool(info and info.get("provider") == "gemini")
    except Exception:
        # Fallback to the known Google slugs if the registry can't be read.
        return (slug or "").strip() in {"nano-banana", "nano-banana-pro"}


# Swap models retired from the picker whose stored jobs must not regenerate
# through them. higgsfield-swap: Soul regenerates an unrelated scene (the
# 2026-06-10 bake-off scored it 2.5-3.3 with fatal flaws on every output).
_RETIRED_SWAP_MODELS = {"higgsfield-swap"}


def _swap_image_model(job: Job) -> str:
    """Effective image model for SWAP generation.

    Google-DIRECT (Gemini-key) models were removed from the Swap picker, so
    Swap must never generate via the Gemini API — even for jobs created
    BEFORE the removal that still carry `image_model="nano-banana-pro"`.
    (The fal-hosted nbp-swap/nb2-swap slugs are allowed: provider "fal",
    different quota + billing.) Retired models (higgsfield-swap) are coerced
    for the same reason: regenerating an old job through them reproduces a
    known-bad failure mode. Both coerce to the default `gpt-image`."""
    m = (job.image_model or "gpt-image").strip()
    if _is_gemini_image_model(m) or m in _RETIRED_SWAP_MODELS:
        return "gpt-image"
    return m


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
        extra_ref: Path | None = None
        if job.extra_reference_path:
            candidate = Path(job.extra_reference_path)
            if candidate.exists():
                extra_ref = candidate

        # Generate → vision-QC → regenerate-with-corrective-hint loop. QC
        # checks identity (right person?) + obvious defects; a failed verdict
        # re-runs the slot with the judge's hint appended, up to
        # swap_qc_max_retries extra attempts. QC unavailable → single attempt,
        # qc_status="skipped". Exhausted retries KEEP the last image (a
        # false-positive judge must not destroy a usable variant) with a ⚠
        # qc_status="failed" for the UI.
        scene_path = _scene_path_for_variant(job, variant)
        char_path = Path(jc.source_image_path)
        max_attempts = 1 + max(0, settings.swap_qc_max_retries)
        # Per-attempt inputs. After a QC failure the FIRST retry runs in
        # repair mode: the failed image itself becomes the scene input with a
        # fix-only-this instruction, so the result changes as little as
        # possible. A second failure falls back to a fresh re-roll from the
        # original scene with the judge's hint appended. (grok-image is
        # text-only — repair mode would be ignored, so it re-rolls directly.)
        attempt_scene = scene_path
        prompt = variant.prompt
        verdict = None
        try:
            for attempt in range(1, max_attempts + 1):
                variant.qc_attempts = attempt
                await asyncio.to_thread(
                    pipeline.generate_variant,
                    model=_swap_image_model(job),
                    scene_image=attempt_scene,
                    character_image=char_path,
                    character_name=jc.name,
                    prompt=prompt,
                    dest=dest,
                    job_id=job.job_id,
                    extra_reference_image=extra_ref,
                )
                verdict = await asyncio.to_thread(
                    swap_qc.inspect_variant,
                    scene_image=scene_path,
                    character_image=char_path,
                    result_image=dest,
                    background_replaced=extra_ref is not None,
                    outfit_from_character=bool(job.prompt and
                                               "own outfit from Image 2" in job.prompt),
                    job_id=job.job_id,
                )
                if verdict is None:
                    variant.qc_status = "skipped"
                    variant.qc_reason = None
                    break
                if verdict.passed:
                    variant.qc_status = "passed"
                    variant.qc_reason = None
                    break
                variant.qc_status = "failed"
                variant.qc_reason = verdict.reason
                if attempt < max_attempts:
                    await _emit(job.job_id, "variant.qc_retry",
                                char_id=jc.char_id, variant_id=variant.variant_id,
                                attempt=attempt, reason=verdict.reason)
                    hint = (verdict.corrective_hint or verdict.reason or "").strip()
                    if attempt == 1 and _swap_image_model(job) != "grok-image":
                        # Repair mode: minimal-change edit of the failed image.
                        failed_copy = dest.with_name(dest.stem + ".qcfail.png")
                        failed_copy.write_bytes(dest.read_bytes())
                        attempt_scene = failed_copy
                        prompt = swap_qc.repair_prompt(hint)
                    else:
                        # Fresh re-roll from the original scene + hint.
                        attempt_scene = scene_path
                        prompt = variant.prompt + (
                            f"\nIMPORTANT — the previous attempt was rejected by "
                            f"quality control: {hint}" if hint else "")
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


def _parse_director_plan(job: Job):
    """Parse the cached SwapDirectorPlan JSON on the Job. Returns None if no
    plan is cached or parsing fails. Cached on `job.director_prompts_json`
    by `_maybe_run_director_swap`."""
    if not job.director_prompts_json:
        return None
    try:
        from character_swap.prompt_director import SwapDirectorPlan
        return SwapDirectorPlan.model_validate_json(job.director_prompts_json)
    except Exception:
        return None


async def _kick_char(job: Job, jc: JobCharacter, n: int, sem: asyncio.Semaphore) -> None:
    """Reset a character and start N fresh variants per scene.

    When the job has multiple scene_ids, we generate `n` variants for
    each scene — so the total per-character variant count is `n × len(scene_ids)`.
    Each placeholder records which scene it belongs to via `scene_id`, so
    the runner picks the right reference image and the UI can group
    results per scene.

    Prompt precedence (highest to lowest): per-variant from Director plan
    cache → `enriched_image_prompt` → `prompt` → `GENERATION_PROMPT`.
    """
    jc.images = []
    jc.videos = []
    jc.approved_variant_id = None
    jc.error = None
    _persist(job, jc, status=CharStatus.QUEUED)

    # Effective scene list — multi-scene jobs use `scene_ids`; legacy
    # single-scene jobs fall back to a 1-item list.
    scene_ids = list(job.scene_ids) if job.scene_ids else [job.scene_id]

    # Fallback prompt when Director plan is missing OR doesn't cover a slot.
    fallback_prompt = (job.enriched_image_prompt
                       or job.prompt
                       or pipeline.GENERATION_PROMPT)
    director_plan = _parse_director_plan(job)

    placeholders: list[GeneratedImage] = []
    for sid in scene_ids:
        # Pull this (char, scene)'s ordered per-variant prompts from the
        # Director cache, if present. Indexed by variant_index in plan; we
        # consume them in order. Missing entries fall back.
        director_variant_prompts: list[str] = (
            director_plan.lookup(jc.char_id, sid) if director_plan else []
        )
        for i in range(n):
            variant_id = _short("v_")
            path = _output_dir(job.job_id, jc.char_id) / f"variant_{variant_id}.png"
            tailored = (director_variant_prompts[i]
                        if i < len(director_variant_prompts) else None)
            v = GeneratedImage(
                variant_id=variant_id,
                path=str(path),
                prompt=tailored or fallback_prompt,
                scene_id=sid,
                status=VariantStatus.GENERATING,
            )
            placeholders.append(v)
            jc.images.append(v)
    _persist(job, jc)
    await _emit(job.job_id, "char.queued", char_id=jc.char_id,
                images_per_character=n,
                n_scenes=len(scene_ids),
                director_applied=bool(director_plan))

    await asyncio.gather(
        *[_generate_one_variant(job, jc, v, sem) for v in placeholders]
    )

    # End frames: for each scene with an uploaded end-pose ref, swap THIS
    # character into the pose so the scene's Kling 3.0 end frame features the
    # same person. Generated here (Step 3, alongside the variants — per Hugo)
    # so the user sees it before approving. Per-scene + best-effort: a failure
    # is RECORDED on `end_frame_errors` and emitted (never swallowed), and just
    # skips that one end frame.
    end_poses = {
        sid: pose for sid, pose in (job.end_frames_by_scene or {}).items()
        if pose and Path(pose).exists()
    }
    if end_poses:
        jc.end_frame_paths = dict(jc.end_frame_paths or {})
        jc.end_frame_errors = dict(jc.end_frame_errors or {})

        async def _gen_end(sid: str, pose: str) -> None:
            await _emit(job.job_id, "char.end_frame_started",
                        char_id=jc.char_id, scene_id=sid)
            try:
                async with sem:
                    out = await asyncio.to_thread(
                        _ensure_end_frame_swap, job, jc, sid, pose)
                jc.end_frame_paths[sid] = str(out)
                jc.end_frame_errors.pop(sid, None)
                await _emit(job.job_id, "char.end_frame_done",
                            char_id=jc.char_id, scene_id=sid)
            except Exception as e:  # noqa: BLE001 — surfaced, never swallowed
                jc.end_frame_errors[sid] = str(e)
                jc.end_frame_paths.pop(sid, None)
                await _emit(job.job_id, "char.end_frame_failed",
                            char_id=jc.char_id, scene_id=sid, error=str(e))

        await asyncio.gather(*[_gen_end(sid, pose)
                               for sid, pose in end_poses.items()])
        _persist(job, jc)


async def regen_scene_end_frames(job_id: str, scene_id: str) -> None:
    """Regenerate the END-FRAME swap for ONE scene across every character that
    already has variants. Used when the user sets/replaces a scene's end pose
    AFTER Step 3 has run (via the set-end-frame endpoint), so the preview end
    frame matches the new pose. `force=True` overwrites the cached swap. Errors
    are surfaced on `end_frame_errors` (never swallowed), same as Step 3."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return
    pose = (job.end_frames_by_scene or {}).get(scene_id)
    if not pose or not Path(pose).exists():
        return
    sem = asyncio.Semaphore(max(1, settings.image_concurrency))
    targets = [jc for jc in job.characters.values() if jc.images]

    async def _one(jc: JobCharacter) -> None:
        jc.end_frame_paths = dict(jc.end_frame_paths or {})
        jc.end_frame_errors = dict(jc.end_frame_errors or {})
        await _emit(job_id, "char.end_frame_started",
                    char_id=jc.char_id, scene_id=scene_id)
        try:
            async with sem:
                out = await asyncio.to_thread(
                    _ensure_end_frame_swap, job, jc, scene_id, pose, force=True)
            jc.end_frame_paths[scene_id] = str(out)
            jc.end_frame_errors.pop(scene_id, None)
            await _emit(job_id, "char.end_frame_done",
                        char_id=jc.char_id, scene_id=scene_id)
        except Exception as e:  # noqa: BLE001 — surfaced, never swallowed
            jc.end_frame_errors[scene_id] = str(e)
            jc.end_frame_paths.pop(scene_id, None)
            await _emit(job_id, "char.end_frame_failed",
                        char_id=jc.char_id, scene_id=scene_id, error=str(e))
        _persist(job, jc)

    await asyncio.gather(*[_one(jc) for jc in targets])


async def retry_single_variant(job_id: str, char_id: str, variant_id: str,
                               prompt: str | None = None) -> None:
    """Re-run image gen for ONE specific (already-failed) variant slot.

    Unlike `run_image_generation` which wipes all variants for the
    character, this keeps the other (possibly successful) variants intact
    and only re-attempts the failed slot. The variant_id is preserved so
    the UI swaps it in place without losing scroll position.

    `prompt` (optional) overrides the slot's stored prompt before retrying —
    lets the user edit the prompt that failed and regenerate in place.
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
    # Optional edited prompt → use it for this retry (and keep it on the slot).
    if prompt and prompt.strip():
        target.prompt = prompt.strip()
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


async def regen_scene_variants(job_id: str, char_id: str, scene_id: str,
                               prompt: str | None = None) -> None:
    """Generate N fresh variants for ONE (character, scene) pair, ADDING them
    to the character without wiping its other scenes' variants.

    This is the per-scene equivalent of `_kick_char`, but additive and scoped
    to a single scene — used to rebuild a scene whose variants were all
    deleted (or that produced none, e.g. a scene showing "0 variants"). It
    never touches the character's other scenes or its existing approvals.

    `n` follows `job.images_per_character`. Prompt precedence matches
    `_kick_char`: caller override → per-variant Director plan → enriched →
    `job.prompt` → `GENERATION_PROMPT`. Refuses once movement is submitted.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None or job.movement_prompt:
        return
    jc = job.characters.get(char_id)
    if jc is None:
        return
    # The scene must belong to this job.
    scene_ids = list(job.scene_ids) if job.scene_ids else [job.scene_id]
    if scene_id not in scene_ids:
        return

    n = max(1, min(4, job.images_per_character))
    override = prompt.strip() if (prompt and prompt.strip()) else None
    fallback_prompt = (override
                       or job.enriched_image_prompt
                       or job.prompt
                       or pipeline.GENERATION_PROMPT)
    director_plan = _parse_director_plan(job)
    director_variant_prompts = (
        director_plan.lookup(char_id, scene_id) if director_plan else []
    )

    placeholders: list[GeneratedImage] = []
    for i in range(n):
        variant_id = _short("v_")
        path = _output_dir(job.job_id, jc.char_id) / f"variant_{variant_id}.png"
        # A caller override wins; otherwise use the Director's per-variant
        # prompt for this scene when present, else the shared fallback.
        tailored = (director_variant_prompts[i]
                    if (override is None and i < len(director_variant_prompts))
                    else None)
        v = GeneratedImage(
            variant_id=variant_id,
            path=str(path),
            prompt=tailored or fallback_prompt,
            scene_id=scene_id,
            status=VariantStatus.GENERATING,
        )
        placeholders.append(v)
        jc.images.append(v)

    jc.error = None
    _persist(job, jc, status=CharStatus.GENERATING)
    await _emit(job_id, "char.queued", char_id=char_id,
                images_per_character=n, n_scenes=1, scene_id=scene_id)

    sem = asyncio.Semaphore(max(1, settings.image_concurrency))
    await asyncio.gather(
        *[_generate_one_variant(job, jc, v, sem) for v in placeholders]
    )


async def _maybe_run_director_swap(job: Job, s) -> None:
    """If `use_director=True` and the plan isn't cached yet, run a ONE-shot
    Claude Opus call to plan per-(char, scene, variant) prompts and cache
    the result as JSON on `job.director_prompts_json`. Silent no-op on any
    failure — `_kick_char` falls back to enrich/raw automatically."""
    if not job.use_director or job.director_prompts_json:
        return
    from pathlib import Path

    from character_swap import prompt_director
    n = max(1, min(4, job.images_per_character))

    # Build (char_id, name, path) tuples for every character in the job.
    chars = [
        (jc.char_id, jc.name, Path(jc.source_image_path))
        for jc in job.characters.values()
    ]
    # Multi-scene → list of (scene_id, scene_path). Legacy single-scene
    # collapses to a 1-tuple list.
    scene_ids = list(job.scene_ids) if job.scene_ids else [job.scene_id]
    scene_paths = (list(job.scene_image_paths) if job.scene_image_paths
                   else [job.scene_image_path])
    scenes = [(sid, Path(p)) for sid, p in zip(scene_ids, scene_paths)]
    if not chars or not scenes:
        return

    plan = await asyncio.to_thread(
        prompt_director.direct_swap,
        user_prompt=job.prompt or "",
        characters=chars,
        scenes=scenes,
        images_per_character=n,
        job_id=job.job_id,
    )
    if plan is None:
        return
    # Cache + persist so retries / resumes don't re-bill the Anthropic API.
    job.director_prompts_json = plan.model_dump_json()
    s.update_job(job)
    await _emit(job.job_id, "director.ready",
                n_chars=len(plan.characters),
                intent=plan.intent)


async def run_image_generation(job_id: str, char_ids: list[str] | None = None) -> None:
    """Kick off N variants for the listed characters (or every non-progressing char)."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return
    if job.movement_prompt:
        # Approvals locked after movement submission; refuse to disrupt.
        return

    # Coerce away any Google/Gemini model left on an older job (Swap no longer
    # offers them) and PERSIST it, so the Step-2 dropdown reflects the switch
    # and every downstream run uses the corrected model.
    coerced = _swap_image_model(job)
    if coerced != (job.image_model or ""):
        job.image_model = coerced
        s.update_job(job)

    # AI Director runs FIRST when enabled — its per-variant prompts override
    # both enrich and raw. Runs once per job; cached on the Job thereafter.
    await _maybe_run_director_swap(job, s)
    # Re-load job in case Director persisted the plan above.
    job = s.get_job(job_id) or job

    # Optional prompt enrichment for the swap flow. Only triggers when the
    # user provided a custom `job.prompt` (the GENERATION_PROMPT default is
    # already highly detailed and benefits little from expansion). When
    # Director succeeded, its per-variant prompts take precedence in
    # `_kick_char`; enrichment still runs as a fallback safety net.
    if job.enrich_prompt and job.prompt and not job.enriched_image_prompt:
        from character_swap import prompt_enrich
        enriched = await asyncio.to_thread(
            prompt_enrich.enrich_prompt, job.prompt, "swap", job_id=job.job_id,
        )
        if enriched and enriched != job.prompt:
            job.enriched_image_prompt = enriched
            s.update_job(job)

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
    duration_secs: int | None = None, end_image: Path | None = None,
) -> None:
    await _emit(job.job_id, "video.started",
                char_id=jc.char_id, video_id=video.video_id)

    # Submit. The `video_model` field on the Job (set in Step 4) chooses
    # which provider — defaults to grok-imagine. All providers fail through
    # the same VideoStatus.ERROR path so the UI doesn't need per-provider
    # handling.
    video_model = job.video_model or "grok-imagine"
    # Each VideoVariant remembers WHICH approved variant it animates via
    # `source_variant_id` — so multi-scene jobs (with multiple approved
    # variants per char) animate every approval in parallel and keep
    # their per-frame source mapping correct.
    target_variant_id = video.source_variant_id or jc.approved_variant_id
    approved = next(
        (v for v in jc.images if v.variant_id == target_variant_id), None
    )
    if approved is None:
        video.status = VideoStatus.ERROR
        video.error = "submit: approved variant missing on disk"
        _replace_video(job, jc, video)
        await _emit(job.job_id, "video.failed",
                    char_id=jc.char_id, video_id=video.video_id, error=video.error)
        _maybe_complete_char(job, jc)
        return

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

    # Generate → clip-QC → resubmit-with-corrective-hint loop. QC transcribes
    # the clip and compares against the prompt's expected dialogue (catches
    # garbled TTS like "baking goda") and vision-checks sampled frames for
    # impossible motion/anatomy. Video is the EXPENSIVE step → 1 retry by
    # default; QC unavailable → single attempt, qc_status="skipped"; exhausted
    # retries keep the last clip with qc_status="failed" (⚠ in UI).
    max_attempts = 1 + (max(0, settings.video_qc_max_retries)
                        if settings.video_qc_enabled else 0)
    prompt_text = movement_prompt
    phase = "submit"
    try:
        for attempt in range(1, max_attempts + 1):
            video.qc_attempts = attempt
            phase = "submit"
            provider_job_id = await asyncio.to_thread(
                pipeline.submit_video,
                image=Path(approved.path),
                movement_prompt=prompt_text,
                character_name=jc.name,
                job_id=job.job_id,
                model=video_model,
                duration_secs=duration_secs if duration_secs is not None else job.duration_secs,
                end_image=end_image,
                generate_audio=job.video_audio,
            )
            # `grok_job_id` is misnamed for non-grok providers but kept for DB
            # back-compat (it's the provider's external job/task id either way).
            video.grok_job_id = provider_job_id
            _replace_video(job, jc, video)
            await _emit(job.job_id, "video.submitted",
                        char_id=jc.char_id, video_id=video.video_id,
                        grok_job_id=provider_job_id)

            phase = "wait"
            await asyncio.to_thread(
                pipeline.wait_for_video,
                job_id=provider_job_id,
                character_name=jc.name,
                dest=dest,
                on_progress=_progress,
                app_job_id=job.job_id,
                model=video_model,
            )

            verdict = await asyncio.to_thread(
                video_qc.inspect_clip, dest,
                movement_prompt=prompt_text, app_job_id=job.job_id,
            )
            if verdict is None:
                video.qc_status = "skipped"
                video.qc_reason = None
                break
            if verdict.passed:
                video.qc_status = "passed"
                video.qc_reason = None
                break
            video.qc_status = "failed"
            video.qc_reason = verdict.reason
            if attempt < max_attempts:
                await _emit(job.job_id, "video.qc_retry",
                            char_id=jc.char_id, video_id=video.video_id,
                            attempt=attempt, reason=verdict.reason)
                hint = (verdict.corrective_hint or verdict.reason or "").strip()
                prompt_text = movement_prompt + (
                    f" IMPORTANT — the previous take was rejected by quality "
                    f"control: {hint}" if hint else "")
                video.status = VideoStatus.PROCESSING
                _replace_video(job, jc, video)
    except Exception as e:
        video.status = VideoStatus.ERROR
        video.error = f"submit: {e}" if phase == "submit" else str(e)
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
    job: Job, jc: JobCharacter, m_videos: int,
    prompt_for_scene,
) -> None:
    """Fan out animation across every approved variant in parallel.

    Per-scene prompts: `prompt_for_scene(scene_id) -> str` resolves the
    movement prompt for a given scene. Multi-scene jobs use a different
    prompt per scene so each scene's animation matches its intended action
    (e.g. scene 1 "pours oil", scene 2 "walks away"). Single-scene legacy
    jobs collapse to one prompt for all variants.
    """
    # Source-of-truth list; fall back to the legacy single field for jobs
    # created before the multi-approve migration.
    approved_ids = list(jc.approved_variant_ids or [])
    if not approved_ids and jc.approved_variant_id:
        approved_ids = [jc.approved_variant_id]
    if not approved_ids:
        return
    _persist(job, jc, status=CharStatus.ANIMATING)

    # Per-image overrides win over the per-scene prompt/duration (the
    # Higgsfield "per-slot" model). Empty/missing → fall back to the scene.
    by_variant = dict(job.movement_prompts_by_variant or {})
    dur_by_variant = dict(job.durations_by_variant or {})
    dur_by_scene = dict(job.durations_by_scene or {})
    end_by_scene = dict(job.end_frames_by_scene or {})

    placeholders: list[tuple[VideoVariant, str, int | None, Path | None]] = []
    for src_variant_id in approved_ids:
        variant = next((iv for iv in jc.images if iv.variant_id == src_variant_id), None)
        scene_id = variant.scene_id if variant else None
        prompt = by_variant.get(src_variant_id) or prompt_for_scene(scene_id)
        duration = (dur_by_variant.get(src_variant_id)
                    or dur_by_scene.get(scene_id)
                    or job.duration_secs)
        # Optional per-scene END FRAME (Kling 3.0 only): prefer the frame we
        # already generated in Step 3 (character swapped into the scene's end
        # pose); fall back to swapping now if it's missing. Errors are surfaced
        # on `end_frame_errors` + an event, never swallowed.
        end_image: Path | None = None
        if job.video_model == "kling-v3":
            pre = (jc.end_frame_paths or {}).get(scene_id)
            if pre and Path(pre).exists():
                end_image = Path(pre)
            else:
                end_pose = end_by_scene.get(scene_id)
                if end_pose and Path(end_pose).exists():
                    try:
                        end_image = await asyncio.to_thread(
                            _ensure_end_frame_swap, job, jc, scene_id, end_pose)
                    except Exception as e:  # noqa: BLE001 — surfaced, not swallowed
                        jc.end_frame_errors = dict(jc.end_frame_errors or {})
                        jc.end_frame_errors[scene_id] = str(e)
                        await _emit(job.job_id, "char.end_frame_failed",
                                    char_id=jc.char_id, scene_id=scene_id, error=str(e))
                        end_image = None
        for _ in range(m_videos):
            vid = _short("vd_")
            v = VideoVariant(
                video_id=vid,
                grok_job_id="",
                status=VideoStatus.PENDING,
                source_variant_id=src_variant_id,
            )
            jc.videos.append(v)
            placeholders.append((v, prompt, duration, end_image))
    _persist(job, jc)

    await asyncio.gather(
        *[_animate_one_video(job, jc, v, mp, dur, end_img)
          for v, mp, dur, end_img in placeholders]
    )


async def generate_more_videos(
    job_id: str, char_id: str, n: int,
    *,
    source_variant_id: str | None = None,
    prompt_override: str | None = None,
) -> None:
    """Append N more videos for an approved character — strictly additive
    (existing videos are left alone). Used by Step 5's "+ N more" button so
    Hugo can produce extra takes for a (char, scene) without wiping the
    initial batch or re-submitting the whole movement prompt.

    When `source_variant_id` is None, generates N videos for EACH of the
    char's approved variants (mirrors the initial-batch fan-out). When
    specified, generates N videos only for that specific approved variant.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None or not job.movement_prompt:
        return
    jc = job.characters.get(char_id)
    if jc is None:
        return

    approved_ids = list(jc.approved_variant_ids or [])
    if not approved_ids and jc.approved_variant_id:
        approved_ids = [jc.approved_variant_id]
    if source_variant_id is not None:
        if source_variant_id not in approved_ids:
            return
        approved_ids = [source_variant_id]
    if not approved_ids:
        return

    n = max(1, min(10, int(n)))
    primary_scene = _first_scene_id(job)
    movement_prompts = dict(job.movement_prompts or {})
    enriched_prompts = dict(job.enriched_movement_prompts or {})
    fallback = (job.enriched_movement_prompt or job.movement_prompt or "")

    def prompt_for(scene_id: str | None) -> str:
        sid = scene_id or primary_scene
        if sid is None:
            return fallback
        return (enriched_prompts.get(sid)
                or movement_prompts.get(sid)
                or fallback)

    by_variant = dict(job.movement_prompts_by_variant or {})
    dur_by_variant = dict(job.durations_by_variant or {})
    dur_by_scene = dict(job.durations_by_scene or {})

    placeholders: list[tuple[VideoVariant, str, int | None]] = []
    for src_id in approved_ids:
        variant = next((iv for iv in jc.images if iv.variant_id == src_id), None)
        scene_id = variant.scene_id if variant else None
        prompt = prompt_override or by_variant.get(src_id) or prompt_for(scene_id)
        duration = (dur_by_variant.get(src_id)
                    or dur_by_scene.get(scene_id)
                    or job.duration_secs)
        for _ in range(n):
            v = VideoVariant(
                video_id=_short("vd_"),
                grok_job_id="",
                status=VideoStatus.PENDING,
                source_variant_id=src_id,
                movement_prompt_override=prompt_override,
            )
            jc.videos.append(v)
            placeholders.append((v, prompt, duration))
    _persist(job, jc, status=CharStatus.ANIMATING)

    await asyncio.gather(
        *[_animate_one_video(job, jc, v, mp, dur) for v, mp, dur in placeholders]
    )


async def retry_one_video(job_id: str, char_id: str, video_id: str,
                          prompt_override: str | None = None) -> None:
    """Re-submit a single video. Replaces the entry in-place with a fresh
    `VideoVariant` (preserving `source_variant_id`) and re-runs
    `_animate_one_video`. Works on ANY status now — FAILED/ERROR for normal
    retries AND DONE for "I want a different take on this clip" regens.
    Critical for multi-approve jobs: the new video must re-target the SAME
    approved variant the old one was animating, not silently fall back.

    `prompt_override` lets the caller tweak the movement prompt for THIS video
    only (Step 5 regen flow). When None, falls back to the per-scene prompt
    on the job (current behavior). Persisted on the new VideoVariant so the
    UI's regen modal can pre-fill with the LAST iteration the user tried.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None or not job.movement_prompt:
        return
    jc = job.characters.get(char_id)
    if jc is None:
        return
    if not (jc.approved_variant_ids or jc.approved_variant_id):
        return
    idx = next((i for i, v in enumerate(jc.videos) if v.video_id == video_id), None)
    if idx is None:
        return
    # DONE/PROCESSING is now also retryable. Skip only if mid-flight to a
    # provider that hasn't returned (we'd leak a running Grok job otherwise).
    if jc.videos[idx].status == VideoStatus.PROCESSING:
        return

    # Inherit an existing override if the caller didn't supply a new one. This
    # makes "regenerate again with the same override" a one-click action.
    inherited_override = jc.videos[idx].movement_prompt_override
    effective_override = (prompt_override
                          if prompt_override is not None
                          else inherited_override)

    # Preserve which approved variant this video was animating — falls back
    # to the first approved one only if the original wasn't recorded.
    source_variant_id = (jc.videos[idx].source_variant_id
                         or (jc.approved_variant_ids[0] if jc.approved_variant_ids
                             else jc.approved_variant_id))
    fresh = VideoVariant(
        video_id=_short("vd_"),
        grok_job_id="",
        status=VideoStatus.PENDING,
        source_variant_id=source_variant_id,
        movement_prompt_override=effective_override,
    )
    jc.videos[idx] = fresh
    _persist(job, jc, status=CharStatus.ANIMATING)
    # Resolve the prompt: per-video override > enriched > raw per-scene > job-level.
    source_variant = next(
        (iv for iv in jc.images if iv.variant_id == source_variant_id), None,
    )
    scene_id = source_variant.scene_id if source_variant else None
    sid = scene_id or _first_scene_id(job)
    movement_prompt = (
        effective_override
        or (job.movement_prompts_by_variant or {}).get(source_variant_id)
        or (job.enriched_movement_prompts or {}).get(sid)
        or (job.movement_prompts or {}).get(sid)
        or job.enriched_movement_prompt
        or job.movement_prompt
        or ""
    )
    duration = (
        (job.durations_by_variant or {}).get(source_variant_id)
        or (job.durations_by_scene or {}).get(sid)
        or job.duration_secs
    )
    await _animate_one_video(job, jc, fresh, movement_prompt, duration)


async def run_video_synthesis(job_id: str) -> None:
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return
    # Need at least one prompt — either the new dict or the legacy singular.
    if not (job.movement_prompts or job.movement_prompt):
        return

    # AI Director runs FIRST when enabled. ONE Claude call with the scene
    # references + approved variant frames; agent writes a cinematic shot
    # description per scene. Result is merged into enriched_movement_prompts
    # so the per-variant resolver below transparently picks it up.
    # Per-image prompts are explicit/verbatim — skip the Director + enrich
    # layers (they operate per-scene and would be ignored by the per-variant
    # resolver below anyway).
    if job.use_director and job.movement_prompts and not job.movement_prompts_by_variant:
        from pathlib import Path

        from character_swap import prompt_director

        # For each scene, collect the approved variant images (across all
        # characters) so the director sees the actual start frames the video
        # model will animate.
        scene_ids = list(job.scene_ids) if job.scene_ids else [job.scene_id]
        scene_paths = (list(job.scene_image_paths) if job.scene_image_paths
                       else [job.scene_image_path])
        scene_path_by_id = dict(zip(scene_ids, scene_paths))

        director_inputs: list[tuple[str, "Path", list["Path"], str]] = []
        for sid in scene_ids:
            raw = (job.movement_prompts or {}).get(sid, "")
            if not raw:
                continue
            scene_path = scene_path_by_id.get(sid)
            if not scene_path:
                continue
            approved_imgs: list[Path] = []
            for jc in job.characters.values():
                approved_ids = set(jc.approved_variant_ids or [])
                if jc.approved_variant_id:
                    approved_ids.add(jc.approved_variant_id)
                for v in jc.images:
                    if v.variant_id in approved_ids and (v.scene_id or scene_ids[0]) == sid:
                        approved_imgs.append(Path(v.path))
            director_inputs.append((sid, Path(scene_path), approved_imgs, raw))

        if director_inputs:
            plan = await asyncio.to_thread(
                prompt_director.direct_movement,
                scenes=director_inputs,
                job_id=job.job_id,
            )
            if plan is not None:
                # Merge into enriched dict (per-scene cache). Director output
                # takes precedence over any prior enriched values.
                merged = dict(job.enriched_movement_prompts or {})
                for s_plan in plan.scenes:
                    if s_plan.prompt:
                        merged[s_plan.scene_id] = s_plan.prompt
                job.enriched_movement_prompts = merged
                primary = (_first_scene_id(job)
                           or next(iter(job.movement_prompts.keys()), None))
                job.enriched_movement_prompt = (
                    merged.get(primary)
                    or next(iter(merged.values()), None)
                )
                s.update_job(job)

    # Per-scene enrichment: each scene's "him pouring oil" / "she waves"
    # direction is expanded into its own cinematic shot description.
    # Cached on `job.enriched_movement_prompts` so a partial failure
    # (e.g. one scene fails enrichment) doesn't re-pay the OpenAI cost
    # on every subsequent run / resume. Skips scenes the Director already
    # filled in (Director output is higher quality).
    if job.enrich_prompt and job.movement_prompts and not job.movement_prompts_by_variant:
        from character_swap import prompt_enrich
        enriched_dict = dict(job.enriched_movement_prompts or {})
        dirty = False
        for sid, raw in job.movement_prompts.items():
            if enriched_dict.get(sid):
                continue
            out = await asyncio.to_thread(
                prompt_enrich.enrich_prompt, raw, "video", job_id=job.job_id,
            )
            if out and out != raw:
                enriched_dict[sid] = out
                dirty = True
        if dirty:
            job.enriched_movement_prompts = enriched_dict
            # Keep legacy singular field in sync for any code path that
            # still reads it.
            primary = (_first_scene_id(job)
                       or next(iter(job.movement_prompts.keys()), None))
            job.enriched_movement_prompt = (
                enriched_dict.get(primary)
                or next(iter(enriched_dict.values()), None)
            )
            s.update_job(job)

    m = max(1, min(10, job.videos_per_character))
    targets = [
        jc for jc in job.characters.values()
        if jc.status == CharStatus.APPROVED
        and (jc.approved_variant_ids or jc.approved_variant_id)
    ]
    if not targets:
        return

    # Build a closure that resolves the right prompt per variant's scene.
    # Order of preference: enriched per-scene → raw per-scene → legacy
    # enriched single → legacy single. Variants with scene_id=None (legacy)
    # fall back to the job's primary scene.
    primary_scene = _first_scene_id(job)
    movement_prompts = dict(job.movement_prompts or {})
    enriched_prompts = dict(job.enriched_movement_prompts or {})
    fallback = (job.enriched_movement_prompt or job.movement_prompt or "")

    def prompt_for_scene(scene_id: str | None) -> str:
        sid = scene_id or primary_scene
        if sid is None:
            return fallback
        return (enriched_prompts.get(sid)
                or movement_prompts.get(sid)
                or fallback)

    await asyncio.gather(
        *[_animate_character(job, jc, m, prompt_for_scene) for jc in targets]
    )


def _first_scene_id(job: Job) -> str | None:
    """The job's primary (first) scene_id. Multi-scene jobs use
    `scene_ids[0]`; legacy single-scene jobs use `scene_id`."""
    if job.scene_ids:
        return job.scene_ids[0]
    return job.scene_id or None


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
            model=job.video_model or "grok-imagine",
        )
    except Exception as e:
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
