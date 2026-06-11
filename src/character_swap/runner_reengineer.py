"""Reengineer pipeline — orchestration.

Rebuilds an uploaded reference video with different characters:

    queued → analyzing       ffmpeg scene detection + frame per scene +
                             Whisper transcript + Claude vision agent writes a
                             motion+speech prompt per scene (fallback: generic
                             prompt + verbatim dialogue)
           → swapping        a REAL Swap job is created from the frames
                             (origin="reengineer:<re_id>") and the normal
                             image-generation runner produces one swapped
                             variant per (character × scene)
           → awaiting_approval   user ✓-approves images in the Reengineer tab
                                 (skipped in auto mode: first READY variant per
                                 slot is approved automatically)
           → animating       movement is submitted with the agent's per-scene
                             prompts + per-scene durations matched to the
                             original scene lengths; Kling v3 via fal generates
                             clips WITH NATIVE AUDIO (the new character's voice)
           → assembling      per character: first DONE clip per scene, trimmed
                             to the original scene duration, concatenated in
                             scene order (Kling audio kept — no voice swap, no
                             captions)
           → done | partial_success | failed

State lives at output/reengineer/<re_id>/state.json (same pattern as B-roll).
The underlying Swap job is the source of truth for variants/videos; this
module only reads it and mutates the approval/movement fields the same way
the corresponding API endpoints do.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import secrets
from datetime import datetime
from pathlib import Path

from character_swap import events, reengineer, runner, runner_media, video_edit
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    Job,
    JobCharacter,
    SceneAsset,
    VariantStatus,
    VideoStatus,
)
from character_swap.state import store

_log = logging.getLogger("reengineer")

_POLL_SECS = 4.0
# Generous ceiling so a hung provider can't spin the video watcher forever.
# (The swap phase uses a PROGRESS-based watchdog instead — see
# _watch_swap_phase + settings.swap_stall_timeout_secs.)
_VIDEO_PHASE_TIMEOUT_SECS = 60 * 60


def _now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"


# Strong references to fire-and-forget resume tasks (bare create_task results
# can be garbage-collected mid-flight).
_RESUME_TASKS: set[asyncio.Task] = set()


def _spawn(coro, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    _RESUME_TASKS.add(task)
    task.add_done_callback(_RESUME_TASKS.discard)


def _update(re_id: str, **changes) -> dict:
    state = reengineer.load_state(re_id) or {"re_id": re_id}
    state.update(changes)
    state["updated_at"] = _now()
    reengineer.save_state(state)
    return state


# --------------------------------------------------------------------------- crash resume

_TERMINAL_RUN_STATES = {"done", "partial_success", "failed"}


async def resume_all() -> None:
    """Re-attach every non-terminal reengineer run after a server restart.

    The phase watchers are in-process asyncio tasks — a restart kills them and
    a run would otherwise sit in swapping/animating forever (and variants the
    restart interrupted would stay failed until manually retried; an entire
    character lost 9/9 slots this way on 2026-06-11). Called from the FastAPI
    lifespan AFTER runner.resume_pending has run for all jobs (it marks stale
    GENERATING images as failed with an "interrupted (server restart)" error,
    which is what we auto-retry here).
    """
    for state in reengineer.list_states():
        re_id = state.get("re_id")
        status = state.get("status")
        if not re_id or status in _TERMINAL_RUN_STATES:
            continue
        if status == "awaiting_approval":
            continue                       # user gate — nothing to re-attach
        _log.info("resuming reengineer %s from status=%r", re_id, status)
        if status in {"queued", "analyzing"}:
            # Analysis died mid-flight — safe to redo from the source video.
            _spawn(run_reengineer(re_id), f"reengineer-resume-{re_id}")
        elif status == "swapping":
            _spawn(_resume_swapping(re_id, state), f"reengineer-resume-{re_id}")
        elif status == "animating":
            _spawn(_resume_animating(re_id, state), f"reengineer-resume-{re_id}")
        elif status == "assembling":
            _spawn(assemble(re_id), f"reengineer-resume-{re_id}")


async def _resume_swapping(re_id: str, state: dict) -> None:
    job_id = state.get("job_id")
    job = store().get_job(job_id) if job_id else None
    if job is None:
        _update(re_id, status="failed", error="underlying job lost across restart")
        return
    # Auto-retry the slots the restart killed (marked failed by resume_pending).
    # Tasks are collected (not just _spawn'ed) so the stall watchdog can
    # cancel them if a retried slot hangs. ONE shared semaphore across all
    # retries — without it, a resume with 30 dead slots would fire 30
    # simultaneous provider calls (each retry otherwise creates its own).
    retry_sem = asyncio.Semaphore(
        runner._image_concurrency_for_model(runner._swap_image_model(job)))
    retry_tasks: list[asyncio.Task] = []
    for cid, jc in job.characters.items():
        for v in jc.images:
            if (v.status == VariantStatus.FAILED
                    and "interrupted" in (v.error or "")):
                task = asyncio.create_task(
                    runner.retry_single_variant(job_id, cid, v.variant_id,
                                                sem=retry_sem),
                    name=f"reengineer-retry-{v.variant_id}")
                _RESUME_TASKS.add(task)
                task.add_done_callback(_RESUME_TASKS.discard)
                retry_tasks.append(task)
    await _watch_swap_phase(re_id, job_id, tasks=retry_tasks)


async def _resume_animating(re_id: str, state: dict) -> None:
    # Video polling itself is already resumed by runner.resume_pending in the
    # lifespan; we only need to re-attach the watcher that assembles at the end.
    job_id = state.get("job_id")
    if not job_id or store().get_job(job_id) is None:
        _update(re_id, status="failed", error="underlying job lost across restart")
        return
    await _watch_video_phase(re_id, job_id)


# --------------------------------------------------------------------------- phase 1: analyze + create job

def _register_frame_as_scene(frame: Path) -> tuple[str, Path]:
    """Content-address a frame into the scene library (same scheme as
    POST /api/scenes) and return (scene_id, path-on-disk)."""
    data = frame.read_bytes()
    scene_id = "sc_" + hashlib.sha256(data).hexdigest()[:10]
    s = store()
    existing = s.get_scene(scene_id)
    dest = settings.scenes_dir / f"{scene_id}.png"
    if existing is not None:
        dest = settings.scenes_dir / existing.filename
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(dest)
    if existing is None:
        s.add_scene(SceneAsset(scene_id=scene_id, filename=dest.name,
                               original_name=frame.name))
    return scene_id, dest


async def run_reengineer(re_id: str) -> None:
    """Phase 1: analyze the source video and kick off the swap job."""
    state = reengineer.load_state(re_id)
    if not state:
        return
    try:
        await _do_analyze_and_swap(re_id, state)
    except Exception as e:
        _log.exception("reengineer %s failed", re_id)
        _update(re_id, status="failed", error=f"{type(e).__name__}: {e}")


def _load_cached_plan(run_dir: Path) -> list[dict] | None:
    """Reuse a previous attempt's plan.json after a crash-resume: a crash
    1 second before status flipped to 'swapping' used to recompute scene
    detection, Whisper, AND the Claude analyst from scratch (double-billing
    both API calls). The plan is only trusted when every referenced scene
    PNG still exists in the library (they're content-addressed, so this is
    a safe consistency check)."""
    plan_file = run_dir / "plan.json"
    if not plan_file.exists():
        return None
    try:
        import json as _json
        entries = _json.loads(plan_file.read_text(encoding="utf-8"))
        if not isinstance(entries, list) or not entries:
            return None
        for e in entries:
            sid = e.get("scene_id")
            if not sid:
                return None
            scene = store().get_scene(sid)
            if scene is None or not (settings.scenes_dir / scene.filename).exists():
                return None
        return entries
    except Exception:
        return None


async def _do_analyze_and_swap(re_id: str, state: dict) -> None:
    source = Path(state["source_path"])
    run_dir = reengineer.reengineer_dir(re_id)

    # --- resume short-circuit ---------------------------------------------
    # A crash between add_job and the status flip used to leave status=
    # "analyzing" with a live job in the store; re-running the analysis then
    # created a DUPLICATE swap job. If the recorded job already exists,
    # re-attach instead of re-analyzing.
    if state.get("job_id") and store().get_job(state["job_id"]) is not None:
        _log.info("reengineer %s: job %s already exists — re-attaching "
                  "instead of re-analyzing", re_id, state["job_id"])
        _update(re_id, status="swapping")
        await _resume_swapping(re_id, state)
        return

    _update(re_id, status="analyzing")
    scene_entries = _load_cached_plan(run_dir)
    if scene_entries is None:
        scene_entries = await _analyze(re_id, state, source, run_dir)

    # --- create the underlying Swap job (job_id persisted FIRST so a crash
    # in this window resumes into the same id instead of duplicating) -------
    job_id = state.get("job_id") or ("j_" + secrets.token_hex(5))
    state = _update(re_id, job_id=job_id)
    await _create_job_and_swap(re_id, state, scene_entries, job_id)


async def _analyze(re_id: str, state: dict, source: Path,
                   run_dir: Path) -> list[dict]:
    """Scene detection + frames + Whisper + Claude analyst → scene_entries.
    Whisper runs CONCURRENTLY with the frame-extract loop (independent
    inputs); a words.json from a previous crashed attempt is reused."""
    threshold = reengineer.SENSITIVITY_THRESHOLDS.get(
        state.get("scene_sensitivity") or "high", reengineer.SCENE_THRESHOLD)
    spans = await asyncio.to_thread(reengineer.detect_scenes, source,
                                    threshold=threshold)

    # --- transcript (parallel with frame extraction) ----------------------
    words_file = run_dir / "words.json"
    words_task: asyncio.Task | None = None
    cached_words = None
    if words_file.exists():
        try:
            cached_words = video_edit.words_from_json(
                words_file.read_text(encoding="utf-8"))
        except Exception:
            cached_words = None
    if cached_words is None:
        words_task = asyncio.create_task(asyncio.to_thread(
            video_edit.transcribe_words, source, job_id=re_id))

    # --- frames (bounded parallel ffmpeg extracts) -------------------------
    frame_sem = asyncio.Semaphore(4)

    async def _extract(i: int, a: float, b: float) -> Path:
        dest = run_dir / "scenes" / f"scene-{i:02d}.png"
        async with frame_sem:
            await asyncio.to_thread(
                reengineer.extract_frame, source, (a + b) / 2.0, dest)
        return dest

    frames = list(await asyncio.gather(
        *[_extract(i, a, b) for i, (a, b) in enumerate(spans)]))

    if cached_words is not None:
        words = cached_words
    else:
        words = await words_task
        words_file.write_text(video_edit.words_to_json(words), encoding="utf-8")

    # --- agent analysis (fallback never blocks the pipeline) -------------
    plans = await asyncio.to_thread(
        reengineer.analyze_scenes,
        frames=frames, spans=spans, words=words, re_id=re_id,
    ) or reengineer.fallback_plans(spans, words)

    # --- register frames as scenes + persist the plan ---------------------
    scene_entries: list[dict] = []
    for i, ((a, b), frame, plan) in enumerate(zip(spans, frames, plans)):
        scene_id, _path = _register_frame_as_scene(frame)
        scene_entries.append({
            "idx": i,
            "scene_id": scene_id,
            "start": round(a, 3),
            "end": round(b, 3),
            "duration": round(b - a, 3),
            "motion_prompt": plan.motion_prompt,
            "speech": plan.speech,
            "summary": plan.summary,
        })
    import json as _json
    (run_dir / "plan.json").write_text(_json.dumps(scene_entries, indent=2),
                                       encoding="utf-8")
    return scene_entries


async def _create_job_and_swap(re_id: str, state: dict,
                               scene_entries: list[dict], job_id: str) -> None:

    # --- create the underlying Swap job -----------------------------------
    s = store()
    image_model = state.get("image_model") or "nbp-swap"
    info = runner_media.IMAGE_MODELS.get(image_model)
    if info is None:
        raise ValueError(f"Unknown image_model '{image_model}'")
    if not settings.has_provider(info["provider"]):
        raise RuntimeError(f"{info['label']} is not configured (missing API key)")

    chars: dict[str, JobCharacter] = {}
    names: list[str] = []
    source_overrides = state.get("character_source_image_ids") or {}
    for cid in state["character_ids"]:
        ch = s.get_character(cid)
        if ch is None:
            raise ValueError(f"Character not found: {cid}")
        # Per-character reference-image pick (e.g. a specific outfit) — same
        # resolution as POST /api/jobs.
        src = settings.characters_dir / ch.resolve_source_filename(
            source_overrides.get(cid))
        if not src.exists():
            raise RuntimeError(f"Character file missing on disk: {src}")
        chars[cid] = JobCharacter(char_id=cid, name=ch.name,
                                  source_image_path=str(src),
                                  status=CharStatus.QUEUED)
        names.append(ch.name)

    scene_ids = [e["scene_id"] for e in scene_entries]
    scene_paths = [str(settings.scenes_dir / f"{sid}.png") for sid in scene_ids]
    # Dedup guard: identical frames (static video) collapse to one scene_id —
    # keep order but drop duplicates so the job doesn't double-generate.
    seen: set[str] = set()
    uniq_ids: list[str] = []
    uniq_paths: list[str] = []
    for sid, sp in zip(scene_ids, scene_paths):
        if sid in seen:
            continue
        seen.add(sid)
        uniq_ids.append(sid)
        uniq_paths.append(sp)

    # Outfit choice ("Kläder" in the upload form): scene = wear the original
    # person's clothes (default — job.prompt stays None so the validated
    # default prompt chain applies); character/custom get an explicit prompt.
    # A replacement BACKGROUND (optional upload) rides as reference #3 and
    # always needs the explicit background prompt (relight to Image 3).
    outfit_mode = state.get("outfit_mode") or "scene"
    background_path = state.get("background_path")
    if background_path and not Path(background_path).exists():
        raise RuntimeError(f"Background image missing on disk: {background_path}")
    swap_prompt: str | None = None
    if outfit_mode != "scene" or background_path:
        from character_swap import pipeline
        swap_prompt = pipeline.build_edit_swap_prompt(
            outfit_mode, state.get("outfit_text"),
            background=bool(background_path))

    job = Job(
        job_id=job_id,
        title=f"Reengineer {state.get('source_name') or re_id} — {', '.join(names)}",
        scene_id=uniq_ids[0],
        scene_image_path=uniq_paths[0],
        scene_ids=uniq_ids,
        scene_image_paths=uniq_paths,
        characters=chars,
        images_per_character=1,
        image_model=image_model,
        prompt=swap_prompt,
        extra_reference_path=background_path,
        video_model=state.get("video_model") or "kling-v3",
        video_audio=True,
        outfit_mode=state.get("outfit_mode") or "scene",
        outfit_text=state.get("outfit_text") or None,
        origin=f"reengineer:{re_id}",
    )
    s.add_job(job)
    _update(re_id, status="swapping", job_id=job.job_id, scenes=scene_entries,
            n_scenes=len(scene_entries))

    swap_task = asyncio.create_task(runner.run_image_generation(job.job_id))
    await _watch_swap_phase(re_id, job.job_id, tasks=[swap_task])
    # The watcher cancels swap_task on stall — CancelledError is BaseException
    # in 3.11+, so run_reengineer's `except Exception` would NOT swallow it
    # and the stall reason written by the watcher would get clobbered.
    with contextlib.suppress(asyncio.CancelledError):
        await swap_task


def _variants_terminal(job: Job) -> bool:
    all_v = [v for jc in job.characters.values() for v in jc.images]
    return bool(all_v) and all(v.status in {VariantStatus.READY, VariantStatus.FAILED}
                               for v in all_v)


def _swap_progress_marker(job: Job) -> tuple[int, int]:
    """A cheap fingerprint of image-phase progress: (terminal slot count,
    total QC attempts). Either number moving = something is happening —
    a finished/failed slot OR a generation attempt inside the QC-retry loop
    (the runner bumps qc_attempts in place before each attempt)."""
    vs = [v for jc in job.characters.values() for v in jc.images]
    terminal = sum(1 for v in vs
                   if v.status in {VariantStatus.READY, VariantStatus.FAILED})
    return terminal, sum(v.qc_attempts or 1 for v in vs)


async def _watch_swap_phase(
    re_id: str, job_id: str, tasks: list[asyncio.Task] | None = None,
) -> None:
    """Wait for the image phase to finish; then gate or auto-continue.

    PROGRESS-based watchdog (not a fixed deadline): the run only fails when
    NOTHING has moved for swap_stall_timeout_secs — the old fixed 30-min
    ceiling fired below the realistic duration of large gpt-image runs and
    marked them failed while generation kept going (and billing).
    swap_phase_max_secs is a generous absolute backstop. On stall/ceiling
    every `tasks` entry is CANCELLED (so no further attempts bill) and
    still-generating variants are marked failed so the UI isn't stuck."""
    loop = asyncio.get_event_loop()
    start = loop.time()
    last_marker: tuple[int, int] | None = None
    last_change = start
    while True:
        await asyncio.sleep(_POLL_SECS)
        job = store().get_job(job_id)
        if job is None:
            _update(re_id, status="failed", error="underlying job disappeared")
            return
        if _variants_terminal(job):
            break
        marker = _swap_progress_marker(job)
        if marker != last_marker:
            last_marker = marker
            last_change = loop.time()
        stalled = loop.time() - last_change > settings.swap_stall_timeout_secs
        over_ceiling = loop.time() - start > settings.swap_phase_max_secs
        if stalled or over_ceiling:
            reason = (
                f"image phase stalled — no progress in "
                f"{settings.swap_stall_timeout_secs // 60} min"
                if stalled else
                f"image phase exceeded {settings.swap_phase_max_secs // 60} min ceiling"
            )
            for t in tasks or []:
                t.cancel()
            # Mark in-flight slots failed so _variants_terminal holds for
            # later reads and the UI shows ✕ retry instead of forever-skeletons.
            for jc in job.characters.values():
                for v in jc.images:
                    if v.status == VariantStatus.GENERATING:
                        v.status = VariantStatus.FAILED
                        v.error = reason
            store().update_job(job)
            _update(re_id, status="failed", error=reason)
            return

    job = store().get_job(job_id)
    any_ready = any(v.status == VariantStatus.READY
                    for jc in job.characters.values() for v in jc.images)
    if not any_ready:
        _update(re_id, status="failed", error="every swap variant failed")
        return

    state = reengineer.load_state(re_id) or {}
    if state.get("auto_mode"):
        _auto_approve(job)
        await animate(re_id)
    else:
        _update(re_id, status="awaiting_approval")


def _auto_approve(job: Job) -> None:
    """First READY variant per (character, scene) — mirror of approve_all."""
    s = store()
    for jc in job.characters.values():
        if jc.status in {CharStatus.REJECTED, CharStatus.ANIMATING, CharStatus.DONE}:
            continue
        approved = list(jc.approved_variant_ids or [])
        covered = {v.scene_id for v in jc.images if v.variant_id in approved}
        for v in jc.images:
            if v.status != VariantStatus.READY or v.scene_id in covered:
                continue
            approved.append(v.variant_id)
            covered.add(v.scene_id)
        if approved:
            jc.approved_variant_ids = approved
            jc.approved_variant_id = approved[0]
            jc.status = CharStatus.APPROVED
            jc.updated_at = datetime.utcnow()
    s.update_job(job)


# --------------------------------------------------------------------------- phase 2: animate

async def animate(re_id: str) -> None:
    """Submit movement with the agent's per-scene prompts + durations, then
    watch the video phase and assemble. Called automatically in auto mode, or
    via POST /api/reengineer/{id}/animate after manual approval."""
    state = reengineer.load_state(re_id)
    if not state or not state.get("job_id"):
        return
    try:
        await _do_animate(re_id, state)
    except Exception as e:
        _log.exception("reengineer %s animate failed", re_id)
        _update(re_id, status="failed", error=f"{type(e).__name__}: {e}")


def _clamp_kling(secs: float) -> int:
    return max(3, min(15, round(secs)))


def _with_accent(prompt: str) -> str:
    """Kling synthesizes the voice from the prompt — enforce accent AND clear
    pronunciation centrally so every clip speaks American English with each
    word pronounced correctly, even if a scene's agent-written prompt forgot
    to say so (Hugo, 2026-06-11; garbled words like "baking goda" observed)."""
    out = prompt
    if "american" not in out.lower():
        out = (out.rstrip() + " The person speaks fluent American English "
               "with a natural American accent.")
    if "pronounc" not in out.lower():
        out = (out.rstrip() + " Every word is pronounced clearly, correctly "
               "and distinctly.")
    return out


async def _do_animate(re_id: str, state: dict) -> None:
    s = store()
    job = s.get_job(state["job_id"])
    if job is None:
        raise RuntimeError("underlying job disappeared")
    approved_any = any(jc.approved_variant_ids or jc.approved_variant_id
                       for jc in job.characters.values())
    if not approved_any:
        raise RuntimeError("no approved variants — approve images first")

    movement_prompts = {e["scene_id"]: _with_accent(e["motion_prompt"])
                        for e in state["scenes"]}
    durations = {e["scene_id"]: _clamp_kling(e["duration"]) for e in state["scenes"]}

    job.movement_prompts = movement_prompts
    job.movement_prompt = movement_prompts.get(job.scene_ids[0] if job.scene_ids
                                               else job.scene_id) or "animate"
    job.durations_by_scene = durations
    job.videos_per_character = 1
    job.video_model = state.get("video_model") or "kling-v3"
    job.video_audio = True
    for jc in job.characters.values():
        if jc.status == CharStatus.APPROVED or jc.approved_variant_ids:
            jc.status = CharStatus.APPROVED
    s.update_job(job)
    await events.publish(job.job_id, {"kind": "movement.set", "job_id": job.job_id})

    _update(re_id, status="animating")
    video_task = asyncio.create_task(runner.run_video_synthesis(job.job_id))
    await _watch_video_phase(re_id, job.job_id)
    await video_task


def _videos_terminal(job: Job) -> bool:
    vids = [v for jc in job.characters.values() for v in jc.videos]
    return bool(vids) and all(v.status in {VideoStatus.DONE, VideoStatus.FAILED,
                                           VideoStatus.ERROR} for v in vids)


async def _watch_video_phase(re_id: str, job_id: str) -> None:
    deadline = asyncio.get_event_loop().time() + _VIDEO_PHASE_TIMEOUT_SECS
    while True:
        await asyncio.sleep(_POLL_SECS)
        job = store().get_job(job_id)
        if job is None:
            _update(re_id, status="failed", error="underlying job disappeared")
            return
        if _videos_terminal(job):
            break
        if asyncio.get_event_loop().time() > deadline:
            _update(re_id, status="failed", error="video phase timed out")
            return
    await assemble(re_id)


# --------------------------------------------------------------------------- phase 3: assemble

async def assemble(re_id: str) -> None:
    """Per character: pick the first DONE clip per scene (in scene order),
    trim each to the ORIGINAL scene duration, concat keeping Kling audio."""
    state = reengineer.load_state(re_id)
    if not state or not state.get("job_id"):
        return
    try:
        await _do_assemble(re_id, state)
    except Exception as e:
        _log.exception("reengineer %s assemble failed", re_id)
        _update(re_id, status="failed", error=f"{type(e).__name__}: {e}")


async def _do_assemble(re_id: str, state: dict) -> None:
    _update(re_id, status="assembling")
    job = store().get_job(state["job_id"])
    if job is None:
        raise RuntimeError("underlying job disappeared")
    run_dir = reengineer.reengineer_dir(re_id)
    scene_order = [e["scene_id"] for e in state["scenes"]]
    durations = {e["scene_id"]: e["duration"] for e in state["scenes"]}
    finals: dict[str, dict] = {}

    for cid, jc in job.characters.items():
        try:
            approved = set(jc.approved_variant_ids or
                           ([jc.approved_variant_id] if jc.approved_variant_id else []))
            variant_by_scene: dict[str, str] = {}
            for v in jc.images:
                if v.variant_id in approved and v.scene_id and v.scene_id not in variant_by_scene:
                    variant_by_scene[v.scene_id] = v.variant_id
            clips: list[Path] = []
            for i, sid in enumerate(scene_order):
                vid_variant = variant_by_scene.get(sid)
                video = next((vv for vv in jc.videos
                              if vv.status == VideoStatus.DONE
                              and vv.source_variant_id == vid_variant
                              and vv.final_video_path
                              and Path(vv.final_video_path).exists()), None)
                if video is None:
                    continue
                src = Path(video.final_video_path)
                # ALWAYS cut each scene clip to AUDIO onset first (Hugo
                # 2026-06-11: every clip starts when there's enough sound) —
                # Kling clips often open with dead air before the line.
                # Audio-less clips pass through untouched (built into the
                # primitive); on failure keep the untrimmed source.
                no_lead = run_dir / f"clip_{cid}_{i:02d}_noLead.mp4"
                try:
                    await asyncio.to_thread(
                        video_edit.trim_leading_silence, src, no_lead,
                        threshold_db=-30.0,
                        min_silence_secs=0.05,  # very aggressive — exact start
                        job_id=re_id,
                    )
                    src = no_lead
                except (RuntimeError, ValueError):
                    pass
                dur = durations.get(sid)
                clip = run_dir / f"clip_{cid}_{i:02d}.mp4"
                actual = video_edit._probe_duration(src)
                # CAP at the original scene duration (after the onset trim the
                # clip is shorter, so finals are tighter than the original —
                # "never longer than the original scene", not "match exactly").
                if dur and actual > dur + 0.15:
                    await asyncio.to_thread(video_edit.trim_range, src, clip,
                                            start_secs=0.0, end_secs=dur)
                else:
                    clip = src
                clips.append(clip)
            if not clips:
                finals[cid] = {"status": "failed", "error": "no finished clips"}
                continue
            final = run_dir / f"final_{cid}.mp4"
            await asyncio.to_thread(video_edit.concat_videos, clips, final,
                                    aspect_ratio="9:16")
            finals[cid] = {"status": "done", "final_path": str(final),
                           "n_clips": len(clips)}
        except Exception as e:
            finals[cid] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    ok = [f for f in finals.values() if f["status"] == "done"]
    status = ("done" if len(ok) == len(finals) and ok
              else "partial_success" if ok else "failed")
    _update(re_id, status=status, finals=finals,
            completed_at=_now())
