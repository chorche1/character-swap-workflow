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


def _scene_dialogue(job: Job, sid: str) -> str:
    """The known spoken line for one scene — the movement prompt's says-clause
    (via the shared `video_edit.extract_dialogue`). Drives per-clip caption
    alignment + the Whisper bias hint. Empty when the scene has no dialogue
    (e.g. a silent action shot)."""
    return video_edit.extract_dialogue((job.movement_prompts or {}).get(sid) or "")


def _ordered_scene_videos(
        job: Job, jc: JobCharacter) -> tuple[list[Path], list[str], list[str]]:
    """Build the ordered list of per-scene video file paths for one character.

    Iterates `job.scene_ids` in order. For each scene, finds the approved
    variant for THIS character on THAT scene, then picks the first DONE
    VideoVariant whose `source_variant_id` matches.

    Returns (paths, dialogues, missing): `dialogues[i]` is the known spoken
    line for `paths[i]`'s scene (for per-clip caption alignment — aligned in
    lockstep with `paths`); `missing` names every scene that contributes NO
    clip, with the reason. Backlog #9 (2026-06-12): these scenes used to be
    dropped silently — the final shipped with whole lines of dialogue absent
    and status 'done'."""
    scene_ids = list(job.scene_ids) if job.scene_ids else [job.scene_id]
    approved_set = set(jc.approved_variant_ids or [])
    if jc.approved_variant_id:
        approved_set.add(jc.approved_variant_id)

    paths: list[Path] = []
    dialogues: list[str] = []
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
        # DONE videos for this slot whose file is on disk. Prefer a
        # user-IMPORTED take over a generated one (Hugo 2026-06-21; mirrors
        # runner.pick_clip_for_variant); otherwise the first DONE take.
        cands = [vv for vv in jc.videos
                 if vv.source_variant_id == approved_variant.variant_id
                 and vv.status == VideoStatus.DONE
                 and vv.final_video_path
                 and Path(vv.final_video_path).exists()]
        video = next((vv for vv in cands if vv.imported),
                     cands[0] if cands else None)
        if video is not None:
            paths.append(Path(video.final_video_path))
            dialogues.append(_scene_dialogue(job, sid))
            continue
        # No usable clip — keep the granular reason (backlog #9): a DONE row
        # with its file gone vs. nothing finished at all.
        done_any = any(vv.source_variant_id == approved_variant.variant_id
                       and vv.status == VideoStatus.DONE and vv.final_video_path
                       for vv in jc.videos)
        missing.append(f"{sid} (video file missing on disk)" if done_any
                       else f"{sid} (no finished video)")
    return paths, dialogues, missing


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


_GIANT_GAP_SECS = 8.0   # a single word longer than this = Whisper stuck on
                        # silence/room-tone — a degenerate caption (one card
                        # frozen on screen for seconds). Treated as unhealthy.


def _maxword(words: list) -> float:
    return max((getattr(w, "end", 0) - getattr(w, "start", 0)
                for w in words), default=0.0)


def _caption_score(words: list, cov: float, *, is_plain: bool) -> tuple:
    """Rank a caption transcript candidate. Higher tuple wins:
    coverage of the script (rounded, dominant) → no giant gap → prefer the
    unprompted ('plain') transcript on a tie → more words (more complete)."""
    return (round(cov, 2), _maxword(words) <= _GIANT_GAP_SECS,
            is_plain, len(words))


async def _resolve_caption_words(words: list, current: Path, *, script_hint: str,
                                 edit_id: str, threshold: float,
                                 warn=None) -> list:
    """Choose the caption word source (Hugo 2026-06-17 / 06-22). `words` is the
    Step-4a transcript taken WITH `script_hint` as Whisper's `prompt` (backlog
    #20, to bias wording). That prompt is double-edged: when the audio closely
    matches a long prompt, Whisper treats the prompt as already-transcribed
    context and drops most of the audio — emitting only the trailing tail
    (e.g. 39 of 96 words) and/or a single word frozen for 20+ s. That is NOT
    garbled speech, and on clean TTS the UNPROMPTED transcript is reliably
    complete with accurate timing.

    So we transcribe BOTH ways and pick the BEST of the two by
    `_caption_score` (coverage → no giant gap → prefer plain → completeness),
    which never regresses below either candidate. Only when BOTH genuinely
    diverge from the script (real Kling garble) do we rebuild evenly-timed
    words from the known script — correct WORDS, approximate timing."""
    def _cov(ws: list) -> float:
        return video_edit.caption_transcript_ratio(
            " ".join(getattr(w, "text", "") for w in ws), script_hint)

    hint_words, hint_cov = words, _cov(words)
    try:
        plain_words = await asyncio.to_thread(
            video_edit.transcribe_words, current, job_id=edit_id,
            script_hint=None,
        )
    except Exception:
        plain_words = []
    plain_cov = _cov(plain_words)

    candidates = []
    if hint_words:
        candidates.append(("hint", hint_words, hint_cov))
    if plain_words:
        candidates.append(("plain", plain_words, plain_cov))
    if candidates:
        label, best, best_cov = max(
            candidates,
            key=lambda c: _caption_score(c[1], c[2], is_plain=c[0] == "plain"))
        if best_cov >= threshold:
            if label == "plain" and best is not hint_words:
                logger.info(
                    "%s: using unprompted transcript (cov %.2f, %d words) over "
                    "the script-biased one (cov %.2f, %d words, maxword %.1fs) — "
                    "script_hint made Whisper skip audio", edit_id, best_cov,
                    len(best), hint_cov, len(hint_words), _maxword(hint_words))
            return best

    # Both diverge from the script → genuine garble → even-timed script words.
    dur = await asyncio.to_thread(video_edit._probe_duration, current)
    fallback = video_edit.script_fallback_words(script_hint, dur)
    if fallback:
        logger.warning(
            "%s: both transcripts diverged from the script (hint %.2f / plain "
            "%.2f < %.2f) — captions rebuilt from the known script",
            edit_id, hint_cov, plain_cov, threshold)
        if warn is not None:
            await warn(f"captions: Whisper matchade inte repliken "
                       f"(likhet {max(hint_cov, plain_cov):.0%}) — byggde "
                       f"captions från den kända repliken (jämn timing)")
        return fallback
    return words


def _cov(ws: list, dialogue: str) -> float:
    return video_edit.caption_transcript_ratio(
        " ".join(getattr(w, "text", "") for w in ws), dialogue)


async def _align_one_clip(path: Path, keeps: list | None, dialogue: str,
                          out_dur: float, *, edit_id: str,
                          threshold: float) -> tuple[list, bool]:
    """Caption words for ONE clip, on that clip's own (trimmed) timeline.

    Returns (words, fell_back): `fell_back` is True only when both Whisper
    reads diverged from the KNOWN line and the words were rebuilt evenly-timed
    from it (for the caller's aggregate warning).

    The reliable per-clip path (Hugo 2026-06-26). Whisper is far more
    dependable on a SHORT single clip than on a long stitched reel — the
    'continuation-skip' that drops most of a concat doesn't happen. We
    transcribe the raw clip, remap its word times through `keeps` onto the
    trimmed timeline, and:
      • no known line → trust Whisper's words (real timing; [] if silent);
      • a good Whisper read of the line → keep its real per-word timing;
      • a poor read (garbled Kling voice) → even-time the KNOWN line across
        this clip's duration — correct WORDS in the RIGHT clip, never the
        whole script smeared uniformly across the whole video.

    Frugal: 1 Whisper call when the script-biased read already covers the
    line cleanly, a 2nd (unprompted) only when it doesn't."""
    def _remap(ws: list) -> list:
        return (video_edit.remap_words_through_keeps(ws, keeps)
                if keeps else list(ws))

    async def _tx(hint: str | None) -> list:
        try:
            return await asyncio.to_thread(
                video_edit.transcribe_words, path, job_id=edit_id,
                script_hint=hint)
        except Exception:
            return []

    dialogue = (dialogue or "").strip()
    if not dialogue:
        # No known line for this clip (e.g. a silent action shot, or speech
        # the script doesn't carry) — the unprompted read is the most faithful.
        return _remap(await _tx(None)), False

    hint = _remap(await _tx(dialogue))
    hint_cov = _cov(hint, dialogue)
    if hint and hint_cov >= threshold and _maxword(hint) <= _GIANT_GAP_SECS:
        return hint, False  # clean read — keep real timing, skip the 2nd call

    plain = _remap(await _tx(None))
    plain_cov = _cov(plain, dialogue)
    cands = []
    if hint:
        cands.append(("hint", hint, hint_cov))
    if plain:
        cands.append(("plain", plain, plain_cov))
    if cands:
        _, best, best_cov = max(
            cands,
            key=lambda c: _caption_score(c[1], c[2], is_plain=c[0] == "plain"))
        if best_cov >= threshold:
            return best, False
    # Both reads diverge from the known line → even-time it across THIS clip.
    return video_edit.script_fallback_words(dialogue, out_dur), True


async def _resolve_caption_words_per_clip(
        clip_paths: list[Path], clip_keeps: list, clip_dialogues: list[str],
        *, edit_id: str, threshold: float, warn=None) -> list:
    """Per-clip caption alignment across the whole reel (Hugo 2026-06-26).

    Each clip is aligned on its own timeline by `_align_one_clip`, then shifted
    onto the concatenated timeline by the cumulative duration of the preceding
    clips' kept ranges (so a per-clip fallback lands in the RIGHT clip's slot).
    Replaces the old whole-concat transcribe → even-time-the-WHOLE-script
    fallback, which smeared the script uniformly across the entire video when
    Whisper couldn't read one character's Kling voice."""
    all_words: list = []
    offset = 0.0
    n_fallback = 0
    for i, path in enumerate(clip_paths):
        keeps = clip_keeps[i] if i < len(clip_keeps) else None
        dialogue = (clip_dialogues[i] if i < len(clip_dialogues) else "") or ""
        out_dur = (sum(e - s for s, e in keeps) if keeps
                   else await asyncio.to_thread(video_edit._probe_duration, path))
        clip_words, fell_back = await _align_one_clip(
            path, keeps, dialogue, out_dur, edit_id=edit_id, threshold=threshold)
        if fell_back:
            n_fallback += 1
        for w in clip_words:
            all_words.append(video_edit.Word(
                text=w.text, start=round(w.start + offset, 3),
                end=round(w.end + offset, 3)))
        offset += out_dur
    if n_fallback and warn is not None:
        await warn(f"captions: {n_fallback} klipp kunde inte läsas av Whisper "
                   f"— byggde captions från den kända repliken (jämn timing "
                   f"inom de klippen)")
    return all_words


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
    mirror_h: bool = False,
    warn=None,
    script_hint: str | None = None,
    clip_dialogues: list[str] | None = None,
) -> EditorResult:
    """Concat + Editor finishing, shared by the Step-6 compile and the
    Reengineer assemble: per-clip audio-onset trim → concat → interior
    silence trim → ElevenLabs voice swap (when `voice_id` is set) → Whisper
    transcribe → WPM normalize → caption burn-in. The result lives inside
    `edit_dir` under the given `edit_id`, so it is re-renderable from the
    Editor tab like any other edit.

    `clip_dialogues` (aligned with `paths`, optional): the known spoken line
    per clip. When provided with at least one non-empty line, captions use
    PER-CLIP alignment (`_resolve_caption_words_per_clip`) — each clip
    transcribed on its own and a garbled clip's line even-timed within ITS
    OWN slot, instead of the whole-concat transcribe whose fallback smeared
    the script uniformly across the whole video. Falls back to the
    whole-concat path when absent (Editor multi-clip / drive watcher) or when
    `assemble_clips` took the legacy chain (no per-clip keeps).

    `mirror_h` (the "Repurpose" transform): when True, each source clip is
    HORIZONTALLY mirrored (a near-lossless video-only re-encode, audio copied)
    BEFORE anything else, so the whole reel reads flipped while captions still
    burn in upright on top. The flip happens here — before concat AND before
    per-clip transcription — so every downstream step (single-encode path or
    legacy fallback, per-clip caption alignment) operates on the flipped video
    and mirroring can never silently no-op.

    `warn` is an optional async callback(message) for non-fatal step
    failures (currently: caption render) — callers surface it their own way.
    """
    # Repurpose pre-pass (mirror_h): flip every source clip left↔right once, up
    # front, then proceed exactly as normal on the flipped copies. One extra
    # near-lossless video generation per clip — only in repurpose mode; the
    # original master clips are never touched.
    if mirror_h:
        flipped: list[Path] = []
        for i, p in enumerate(paths):
            dst = edit_dir / f"mirror-{i:02d}.mp4"
            try:
                await asyncio.to_thread(
                    video_edit.hflip_video, p, dst, job_id=edit_id)
            except Exception as flip_err:
                # REFUSE LOUDLY (Hugo's standing rule): a half-mirrored reel —
                # some clips flipped, one not — is a broken output, not a
                # usable one. Fail the whole character so it surfaces as
                # repurpose_status="failed" with a clear message (the outer
                # handler catches this), never ship an inconsistent video.
                # Repurpose is non-destructive (originals untouched) so the
                # user just clicks retry.
                raise RuntimeError(
                    f"spegling (hflip) misslyckades för klipp {i + 1} "
                    f"({type(flip_err).__name__}: {flip_err}) — "
                    f"ingen halvspeglad video byggs; försök igen") from flip_err
            flipped.append(dst)
        paths = flipped

    # Steps 0.5 + 1 + 2 in ONE encode (2026-06-12): per-clip audio-onset trim
    # (ALWAYS — Hugo 2026-06-11: every clip starts exactly when there's
    # enough sound), interior/trailing silence trim (enable_trim), scale to
    # the target canvas, and concat — a single libx264 generation instead of
    # three. The old three-step chain lost a CRF generation per step; with a
    # ~21 Mbps Kling master the FIRST hop alone measured ~2-3 Mbps.
    concat_out = edit_dir / "00-concat.mp4"
    # The ORIGINAL per-clip inputs (for per-clip caption transcription) — the
    # legacy-chain branch below reassigns `paths` to trimmed copies, so capture
    # them before that. `clip_keeps` stays None unless the single-encode pass
    # succeeds and returns per-clip kept ranges.
    clip_paths = list(paths)
    clip_keeps: list | None = None
    try:
        asm = await asyncio.to_thread(
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
        clip_keeps = (asm or {}).get("clip_keeps")
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

    # Step 4a / 4a.1: caption + WPM word source. Two paths:
    #
    # PER-CLIP (Hugo 2026-06-26, the reliable path for Step-6 + Reengineer):
    # when the caller knows each clip's spoken line (`clip_dialogues`) and the
    # single-encode assemble returned per-clip kept ranges (`clip_keeps`),
    # transcribe each clip on its OWN — short clips dodge the long-concat
    # 'continuation-skip' — and even-time only a garbled clip's own line within
    # its own slot. This never smears the whole script uniformly across the
    # whole video (the old fallback's failure that mis-timed every caption).
    #
    # WHOLE-CONCAT (Editor multi-clip / drive watcher / legacy assemble chain /
    # no known per-clip dialogue): transcribe the stitched reel, then
    # best-of-both + even-timed SCRIPT fallback (Hugo 2026-06-17 / 06-22).
    words: list = []
    use_per_clip = (
        clip_dialogues is not None
        and clip_keeps is not None
        and len(clip_dialogues) == len(clip_paths) == len(clip_keeps)
        and any((d or "").strip() for d in clip_dialogues)
        and (enable_captions or enable_wpm_normalize)
    )
    if use_per_clip:
        # Per-clip transcription uses the RAW clip inputs + their kept ranges
        # (pre-voice-swap timing; ElevenLabs STS preserves duration so swapped
        # audio stays aligned). Runs before WPM/gap-trim so everything
        # downstream (incl. the persisted words.json) uses the aligned words.
        words = await _resolve_caption_words_per_clip(
            clip_paths, clip_keeps, clip_dialogues, edit_id=edit_id,
            threshold=settings.caption_script_fallback_ratio, warn=warn,
        )
    else:
        if (enable_transcribe or enable_captions or enable_wpm_normalize
                or enable_gap_trim):
            try:
                words = await asyncio.to_thread(
                    video_edit.transcribe_words, current, job_id=edit_id,
                    script_hint=script_hint,
                )
            except Exception:
                words = []
        if (enable_captions or enable_wpm_normalize) and script_hint:
            words = await _resolve_caption_words(
                words, current, script_hint=script_hint, edit_id=edit_id,
                threshold=settings.caption_script_fallback_ratio, warn=warn,
            )

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


class _CompileSlot(NamedTuple):
    """Which JobCharacter fields / WS events / output file a per-character build
    writes to. `_COMPILE_SLOT` is the Step-6 compile; `_REPURPOSE_SLOT` is the
    mirror-flipped "Repurpose" variant (same source clips, kept ALONGSIDE the
    compile output). Lets `_compile_one_character` / `compile_job_videos` serve
    both without duplicating the pipeline."""
    status_field: str
    edit_field: str
    path_field: str
    error_field: str
    warning_field: str
    event_prefix: str        # WS "char.{prefix}_started|_done|_failed|_warning"
    filename: str            # output basename, with a "{cid}" placeholder
    mirror_h: bool
    push_label: str          # phone-push noun ("Slutvideor" / "Spegelvända videor")


_COMPILE_SLOT = _CompileSlot(
    status_field="compile_status", edit_field="compile_edit_id",
    path_field="compiled_video_path", error_field="compile_error",
    warning_field="compile_warning", event_prefix="compile",
    filename="{cid}.mp4", mirror_h=False, push_label="Slutvideor")

_REPURPOSE_SLOT = _CompileSlot(
    status_field="repurpose_status", edit_field="repurpose_edit_id",
    path_field="repurposed_video_path", error_field="repurpose_error",
    warning_field="repurpose_warning", event_prefix="repurpose",
    filename="{cid}__repurpose.mp4", mirror_h=True,
    push_label="Spegelvända videor")


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
    playback_speed: float = 1.0,
    slot: _CompileSlot = _COMPILE_SLOT,
) -> None:
    """Compile one character's per-scene videos into a single final MP4.

    Mirrors the steps of /api/editor/auto_edit but with an in-memory
    concatenation step prepended. Each step is opt-out via the boolean
    flags; voice swap auto-applies the character's preset voice (or
    `voice_override` if given) UNLESS `enable_voice_swap` is False, in which
    case the original generated/Kling audio is kept untouched.

    `slot` selects which JobCharacter fields + WS events + output filename this
    build writes to (compile vs. the mirror-flipped Repurpose variant); when
    `slot.mirror_h` is True the source clips are horizontally mirrored before
    everything else (captions stay upright).
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

    _persist_jc(job, jc, **{
        slot.status_field: "compiling",
        slot.edit_field: edit_id,
        slot.error_field: None,
        slot.warning_field: None,
    })
    await _emit(job_id, f"char.{slot.event_prefix}_started",
                char_id=char_id, edit_id=edit_id)

    # Step 0: build ordered scene-video list. Bail if empty; warn LOUDLY
    # when scenes are missing (backlog #9: a final that silently skips
    # scenes ships with missing dialogue and status 'done').
    paths, dialogues, missing_scenes = _ordered_scene_videos(job, jc)
    if not paths:
        _persist_jc(job, jc, **{
            slot.status_field: "failed",
            slot.error_field: "no DONE videos found for any scene",
        })
        await _emit(job_id, f"char.{slot.event_prefix}_failed",
                    char_id=char_id, error="no DONE videos found")
        return
    scene_warning = None
    if missing_scenes:
        scene_warning = (f"final is missing {len(missing_scenes)} scene(s): "
                         + ", ".join(missing_scenes))
        await _emit(job_id, f"char.{slot.event_prefix}_warning",
                    char_id=char_id, message=scene_warning)

    try:
        pipeline_warnings: list[str] = []

        async def _warn(message: str) -> None:
            pipeline_warnings.append(message)
            await _emit(job_id, f"char.{slot.event_prefix}_warning",
                        char_id=char_id, message=message)

        # Known dialogue per clip (says-clauses, in scene order, aligned with
        # `paths`) drives PER-CLIP caption alignment; the joined form is the
        # whole-concat Whisper bias hint / fallback (backlog #20).
        script_hint = " ".join(d for d in dialogues if d.strip()) or None

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
            playback_speed=playback_speed, mirror_h=slot.mirror_h,
            warn=_warn,
            script_hint=script_hint,
            clip_dialogues=dialogues,
        )

        # Copy the final result to the canonical per-character location so the
        # UI can grab it from `output/<job_id>/compiled/<char_id>[__repurpose].mp4`.
        compiled_final = compiled_dir / slot.filename.format(cid=char_id)
        shutil.copyfile(result.final, compiled_final)

        all_warnings = ([scene_warning] if scene_warning else []) \
            + pipeline_warnings
        combined_warning = "; ".join(all_warnings) or None
        _persist_jc(job, jc, **{
            slot.path_field: str(compiled_final),
            slot.status_field: "done",
            slot.error_field: None,
            slot.warning_field: combined_warning,
        })
        await _emit(job_id, f"char.{slot.event_prefix}_done",
                    char_id=char_id, edit_id=edit_id,
                    output_path=str(compiled_final),
                    voice_id=effective_voice_id,
                    voice_applied=result.voice_applied,
                    warning=combined_warning)
    except Exception as e:
        _persist_jc(job, jc, **{
            slot.status_field: "failed",
            slot.error_field: f"{type(e).__name__}: {e}",
        })
        await _emit(job_id, f"char.{slot.event_prefix}_failed",
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


def _compile_push_spec(ok: int, total: int, *, label: str = "Slutvideor"
                       ) -> tuple[str, str, int, list[str]] | None:
    """(title, body, priority, tags) for the batch-settled compile push, or
    None when there's nothing to report (no compilable characters). `label` is
    the noun for the build kind ("Slutvideor" / "Spegelvända videor").

    ``ok``/``total`` are counted over the WHOLE job's compilable characters —
    not just this run's targets — so a per-character retry reports true
    job-wide progress instead of a misleading "1/1". A total failure
    (``ok == 0 < total``) is a LOUD failure, never "klara (delvis)" — a batch
    where nothing compiled must not read as partial success on the phone."""
    if total <= 0:
        return None
    if ok == total:
        return (f"{label} klara", f"{ok}/{total} karaktarer kompilerade",
                3, ["white_check_mark"])
    if ok == 0:
        return (f"{label} misslyckades", f"0/{total} kompilerade",
                5, ["rotating_light"])
    return (f"{label} klara (delvis)", f"{ok}/{total} lyckades",
            4, ["warning"])


async def compile_job_videos(
    job_id: str,
    *,
    template: str = "capcut-bluebox",   # Hugo 2026-06-21: editor-wide standard
    overrides: dict | None = None,
    enable_trim: bool = True,
    enable_captions: bool = True,
    enable_wpm_normalize: bool = False,
    target_wpm: float = 190.0,
    threshold_db: float = -24.0,
    min_silence_secs: float = 0.4,
    pad_secs: float = 0.1,
    voice_override: str | None = None,
    enable_voice_swap: bool = False,
    char_ids: list[str] | None = None,
    enable_transcribe: bool = True,
    enable_gap_trim: bool = False,
    gap_max_secs: float = 0.35,
    playback_speed: float = 1.0,
    slot: _CompileSlot = _COMPILE_SLOT,
) -> None:
    """Fan out compile across every (or selected) approved character. All M
    chars compile in parallel via asyncio.gather. Settings apply uniformly
    — the only per-character thing is the preset voice (and `voice_override`
    takes precedence over it batch-wide if set).

    `slot` (default the Step-6 compile) selects the build kind; pass
    `_REPURPOSE_SLOT` for the mirror-flipped Repurpose variant.

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
            playback_speed=playback_speed, slot=slot,
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
                 if getattr(fresh.characters[cid], slot.status_field) == "done")
        spec = _compile_push_spec(ok, len(eligible), label=slot.push_label)
        if spec is not None:
            title, body, priority, tags = spec
            push.notify(title, body, priority=priority, tags=tags)


async def repurpose_job_videos(job_id: str, **kwargs) -> None:
    """Step-6 "Repurpose": build a HORIZONTALLY-MIRRORED variant of every
    eligible character's final (captions upright), from the SAME source clips,
    kept alongside the compile output. Thin wrapper over `compile_job_videos`
    with the repurpose slot — accepts the same settings kwargs (incl.
    `playback_speed`)."""
    await compile_job_videos(job_id, slot=_REPURPOSE_SLOT, **kwargs)
