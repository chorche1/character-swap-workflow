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
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from character_swap import events, video_edit
from character_swap.config import settings
from character_swap.models import CharStatus, Job, JobCharacter, VideoStatus
from character_swap.state import store


async def _emit(job_id: str, kind: str, char_id: str | None = None, **data) -> None:
    payload = {"kind": kind, "job_id": job_id,
               "ts": datetime.utcnow().isoformat() + "Z"}
    if char_id is not None:
        payload["char_id"] = char_id
    payload.update(data)
    await events.publish(job_id, payload)


def _ordered_scene_videos(job: Job, jc: JobCharacter) -> list[Path]:
    """Build the ordered list of per-scene video file paths for one character.

    Iterates `job.scene_ids` in order. For each scene, finds the approved
    variant for THIS character on THAT scene, then picks the first DONE
    VideoVariant whose `source_variant_id` matches. Skips scenes with no
    DONE video (with a console warning — caller's job to surface)."""
    scene_ids = list(job.scene_ids) if job.scene_ids else [job.scene_id]
    approved_set = set(jc.approved_variant_ids or [])
    if jc.approved_variant_id:
        approved_set.add(jc.approved_variant_id)

    paths: list[Path] = []
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
            continue
        # First DONE video whose source_variant_id matches.
        video = next(
            (vv for vv in jc.videos
             if vv.source_variant_id == approved_variant.variant_id
             and vv.status == VideoStatus.DONE
             and vv.final_video_path),
            None,
        )
        if video and Path(video.final_video_path).exists():
            paths.append(Path(video.final_video_path))
    return paths


def _persist_jc(job: Job, jc: JobCharacter, **fields) -> JobCharacter:
    for k, v in fields.items():
        setattr(jc, k, v)
    jc.updated_at = datetime.utcnow()
    job.characters[jc.char_id] = jc
    store().update_job(job)
    return jc


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
    enable_transcribe: bool = True,
) -> None:
    """Compile one character's per-scene videos into a single final MP4.

    Mirrors the steps of /api/editor/auto_edit but with an in-memory
    concatenation step prepended. Each step is opt-out via the boolean
    flags; voice swap auto-applies the character's preset voice (or
    `voice_override` if given).
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
    char_asset = s.get_character(char_id)
    effective_voice_id: str | None = (voice_override or "").strip() or None
    if not effective_voice_id and char_asset and char_asset.voice_id:
        effective_voice_id = char_asset.voice_id.strip() or None

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
                compile_error=None)
    await _emit(job_id, "char.compile_started",
                char_id=char_id, edit_id=edit_id)

    # Step 0: build ordered scene-video list. Bail if empty.
    paths = _ordered_scene_videos(job, jc)
    if not paths:
        _persist_jc(job, jc,
                    compile_status="failed",
                    compile_error="no DONE videos found for any scene")
        await _emit(job_id, "char.compile_failed",
                    char_id=char_id, error="no DONE videos found")
        return

    try:
        # Step 1: concat per-scene MP4s into one.
        concat_out = edit_dir / "00-concat.mp4"
        await asyncio.to_thread(
            video_edit.concat_videos, paths, concat_out,
            aspect_ratio=settings.video_aspect_ratio,
        )
        current = concat_out

        # Step 2: trim silences (optional). Skipped when user disables it.
        if enable_trim:
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

        # Step 3: voice swap via ElevenLabs (optional — only if char has a
        # preset OR the batch override is set, AND the key is configured).
        swap_summary: dict | None = None
        if effective_voice_id and settings.has_provider("elevenlabs"):
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
                    voice_id=effective_voice_id,
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
                swap_summary = {"voice_id": effective_voice_id}
                tmp_audio_in.unlink(missing_ok=True)
            except Exception:
                # Voice swap is the most fragile step (network / quota /
                # bad audio). Don't fail the whole compile — captions +
                # WPM still produce a usable MP4 without it.
                pass

        # Step 4a: transcribe. Needed for: captions, WPM normalize, AND the
        # Resolve-export flow (SRT generated from words.json). `enable_transcribe`
        # defaults True so the words are always available for downstream consumers
        # even when captions + WPM are both off.
        words: list = []
        if enable_transcribe or enable_captions or enable_wpm_normalize:
            try:
                words = await asyncio.to_thread(
                    video_edit.transcribe_words, current, job_id=edit_id,
                )
            except Exception:
                words = []

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
                await _emit(job_id, "char.compile_warning",
                            char_id=char_id,
                            message=f"caption render failed: {e}")
        else:
            # No captions → the compiled output IS the pre-caption file. Record
            # that for the Resolve-export endpoint (so it can pick the right
            # video as pre-caption AND skip the duplicate copy).
            try:
                (edit_dir / "pre_caption.txt").write_text(
                    str(current), encoding="utf-8",
                )
            except OSError:
                pass

        # Copy the final result to the canonical per-character location so
        # the UI can grab it from `output/<job_id>/compiled/<char_id>.mp4`.
        compiled_final = compiled_dir / f"{char_id}.mp4"
        shutil.copyfile(current, compiled_final)

        _persist_jc(job, jc,
                    compiled_video_path=str(compiled_final),
                    compile_status="done",
                    compile_error=None)
        await _emit(job_id, "char.compile_done",
                    char_id=char_id, edit_id=edit_id,
                    output_path=str(compiled_final),
                    voice_id=effective_voice_id,
                    voice_applied=swap_summary is not None)
    except Exception as e:
        _persist_jc(job, jc,
                    compile_status="failed",
                    compile_error=f"{type(e).__name__}: {e}")
        await _emit(job_id, "char.compile_failed",
                    char_id=char_id, error=str(e))


async def compile_job_videos(
    job_id: str,
    *,
    template: str = "submagic-pro",
    overrides: dict | None = None,
    enable_trim: bool = True,
    enable_captions: bool = True,
    enable_wpm_normalize: bool = True,
    target_wpm: float = 190.0,
    threshold_db: float = -30.0,
    min_silence_secs: float = 0.4,
    pad_secs: float = 0.05,
    voice_override: str | None = None,
    char_ids: list[str] | None = None,
    enable_transcribe: bool = True,
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
    targets: list[str] = []
    for cid, jc in job.characters.items():
        if char_ids is not None and cid not in char_ids:
            continue
        # Skip rejected / never-approved chars; we need at least one
        # approved variant + one DONE video to have anything to compile.
        if jc.status == CharStatus.REJECTED:
            continue
        if not (jc.approved_variant_ids or jc.approved_variant_id):
            continue
        has_any_done = any(
            v.status == VideoStatus.DONE and v.final_video_path
            for v in jc.videos
        )
        if not has_any_done:
            continue
        targets.append(cid)

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
            enable_transcribe=enable_transcribe,
        )
        for cid in targets
    ])
