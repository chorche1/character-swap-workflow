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
           → assembling      per character: first DONE clip per scene (FULL
                             length — never cut to the original scene duration;
                             that cap chopped spoken lines mid-word), concatenated
                             in scene order and finished through the shared
                             Editor pipeline (audio-onset trim → interior-silence
                             trim → optional voice swap → Whisper → optional WPM
                             → captions), same as Swap Step 6. Settings come from
                             state["assemble_settings"] (set by the ⚙ panel via
                             the animate/assemble endpoints); defaults keep
                             Kling's voice + pacing (voice swap OFF, WPM OFF)
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
import math
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from character_swap import (
    events,
    push,
    reengineer,
    runner,
    runner_compile,
    runner_media,
    swap_qc,
    video_edit,
)
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
# Analyst "low-fps video" sampling (Hugo 2026-06-12): ~2.5 fps per scene,
# 3-8 frames each, ≤ ~90 images per run (Anthropic caps requests at 100).
_ANALYST_FRAMES_PER_SEC = 2.5
_ANALYST_TOTAL_FRAME_BUDGET = 90
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
    old_status = state.get("status")
    # Backlog #36 (2026-06-12): a run that recovered (retry → done) kept
    # showing the OLD failure banner — the error field survived every
    # status transition. Moving to a non-failed status clears it unless the
    # caller explicitly sets one.
    if (changes.get("status") and changes["status"] != "failed"
            and "error" not in changes):
        changes["error"] = None
    state.update(changes)
    state["updated_at"] = _now()
    reengineer.save_state(state)
    new_status = state.get("status")
    if new_status != old_status:
        _push_status(state, new_status)
    return state


# Reengineer milestones worth a phone push (approval gates + finished runs).
# Maps the run STATUS we transition INTO → (title, ntfy tags, priority).
# Gates use a higher priority so they break through; failures are loudest.
_RE_PUSH: dict[str, tuple[str, list[str], int]] = {
    "awaiting_approval":      ("Granska klippen", ["mag"], 4),
    "awaiting_person_choice": ("Valj person i scenen", ["bust_in_silhouette"], 4),
    "awaiting_assembly":      ("Bilder godkanda – redo att bygga", ["clapper"], 3),
    "done":                   ("Reengineer klar", ["white_check_mark"], 3),
    "partial_success":        ("Reengineer delvis klar", ["warning"], 4),
    "failed":                 ("Reengineer misslyckades", ["rotating_light"], 5),
}


def _push_status(state: dict, status: str) -> None:
    """Best-effort phone push on a reengineer status transition (no-op unless
    NTFY_TOPIC is set; never raises — push.notify swallows everything)."""
    spec = _RE_PUSH.get(status)
    if not spec:
        return
    title, tags, priority = spec
    n_scenes = len(state.get("scenes") or [])
    name = state.get("name") or state.get("title") or state.get("re_id") or ""
    parts = []
    if name:
        parts.append(str(name))
    if n_scenes:
        parts.append(f"{n_scenes} scener")
    if status == "failed" and state.get("error"):
        parts.append(str(state["error"])[:200])
    push.notify(title, " · ".join(parts), priority=priority, tags=tags)


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
        if status in {"awaiting_approval", "awaiting_assembly",
                      "awaiting_person_choice"}:
            continue                       # user gates — nothing to re-attach
        _log.info("resuming reengineer %s from status=%r", re_id, status)
        if status in {"queued", "analyzing"}:
            # Analysis died mid-flight — safe to redo from the source video.
            # Image-sourced runs (Swap tab) have no video to re-analyze; they
            # just recreate the job from the already-built scene entries.
            if state.get("from_images"):
                _spawn(run_reengineer_from_images(re_id),
                       f"reengineer-resume-{re_id}")
            else:
                _spawn(run_reengineer(re_id), f"reengineer-resume-{re_id}")
        elif status == "swapping":
            _spawn(_resume_swapping(re_id, state), f"reengineer-resume-{re_id}")
        elif status == "animating":
            _spawn(_resume_animating(re_id, state), f"reengineer-resume-{re_id}")
        elif status == "reanimating":
            # Edit-mode re-animation: finalize WITHOUT assembling.
            _spawn(_resume_reanimating(re_id, state), f"reengineer-resume-{re_id}")
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
    # Direct-scene shared clips live OUTSIDE the job (no VideoVariant), so
    # resume_pending never revives them — re-render any that didn't finish.
    direct_tasks = [asyncio.create_task(_render_direct_clip(re_id, e["scene_id"]))
                    for e in (state.get("scenes") or [])
                    if e.get("is_direct") and not e.get("shared_clip_path")]
    await _watch_video_phase(re_id, job_id, direct_tasks=direct_tasks)
    if direct_tasks:
        await asyncio.gather(*direct_tasks, return_exceptions=True)


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


async def run_reengineer_from_images(re_id: str) -> None:
    """Phase 1 for the image-sourced flow (the Swap tab): there is no source
    video to analyze — the scene entries were already built from the uploaded
    images at creation, with the user's manual motion prompt + Kling length per
    scene — so go straight to creating the swap job. Everything downstream
    (gate → animate → assemble → edit mode) is identical to the video flow."""
    state = reengineer.load_state(re_id)
    if not state:
        return
    try:
        await _do_create_from_images(re_id, state)
    except Exception as e:
        _log.exception("reengineer(from_images) %s failed", re_id)
        _update(re_id, status="failed", error=f"{type(e).__name__}: {e}")


async def _do_create_from_images(re_id: str, state: dict) -> None:
    # Resume short-circuit (same contract as _do_analyze_and_swap): a crash
    # between add_job and the status flip leaves a live job in the store;
    # re-attach instead of creating a duplicate.
    if state.get("job_id") and store().get_job(state["job_id"]) is not None:
        _log.info("reengineer(from_images) %s: job %s already exists — "
                  "re-attaching", re_id, state["job_id"])
        _update(re_id, status="swapping")
        await _resume_swapping(re_id, state)
        return
    scene_entries = state.get("scenes") or []
    if not scene_entries:
        _update(re_id, status="failed", error="no scenes")
        return
    # job_id persisted FIRST (crash-resume contract — mirrors the video path)
    job_id = state.get("job_id") or ("j_" + secrets.token_hex(5))
    state = _update(re_id, job_id=job_id)
    await _create_job_and_swap(re_id, state, scene_entries, job_id)


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
    # Defensive: an image-sourced run has no source_path to analyze. Should be
    # routed to _do_create_from_images by the caller, but guard against any
    # other entry (resume, retry) reaching here and KeyError-ing on source_path.
    if state.get("from_images"):
        return await _do_create_from_images(re_id, state)
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
    """Scene detection + Whisper + boundary snap + frames + Claude analyst
    → scene_entries. Whisper now runs BEFORE frame extraction (backlog #31:
    the visual scene boundaries must snap to inter-word gaps before any
    frames are sampled — a phrase crossing a visual cut used to be split
    mid-word across two Kling clips). A words.json from a previous crashed
    attempt is reused."""
    threshold = reengineer.SENSITIVITY_THRESHOLDS.get(
        state.get("scene_sensitivity") or "high", reengineer.SCENE_THRESHOLD)
    spans = await asyncio.to_thread(reengineer.detect_scenes, source,
                                    threshold=threshold)

    # --- transcript (needed before frames for the boundary snap) ----------
    words_file = run_dir / "words.json"
    cached_words = None
    if words_file.exists():
        try:
            cached_words = video_edit.words_from_json(
                words_file.read_text(encoding="utf-8"))
        except Exception:
            cached_words = None
    if cached_words is not None:
        words = cached_words
    else:
        words = await asyncio.to_thread(
            video_edit.transcribe_words, source, job_id=re_id)
        words_file.write_text(video_edit.words_to_json(words),
                              encoding="utf-8")

    # Visual cuts land mid-word — snap boundaries into word gaps so each
    # scene's dialogue is whole phrases (backlog #31).
    spans = reengineer.snap_spans_to_word_gaps(spans, words)

    # --- frames (bounded parallel ffmpeg extracts) -------------------------
    # Per scene: the MID frame stays the canonical scene asset (swap input),
    # but the analyst gets a DENSE timestamped frame sequence so it reads
    # each scene like a low-fps VIDEO — a single frame reduced "pours baking
    # soda over the kiwis" to a static "holds kiwis" prompt, and 3 fixed
    # samples still left 2-3s gaps on long scenes where a quick action could
    # hide (Hugo 2026-06-12). ~2.5 fps per scene — denser than Gemini's
    # native 1 fps video sampling — capped per scene AND in total so a
    # 20-scene run stays under the Anthropic 100-images-per-request limit.
    frame_sem = asyncio.Semaphore(4)
    per_scene_cap = max(3, min(8, _ANALYST_TOTAL_FRAME_BUDGET
                               // max(1, len(spans))))

    async def _extract_at(i: int, t_abs: float, dest: Path) -> Path:
        async with frame_sem:
            await asyncio.to_thread(reengineer.extract_frame, source,
                                    t_abs, dest)
        return dest

    async def _extract_sequence(i: int, a: float,
                                b: float) -> list[tuple[Path, float]]:
        dur = max(0.1, b - a)
        k = max(3, min(per_scene_cap, math.ceil(dur * _ANALYST_FRAMES_PER_SEC)))
        # Sample midpoints of k equal slices — never exactly on a cut.
        offsets = [dur * (j + 0.5) / k for j in range(k)]
        mid_j = min(range(k), key=lambda j: abs(offsets[j] - dur / 2))
        tasks = []
        for j, off in enumerate(offsets):
            # The frame closest to 50% IS the canonical scene asset.
            name = (f"scene-{i:02d}.png" if j == mid_j
                    else f"scene-{i:02d}-t{j}.png")
            tasks.append(_extract_at(i, a + off, run_dir / "scenes" / name))
        paths = list(await asyncio.gather(*tasks))
        return list(zip(paths, offsets))

    sequences = list(await asyncio.gather(
        *[_extract_sequence(i, a, b) for i, (a, b) in enumerate(spans)]))
    # Canonical mid frame per scene (the swap input) = the scene-XX.png slot.
    frames = [next(p for p, _ in seq if p.name == f"scene-{i:02d}.png")
              for i, seq in enumerate(sequences)]

    # --- agent analysis (fallback never blocks the pipeline) -------------
    plans = await asyncio.to_thread(
        reengineer.analyze_scenes,
        frames=frames, spans=spans, words=words, re_id=re_id,
        motion_frames=sequences,
    )
    if plans is None:
        # Backlog #23 (2026-06-12): this used to be invisible — generic
        # fallback prompts appeared at the gate with no hint the analyst
        # had failed. The flag renders an amber banner so the prompts get
        # human eyes before any Kling spend.
        _log.warning("reengineer %s: Claude analyst failed — using generic "
                     "fallback motion prompts", re_id)
        state["analyst_fallback"] = True
        plans = reengineer.fallback_plans(spans, words)

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

    # "Direct image — no swap" scenes: NO per-character variants are generated
    # (_kick_char skips them) — one shared Kling clip is reused for every
    # character. swap_ids = the scenes that actually get swapped.
    direct_map = {e["scene_id"]: bool(e.get("is_direct")) for e in scene_entries}
    direct_ids = [sid for sid in uniq_ids if direct_map.get(sid)]
    swap_ids = [sid for sid in uniq_ids if not direct_map.get(sid)]

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

    # Optional AI Director (checkbox at upload, off by default): ONE Claude
    # call LOOKS at every scene frame and writes a tailored compact swap
    # prompt per scene — naming the actual props with position/size in frame
    # and the camera distance, which is exactly where the static template
    # drifts (wrong props / zoomed-out framing). Failure → None → the normal
    # template chain applies; generation never blocks on the Director.
    director_json: str | None = None
    if state.get("use_director"):
        from character_swap import prompt_director
        result = await asyncio.to_thread(
            prompt_director.direct_reengineer_swap,
            scenes=[(sid, Path(p)) for sid, p in zip(uniq_ids, uniq_paths)],
            outfit_mode=outfit_mode,
            outfit_text=state.get("outfit_text"),
            background_path=Path(background_path) if background_path else None,
            job_id=job_id,
        )
        if result is not None:
            intent, prompts, meta = result
            director_json = prompt_director.plan_from_scene_prompts(
                intent, prompts,
                [(cid, jc.name) for cid, jc in chars.items()],
            ).model_dump_json()
            # Flag multi-person SWAP scenes onto their entries — the person-
            # choice gate reads these (direct scenes never swap, so skip them).
            for e in scene_entries:
                m = meta.get(e["scene_id"])
                if m and e["scene_id"] not in direct_ids:
                    e["multi_person"] = True
                    e["people"] = m["people"]

    job = Job(
        job_id=job_id,
        title=f"Reengineer {state.get('source_name') or re_id} — {', '.join(names)}",
        use_director=bool(director_json),
        director_prompts_json=director_json,
        scene_id=uniq_ids[0],
        scene_image_path=uniq_paths[0],
        scene_ids=uniq_ids,
        scene_image_paths=uniq_paths,
        direct_scene_ids=direct_ids,
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

    if not swap_ids:
        # Every scene is a direct image — nothing to swap. Skip the image-gen
        # phase entirely (its watcher would never go terminal with zero
        # variants) and go to the gate, or auto-animate.
        if state.get("auto_mode"):
            await animate(re_id)
        else:
            _update(re_id, status="awaiting_approval")
        return

    ambiguous = [e for e in scene_entries
                 if e.get("multi_person") and e["scene_id"] not in direct_ids]
    if ambiguous:
        # Multiple people in one or more swap scenes — PAUSE and ask the user
        # which person to swap + what to do with the other(s) before any image
        # is generated (even in auto mode: ambiguity needs a decision). The job
        # + Director plan are already persisted; resolve_people patches the plan
        # and kicks the swap phase.
        _update(re_id, status="awaiting_person_choice", scenes=scene_entries)
        return

    swap_task = asyncio.create_task(runner.run_image_generation(job.job_id))
    await _watch_swap_phase(re_id, job.job_id, tasks=[swap_task])
    # The watcher cancels swap_task on stall — CancelledError is BaseException
    # in 3.11+, so run_reengineer's `except Exception` would NOT swallow it
    # and the stall reason written by the watcher would get clobbered.
    with contextlib.suppress(asyncio.CancelledError):
        await swap_task


async def _resolve_people_and_swap(re_id: str) -> None:
    """Resume the swap phase after the user answered the multi-person gate.
    The resolve_people endpoint already patched the Director plan on the job
    with the chosen person + keep/remove directive; here we just kick image
    generation + watch it (the same tail as _create_job_and_swap)."""
    state = reengineer.load_state(re_id)
    if not state or not state.get("job_id"):
        return
    job_id = state["job_id"]
    if store().get_job(job_id) is None:
        _update(re_id, status="failed", error="underlying job disappeared")
        return
    try:
        _update(re_id, status="swapping")
        swap_task = asyncio.create_task(runner.run_image_generation(job_id))
        await _watch_swap_phase(re_id, job_id, tasks=[swap_task])
        with contextlib.suppress(asyncio.CancelledError):
            await swap_task
    except Exception as e:
        _log.exception("reengineer %s resolve_people swap failed", re_id)
        _update(re_id, status="failed", error=f"{type(e).__name__}: {e}")


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

    # Cross-scene consistency annotation (backlog #13): every variant passed
    # solo QC, but nothing compared them ACROSS scenes — sleeves/gloves/
    # glasses wobbled between scenes of the same final. One cheap vision
    # call per character; advisory only (amber chips at the gate).
    try:
        warnings = await _consistency_warnings(job)
        if warnings:
            _update(re_id, consistency_warnings=warnings)
    except Exception as e:
        _log.warning("reengineer %s: consistency annotation failed: %s",
                     re_id, e)

    state = reengineer.load_state(re_id) or {}
    if state.get("auto_mode"):
        _auto_approve(job)
        await animate(re_id)
    else:
        _update(re_id, status="awaiting_approval")


async def _consistency_warnings(job: Job) -> dict[str, list[dict]]:
    """{char_id: [{scene_id, issue}, ...]} for characters whose READY
    variants contradict each other across scenes. Never raises past the
    caller's guard; unavailable QC → no annotation."""
    out: dict[str, list[dict]] = {}
    for cid, jc in job.characters.items():
        ready = [(v.scene_id or "", Path(v.path)) for v in jc.images
                 if v.status == VariantStatus.READY and v.path
                 and Path(v.path).exists()]
        if len(ready) < 2:
            continue
        issues = await asyncio.to_thread(
            swap_qc.inspect_consistency,
            variants=ready,
            character_image=Path(jc.source_image_path),
            job_id=job.job_id)
        if issues:
            out[cid] = issues
    return out


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

# In-process guard against duplicate animation triggers (double-click,
# second tab): a second animate/reanimate for the same run while one is in
# flight would submit a SECOND full Kling batch — double billing. Mirrors
# _ASSEMBLING below. Cleared on process restart, so the crash-recovery
# re-trigger from status "animating" still works.
_ANIMATING: set[str] = set()


async def animate(re_id: str) -> None:
    """Submit movement with the agent's per-scene prompts + durations, then
    watch the video phase and assemble. Called automatically in auto mode, or
    via POST /api/reengineer/{id}/animate after manual approval."""
    state = reengineer.load_state(re_id)
    if not state or not state.get("job_id"):
        return
    if re_id in _ANIMATING:
        _log.warning("reengineer %s: animation already in flight — "
                     "ignoring duplicate trigger", re_id)
        return
    _ANIMATING.add(re_id)
    try:
        await _do_animate(re_id, state)
    except Exception as e:
        _log.exception("reengineer %s animate failed", re_id)
        _update(re_id, status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        _ANIMATING.discard(re_id)


def _clamp_kling(secs: float) -> int:
    """Whole Kling seconds, always rounded UP (Hugo 2026-06-12: never round
    a scene's time budget down), inside Kling's [3, 15] range."""
    return max(3, min(15, math.ceil(secs - 1e-9)))


# Conversational speech needs ~2.2 words/sec (≈130 wpm) plus a settle-in
# margin — Kling rushing or truncating a line because the clip is shorter
# than the dialogue was the root cause of "mycket av det som ska sägas
# finns inte med" (Hugo 2026-06-12). Mirrored in app.js klingDuration();
# a pytest keeps the constants in sync.
_SPEECH_WORDS_PER_SEC = 2.2
_SPEECH_MARGIN_SECS = 1.0
# Tolerates the analyst's documented attribution idiom — 'The person says,
# in a casual conversational tone with a natural American accent: "..."' —
# i.e. up to 160 descriptor chars between `says` and the opening quote
# (backlog #7, 2026-06-12: the old `says\s*:?\s*"` form never matched it, so
# a dialogue EDITED at the gate fell back to the stale analyst speech field
# and the clip kept the old, shorter duration — chopped lines through the
# edit path). Mirrored in app.js klingDuration(); a pytest keeps them in sync.
_DIALOGUE_RE = re.compile(r'says[^"“”]{0,160}?["“]([^"”]+)["”]',
                          re.IGNORECASE)


def _spoken_text(entry: dict) -> str:
    """The scene's dialogue: the motion prompt's says-clause(s) — the text
    the user sees and edits at the gate — falling back to the analyst's
    verbatim `speech` field."""
    texts = _DIALOGUE_RE.findall(entry.get("motion_prompt") or "")
    return " ".join(texts).strip() or (entry.get("speech") or "").strip()


def _speech_secs(entry: dict) -> float:
    """Seconds Kling needs to comfortably SAY the scene's dialogue.
    No dialogue → 0."""
    words = len(_spoken_text(entry).split())
    if not words:
        return 0.0
    return words / _SPEECH_WORDS_PER_SEC + _SPEECH_MARGIN_SECS


def _kling_duration(entry: dict) -> int:
    """Effective whole-second Kling duration for one scene entry.

    Priority: (1) the user's manual `kling_secs` override (the editable
    field at the gate) — clamped to Kling's [3, 15]; (2) AUTO = the ORIGINAL
    scene clip's length rounded UP to a whole second with a margin that is
    ALWAYS strictly more than 1 s and at most 2 s — i.e. floor + 2 (Hugo
    2026-06-13, second revision: "6,4 s → 8 s" AND an exact 6,0 s original
    must give 8, never 7 = exactly 1,0 s margin). Never the old
    speech-fitted extension. When the dialogue needs even more time, the
    gate shows a '⚠ replik ~Ns' hint instead of silently extending —
    bumping the manual field is the user's call."""
    override = entry.get("kling_secs")
    if override:
        return _clamp_kling(float(override))
    return _clamp_kling(math.floor(float(entry.get("duration") or 0.0)) + 2.0)


def _with_accent(prompt: str) -> str:
    """Kling synthesizes voice AND ambience from the prompt — enforce three
    guarantees centrally, even if a scene's agent-written prompt forgot them:
    American accent + clear pronunciation (Hugo 2026-06-11; garbled words
    like "baking goda" observed) and NO music bed (research 2026-06-12:
    generate_audio invents background music unless told otherwise; there is
    no API switch, suppression is prompt-level). Each clause is skipped when
    the prompt already covers it. Mirrored in app.js klingSuffix() —
    a pytest keeps the clause strings byte-identical."""
    out = prompt
    if "american" not in out.lower():
        out = (out.rstrip() + " The person speaks fluent American English "
               "with a natural American accent.")
    if "pronounc" not in out.lower():
        out = (out.rstrip() + " Every word is pronounced clearly, correctly "
               "and distinctly.")
    if "music" not in out.lower():
        out = (out.rstrip() + " No background music — natural ambient room "
               "sound only.")
    return out


# Per-run lock so concurrent direct-clip tasks don't clobber each other's
# state writes (each writes a different scene's shared_clip_path).
_DIRECT_LOCKS: dict[str, asyncio.Lock] = {}


def _direct_lock(re_id: str) -> asyncio.Lock:
    lock = _DIRECT_LOCKS.get(re_id)
    if lock is None:
        lock = asyncio.Lock()
        _DIRECT_LOCKS[re_id] = lock
    return lock


async def _persist_direct(re_id: str, scene_id: str, **fields) -> None:
    """Clobber-safe write of a direct scene's fields (shared_clip_path /
    direct_error): reload → mutate the matching scene → save, under the
    per-run lock. save_state is already atomic (tmp + replace)."""
    async with _direct_lock(re_id):
        st = reengineer.load_state(re_id)
        if not st:
            return
        for e in st.get("scenes") or []:
            if e.get("scene_id") == scene_id:
                e.update(fields)
                break
        reengineer.save_state(st)


async def _render_direct_clip(re_id: str, scene_id: str) -> None:
    """Render ONE shared Kling clip for a 'direct image — no swap' scene and
    store its path on the scene. Reused by EVERY character in assembly, so it
    is rendered once (not per character). Never raises — failures land on
    `direct_error` and surface as an assembly coverage gap."""
    state = reengineer.load_state(re_id) or {}
    job = store().get_job(state.get("job_id"))
    entry = next((e for e in state.get("scenes") or []
                  if e.get("scene_id") == scene_id), None)
    if job is None or entry is None:
        return
    image = Path(entry.get("direct_image_path") or "")
    if not image.exists():
        await _persist_direct(re_id, scene_id, direct_error="direkt bild saknas")
        return
    prompt = _with_accent(entry.get("motion_prompt") or "")
    dur = _kling_duration(entry)
    dest = reengineer.reengineer_dir(re_id) / f"direct_clip_{scene_id}.mp4"
    try:
        from character_swap import pipeline
        provider_job_id = await asyncio.to_thread(
            pipeline.submit_video,
            image=image, movement_prompt=prompt,
            character_name="(delad scen)", job_id=job.job_id,
            model=job.video_model or "kling-v3",
            duration_secs=dur, generate_audio=job.video_audio)
        await asyncio.to_thread(
            pipeline.wait_for_video,
            job_id=provider_job_id, character_name="(delad scen)",
            dest=dest, app_job_id=job.job_id,
            model=job.video_model or "kling-v3")
        await _persist_direct(re_id, scene_id,
                              shared_clip_path=str(dest), direct_error=None)
        await events.publish(job.job_id, {"kind": "direct.clip.done",
                                          "job_id": job.job_id,
                                          "scene_id": scene_id})
    except Exception as e:
        _log.exception("reengineer %s: direct clip for scene %s failed",
                       re_id, scene_id)
        await _persist_direct(re_id, scene_id,
                              direct_error=f"{type(e).__name__}: {e}")


async def _do_animate(re_id: str, state: dict) -> None:
    # Backlog #35 (2026-06-12): always animate the FRESHEST scenes. The
    # caller loaded `state` at trigger time — a prompt edit saved between
    # the trigger and this point would otherwise be ignored here and then
    # CLOBBERED by the snapshot write-back below.
    state = reengineer.load_state(re_id) or state
    s = store()
    job = s.get_job(state["job_id"])
    if job is None:
        raise RuntimeError("underlying job disappeared")
    approved_any = any(jc.approved_variant_ids or jc.approved_variant_id
                       for jc in job.characters.values())
    direct_scenes = [e for e in state["scenes"] if e.get("is_direct")]
    if not approved_any and not direct_scenes:
        raise RuntimeError("no approved variants — approve images first")

    movement_prompts = {e["scene_id"]: _with_accent(e["motion_prompt"])
                        for e in state["scenes"]}
    durations = {e["scene_id"]: _kling_duration(e) for e in state["scenes"]}

    job.movement_prompts = movement_prompts
    job.movement_prompt = movement_prompts.get(job.scene_ids[0] if job.scene_ids
                                               else job.scene_id) or "animate"
    # The gate textarea + accent suffix IS the exact Kling prompt — wipe any
    # enriched/Director layer (it outranks movement_prompts in the resolver
    # and would silently replace the text the user just approved).
    job.enriched_movement_prompts = {}
    job.enriched_movement_prompt = None
    job.durations_by_scene = durations
    job.videos_per_character = 1
    job.video_model = state.get("video_model") or "kling-v3"
    job.video_audio = True
    for jc in job.characters.values():
        if jc.status == CharStatus.APPROVED or jc.approved_variant_ids:
            jc.status = CharStatus.APPROVED
    s.update_job(job)
    await events.publish(job.job_id, {"kind": "movement.set", "job_id": job.job_id})

    # Full animate covers every scene — any edit-mode dirty flags are moot.
    # Re-read before writing back (backlog #35): edits saved while the job
    # fields were being prepared must survive — never write the snapshot.
    current = reengineer.load_state(re_id) or state
    for e in current.get("scenes") or []:
        e.pop("dirty", None)
        if e.get("is_direct"):       # full (re)animate re-renders the shared clip
            e["shared_clip_path"] = None
            e.pop("direct_error", None)
    _update(re_id, status="animating", scenes=current.get("scenes") or [])

    # ONE shared Kling clip per direct scene (no swap), reused by all characters.
    direct_tasks = [asyncio.create_task(_render_direct_clip(re_id, e["scene_id"]))
                    for e in (current.get("scenes") or []) if e.get("is_direct")]
    # Per-character clips only when there are swap scenes (skip for all-direct).
    swap_present = bool(set(job.scene_ids) - set(job.direct_scene_ids or []))
    video_task = (asyncio.create_task(runner.run_video_synthesis(job.job_id))
                  if swap_present else None)
    await _watch_video_phase(re_id, job.job_id, direct_tasks=direct_tasks)
    if video_task is not None:
        await video_task
    if direct_tasks:
        await asyncio.gather(*direct_tasks, return_exceptions=True)


def _approved_variant_for(jc: JobCharacter, scene_id: str) -> str | None:
    """First APPROVED variant for this (character, scene) — the image its
    Kling clip animates. Shared by assembly and edit-mode re-animation."""
    approved = set(jc.approved_variant_ids or
                   ([jc.approved_variant_id] if jc.approved_variant_id else []))
    for v in jc.images:
        if v.variant_id in approved and v.scene_id == scene_id:
            return v.variant_id
    return None


def _videos_terminal(job: Job) -> bool:
    vids = [v for jc in job.characters.values() for v in jc.videos]
    return bool(vids) and all(v.status in {VideoStatus.DONE, VideoStatus.FAILED,
                                           VideoStatus.ERROR} for v in vids)


def _swap_videos_done(job: Job) -> bool:
    """Per-character (swap-scene) clips done. Trivially True when every scene is
    a direct image — no per-character clips are expected, so the empty-videos
    case must NOT block the phase (that's the all-direct landmine)."""
    swap = set(job.scene_ids) - set(job.direct_scene_ids or [])
    return True if not swap else _videos_terminal(job)


async def _watch_video_phase(re_id: str, job_id: str, *,
                             direct_tasks: list = ()) -> None:
    deadline = asyncio.get_event_loop().time() + _VIDEO_PHASE_TIMEOUT_SECS
    while True:
        await asyncio.sleep(_POLL_SECS)
        job = store().get_job(job_id)
        if job is None:
            _update(re_id, status="failed", error="underlying job disappeared")
            return
        if _swap_videos_done(job) and all(t.done() for t in direct_tasks):
            break
        if asyncio.get_event_loop().time() > deadline:
            _update(re_id, status="failed", error="video phase timed out")
            return
    state = reengineer.load_state(re_id) or {}
    if state.get("auto_mode"):
        await assemble(re_id)
    else:
        # Clip-review gate (Hugo 2026-06-12): without ⚡ fully automatic the
        # run STOPS here so every Kling clip can be inspected — and redone
        # per scene — and the ⚙ Editor settings tweaked, BEFORE the final
        # build. The user continues with ▶ Bygg ihop.
        _update(re_id, status="awaiting_assembly")


# --------------------------------------------------------------------------- phase 3: assemble

# In-process duplicate-assemble guard. Assembly used to be a seconds-long
# local concat where overlap was harmless; it now bills Whisper (+ optional
# ElevenLabs/Remotion) per character and writes the same final_<cid>.mp4
# paths — a double-click or the watcher firing alongside a manual
# "Bygg ihop igen" must not run two builds for the same run.
_ASSEMBLING: set[str] = set()


async def assemble(re_id: str) -> None:
    """Per character: pick the first DONE clip per scene (in scene order,
    FULL length), concat, then finish through the shared Editor pipeline —
    the same flow as Swap Step 6 (Hugo 2026-06-12)."""
    state = reengineer.load_state(re_id)
    if not state or not state.get("job_id"):
        return
    if re_id in _ASSEMBLING:
        _log.info("reengineer %s: assemble already in flight — skipping duplicate", re_id)
        return
    _ASSEMBLING.add(re_id)
    try:
        await _do_assemble(re_id, state)
    except Exception as e:
        _log.exception("reengineer %s assemble failed", re_id)
        _update(re_id, status="failed", error=f"{type(e).__name__}: {e}")
    finally:
        _ASSEMBLING.discard(re_id)


# Editor finishing defaults — mirrors Swap Step 6 EXCEPT voice swap + WPM
# normalize, which default OFF here (Hugo 2026-06-12): Kling clips already
# speak with the character's own lip-synced voice at its natural pace, so
# both are opt-in via the ⚙ panel. Only keys listed here are accepted from
# state["assemble_settings"] (anything else is ignored).
ASSEMBLE_DEFAULTS: dict = {
    # Hugo 2026-06-16: capcut-bluebox at size 60 is the Swap/Reengineer-final
    # standard (the classic Step-6 compile keeps its own purple-pill default).
    # The size rides as a caption style override and is user-tunable in the ⚙
    # panel. (Matches the baked capcut-bluebox template size in video_edit.py.)
    "template": "capcut-bluebox",
    "overrides": {"size": 60},
    "enable_trim": True,
    "enable_captions": True,
    "enable_wpm_normalize": False,
    "target_wpm": 190.0,
    # Hugo 2026-06-17: level-trim base raised to −23 dB / pad 0.05 s (was
    # −30/0.02) — Kling's room tone sits ~−20..−25 dB, so −30 found almost
    # no silence and finals shipped with audible dead time. min-silence
    # stays 0.20 s. See also the opt-in word-gap trim below.
    "threshold_db": -23.0,
    "min_silence_secs": 0.20,
    "pad_secs": 0.05,
    # Opt-in word-gap trim (Hugo 2026-06-17): when ON, the level interior
    # trim is replaced by a Whisper-word-boundary pause cut — robust against
    # Kling room tone. Default OFF; max_gap tunable in the ⚙ panel.
    "enable_gap_trim": False,
    "gap_max_secs": 0.35,
    "enable_voice_swap": False,
    "voice_override": None,
    # Global playback speed (Hugo 2026-06-13 — the Editor tab's Speed
    # control): pitch-preserving, captions stay in sync. 1.0 = off.
    "playback_speed": 1.0,
}


def _assemble_settings(state: dict) -> dict:
    cfg = dict(ASSEMBLE_DEFAULTS)
    stored = state.get("assemble_settings") or {}
    cfg.update({k: v for k, v in stored.items() if k in ASSEMBLE_DEFAULTS})
    return cfg


# Coverage-wait knobs for assembly (2026-06-12, re_57266cfec0): the video
# watcher fired while scene 5's clip was still finishing — the final shipped
# with 5/6 scenes and NO hint. Collection now waits (bounded) for every
# approved scene's clip, and anything still missing becomes a loud warning.
_ASSEMBLE_COVERAGE_WAIT_SECS = 120.0
_ASSEMBLE_COVERAGE_POLL_SECS = 5.0


def _collect_clips(state: dict, jc: JobCharacter) -> tuple[list[Path], list[str], bool]:
    """(clips, missing, waitable) for one character, in state-scene order.

    A scene is EXPECTED when the character has an approved variant for it.
    `missing` names expected scenes without a DONE clip on disk; `waitable`
    is True when at least one missing scene's clip is plausibly still
    coming (row in flight, or no row yet — rows can lag), so the caller may
    poll again instead of building an incomplete final."""
    clips: list[Path] = []
    missing: list[str] = []
    waitable = False
    for e in state["scenes"]:
        if e.get("is_direct"):
            # "Direct image — no swap": ONE shared clip, identical for every
            # character. Append it here (no per-character variant lookup).
            shared = e.get("shared_clip_path")
            if shared and Path(shared).exists():
                clips.append(Path(shared))
            elif e.get("direct_error"):
                missing.append(f"scen {e['idx'] + 1} (direktklipp misslyckades)")
            else:
                missing.append(f"scen {e['idx'] + 1} (direktklipp ej klart)")
                waitable = True
            continue
        vid_variant = _approved_variant_for(jc, e["scene_id"])
        if vid_variant is None:
            # No approved image. A scene the character has NO slots for was
            # never theirs — skip. But slots WITHOUT approval (e.g. the
            # scene-level image regen withdrew them) are a TRUE GAP: report
            # it loudly instead of silently shipping a final without the
            # scene (review 2026-06-13). Not waitable — a clip can only
            # appear after a manual approve + re-animate.
            if any(v.scene_id == e["scene_id"] for v in jc.images):
                missing.append(f"scen {e['idx'] + 1} (ingen godkänd bild)")
            continue
        video = next((vv for vv in jc.videos
                      if vv.status == VideoStatus.DONE
                      and vv.source_variant_id == vid_variant
                      and vv.final_video_path
                      and Path(vv.final_video_path).exists()), None)
        if video is not None:
            clips.append(Path(video.final_video_path))
            continue
        missing.append(f"scen {e['idx'] + 1}")
        rows = [vv for vv in jc.videos
                if vv.source_variant_id == vid_variant]
        if not rows or any(vv.status in {VideoStatus.PENDING,
                                         VideoStatus.PROCESSING}
                           for vv in rows):
            waitable = True
    return clips, missing, waitable


def _assembly_gaps(state: dict, job: Job) -> dict:
    """Pre-flight coverage report for a MANUAL re-assembly ("Bygg ihop igen").

    Hugo 2026-06-17: the rebuild must REFUSE LOUDLY instead of silently
    shipping a stale or shorter final. Returns three buckets naming every
    reason the run can't produce complete, up-to-date finals right now:

      dirty   — scenes edited but not re-animated; their existing clips are
                stale (pre-edit) → the user must ▶ Animera om ändrade first.
      hard    — a scene the character should have whose clip FAILED / is gone,
                or that has slots but no approved image → won't resolve on its
                own (re-animate / approve, then rebuild).
      pending — a scene whose clip is still rendering → simply not ready yet.

    Per-(char, scene) rules mirror _collect_clips EXACTLY so the gate and the
    actual build never disagree. All three buckets empty ⇒ a clean rebuild."""
    entries = state.get("scenes") or []
    dirty = [{"idx": e["idx"], "label": f"scen {e['idx'] + 1}"}
             for e in entries if e.get("dirty")]
    hard: list[dict] = []
    pending: list[dict] = []

    def _gap(bucket: list[dict], cid: str, name: str, e: dict,
             reason: str) -> None:
        bucket.append({"char_id": cid, "name": name, "idx": e["idx"],
                       "label": f"scen {e['idx'] + 1}", "reason": reason})

    for cid, jc in job.characters.items():
        name = jc.name or cid
        for e in entries:
            if e.get("is_direct"):
                shared = e.get("shared_clip_path")
                if shared and Path(shared).exists():
                    continue
                if e.get("direct_error"):
                    _gap(hard, cid, name, e, "direktklipp misslyckades")
                else:
                    _gap(pending, cid, name, e, "direktklipp ej klart")
                continue
            vid = _approved_variant_for(jc, e["scene_id"])
            if vid is None:
                # Slots but no approval = a true gap; no slots = never theirs.
                if any(v.scene_id == e["scene_id"] for v in jc.images):
                    _gap(hard, cid, name, e, "ingen godkänd bild")
                continue
            done = next((vv for vv in jc.videos
                         if vv.status == VideoStatus.DONE
                         and vv.source_variant_id == vid
                         and vv.final_video_path
                         and Path(vv.final_video_path).exists()), None)
            if done is not None:
                continue
            rows = [vv for vv in jc.videos if vv.source_variant_id == vid]
            if not rows or any(vv.status in {VideoStatus.PENDING,
                                             VideoStatus.PROCESSING}
                               for vv in rows):
                _gap(pending, cid, name, e, "klippet renderas fortfarande")
            else:
                _gap(hard, cid, name, e, "klippet saknas/misslyckades")
    return {"dirty": dirty, "hard": hard, "pending": pending}


async def _do_assemble(re_id: str, state: dict) -> None:
    _update(re_id, status="assembling")
    job = store().get_job(state["job_id"])
    if job is None:
        raise RuntimeError("underlying job disappeared")
    run_dir = reengineer.reengineer_dir(re_id)
    cfg = _assemble_settings(state)
    finals: dict[str, dict] = {}

    async def _one_character(cid: str, jc: JobCharacter) -> None:
        try:
            # FULL clips, in state-scene order. The old hard cap at the
            # original scene duration chopped Kling's spoken lines mid-word
            # (scenes are often 1-2s where the line takes 5-10s) — pacing is
            # tightened by the Editor pass cutting SILENCE instead.
            # COVERAGE WAIT (2026-06-12): never build while an approved
            # scene's clip is plausibly still finishing — re_57266cfec0
            # shipped a 5/6-scene final because the watcher fired 3s early.
            deadline = (asyncio.get_event_loop().time()
                        + _ASSEMBLE_COVERAGE_WAIT_SECS)
            jc_now = jc
            while True:
                clips, missing, waitable = _collect_clips(state, jc_now)
                if (not missing or not waitable
                        or asyncio.get_event_loop().time() > deadline):
                    break
                _log.info("reengineer %s %s: waiting for clip(s) still "
                          "finishing: %s", re_id, cid, ", ".join(missing))
                await asyncio.sleep(_ASSEMBLE_COVERAGE_POLL_SECS)
                fresh_job = store().get_job(state["job_id"])
                if fresh_job is not None and cid in fresh_job.characters:
                    jc_now = fresh_job.characters[cid]
            if not clips:
                finals[cid] = {"status": "failed", "error": "no finished clips"}
                return
            # Hugo 2026-06-17: NEVER ship an incomplete final. A scene the
            # character should have (approved variant) but whose clip didn't
            # finish — failed, or still missing after the coverage wait — fails
            # the WHOLE character loudly instead of silently concatenating a
            # shorter video. The user re-animates the gap, then rebuilds (the
            # manual "Bygg ihop igen" endpoint also refuses up front, but this
            # guards the auto-assemble path and any direct call too).
            if missing:
                err = ("finalen saknar " + str(len(missing)) + " scen(er): "
                       + ", ".join(missing) + " — inget färdigt klipp; ta om "
                       "scenen och bygg ihop igen")
                _log.error("reengineer %s %s: %s", re_id, cid, err)
                finals[cid] = {"status": "failed", "error": err,
                               "n_clips": len(clips)}
                return

            # One editor edit_id per character so the result also shows up
            # in the Editor tab (re-render captions etc. without re-billing).
            edit_id = "ed_" + secrets.token_hex(5)
            edit_dir = settings.output_dir / "editor" / edit_id
            edit_dir.mkdir(parents=True, exist_ok=True)
            voice_id = runner_compile._resolve_compile_voice(
                cfg["voice_override"], store().get_character(cid),
                cfg["enable_voice_swap"])

            # Non-fatal step failures (caption render is the known one) must
            # be LOUD: logged + surfaced on the final's card via finals[cid].
            # (Missing-clip coverage is now a hard failure above, not a
            # warning — a final is either complete or it fails.)
            warnings: list[str] = []

            async def _warn(message: str) -> None:
                _log.warning("reengineer %s %s: %s", re_id, cid, message)
                warnings.append(message)

            # The exact dialogue is KNOWN (gate-approved says-clauses in
            # scene order) — bias Whisper toward it so captions stop
            # burning in mis-hearings (backlog #20).
            script_hint = " ".join(
                t for t in (_spoken_text(e)
                            for e in state.get("scenes") or []) if t) or None

            result = await runner_compile.run_editor_pipeline(
                clips,
                edit_id=edit_id, edit_dir=edit_dir,
                template=cfg["template"], overrides=cfg["overrides"],
                enable_trim=cfg["enable_trim"],
                enable_captions=cfg["enable_captions"],
                enable_wpm_normalize=cfg["enable_wpm_normalize"],
                target_wpm=cfg["target_wpm"],
                threshold_db=cfg["threshold_db"],
                min_silence_secs=cfg["min_silence_secs"],
                pad_secs=cfg["pad_secs"],
                enable_gap_trim=cfg["enable_gap_trim"],
                gap_max_secs=cfg["gap_max_secs"],
                voice_id=voice_id,
                playback_speed=cfg["playback_speed"],
                warn=_warn,
                script_hint=script_hint,
            )
            final = run_dir / f"final_{cid}.mp4"
            await asyncio.to_thread(shutil.copyfile, result.final, final)
            finals[cid] = {"status": "done", "final_path": str(final),
                           "n_clips": len(clips), "edit_id": edit_id}
            if warnings:
                finals[cid]["warning"] = "; ".join(warnings)
        except Exception as e:
            finals[cid] = {"status": "failed", "error": f"{type(e).__name__}: {e}"}

    # All characters in parallel, like Step 6 — Whisper calls overlap and the
    # Remotion caption renders are gated process-wide in remotion_render.py.
    await asyncio.gather(*[_one_character(cid, jc)
                           for cid, jc in job.characters.items()])

    ok = [f for f in finals.values() if f["status"] == "done"]
    status = ("done" if len(ok) == len(finals) and ok
              else "partial_success" if ok else "failed")
    _update(re_id, status=status, finals=finals, finals_stale=False,
            completed_at=_now())


# --------------------------------------------------------------------------- edit mode
#
# Opt-in iteration on a run AFTER the default flow finished (or at the gate):
# edit per-scene motion prompts, redo single clips / whole scenes, add scenes
# (uploaded image/video or duplicate), delete + reorder scenes, then rebuild
# finals behind an explicit button. The DEFAULT pipeline above is untouched.
#
# state.json additions: per scenes[i] — `dirty` (prompt/duration changed after
# clips exist, or scene is new/duplicated; cleared by targeted re-animation
# and by the full _do_animate), `source` ("analysis"|"image"|"video"|
# "duplicate"), `transcribing` (transient Whisper prefill). Run level —
# `finals_stale` (clips/scene-list changed since last assemble; cleared by
# _do_assemble), `resume_status` + `reanimate_idxs` + `reanimate_clear_dirty`
# (only while status="reanimating", consumed by the finalizer/crash-resume).

_EDITABLE_RUN_STATES = {"awaiting_approval", "awaiting_assembly",
                        "done", "partial_success", "failed"}

# Generic UGC direction for user-added scenes (mirror of fallback_plans'
# agent-less prompt, sans dialogue). The Whisper prefill appends the spoken
# line when the user uploads a video and asks for transcription.
ADDED_SCENE_PROMPT = (
    "The person continues the action visible in the image naturally, "
    "looking at the camera.")


def _speech_clause(spoken: str) -> str:
    """Dialogue attribution, simple Kling style (Hugo 2026-06-13): accent
    folded into the attribution, nothing else. Gender-neutral — this
    fallback never sees the frames."""
    return (' The person says to the camera with an American accent: '
            f'"{spoken}"')


def _sync_movement_from_state(job: Job, state: dict,
                              idxs: list[int] | None = None) -> None:
    """Push edited per-scene prompts/durations from the run state into the
    underlying job so retry_one_video / generate_more_videos resolve them
    (they read job.movement_prompts[scene_id] + durations_by_scene)."""
    entries = state.get("scenes") or []
    targets = (entries if idxs is None
               else [entries[i] for i in idxs if 0 <= i < len(entries)])
    job.movement_prompts = dict(job.movement_prompts or {})
    job.durations_by_scene = dict(job.durations_by_scene or {})
    enriched = dict(job.enriched_movement_prompts or {})
    for e in targets:
        sid = e["scene_id"]
        job.movement_prompts[sid] = _with_accent(e["motion_prompt"])
        job.durations_by_scene[sid] = _kling_duration(e)
        # Drop any enriched/Director layer for the synced scene — it outranks
        # movement_prompts in the resolver, and the edited text the user SEES
        # must be exactly what the redo clip gets.
        enriched.pop(sid, None)
    job.enriched_movement_prompts = enriched
    if entries:
        first = entries[0]["scene_id"]
        job.movement_prompt = (job.movement_prompts.get(first)
                               or job.movement_prompt)
    store().update_job(job)


async def generate_added_scene(re_id: str, scene_id: str, *,
                               whisper_source: str | None = None) -> None:
    """Background half of '+ Lägg till scen': optional Whisper dialogue
    prefill, then swap-image generation for EVERY character (shared provider
    semaphore; QC runs automatically inside _generate_one_variant). The new
    variants land as normal awaiting-approval slots in the strip."""
    state = reengineer.load_state(re_id)
    if not state or not state.get("job_id"):
        return
    job = store().get_job(state["job_id"])
    if job is None:
        return

    if whisper_source:
        spoken = ""
        try:
            words = await asyncio.to_thread(
                video_edit.transcribe_words, Path(whisper_source), job_id=re_id)
            spoken = " ".join(w.text for w in words).strip()
        except Exception:
            _log.exception("reengineer %s: whisper prefill failed", re_id)
        state = reengineer.load_state(re_id) or state
        for e in state.get("scenes") or []:
            if e.get("scene_id") != scene_id:
                continue
            e.pop("transcribing", None)
            # Prefill the transcribed line when the user opted into "hämta
            # dialog" AND hasn't written their own prompt — now that the
            # default is EMPTY (Hugo 2026-06-17), prefill on empty too (still
            # respects a legacy generic-default scene via the startswith). Fill
            # ONLY the dialogue clause — no generic "continues the action"
            # prefix, so the field stays clean.
            current = e.get("motion_prompt", "") or ""
            if spoken and (not current.strip()
                           or current.startswith(ADDED_SCENE_PROMPT[:40])):
                e["motion_prompt"] = _speech_clause(spoken).strip()
                e["speech"] = spoken
        reengineer.save_state(state)

    sem = asyncio.Semaphore(
        runner._image_concurrency_for_model(runner._swap_image_model(job)))
    await asyncio.gather(*[
        runner.regen_scene_variants(job.job_id, cid, scene_id, sem=sem)
        for cid in job.characters
    ])


async def regen_scene_images_with_prompt(job_id: str, prompt: str | None,
                                         variant_ids: dict[str, str],
                                         change: str | None = None) -> None:
    """Scene-level "ändra bilden" (Hugo 2026-06-13): regenerate ONE scene's
    swap image for EVERY character. `prompt` is the NEW swap prompt
    (Director-rewritten or hand-edited) OR None to keep each slot's existing
    prompt — the latter is used by "byt scenbild", where only the scene
    REFERENCE image changed (`retry_single_variant` leaves the slot's prompt
    untouched when prompt is falsy). `variant_ids` = {char_id: variant_id} — the endpoint picked
    each character's slot and withdrew its approval. Slots regenerate IN
    PLACE (same variant_id) with the shared provider semaphore; QC runs as
    usual inside _generate_one_variant; the prompt persists on each slot so
    later per-slot retries inherit it. `change` = the user's plain-language
    change request, forwarded as the slot's QC intent so the judge treats
    the deviation as requested instead of repairing it back."""
    job = store().get_job(job_id)
    if job is None:
        return
    sem = asyncio.Semaphore(
        runner._image_concurrency_for_model(runner._swap_image_model(job)))
    await asyncio.gather(*[
        runner.retry_single_variant(job_id, cid, vid, prompt,
                                    qc_intent=change, sem=sem)
        for cid, vid in variant_ids.items()
    ])


async def reanimate(re_id: str, idxs: list[int], *,
                    char_id: str | None = None,
                    clear_dirty: bool = True) -> None:
    """Edit-mode animation engine: (re)generate Kling clips for the given
    scene entries — for one character or all. Existing clips are redone IN
    PLACE via retry_one_video (assembly automatically picks the new take);
    scenes without a clip yet (added/duplicated) get their first via
    generate_more_videos. NEVER assembles — the user rebuilds behind the
    explicit button. `clear_dirty=False` for plain redos (the scene's prompt
    wasn't the reason for the redo).

    Wrapped like animate(): the duplicate-trigger guard stops double Kling
    billing, and ANY exception flips the run to `failed` instead of
    stranding it in `reanimating` (which blocked every edit endpoint —
    reanimate used to be the only phase entry-point without a try/except)."""
    if re_id in _ANIMATING:
        _log.warning("reengineer %s: animation already in flight — "
                     "ignoring duplicate trigger", re_id)
        return
    _ANIMATING.add(re_id)
    try:
        await _do_reanimate(re_id, idxs, char_id=char_id,
                            clear_dirty=clear_dirty)
    except Exception as e:
        _log.exception("reengineer %s reanimate failed", re_id)
        _update(re_id, status="failed", error=f"{type(e).__name__}: {e}",
                resume_status=None, reanimate_idxs=None,
                reanimate_clear_dirty=None)
    finally:
        _ANIMATING.discard(re_id)


async def _do_reanimate(re_id: str, idxs: list[int], *,
                        char_id: str | None,
                        clear_dirty: bool) -> None:
    state = reengineer.load_state(re_id)
    if not state or not state.get("job_id"):
        return
    s = store()
    job = s.get_job(state["job_id"])
    if job is None or not (job.movement_prompts or job.movement_prompt):
        return                      # pre-gate: the default animate covers it
    entries = state.get("scenes") or []
    idxs = [i for i in idxs if 0 <= i < len(entries)]
    if not idxs:
        return

    _sync_movement_from_state(job, state, idxs)
    prior = state.get("status")
    resume_to = (prior if prior in {"awaiting_assembly", "done",
                                    "partial_success", "failed"}
                 else "done")

    tasks = []
    acted: list[int] = []
    for i in idxs:
        sid = entries[i]["scene_id"]
        n_before = len(tasks)
        if entries[i].get("is_direct"):
            # Redo of a direct scene re-renders the ONE shared clip (it has no
            # per-character variants). _render_direct_clip overwrites the path.
            tasks.append(_render_direct_clip(re_id, sid))
            acted.append(i)
            continue
        for cid, jc in job.characters.items():
            if char_id and cid != char_id:
                continue
            vid = _approved_variant_for(jc, sid)
            if vid is None:
                continue            # not approved yet — surfaced by the API
            existing = next((v for v in jc.videos
                             if v.source_variant_id == vid), None)
            if existing is not None and existing.status in {
                    VideoStatus.PENDING, VideoStatus.PROCESSING}:
                continue            # already in flight
            if existing is not None:
                tasks.append(runner.retry_one_video(
                    job.job_id, cid, existing.video_id))
            else:
                tasks.append(runner.generate_more_videos(
                    job.job_id, cid, 1, source_variant_id=vid))
        if len(tasks) > n_before:
            acted.append(i)
    # Persist only the idxs that actually spawned a clip task: a scene whose
    # every pair was skipped (e.g. approvals withdrawn by a scene-level image
    # regen still in flight) must KEEP its dirty flag, or the later "Animera
    # om ändrade" has nothing to pick up and the old clip silently ships with
    # the new image (review 2026-06-13).
    _update(re_id, status="reanimating", resume_status=resume_to,
            reanimate_idxs=acted, reanimate_clear_dirty=clear_dirty)
    if tasks:
        await asyncio.gather(*tasks)
    _finalize_reanimate(re_id, clear_dirty=clear_dirty)


def _finalize_reanimate(re_id: str, *, clear_dirty: bool,
                        error: str | None = None) -> None:
    state = reengineer.load_state(re_id) or {"re_id": re_id}
    entries = state.get("scenes") or []
    if clear_dirty:
        for i in state.get("reanimate_idxs") or []:
            if 0 <= i < len(entries):
                entries[i].pop("dirty", None)
    _update(re_id,
            scenes=entries,
            finals_stale=True,
            status=state.get("resume_status") or "done",
            error=error,
            resume_status=None, reanimate_idxs=None,
            reanimate_clear_dirty=None)


async def _resume_reanimating(re_id: str, state: dict) -> None:
    """Crash path: video polling itself is revived by runner.resume_pending
    in the lifespan; we re-attach a watcher that finalizes WITHOUT
    assembling (unlike _watch_video_phase)."""
    job_id = state.get("job_id")
    if not job_id or store().get_job(job_id) is None:
        _update(re_id, status="failed", error="underlying job lost across restart")
        return
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
            st = reengineer.load_state(re_id) or {}
            _update(re_id, status=st.get("resume_status") or "done",
                    error="re-animation timed out",
                    resume_status=None, reanimate_idxs=None,
                    reanimate_clear_dirty=None)
            return
    st = reengineer.load_state(re_id) or {}
    _finalize_reanimate(re_id,
                        clear_dirty=bool(st.get("reanimate_clear_dirty", True)))
