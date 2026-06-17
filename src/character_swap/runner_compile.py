"""
Step 6: Per-character video compile.

When a Swap job has finished all per-(char, scene) videos, one click in
the UI compiles ONE final video per character by:
  1. Picking the FIRST DONE video per scene (in scene_ids order)
  2. Concatenating them into a single MP4
  3. Running the existing Editor pipeline (silence trim → voice swap →
     transcribe → WPM normalize → captions) — same primitives as
     /api/editor/auto_edit, just driven from job state instead of a
     user upload.

All M characters compile in parallel via `asyncio.gather`. Each character's
result lives at `output/<job_id>/compiled/<char_id>.mp4` AND under a fresh
editor edit_id (`output/editor/<edit_id>/`) so users can re-render captions
on the compiled file from the Editor tab without re-running anything else.

Failure is per-character: if char A fails voice swap because the character
has no preset voice, char B still compiles fine. Status flips per char so
the UI can show partial completion.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from character_swap import events, push, video_edit
from character_swap.config import settings
from character_swap.models import CharStatus, Job, JobCharacter, VideoStatus
from character_swap.state import store

logger = logging.getLogger(__name__)


async def _emit(job_id: str, kind: str, char_id: str | None = None, **data) -> None:
    payload = {"kind": kind, "job_id": job_id,
               "ts": datetime.utcnow().isoformat() + "Z"}
    if char_id is not None:
        payload["char_id"] = char_id
    payload.update(data)
    await events.publish(job_id, payload)


def _ordered_scene_videos(
        job: Job, jc: JobCharacter) -> tuple[list[Path], list[str]]:
    """Build the ordered list of per-scene video file paths for one character.

    Iterates `job.scene_ids` in order. For each scene, finds the approved
    variant for THIS character on THAT scene, then picks the first DONE
    VideoVariant whose `source_variant_id` matches.

    Returns (paths, missing): `missing` names every scene that contributes
    NO clip, with the reason. Backlog #9 (2026-06-12): these scenes used to
    be dropped silently — the final shipped with whole lines of dialogue
    absent and status 'done'."""
    scene_ids = list(job.scene_ids) if job.scene_ids else [job.scene_id]
    approved_set = set(jc.approved_variant_ids or [])
    if jc.approved_variant_id:
        approved_set.add(jc.approved_variant_id)

    paths: list[Path] = []
    missing: list[str] = []
    for sid in scene_ids:
        # Approved variant for THIS (char, scene). Variants with
        # scene_id=None map to the first scene (legacy single-scene jobs).
        primary_scene = scene_ids[0]
        approved_variant = next(
            (v for v in jc.images
             if v.variant_id in approved_set
             and (v.scene_id or primary_scene) == sid),
            None,
        )
        if approved_variant is None:
            missing.append(f"{sid} (no approved variant)")
            continue
        # First DONE video whose source_variant_id matches.
        video = next(
            (vv for vv in jc.videos
             if vv.source_variant_id == approved_variant.variant_id
             and vv.status == VideoStatus.DONE
             and vv.final_video_path),
            None,
        )
        if video is None:
            missing.append(f"{sid} (no finished video)")
        elif not Path(video.final_video_path).exists():
            missing.append(f"{sid} (video file missing on disk)")
        else:
            paths.append(Path(video.final_video_path))
    return paths, missing


def _persist_jc(job: Job, jc: JobCharacter, **fields) -> JobCharacter:
    for k, v in fields.items():
        setattr(jc, k, v)
    jc.updated_at = datetime.utcnow()
    job.characters[jc.char_id] = jc
    store().update_job(job)
    return jc


def _resolve_compile_voice(voice_override: str | None, char_asset,
                           enable_voice_swap: bool) -> str | None:
    """Decide which ElevenLabs voice (if any) the compile should swap to.

    Returns None — i.e. KEEP the original generated/Kling audio — when voice
    swap is disabled (Step-6 "Voice swap" unchecked). Otherwise the batch
    `voice_override` wins over the character's library preset voice; an
    empty / absent value means no swap."""
    if not enable_voice_swap:
        return None
    vid = (voice_override or "").strip() or None
    if not vid and char_asset and getattr(char_asset, "voice_id", None):
        vid = (char_asset.voice_id or "").strip() or None
    return vid


class EditorResult(NamedTuple):
    """Outcome of `run_editor_pipeline` — the finished MP4 inside the
    edit dir, plus whether the optional voice swap actually applied."""
    final: Path
    voice_applied: bool


async def run_editor_pipeline(
    paths: list[Path],
    *,
    edit_id: str,
    edit_dir: Path,
    template: str,
    overrides: dict | None,
    enable_trim: bool,
    enable_captions: bool,
    enable_wpm_normalize: bool,
    target_wpm: float,
    threshold_db: float,
    min_silence_secs: float,
    pad_secs: float,
    voice_id: str | None,
    enable_transcribe: bool = True,
    enable_gap_trim: bool = False,
    gap_max_secs: float = 0.35,
    playback_speed: float = 1.0,
    warn=None,
    script_hint: str | None = None,
) -> EditorResult:
    """Concat + Editor finishing, shared by the Step-6 compile and the
    Reengineer assemble: per-clip audio-onset trim → concat → interior
    silence trim → ElevenLabs voice swap (when `voice_id` is set) → Whisper
    transcribe → WPM normalize → caption burn-in. The result lives inside
    `edit_dir` under the given `edit_id`, so it is re-renderable from the
    Editor tab like any other edit.

    `warn` is an optional async callback(message) for non-fatal step
    failures (currently: caption render) — callers surface it their own way.
    """
    # Steps 0.5 + 1 + 2 in ONE encode (2026-06-12): per-clip audio-onset trim
    # (ALWAYS — Hugo 2026-06-11: every clip starts exactly when there's
    # enough sound), interior/trailing silence trim (enable_trim), scale to
    # the target canvas, and concat — a single libx264 generation instead of
    # three. The old three-step chain lost a CRF generation per step; with a
    # ~21 Mbps Kling master the FIRST hop alone measured ~2-3 Mbps.
    concat_out = edit_dir / "00-concat.mp4"
    try:
        await asyncio.to_thread(
            video_edit.assemble_clips, paths, concat_out,
            aspect_ratio=settings.video_aspect_ratio,
            # "Ersätt"-läge (Hugo 2026-06-17): when word-gap trim is on, the
            # level-based interior trim here is SKIPPED — only the always-on
            # per-clip audio-onset trim runs, and the gap trim removes interior
            # pauses post-transcribe below. The two never stack.
            enable_interior_trim=enable_trim and not enable_gap_trim,
            threshold_db=threshold_db,
            min_silence_secs=min_silence_secs,
            pad_secs=pad_secs,
            job_id=edit_id,
        )
        current = concat_out
    except Exception as assemble_err:
        # Reliability first: any failure in the combined pass falls back to
        # the proven legacy chain (3 separate encodes, lower quality but
        # battle-tested) rather than failing the build. NEVER silently
        # (backlog #27): the quality cliff + the original exception must be
        # visible — in the log AND as a warning on the result.
        logger.warning(
            "%s: assemble_clips failed (%s: %s) — falling back to the "
            "legacy 3-encode chain (lower quality)", edit_id,
            type(assemble_err).__name__, assemble_err)
        if warn is not None:
            await warn("single-encode assemble failed "
                       f"({type(assemble_err).__name__}: "
                       f"{str(assemble_err)[:200]}) — legacy multi-encode "
                       "chain used; final has extra encode generations")
        no_lead: list[Path] = []
        for i, p in enumerate(paths):
            cut = edit_dir / f"scene-{i:02d}-noLead.mp4"
            try:
                await asyncio.to_thread(
                    video_edit.trim_leading_silence, p, cut,
                    threshold_db=threshold_db,
                    min_silence_secs=0.05,  # very aggressive — exact start
                    job_id=edit_id,
                )
                no_lead.append(cut)
            except (RuntimeError, ValueError):
                no_lead.append(p)
        paths = no_lead

        await asyncio.to_thread(
            video_edit.concat_videos, paths, concat_out,
            aspect_ratio=settings.video_aspect_ratio,
        )
        current = concat_out

        if enable_trim and not enable_gap_trim:
            trimmed = edit_dir / "01-trimmed.mp4"
            try:
                await asyncio.to_thread(
                    video_edit.trim_silences, current, trimmed,
                    threshold_db=threshold_db,
                    min_silence_secs=min_silence_secs,
                    pad_secs=pad_secs, job_id=edit_id,
                )
                current = trimmed
            except RuntimeError:
                # Trim failed (e.g. no silences detected) — keep going with
                # the un-trimmed source rather than failing the whole compile.
                pass

    # Step 3: voice swap via ElevenLabs (optional — only when the caller
    # resolved a voice AND the key is configured).
    voice_applied = False
    if voice_id and settings.has_provider("elevenlabs"):
        try:
            from character_swap.clients import elevenlabs as _eleven
            tmp_audio_in = edit_dir / "02-original.wav"
            await asyncio.to_thread(
                video_edit._run,
                [video_edit._ffmpeg(), "-y", "-i", str(current),
                 "-vn", "-ac", "1", "-ar", "44100", str(tmp_audio_in)],
            )
            new_audio_bytes = await asyncio.to_thread(
                _eleven.voice_changer,
                voice_id=voice_id,
                source_audio=tmp_audio_in,
                app_job_id=edit_id,
            )
            new_audio = edit_dir / "02-swapped.mp3"
            new_audio.write_bytes(new_audio_bytes)
            swapped = edit_dir / "02-swapped.mp4"
            await asyncio.to_thread(
                video_edit.replace_audio, current, new_audio, swapped,
            )
            current = swapped
            voice_applied = True
            tmp_audio_in.unlink(missing_ok=True)
        except Exception as voice_err:
            # Voice swap is the most fragile step (network / quota /
            # bad audio). Don't fail the whole compile — captions +
            # WPM still produce a usable MP4 without it. But NEVER
            # silently (backlog #26): the cause is logged + surfaced as
            # a warning; account-level errors trip the client breaker so
            # sibling characters in the batch fail fast instead of
            # repeating the doomed upload (measured 15/17 lifetime fails,
            # all the same subscription error).
            logger.warning("%s: voice swap skipped: %s: %s", edit_id,
                           type(voice_err).__name__, voice_err)
            if warn is not None:
                await warn(f"voice swap skipped ({type(voice_err).__name__}: "
                           f"{str(voice_err)[:200]})")

    # Step 4a: transcribe. Needed for: captions, WPM normalize, AND the
    # Resolve-export flow (SRT generated from words.json). `enable_transcribe`
    # defaults True so the words are always available for downstream consumers
    # even when captions + WPM are both off.
    words: list = []
    if (enable_transcribe or enable_captions or enable_wpm_normalize
            or enable_gap_trim):
        try:
            words = await asyncio.to_thread(
                video_edit.transcribe_words, current, job_id=edit_id,
                script_hint=script_hint,
            )
        except Exception:
            words = []

    # Step 4a.5: WORD-GAP TRIM (opt-in, Hugo 2026-06-17). Replaces the
    # level-based interior trim (skipped above) — cuts spoken pauses longer
    # than `gap_max_secs` by Whisper word boundaries, robust against Kling's
    # loud room tone. Re-times `words` onto the trimmed timeline so captions +
    # WPM downstream stay in sync. Passthrough when no qualifying gaps.
    if enable_gap_trim and words:
        try:
            gaptrimmed = edit_dir / "02b-gaptrim.mp4"
            summary, words = await asyncio.to_thread(
                video_edit.trim_word_gaps, current, gaptrimmed, words,
                max_gap_secs=gap_max_secs, job_id=edit_id,
            )
            # `trimmed` is the single source of truth: when True the file was
            # written + words re-timed; when False keep the existing `current`
            # and the original words (avoids a words-vs-video desync at the
            # removed_secs rounding boundary — review 2026-06-17).
            if summary.get("trimmed"):
                current = gaptrimmed
        except Exception as gap_err:
            logger.warning("%s: word-gap trim skipped: %s: %s", edit_id,
                           type(gap_err).__name__, gap_err)
            if warn is not None:
                await warn(f"ordglapp-trim hoppades över "
                           f"({type(gap_err).__name__}: "
                           f"{str(gap_err)[:200]})")

    # (The old Step 4a.5 Whisper-first-word recut was removed 2026-06-11:
    # audio energy is the start marker — Step 0.5's unconditional
    # per-clip audio-onset trim is the contract; sub-threshold ambient
    # before speech is intentional content.)

    # Step 4b: WPM normalize (time-stretch).
    if enable_wpm_normalize and words:
        try:
            speed = video_edit.compute_speed_factor(
                words, target_wpm=target_wpm,
            )
            if abs(speed - 1.0) > 1e-3:
                stretched = edit_dir / "03-stretched.mp4"
                await asyncio.to_thread(
                    video_edit.time_stretch, current, stretched,
                    speed_factor=speed, job_id=edit_id,
                )
                words = video_edit.scale_word_timestamps(words, speed)
                current = stretched
        except Exception:
            pass

    # Step 4b.5: global playback speed (Hugo 2026-06-13 — the same Speed
    # control as the Editor tab). Pitch-preserving time-stretch applied on
    # top of any WPM normalization; word timestamps scale in lockstep so
    # the captions burned below stay perfectly in sync. 1.0 = off.
    speed = max(0.5, min(2.0, float(playback_speed or 1.0)))
    if abs(speed - 1.0) > 1e-3:
        try:
            sped = edit_dir / "035-speed.mp4"
            await asyncio.to_thread(
                video_edit.time_stretch, current, sped,
                speed_factor=speed, job_id=edit_id,
            )
            if words:
                words = video_edit.scale_word_timestamps(words, speed)
            current = sped
        except Exception as speed_err:
            logger.warning("%s: playback speed %.2fx skipped: %s: %s",
                           edit_id, speed, type(speed_err).__name__,
                           speed_err)
            if warn is not None:
                await warn(f"hastighet {speed:g}× hoppades över "
                           f"({type(speed_err).__name__}: "
                           f"{str(speed_err)[:200]})")

    # Persist the transcript NOW (after any WPM scaling) so the Resolve
    # export can build an SRT even when caption burn-in is skipped, AND
    # so re-renders / debug have the canonical word list.
    if words:
        try:
            (edit_dir / "words.json").write_text(
                video_edit.words_to_json(words), encoding="utf-8",
            )
        except OSError:
            pass

    # Step 4c: captions burn-in.
    if enable_captions and words:
        try:
            style = video_edit.style_from_params(template, overrides)
            (edit_dir / "pre_caption.txt").write_text(
                str(current), encoding="utf-8",
            )
            final_out = edit_dir / "04-final.mp4"
            await asyncio.to_thread(
                video_edit.render_captions, current, final_out,
                words=words, style=style, job_id=edit_id,
            )
            current = final_out
        except Exception as e:
            # Caption render is the LAST step; if it fails we still ship
            # the WPM-normalized + voice-swapped result.
            if warn is not None:
                await warn(f"caption render failed: {e}")
    else:
        # No captions → the output IS the pre-caption file. Record that
        # for the Resolve-export endpoint (so it can pick the right video
        # as pre-caption AND skip the duplicate copy).
        try:
            (edit_dir / "pre_caption.txt").write_text(
                str(current), encoding="utf-8",
            )
        except OSError:
            pass

    return EditorResult(final=current, voice_applied=voice_applied)


async def _compile_one_character(
    job_id: str, char_id: str,
    *,
    template: str,
    overrides: dict | None,
    enable_trim: bool,
    enable_captions: bool,
    enable_wpm_normalize: bool,
    target_wpm: float,
    threshold_db: float,
    min_silence_secs: float,
    pad_secs: float,
    voice_override: str | None,
    enable_voice_swap: bool = True,
    enable_transcribe: bool = True,
    enable_gap_trim: bool = False,
    gap_max_secs: float = 0.35,
) -> None:
    """Compile one character's per-scene videos into a single final MP4.

    Mirrors the steps of /api/editor/auto_edit but with an in-memory
    concatenation step prepended. Each step is opt-out via the boolean
    flags; voice swap auto-applies the character's preset voice (or
    `voice_override` if given) UNLESS `enable_voice_swap` is False, in which
    case the original generated/Kling audio is kept untouched.
    """
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return
    jc = job.characters.get(char_id)
    if jc is None:
        return

    # Find the character's preset voice (Phase B). voice_override (batch UI
    # setting) wins over per-character preset. Empty string → no voice swap.
    # When voice swap is disabled (Step-6 "Voice swap" unchecked) we keep the
    # original generated/Kling audio — ignore BOTH the batch override and the
    # character's library preset voice.
    char_asset = s.get_character(char_id)
    effective_voice_id = _resolve_compile_voice(
        voice_override, char_asset, enable_voice_swap)

    # Working dirs: one per-compile editor edit_id (so the result also shows
    # up in the Editor tab's history), plus a tidy `output/<job>/compiled/`
    # location for the per-character final.
    edit_id = "ed_" + secrets.token_hex(5)
    edit_dir = settings.output_dir / "editor" / edit_id
    edit_dir.mkdir(parents=True, exist_ok=True)
    compiled_dir = settings.output_dir / job_id / "compiled"
    compiled_dir.mkdir(parents=True, exist_ok=True)

    _persist_jc(job, jc,
                compile_status="compiling",
                compile_edit_id=edit_id,
                compile_error=None,
                compile_warning=None)
    await _emit(job_id, "char.compile_started",
                char_id=char_id, edit_id=edit_id)

    # Step 0: build ordered scene-video list. Bail if empty; warn LOUDLY
    # when scenes are missing (backlog #9: a final that silently skips
    # scenes ships with missing dialogue and status 'done').
    paths, missing_scenes = _ordered_scene_videos(job, jc)
    if not paths:
        _persist_jc(job, jc,
                    compile_status="failed",
                    compile_error="no DONE videos found for any scene")
        await _emit(job_id, "char.compile_failed",
                    char_id=char_id, error="no DONE videos found")
        return
    scene_warning = None
    if missing_scenes:
        scene_warning = (f"final is missing {len(missing_scenes)} scene(s): "
                         + ", ".join(missing_scenes))
        await _emit(job_id, "char.compile_warning",
                    char_id=char_id, message=scene_warning)

    try:
        pipeline_warnings: list[str] = []

        async def _warn(message: str) -> None:
            pipeline_warnings.append(message)
            await _emit(job_id, "char.compile_warning",
                        char_id=char_id, message=message)

        # Known dialogue from the movement prompts' says-clauses (in scene
        # order) biases Whisper toward the real wording (backlog #20).
        # Lazy import: runner_reengineer imports this module at top level.
        from character_swap.runner_reengineer import _DIALOGUE_RE
        spoken_parts: list[str] = []
        for sid in (job.scene_ids or [job.scene_id]):
            prompt_text = (job.movement_prompts or {}).get(sid) or ""
            spoken_parts += _DIALOGUE_RE.findall(prompt_text)
        script_hint = " ".join(t.strip() for t in spoken_parts
                               if t.strip()) or None

        result = await run_editor_pipeline(
            paths,
            edit_id=edit_id, edit_dir=edit_dir,
            template=template, overrides=overrides,
            enable_trim=enable_trim, enable_captions=enable_captions,
            enable_wpm_normalize=enable_wpm_normalize, target_wpm=target_wpm,
            threshold_db=threshold_db, min_silence_secs=min_silence_secs,
            pad_secs=pad_secs, voice_id=effective_voice_id,
            enable_transcribe=enable_transcribe,
            enable_gap_trim=enable_gap_trim, gap_max_secs=gap_max_secs,
            warn=_warn,
            script_hint=script_hint,
        )

        # Copy the final result to the canonical per-character location so
        # the UI can grab it from `output/<job_id>/compiled/<char_id>.mp4`.
        compiled_final = compiled_dir / f"{char_id}.mp4"
        shutil.copyfile(result.final, compiled_final)

        all_warnings = ([scene_warning] if scene_warning else []) \
            + pipeline_warnings
        combined_warning = "; ".join(all_warnings) or None
        _persist_jc(job, jc,
                    compiled_video_path=str(compiled_final),
                    compile_status="done",
                    compile_error=None,
                    compile_warning=combined_warning)
        await _emit(job_id, "char.compile_done",
                    char_id=char_id, edit_id=edit_id,
                    output_path=str(compiled_final),
                    voice_id=effective_voice_id,
                    voice_applied=result.voice_applied,
                    warning=combined_warning)
    except Exception as e:
        _persist_jc(job, jc,
                    compile_status="failed",
                    compile_error=f"{type(e).__name__}: {e}")
        await _emit(job_id, "char.compile_failed",
                    char_id=char_id, error=str(e))


def _eligible_for_compile(jc: JobCharacter) -> bool:
    """A character can be compiled iff it isn't rejected, has at least one
    approved variant, and has at least one DONE video to concatenate. Shared
    by the target selection and the batch-settled phone push so both agree on
    the denominator."""
    if jc.status == CharStatus.REJECTED:
        return False
    if not (jc.approved_variant_ids or jc.approved_variant_id):
        return False
    return any(
        v.status == VideoStatus.DONE and v.final_video_path
        for v in jc.videos
    )


def _compile_push_spec(ok: int, total: int) -> tuple[str, str, int, list[str]] | None:
    """(title, body, priority, tags) for the batch-settled compile push, or
    None when there's nothing to report (no compilable characters).

    ``ok``/``total`` are counted over the WHOLE job's compilable characters —
    not just this run's targets — so a per-character retry reports true
    job-wide progress instead of a misleading "1/1". A total failure
    (``ok == 0 < total``) is a LOUD failure, never "klara (delvis)" — a batch
    where nothing compiled must not read as partial success on the phone."""
    if total <= 0:
        return None
    if ok == total:
        return ("Slutvideor klara", f"{ok}/{total} karaktarer kompilerade",
                3, ["white_check_mark"])
    if ok == 0:
        return ("Slutvideor misslyckades", f"0/{total} kompilerade",
                5, ["rotating_light"])
    return ("Slutvideor klara (delvis)", f"{ok}/{total} lyckades",
            4, ["warning"])


async def compile_job_videos(
    job_id: str,
    *,
    template: str = "capcut-purple-pill",   # matches the UI default; was submagic-pro
    overrides: dict | None = None,
    enable_trim: bool = True,
    enable_captions: bool = True,
    enable_wpm_normalize: bool = True,
    target_wpm: float = 190.0,
    threshold_db: float = -23.0,
    min_silence_secs: float = 0.30,
    pad_secs: float = 0.04,
    voice_override: str | None = None,
    enable_voice_swap: bool = True,
    char_ids: list[str] | None = None,
    enable_transcribe: bool = True,
    enable_gap_trim: bool = False,
    gap_max_secs: float = 0.35,
) -> None:
    """Fan out compile across every (or selected) approved character. All M
    chars compile in parallel via asyncio.gather. Settings apply uniformly
    — the only per-character thing is the preset voice (and `voice_override`
    takes precedence over it batch-wide if set).

    `enable_transcribe` defaults True so the words.json is always written
    even when captions + WPM normalize are off — the Resolve-export flow
    needs the transcript for SRT generation. Pass False ONLY when you don't
    need any downstream caption work (saves one Whisper call per character).
    """
    job = store().get_job(job_id)
    if job is None:
        return

    # Compile for every approved char by default; allow filter for retry-one.
    # Eligibility (not rejected + an approved variant + a DONE video to concat)
    # is shared with the batch-settled push below via `_eligible_for_compile`.
    targets: list[str] = [
        cid for cid, jc in job.characters.items()
        if (char_ids is None or cid in char_ids) and _eligible_for_compile(jc)
    ]

    if not targets:
        return

    await asyncio.gather(*[
        _compile_one_character(
            job_id, cid,
            template=template, overrides=overrides,
            enable_trim=enable_trim, enable_captions=enable_captions,
            enable_wpm_normalize=enable_wpm_normalize, target_wpm=target_wpm,
            threshold_db=threshold_db, min_silence_secs=min_silence_secs,
            pad_secs=pad_secs, voice_override=voice_override,
            enable_voice_swap=enable_voice_swap,
            enable_transcribe=enable_transcribe,
            enable_gap_trim=enable_gap_trim, gap_max_secs=gap_max_secs,
        )
        for cid in targets
    ])

    # One phone push when the JOB's compile state settles. Counted over every
    # compilable character (not just this run's targets) so a per-character
    # retry reports true job-wide progress instead of a misleading "1/1", and
    # a total failure pushes loudly instead of "klara (delvis)". Per-character
    # pushes are deliberately avoided (they'd spam a 5-char compile). No-op
    # unless NTFY_TOPIC is configured.
    fresh = store().get_job(job_id)
    if fresh is not None:
        eligible = [cid for cid, jc in fresh.characters.items()
                    if _eligible_for_compile(jc)]
        ok = sum(1 for cid in eligible
                 if fresh.characters[cid].compile_status == "done")
        spec = _compile_push_spec(ok, len(eligible))
        if spec is not None:
            title, body, priority, tags = spec
            push.notify(title, body, priority=priority, tags=tags)
