"""
End-to-end B-roll generation runner.

Pipeline (background):
    1. Transcribe the source audio with Whisper (verbose_json, word ts).
    2. Send the transcript text to GPT-4o with Hugo's creative-director
       system prompt → list of {line, prompt} pairs.
    3. For each prompt, generate a video clip:
         - text-to-image (Grok or OpenAI gpt-image-2) to produce a seed frame
         - image-to-video (Grok Imagine, Veo, Kling, etc.) using the seed
       N clips run in parallel, capped by a small concurrency limit so we
       don't melt the provider quotas.
    4. Concat all clips into one video.
    5. Mux the original narration audio over the concat result.
    6. Mark state DONE, write final.mp4 path.

State is written to `output/broll/<broll_id>/state.json` after every
status flip so the polling endpoint can stream progress to the UI.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from character_swap import broll, video_edit
from character_swap.clients import (
    ProviderNotConfigured,
    _stubs,
    google_genai,
    grok,
    kling,
)
from character_swap.config import settings


# At most N clips generate simultaneously — prevents 20 parallel Grok jobs
# from causing rate-limit chaos and lets the UI show smooth progress.
_CLIP_CONCURRENCY = 3

# Max attempts per clip (1 retry by default = 2 total tries). Grok video
# isn't free; unlimited retries would burn budget on persistent provider
# errors. Bump if you want to be more resilient at the cost of $.
_CLIP_MAX_ATTEMPTS = 2

# Minimum duration we'll request from the video model. Grok Imagine appears
# to reject anything under ~5s; floor here so very short phrases still get
# *a* clip we can trim down at concat time.
_MIN_PROVIDER_DURATION_SECS = 5

# Hard guard appended to every image-gen prompt. Grok's text-to-image model
# tends to interpret "BEFORE → TRIGGER → AFTER" language as a literal
# split-screen / vertical-strip layout (real fail we observed: 3-panel
# storyboard of a swollen ankle). This suffix slaps the constraint directly
# onto the image-gen call so even if the LLM's planned prompt is
# ambiguous, the image model still produces a single continuous frame.
_IMAGE_GUARD_SUFFIX = (
    "\n\nIMAGE COMPOSITION RULES (these override anything above): "
    "Produce ONE single photographic frame from a single camera position. "
    "Never a split-screen, never a before/after side-by-side, never a "
    "vertical strip, never a horizontal strip, never a 2x2 or 3x3 grid, "
    "never stacked panels, never a comic-strip storyboard, never multiple "
    "stages of a transformation shown together in one image. The frame "
    "captures a SINGLE MOMENT in time — the opening of the clip — and any "
    "transformation described will happen across the video's time, not "
    "across the frame's space. Single seamless image only."
)


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _update_state(broll_id: str, **changes) -> dict:
    state = broll.load_state(broll_id) or {}
    state.update(changes)
    state["updated_at"] = _now()
    broll.save_state(state)
    return state


def _update_clip(broll_id: str, idx: int, **changes) -> dict:
    state = broll.load_state(broll_id) or {}
    clips = state.get("clips", [])
    if 0 <= idx < len(clips):
        # Stamp updated_at on the clip itself so the UI can show
        # "running 42s" per clip without a separate endpoint.
        changes.setdefault("updated_at", _now())
        # If the status is flipping to a new transient state, also stamp a
        # "transition_at" so the elapsed-time counter only measures TIME
        # IN CURRENT STATE, not total time since the clip was queued.
        if "status" in changes and changes["status"] != clips[idx].get("status"):
            changes["transition_at"] = changes.get("updated_at")
        clips[idx].update(changes)
    state["clips"] = clips
    state["updated_at"] = _now()
    broll.save_state(state)
    return state


async def run_broll(broll_id: str) -> None:
    """Main entry. Kicked off as an asyncio background task by the API.

    Runs through generation only — stops at `awaiting_approval` so the
    user can review each clip and reject ones they don't like. The
    finalize step (trim + concat + mux) is kicked off separately by
    `POST /api/broll/{id}/finalize`.
    """
    state = broll.load_state(broll_id)
    if not state:
        return
    try:
        await _do_run_generation(broll_id, state)
    except ProviderNotConfigured as e:
        _update_state(broll_id, status="failed", error=str(e),
                      completed_at=_now())
    except Exception as e:
        _update_state(broll_id, status="failed",
                      error=f"{type(e).__name__}: {e}",
                      completed_at=_now())


async def finalize_broll(broll_id: str) -> None:
    """Trim + concat + mux. Called as a background task after the user
    reviews clips in `awaiting_approval` and clicks Finalize."""
    state = broll.load_state(broll_id)
    if not state:
        return
    try:
        await _do_run_finalize(broll_id, state)
    except Exception as e:
        _update_state(broll_id, status="failed",
                      error=f"{type(e).__name__}: {e}",
                      completed_at=_now())


def _prev_chained_video_path(broll_id: str, idx: int) -> Path | None:
    """If clip `idx` is part of a scene group, return the most recent
    DONE upstream clip's video_path so the regen can chain from it.
    Returns None if the clip is solo, has no upstream, or upstream isn't
    available yet.
    """
    state = broll.load_state(broll_id) or {}
    clips = state.get("clips") or []
    if idx <= 0 or idx >= len(clips):
        return None
    this_group = (clips[idx].get("scene_group") or "").strip()
    if not this_group:
        return None
    # Walk backward from idx-1 to find the nearest done clip in the same group.
    for j in range(idx - 1, -1, -1):
        c = clips[j]
        if (c.get("scene_group") or "").strip() != this_group:
            # Different (or empty) group — stop; group boundary reached.
            break
        if c.get("status") == "done" and c.get("video_path"):
            p = Path(c["video_path"])
            if p.exists():
                return p
    return None


async def regenerate_clip(broll_id: str, idx: int) -> None:
    """Re-run generation for a single clip — used when the user rejects
    a clip in the awaiting_approval review. Re-uses the existing line +
    prompt + target_duration_secs; just bumps the attempt counter. If
    the clip is in a scene group, the regen chains from the previous
    DONE clip in that same group, preserving visual continuity."""
    state = broll.load_state(broll_id)
    if not state:
        return
    clips = state.get("clips") or []
    if idx < 0 or idx >= len(clips):
        return
    clip = clips[idx]
    work = broll.broll_dir(broll_id)
    clip_dir = work / "clips"
    video_model = state.get("video_model") or "grok-imagine"
    aspect_ratio = state.get("aspect_ratio") or "9:16"
    target = int(clip.get("target_duration_secs") or _MIN_PROVIDER_DURATION_SECS)
    prompt = clip.get("prompt") or ""
    prev_path = _prev_chained_video_path(broll_id, idx)
    try:
        await _generate_one_clip(
            broll_id, idx, prompt, video_model, clip_dir,
            target_duration_secs=target,
            aspect_ratio=aspect_ratio,
            prev_video_path=prev_path,
        )
    except Exception as e:
        _update_clip(broll_id, idx, status="failed",
                     error=f"{type(e).__name__}: {e}")


async def _do_run_generation(broll_id: str, state: dict) -> None:
    """Phase 1: transcribe → plan → generate clips → retry failures.
    Stops at `awaiting_approval` so the user can review."""
    audio_path = Path(state["audio_path"])
    video_model = state.get("video_model") or "grok-imagine"
    aspect_ratio = state.get("aspect_ratio") or "9:16"
    work = broll.broll_dir(broll_id)

    # --- 1. transcribe -------------------------------------------------------
    _update_state(broll_id, status="transcribing")
    words = await asyncio.to_thread(
        video_edit.transcribe_words, audio_path, job_id=broll_id,
    )
    transcript_text = " ".join(w.text for w in words).strip()
    (work / "words.json").write_text(
        video_edit.words_to_json(words), encoding="utf-8"
    )
    _update_state(broll_id, transcript=transcript_text,
                  n_words=len(words))

    if not transcript_text:
        _update_state(broll_id, status="failed",
                      error="Transcript was empty — check your audio",
                      completed_at=_now())
        return

    # --- 2. plan visuals -----------------------------------------------------
    _update_state(broll_id, status="planning")
    planned = await asyncio.to_thread(
        broll.plan_visuals, transcript_text, broll_id=broll_id,
        aspect_ratio=aspect_ratio,
    )
    if not planned:
        raw_path = work / "plan_raw.txt"
        hint = (f" — raw LLM output saved to {raw_path.name} for inspection"
                if raw_path.exists() else "")
        _update_state(broll_id, status="failed",
                      error=("LLM returned no valid LINE:/PROMPT: pairs"
                             + hint),
                      completed_at=_now())
        return

    # --- 2b. map each planned line back to its Whisper timestamp window ----
    # Each clip's `timing.duration` is how long it should play in the final
    # video (gap-inclusive, so silences between phrases fold into the
    # preceding clip). `target_duration_secs` is what we request from the
    # video model: spoken_duration + 1s margin, floored at the provider
    # minimum.
    import math
    audio_total = await asyncio.to_thread(video_edit._probe_duration, audio_path)
    timings = broll.match_lines_to_timestamps(planned, words, audio_total)

    clip_dir = work / "clips"
    clip_dir.mkdir(parents=True, exist_ok=True)
    clips = []
    for i, p in enumerate(planned):
        t = timings[i]
        # +1s safety margin so we never come up short of audio when trimming.
        target = max(_MIN_PROVIDER_DURATION_SECS,
                     int(math.ceil(t["spoken_duration"] + 1)))
        clips.append({
            "idx": i,
            "line": p.line,
            "mode": p.mode,
            "scene_group": p.scene_group,  # empty = standalone; non-empty = chained
            "prompt": p.prompt,
            "status": "pending",
            "attempts": 0,
            "timing": t,
            "target_duration_secs": target,
            "image_url": None,
            "video_url": None,
            "video_path": None,
            "error": None,
        })

    (work / "plan.json").write_text(
        __import__("json").dumps(
            [{"line": c["line"], "mode": c["mode"], "prompt": c["prompt"],
              "timing": c["timing"], "target_duration_secs": c["target_duration_secs"]}
             for c in clips],
            indent=2,
        ),
        encoding="utf-8",
    )
    _update_state(broll_id, status="generating_clips", clips=clips,
                  n_clips=len(clips), audio_duration_secs=round(audio_total, 2))

    # --- 3. generate clips: groups in parallel, clips within a group strictly
    # sequential so each later clip in a scene group can chain its seed
    # image from the previous clip's last frame. Solo clips (no scene_group)
    # become singleton groups — same code path, just one iteration each.
    sem = asyncio.Semaphore(_CLIP_CONCURRENCY)

    # Bucket clips by scene_group. Empty/missing group = unique singleton key
    # per clip so each lives in its own group (same as the old all-parallel
    # behaviour for those).
    groups: dict[str, list[int]] = {}
    for c in clips:
        key = (c.get("scene_group") or "").strip() or f"__solo__{c['idx']}"
        groups.setdefault(key, []).append(c["idx"])

    async def gen_one(i: int, prev_video_path: Path | None = None) -> None:
        cur = broll.load_state(broll_id) or {}
        cur_clip = (cur.get("clips") or [{}] * (i + 1))[i]
        target = cur_clip.get("target_duration_secs") or _MIN_PROVIDER_DURATION_SECS
        prompt = cur_clip.get("prompt") or ""
        await _generate_one_clip(
            broll_id, i, prompt, video_model, clip_dir,
            target_duration_secs=int(target),
            aspect_ratio=aspect_ratio,
            prev_video_path=prev_video_path,
        )

    async def gen_group(group_idxs: list[int]) -> None:
        """Generate one scene group strictly in order. The semaphore is
        held for the whole group, so cross-group parallelism is capped at
        _CLIP_CONCURRENCY but within-group ordering is preserved."""
        async with sem:
            prev_video_path: Path | None = None
            for idx in sorted(group_idxs):
                await gen_one(idx, prev_video_path=prev_video_path)
                # Re-load to find the just-finished clip's video path.
                cur = broll.load_state(broll_id) or {}
                cclip = ((cur.get("clips") or []) + [{}])[idx]
                if (cclip.get("status") == "done"
                        and cclip.get("video_path")
                        and Path(cclip["video_path"]).exists()):
                    prev_video_path = Path(cclip["video_path"])
                else:
                    # Failure breaks the chain: the next clip in the group
                    # falls back to fresh-seed generation so we don't
                    # cascade one failure into N.
                    prev_video_path = None

    # First pass: kick every group concurrently.
    await asyncio.gather(
        *(gen_group(idxs) for idxs in groups.values()),
        return_exceptions=True,
    )

    # Retry pass(es): only the failed ones. Sequential so a flaky provider
    # doesn't get hammered with the same N parallel requests that just
    # rate-limited it. _CLIP_MAX_ATTEMPTS == 2 means: 1 initial + 1 retry.
    for attempt in range(2, _CLIP_MAX_ATTEMPTS + 1):
        cur_state = broll.load_state(broll_id) or state
        to_retry = [c["idx"] for c in (cur_state.get("clips") or [])
                    if c.get("status") == "failed" and (c.get("attempts") or 0) < attempt]
        if not to_retry:
            break
        _update_state(broll_id, status=f"retrying_clips (attempt {attempt})")
        for i in to_retry:
            # If this clip belongs to a scene group, re-chain from the
            # previous DONE clip in the same group (if any) so retries
            # preserve continuity.
            prev_path = _prev_chained_video_path(broll_id, i)
            await gen_one(i, prev_video_path=prev_path)
        _update_state(broll_id, status="generating_clips")

    # Re-load post-retries.
    state = broll.load_state(broll_id) or state
    all_clips = state.get("clips", [])
    successful = [c for c in all_clips
                  if c.get("status") == "done" and c.get("video_path")]
    if not successful:
        _update_state(broll_id, status="failed",
                      error="All clip generations failed — check per-clip errors",
                      completed_at=_now())
        return

    # --- 4. PAUSE: hand off to user for review -----------------------------
    # Set status to awaiting_approval and stop here. The user reviews each
    # clip and can call `POST /api/broll/{id}/regenerate_clip` to retry any
    # they don't like, or `POST /api/broll/{id}/finalize` when they're
    # happy. The finalize endpoint kicks off `_do_run_finalize` as a
    # separate background task.
    _update_state(broll_id, status="awaiting_approval")


async def _do_run_finalize(broll_id: str, state: dict) -> None:
    """Phase 2: trim each clip to its allotted duration, concat, mux audio.
    Triggered by the user clicking Finalize after reviewing clips."""
    audio_path = Path(state["audio_path"])
    aspect_ratio = state.get("aspect_ratio") or "9:16"
    work = broll.broll_dir(broll_id)
    clip_dir = work / "clips"
    all_clips = state.get("clips", [])
    successful = [c for c in all_clips
                  if c.get("status") == "done" and c.get("video_path")]
    failed_remaining = [c for c in all_clips if c.get("status") == "failed"]
    if not successful:
        _update_state(broll_id, status="failed",
                      error="No successful clips to concatenate",
                      completed_at=_now())
        return

    # --- 4. trim each clip to its allotted duration, then concat -----------
    _update_state(broll_id, status="concatenating")
    trimmed_paths: list[Path] = []
    for c in successful:
        allotted = float((c.get("timing") or {}).get("duration") or 0.0)
        src = Path(c["video_path"])
        if allotted < 0.1:
            # Degenerate timing — fall back to using the full generated clip.
            trimmed_paths.append(src)
            continue
        trimmed = clip_dir / f"clip-{c['idx']:02d}-trimmed.mp4"
        try:
            await asyncio.to_thread(
                video_edit.trim_range, src, trimmed,
                start_secs=0.0, end_secs=allotted,
            )
            trimmed_paths.append(trimmed)
        except Exception:
            # If the generated clip is shorter than the allotted slot
            # (provider gave us less than we asked for), fall back to the
            # untrimmed clip; concat normalizer will pad-or-crop as needed.
            trimmed_paths.append(src)

    concat_path = work / "concat.mp4"
    await asyncio.to_thread(
        video_edit.concat_videos, trimmed_paths, concat_path,
        aspect_ratio=aspect_ratio,
    )

    # --- 5. mux audio over the concatenated track --------------------------
    final_path = work / "final.mp4"
    await asyncio.to_thread(
        video_edit.replace_audio, concat_path, audio_path, final_path,
    )
    concat_path.unlink(missing_ok=True)
    # Tidy up intermediate trimmed clips; keep the originals for transparency.
    for tp in trimmed_paths:
        if "-trimmed.mp4" in tp.name:
            tp.unlink(missing_ok=True)

    final_status = "partial_success" if failed_remaining else "done"
    _update_state(
        broll_id,
        status=final_status,
        final_video_path=str(final_path),
        final_video_url=f"/files/output/broll/{broll_id}/{final_path.name}",
        n_failed_clips=len(failed_remaining),
        completed_at=_now(),
    )


async def _generate_one_clip(broll_id: str, idx: int, prompt: str,
                             video_model: str, clip_dir: Path,
                             *, target_duration_secs: int,
                             aspect_ratio: str = "9:16",
                             prev_video_path: Path | None = None) -> None:
    """Text → image → video for one clip. Updates the per-clip state inline.

    For models that support direct text-to-video (Veo, some Sora tiers),
    the image step is skipped. Grok Imagine requires a seed image, so the
    pipeline always produces one first when the chosen model is Grok.

    `target_duration_secs` is computed by the caller from the matched
    Whisper timestamps (spoken_duration + 1s margin), so the model gets
    a clip roughly the right length for its phrase. The final concat trims
    it down to exactly the allotted slot.

    `prev_video_path` is set when this clip is a continuation step in a
    scene group (recipe step 2/3, skincare step 2/3, etc.). When set, we
    extract the LAST FRAME of that previous clip and use it as the seed
    image for THIS clip's video gen — so the new clip starts where the
    prior ended (same glass, same lighting, cumulative state). If the
    frame extraction fails, we silently fall back to a fresh seed.
    """
    image_path = clip_dir / f"clip-{idx:02d}.png"
    video_path = clip_dir / f"clip-{idx:02d}.mp4"

    # Bump attempts BEFORE the work, so a hard crash still leaves a record.
    # The attempt count drives two things: (1) cache-busting query params
    # on the served URLs so the browser doesn't show the rejected clip
    # from cache, (2) a variation hint added to the prompt so the image
    # model doesn't reproduce the same composition.
    cur = broll.load_state(broll_id) or {}
    cur_clips = cur.get("clips") or []
    prev_attempts = (cur_clips[idx].get("attempts") if idx < len(cur_clips) else 0) or 0
    this_attempt = prev_attempts + 1
    _update_clip(broll_id, idx, attempts=this_attempt, error=None)

    # Delete any previous image/video for this slot — the rejected output
    # must NOT survive into the new attempt (and it'll be overwritten
    # anyway, but explicit unlink avoids any race where the old file is
    # still on disk if the new gen fails mid-write).
    image_path.unlink(missing_ok=True)
    video_path.unlink(missing_ok=True)

    # On retries, append a variation hint so the image model doesn't
    # produce a near-duplicate of the rejected attempt. The user pressed
    # ✕ for a reason — give them something visibly different to react to.
    variation_hint = ""
    if this_attempt > 1:
        variation_hint = (
            "\n\nThis is a regeneration attempt — the previous version was "
            "rejected. Produce a visually DISTINCT interpretation from any "
            "prior attempt: pick a noticeably different camera angle, "
            "different framing distance, different specific orientation of "
            "the body part, different background environment, and different "
            "lighting direction. Keep the anatomical subject and the "
            "transformation arc the same, but change the cinematography."
        )

    # Append the duration hint + variation hint + (when chained) the
    # scene-continuity hint. All three are technical / workflow notes
    # that go to the image+video models but NOT into the stored
    # LINE/PROMPT plan.
    continuity_hint = ""
    if prev_video_path is not None:
        continuity_hint = (
            "\n\nSCENE CONTINUITY: This shot continues directly from the "
            "previous shot in the same scene group. The starting frame is "
            "literally the LAST FRAME of the previous clip — same camera "
            "position, same lighting, same objects, same cumulative state. "
            "Do NOT reset the scene, do NOT change the camera angle, do "
            "NOT introduce a new environment. The only thing that should "
            "change is the new action described in the prompt being added "
            "on top of what's already in frame."
        )

    prompt_with_duration = (
        f"{prompt}\n\nClip duration target: ~{target_duration_secs} seconds."
        f"{variation_hint}{continuity_hint}"
    )
    # The image-gen prompt gets the variation hint too — without it the
    # text-to-image step would happily re-roll something nearly identical.
    # Plus the hard image-composition guard suffix — defends against Grok
    # text-to-image's tendency to produce 3-panel storyboards when prompts
    # describe a transformation. See `_IMAGE_GUARD_SUFFIX` docstring.
    image_prompt = f"{prompt}{variation_hint}{_IMAGE_GUARD_SUFFIX}"

    try:
        _update_clip(broll_id, idx, status="image_running")

        seed_image: Path | None = None  # set below by one of three branches

        if video_model in {"veo", "veo-3-fast"}:
            # Veo accepts pure text input — skip the image-gen detour.
            seed_image = None
        elif prev_video_path is not None:
            # CONTINUATION CLIP in a scene group: use the previous clip's
            # last frame as the seed image so the new clip starts where
            # the prior one ended. No text-to-image call needed — Grok
            # video accepts any local image path.
            extracted = await asyncio.to_thread(
                video_edit.extract_last_frame, prev_video_path, image_path,
            )
            if extracted is not None and extracted.exists():
                seed_image = image_path
                _update_clip(broll_id, idx, status="image_done",
                             image_url=f"/files/output/broll/{broll_id}/clips/{image_path.name}?v={this_attempt}&from=prev")
            else:
                # Last-frame extraction failed (corrupt / too-short
                # upstream clip). Silently fall back to fresh-seed gen
                # below so a downstream failure doesn't cascade.
                seed_image = None

        if seed_image is None and video_model not in {"veo", "veo-3-fast"}:
            # Standard path: Grok text-to-image seed. Keeps the whole
            # pipeline inside xAI's stylistic space, so the seed and the
            # Grok Imagine video that consumes it share the same visual
            # training. On retries the prompt has a variation hint
            # appended so we don't re-roll the same composition.
            img_bytes = await asyncio.to_thread(
                grok.generate_image,
                prompt=image_prompt,
                character="broll",
                aspect_ratio=aspect_ratio,
                app_job_id=broll_id,
            )
            image_path.write_bytes(img_bytes)
            seed_image = image_path
            # ?v=N busts any browser/CDN cache of the previous attempt's
            # image at the same path.
            _update_clip(broll_id, idx, status="image_done",
                         image_url=f"/files/output/broll/{broll_id}/clips/{image_path.name}?v={this_attempt}")

        _update_clip(broll_id, idx, status="video_running")

        if video_model == "grok-imagine":
            assert seed_image is not None
            provider_id = await asyncio.to_thread(
                grok.submit, image=seed_image, prompt=prompt_with_duration,
                character="broll", duration_secs=target_duration_secs,
                app_job_id=broll_id,
            )
            from character_swap import pipeline
            await asyncio.to_thread(
                pipeline.wait_for_video,
                job_id=provider_id, character_name="broll",
                dest=video_path, app_job_id=broll_id,
            )
        elif video_model in {"veo", "veo-3-fast"}:
            op_id = await asyncio.to_thread(
                google_genai.submit_veo,
                image=seed_image,           # may be None — Veo handles text-only
                prompt=prompt_with_duration,
                aspect_ratio=aspect_ratio,
                duration_secs=target_duration_secs,
                app_job_id=broll_id,
            )
            await asyncio.to_thread(google_genai.wait_for_veo,
                                    op_id=op_id, dest=video_path)
        elif video_model in {"kling", "kling-2.1-pro", "kling-1.6"}:
            assert seed_image is not None
            task_id = await asyncio.to_thread(
                kling.submit_kling,
                image=seed_image, prompt=prompt_with_duration,
                aspect_ratio=aspect_ratio,
                duration_secs=target_duration_secs,
                app_job_id=broll_id,
            )
            await asyncio.to_thread(kling.wait_for_kling,
                                    task_id=task_id, dest=video_path)
        else:
            # Anything else routed through the stub adapter set — works the
            # moment the corresponding provider is wired in real form.
            st = _stubs
            submit_fn = {
                "runway-gen4": st.submit_runway, "runway-gen3-alpha": st.submit_runway,
                "luma-ray2": st.submit_luma, "pika-2": st.submit_pika,
                "hailuo-02": st.submit_minimax, "hailuo-01": st.submit_minimax,
                "wan-2.2": st.submit_wan, "wan-2.1": st.submit_wan,
                "seedance-1.0": st.submit_seedance, "sora-2": st.submit_sora,
            }.get(video_model)
            wait_fn = {
                "runway-gen4": st.wait_for_runway, "runway-gen3-alpha": st.wait_for_runway,
                "luma-ray2": st.wait_for_luma, "pika-2": st.wait_for_pika,
                "hailuo-02": st.wait_for_minimax, "hailuo-01": st.wait_for_minimax,
                "wan-2.2": st.wait_for_wan, "wan-2.1": st.wait_for_wan,
                "seedance-1.0": st.wait_for_seedance, "sora-2": st.wait_for_sora,
            }.get(video_model)
            if not submit_fn or not wait_fn:
                raise ValueError(f"Unsupported video_model for b-roll: {video_model}")
            assert seed_image is not None
            task_id = await asyncio.to_thread(
                submit_fn, image=seed_image, prompt=prompt_with_duration,
                aspect_ratio=aspect_ratio,
                duration_secs=target_duration_secs,
                app_job_id=broll_id,
            )
            await asyncio.to_thread(wait_fn, task_id=task_id, dest=video_path)

        _update_clip(broll_id, idx, status="done",
                     video_path=str(video_path),
                     video_url=f"/files/output/broll/{broll_id}/clips/{video_path.name}?v={this_attempt}")

    except ProviderNotConfigured as e:
        _update_clip(broll_id, idx, status="failed", error=str(e))
    except Exception as e:
        _update_clip(broll_id, idx, status="failed",
                     error=f"{type(e).__name__}: {e}")
