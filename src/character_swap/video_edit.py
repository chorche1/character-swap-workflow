"""
Video editing primitives for the new Editor tab:

- `trim_silences`: detect silent segments in a video's audio track via
  ffmpeg's silencedetect filter, build a list of keep-ranges, and concat
  them with the concat demuxer. Produces a shorter video with no spoken
  gaps. Tunable threshold (dB) + minimum silence length.

- `transcribe_words`: send the audio track to OpenAI's Whisper API with
  `response_format="verbose_json"` so we get word-level timestamps for
  word-by-word caption rendering.

- `render_captions`: take the word list + a template (built-in or custom
  params) and burn ASS subtitles into the video via ffmpeg's `subtitles`
  filter. The ASS file is generated on the fly to embed exact styling.

ffmpeg binary comes from `imageio_ffmpeg` so users don't need a system install.
"""
from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import imageio_ffmpeg

from character_swap.call_log import record
from character_swap.clients import openai_image  # reuse _client() for OpenAI auth
from character_swap.config import settings


def _ffprobe() -> str | None:
    """Path to an ffprobe binary, or None. imageio-ffmpeg bundles ONLY
    ffmpeg, so we look on PATH and next to the bundled ffmpeg (system
    installs ship the pair side by side). ffprobe answers metadata questions
    (duration, has-audio) in <200ms where a full `ffmpeg -f null -` decode
    of the whole file takes seconds-to-minutes."""
    found = shutil.which("ffprobe")
    if found:
        return found
    try:
        sibling = Path(imageio_ffmpeg.get_ffmpeg_exe()).with_name("ffprobe")
        if sibling.exists():
            return str(sibling)
    except Exception:
        pass
    return None


def _ffmpeg() -> str:
    """Path to the ffmpeg binary (bundled by imageio-ffmpeg, no system install)."""
    return imageio_ffmpeg.get_ffmpeg_exe()


# Bundled fonts — downloaded lazily from Google Fonts (SIL Open Font License,
# free for commercial use) and cached so libass/fontconfig can find them.
#
# Google migrated most families to a single "variable font" file containing
# every weight, replacing the per-weight static files (May 2026 — the old
# /static/Montserrat-Black.ttf etc paths now 404). We mirror that by pointing
# every weight-named entry below at the same variable-font URL. libass 0.16+
# and Chrome (Remotion) both auto-pick the right axis when CSS / ASS specifies
# a fontWeight or bold flag against the variable file. The file is downloaded
# under the requested name (e.g. "Montserrat_Black.ttf") so fontconfig still
# resolves the heavier requests, even if the bytes are identical.
_FONT_URLS = {
    "Anton": "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
    "Bebas Neue": "https://github.com/google/fonts/raw/main/ofl/bebasneue/BebasNeue-Regular.ttf",
    # Variable fonts cover every weight in one file.
    "Montserrat":           "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "Montserrat Black":     "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "Montserrat ExtraBold": "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "Montserrat SemiBold":  "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "Montserrat Bold":      "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "Poppins ExtraBold":    "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-ExtraBold.ttf",
    "Poppins Black":        "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Black.ttf",
    "Poppins":              "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Regular.ttf",
    "Inter":                "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf",
    "Inter ExtraBold":      "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf",
    "Inter Black":          "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bopsz%2Cwght%5D.ttf",
}


def _fonts_dir() -> Path:
    """Where bundled fonts live. Created on first use; safe to inspect."""
    p = settings.state_dir / "fonts"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _ensure_font(name: str) -> Path | None:
    """Resolve a font name to a .ttf path under `state/fonts/`. Returns
    None if the font isn't bundled and isn't pre-installed locally — in
    that case libass falls back to fontconfig + system fonts.

    Order: (1) check for an existing file in `state/fonts/`, so manually
    dropped-in fonts work; (2) try to download from `_FONT_URLS` for the
    fonts we ship by default; (3) give up."""
    safe = name.replace(" ", "_") + ".ttf"
    dest = _fonts_dir() / safe
    if dest.exists():
        return dest
    url = _FONT_URLS.get(name)
    if url is None:
        return None
    try:
        import httpx
        with httpx.Client(timeout=30, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            dest.write_bytes(r.content)
        return dest
    except Exception:
        # Network down or rate-limited — fall through to system fallback.
        if dest.exists():
            dest.unlink(missing_ok=True)
        return None


# Family → [(min_weight, exact_font_name), ...] lookup. Used by the ASS
# render path to swap to a heavier installed font variant when the user
# moves the Style-tab font-weight slider. Ordered heaviest-first; the
# resolver picks the heaviest entry whose min_weight <= requested weight.
# If a variant's font file isn't installed (and can't be downloaded by
# _ensure_font), we skip it and try the next-heaviest.
_FONT_WEIGHT_FAMILIES: dict[str, list[tuple[int, str]]] = {
    "TikTok Sans": [
        (900, "TikTok Sans Black"),
        (800, "TikTok Sans ExtraBold"),
        (700, "TikTok Sans Bold"),
    ],
    "Instagram Sans": [
        (700, "Instagram Sans Bold"),
        (500, "Instagram Sans Medium"),
        (400, "Instagram Sans Regular"),
        (300, "Instagram Sans Light"),
    ],
    "Montserrat": [
        (900, "Montserrat Black"),
        (700, "Montserrat Bold"),
    ],
    "Poppins": [
        (900, "Poppins Black"),
        (800, "Poppins ExtraBold"),
    ],
    "Inter": [
        (900, "Inter Black"),
        (800, "Inter ExtraBold"),
    ],
}


def _resolve_font_for_weight(font_name: str, font_weight: int | None) -> str:
    """Map (font_name, font_weight) to the best-matching installed font file.

    When `font_weight` is None we return the input untouched — preserving
    every existing template's explicit font choice. When the user has
    explicitly set a weight (via the Style-tab slider), we:
      1. Strip a trailing weight suffix from `font_name` so we work from
         the base family (e.g. "TikTok Sans ExtraBold" → "TikTok Sans").
      2. Look up the family in `_FONT_WEIGHT_FAMILIES`. If unknown, fall
         back to the input unchanged.
      3. Pick the heaviest variant whose min_weight <= requested weight.
         If that variant's font file isn't available, try the next.
      4. If nothing matches, return `font_name` unchanged.
    """
    if font_weight is None:
        return font_name
    suffixes = (" Black", " ExtraBold", " Bold", " Medium",
                " Light", " Regular", " Heavy", " Italic")
    base = font_name
    for suffix in suffixes:
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    variants = _FONT_WEIGHT_FAMILIES.get(base)
    if not variants:
        return font_name
    # variants is heaviest-first; find the heaviest whose min_weight <= w.
    for w, name in variants:
        if font_weight >= w and _ensure_font(name) is not None:
            return name
    # Below all thresholds — pick the lightest variant we can install.
    for w, name in reversed(variants):
        if _ensure_font(name) is not None:
            return name
    return font_name


def _run(args: list[str]) -> str:
    """Run ffmpeg with the given args. Returns combined stdout+stderr.
    Raises CalledProcessError with output on failure."""
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit {proc.returncode}):\n"
            f"cmd: {shlex.join(args)}\n"
            f"stderr: {proc.stderr[-2000:]}"
        )
    return (proc.stdout or "") + (proc.stderr or "")


# --- 1. Silence-based jump-cut --------------------------------------------------------

def _enc_v() -> list[str]:
    """Shared libx264 args for EVERY local video re-encode in this module.
    Settings-driven (FFMPEG_CRF=16 / FFMPEG_PRESET=medium by default,
    2026-06-12): a clip passes through several of these generations before
    delivery, so per-generation transparency beats encode speed — the old
    hardcoded veryfast/CRF-20 measured ~2-3 Mbps off a ~21 Mbps Kling master
    at the first hop alone."""
    return ["-c:v", "libx264",
            "-preset", settings.ffmpeg_preset, "-crf", str(settings.ffmpeg_crf)]


def _probe_duration(input_path: Path) -> float:
    """Total duration in seconds.

    ffprobe fast path (<200ms); fallback is a HEADER-ONLY `ffmpeg -i` probe —
    no output target, so ffmpeg prints the metadata and exits without
    decoding a single frame. (The old `-f null -` form decoded the entire
    file just to read one header line: seconds of CPU per call, and this is
    called once per Reengineer analysis + 8+ Editor sites.)"""
    probe = _ffprobe()
    if probe:
        try:
            proc = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(input_path)],
                capture_output=True, text=True, check=False)
            if proc.returncode == 0 and proc.stdout.strip():
                return float(proc.stdout.strip())
        except (ValueError, OSError):
            pass
    # `ffmpeg -i <file>` with no output exits non-zero AFTER printing the
    # metadata to stderr — _run raises with that stderr embedded.
    try:
        out = _run([_ffmpeg(), "-hide_banner", "-i", str(input_path)])
    except RuntimeError as e:
        out = str(e)
    # ffmpeg prints "Duration: HH:MM:SS.ms," — parse it
    for line in out.splitlines():
        if "Duration:" in line:
            dur = line.split("Duration:")[1].split(",")[0].strip()
            if dur == "N/A":
                break
            h, m, s = dur.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


# loudnorm's analysis JSON lands on stderr; input_i = integrated loudness
# (LUFS), input_tp = true peak (dBTP).
_LOUDNORM_JSON_RE = re.compile(
    r'"input_i"\s*:\s*"(?P<i>-?[\d.]+)"[\s\S]*?"input_tp"\s*:\s*"(?P<tp>-?[\d.]+)"')


def _measure_clip_loudness(input_path: Path) -> tuple[float, float] | None:
    """Integrated loudness (LUFS) + true peak (dBTP) of a clip's audio via
    ONE loudnorm analysis pass — decode only, no encode generation. Returns
    None when measurement fails or the clip is essentially silent; callers
    treat None as 'apply no gain' (the equalization never blocks a build)."""
    try:
        out = _run([
            _ffmpeg(), "-hide_banner", "-i", str(input_path),
            "-af", "loudnorm=print_format=json", "-f", "null", "-",
        ])
    except RuntimeError as e:
        out = str(e)
    m = _LOUDNORM_JSON_RE.search(out)
    if not m:
        return None
    try:
        loudness, true_peak = float(m.group("i")), float(m.group("tp"))
    except ValueError:
        return None
    if loudness <= -70.0:           # loudnorm's silence sentinel
        return None
    return loudness, true_peak


def _adaptive_silence_threshold(input_i: float) -> float:
    """Per-clip silencedetect threshold derived from the clip's measured
    integrated loudness (backlog #37, 2026-06-12): a fixed -30 dB threshold
    ate quiet speech on low-level Kling clips and missed pauses on hot ones.
    Speech sits near the integrated level, so silence is ~16 LU below it,
    clamped to a sane window."""
    return min(-25.0, max(-45.0, input_i - 16.0))


def _clip_gain_db(input_i: float, input_tp: float, target_i: float,
                  *, max_gain: float = 12.0, tp_ceiling: float = -1.0) -> float:
    """Static volume gain bringing a clip to the target integrated loudness
    without pushing its true peak above `tp_ceiling` (no clipping) — pure
    linear gain, NO dynamics processing, so every segment cut from the same
    clip moves together (backlog #10: dynamic loudnorm on sub-3s concat
    segments pumps; one static gain per clip cannot)."""
    gain = target_i - input_i
    gain = min(gain, tp_ceiling - input_tp)
    return max(-max_gain, min(max_gain, gain))


def _probe_fps(input_path: Path) -> float | None:
    """Average frame rate of the first video stream, or None when probing
    fails. Parses ffprobe's rational form ('24/1', '2997/100')."""
    probe = _ffprobe()
    if not probe:
        return None
    try:
        proc = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", str(input_path)],
            capture_output=True, text=True, check=False)
        raw = (proc.stdout or "").strip().splitlines()
        if proc.returncode != 0 or not raw:
            return None
        num_s, _, den_s = raw[0].partition("/")
        num, den = float(num_s), float(den_s or "1")
        if num <= 0 or den <= 0:
            return None
        return num / den
    except (ValueError, OSError):
        return None


def _detect_silences(input_path: Path, threshold_db: float = -30.0,
                     min_silence_secs: float = 0.4) -> list[tuple[float, float]]:
    """Return a list of (start, end) silent intervals in seconds."""
    out = _run([
        _ffmpeg(), "-hide_banner", "-i", str(input_path),
        "-af", f"silencedetect=noise={threshold_db}dB:d={min_silence_secs}",
        "-f", "null", "-",
    ])
    silences: list[tuple[float, float]] = []
    current_start: float | None = None
    for line in out.splitlines():
        if "silence_start:" in line:
            try:
                current_start = float(line.split("silence_start:")[1].strip().split()[0])
            except (IndexError, ValueError):
                current_start = None
        elif "silence_end:" in line and current_start is not None:
            try:
                end = float(line.split("silence_end:")[1].strip().split()[0].rstrip(","))
                silences.append((current_start, end))
            except (IndexError, ValueError):
                pass
            current_start = None
    return silences


def _invert_silences(silences: list[tuple[float, float]],
                     total_duration: float,
                     pad_secs: float = 0.05) -> list[tuple[float, float]]:
    """Return list of (start, end) speech-keep ranges by inverting silences.
    Adds a tiny `pad_secs` either side of each INTERIOR keep range so words
    don't get clipped — but NOT on the first keep range. Leading silence is
    fully discarded so the clip starts exactly on speech, matching Hugo's
    "no gap at the start" expectation. Trailing silence is similarly fully
    cut (loop runs only while there's content after the last silence).
    """
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    first_keep = True
    for s_start, s_end in silences:
        if s_start > cursor:
            # First keep starts exactly at `cursor` (zero pre-pad → no
            # leading-silence remnant). Subsequent keeps still get the
            # pre-pad so mid-sentence pauses keep their natural in-breath.
            pre_pad = 0.0 if first_keep else pad_secs
            keep.append((max(0.0, cursor - pre_pad), min(total_duration, s_start + pad_secs)))
            first_keep = False
        cursor = s_end
    if cursor < total_duration:
        pre_pad = 0.0 if first_keep else pad_secs
        keep.append((max(0.0, cursor - pre_pad), total_duration))
    return [(a, b) for a, b in keep if b - a > 0.05]  # drop microscopic slivers


def trim_to_first_word(input_path: Path, output_path: Path, words: list,
                       *, pad_secs: float = 0.0,
                       job_id: str | None = None) -> dict:
    """Trim the start of `input_path` so it begins exactly at the first
    transcribed word.

    NO LONGER wired into the default flows (2026-06-11): Hugo chose AUDIO
    energy as the start marker, so `trim_leading_silence` replaced this in
    auto_edit / multi_auto_edit / compile. Kept as a tested utility for
    callers that explicitly want a speech-onset (vs sound-onset) cut.

    `words` is a list of objects with `.start` attribute (or 'start' key if
    dict) — the canonical Whisper word list this codebase passes around
    everywhere. `pad_secs` defaults to 0 (true "exact") — set to e.g. 0.02
    if you ever want a tiny phoneme-safety cushion.

    No-op (copies the file) when there are no words OR when the first word
    starts within `pad_secs + 50ms` of zero (already starts on speech).

    Returns {leading_silence_secs, original_duration, trimmed_duration}.
    """
    import shutil as _shutil
    with record(phase="editor_trim_to_first_word",
                model="whisper-first-word",
                character="editor", job_id=job_id) as entry:
        duration = _probe_duration(input_path)
        entry["n_words"] = len(words)
        # Extract first-word start, supporting both Word dataclass and dict.
        if not words:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copyfile(input_path, output_path)
            entry["leading_silence_secs"] = 0.0
            entry["trimmed"] = False
            return {"leading_silence_secs": 0.0,
                    "original_duration": round(duration, 2),
                    "trimmed_duration": round(duration, 2)}
        first = words[0]
        first_start = getattr(first, "start", None)
        if first_start is None and isinstance(first, dict):
            first_start = first.get("start")
        if first_start is None:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copyfile(input_path, output_path)
            entry["leading_silence_secs"] = 0.0
            entry["trimmed"] = False
            entry["error"] = "first word missing start timestamp"
            return {"leading_silence_secs": 0.0,
                    "original_duration": round(duration, 2),
                    "trimmed_duration": round(duration, 2)}
        first_start = max(0.0, float(first_start) - max(0.0, pad_secs))
        # If the first word is already at the very start, no-op copy.
        if first_start <= 0.05:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copyfile(input_path, output_path)
            entry["leading_silence_secs"] = 0.0
            entry["trimmed"] = False
            return {"leading_silence_secs": 0.0,
                    "original_duration": round(duration, 2),
                    "trimmed_duration": round(duration, 2)}
        entry["leading_silence_secs"] = round(first_start, 3)
        entry["trimmed"] = True
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # -ss AFTER -i for accurate audio-synced seek (slower than -ss before
        # but matters for voice-swap downstream which keys off the audio).
        _run([
            _ffmpeg(), "-y", "-i", str(input_path),
            "-ss", f"{first_start:.3f}",
            *_enc_v(),
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ])
        trimmed_dur = _probe_duration(output_path)
        return {"leading_silence_secs": round(first_start, 3),
                "original_duration": round(duration, 2),
                "trimmed_duration": round(trimmed_dur, 2)}


def trim_leading_silence(input_path: Path, output_path: Path, *,
                         threshold_db: float = -30.0,
                         min_silence_secs: float = 0.2,
                         job_id: str | None = None) -> dict:
    """Drop ONLY the leading silence from `input_path` — internal silences are
    preserved verbatim.

    THE universal entry trim (Hugo, 2026-06-11): every clip entering any
    pipeline — Editor auto_edit, multi_auto_edit per clip, Step-6 compile per
    scene, Reengineer assemble per scene — is first cut to AUDIO ONSET,
    unconditionally (independent of the enable_trim / "Trim silences" toggle,
    which governs interior pauses only). The marker is audio ENERGY
    (silencedetect vs `threshold_db`), deliberately NOT Whisper's first-word
    timestamp — "när det blir tillräckligt mycket ljud" counts music/breath
    as the start, while sub-threshold room tone does not.

    `min_silence_secs` is smaller than the trim_silences default (0.4) so
    even short half-second pauses at the start get cut; flows pass 0.05 for
    an exact start.

    Returns `{leading_silence_secs, original_duration, trimmed_duration}`.
    No-op (just copies the file) when no leading silence is detected or the
    clip has no audio track.
    """
    import shutil as _shutil
    with record(phase="editor_trim_leading", model="ffmpeg-silencedetect",
                character="editor", job_id=job_id) as entry:
        # Audio-less clips (Higgsfield Supercomputer) can't have "leading
        # silence" — there's no audio energy to detect. Pass through.
        has_audio = _has_audio_stream(input_path)
        entry["has_audio"] = has_audio
        if not has_audio:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copyfile(input_path, output_path)
            entry["leading_silence_secs"] = 0.0
            entry["trimmed"] = False
            duration = _probe_duration(input_path)
            return {"leading_silence_secs": 0.0,
                    "original_duration": round(duration, 2),
                    "trimmed_duration": round(duration, 2)}
        silences = _detect_silences(input_path, threshold_db, min_silence_secs)
        duration = _probe_duration(input_path)
        entry["n_silences"] = len(silences)
        # Only a real leading silence if it STARTS within the first 50ms
        # (otherwise the clip already starts on speech).
        if not silences or silences[0][0] > 0.05:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copyfile(input_path, output_path)
            entry["leading_silence_secs"] = 0.0
            entry["trimmed"] = False
            return {"leading_silence_secs": 0.0,
                    "original_duration": round(duration, 2),
                    "trimmed_duration": round(duration, 2)}
        start_offset = silences[0][1]
        entry["leading_silence_secs"] = round(start_offset, 2)
        entry["trimmed"] = True
        # ffmpeg -ss before -i is keyframe-fast but can misalign audio on
        # some containers; -ss after -i is accurate but slower. We use
        # accurate seek because audio sync matters for voice-swap downstream.
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _run([
            _ffmpeg(), "-y", "-i", str(input_path),
            "-ss", f"{start_offset:.3f}",
            *_enc_v(),
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ])
        trimmed_dur = _probe_duration(output_path)
        return {"leading_silence_secs": round(start_offset, 2),
                "original_duration": round(duration, 2),
                "trimmed_duration": round(trimmed_dur, 2)}


def trim_silences(input_path: Path, output_path: Path, *,
                  threshold_db: float = -30.0,
                  min_silence_secs: float = 0.4,
                  pad_secs: float = 0.05,
                  job_id: str | None = None) -> dict:
    """Remove silent gaps from `input_path`. Writes to `output_path`.

    Returns a summary {original_duration, trimmed_duration, n_cuts}."""
    with record(phase="editor_trim", model="ffmpeg-silencedetect",
                character="editor", job_id=job_id) as entry:
        duration = _probe_duration(input_path)
        silences = _detect_silences(input_path, threshold_db, min_silence_secs)
        keep = _invert_silences(silences, duration, pad_secs)
        entry["n_silences"] = len(silences)
        entry["n_keep_segments"] = len(keep)
        if not keep:
            # All silent — just copy a tiny clip to avoid an empty file.
            _run([_ffmpeg(), "-y", "-i", str(input_path),
                  "-t", "0.5", "-c", "copy", str(output_path)])
            return {"original_duration": duration, "trimmed_duration": 0.5, "n_cuts": 0}

        # Build a single filter_complex with N trims + concat.
        parts: list[str] = []
        for i, (start, end) in enumerate(keep):
            parts.append(
                f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{i}];"
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{i}]"
            )
        labels = "".join(f"[v{i}][a{i}]" for i in range(len(keep)))
        filter_complex = ";".join(parts) + f";{labels}concat=n={len(keep)}:v=1:a=1[v][a]"

        output_path.parent.mkdir(parents=True, exist_ok=True)
        _run([
            _ffmpeg(), "-y", "-i", str(input_path),
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "[a]",
            *_enc_v(),
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ])
        trimmed = _probe_duration(output_path)
        return {
            "original_duration": round(duration, 2),
            "trimmed_duration": round(trimmed, 2),
            "n_cuts": len(keep),
            "saved_secs": round(duration - trimmed, 2),
        }


# --- 2. Whisper word-level transcription ----------------------------------------------

@dataclass
class Word:
    text: str
    start: float
    end: float


def _has_audio_stream(video_path: Path) -> bool:
    """True if `video_path` contains at least one audio stream.

    Higgsfield-generated clips are video-only (no audio), and ffmpeg fails
    with "Output file does not contain any stream" when you try to extract
    audio from them. Callers should probe with this before _extract_audio
    or any atempo/setpts filter chain that maps `[0:a]`.

    ffprobe fast path; fallback is a header-only `ffmpeg -i` probe (exits
    non-zero after printing stream metadata — NO decode). The old `-f null -`
    form decoded the whole file to answer a yes/no question.
    """
    probe = _ffprobe()
    if probe:
        try:
            proc = subprocess.run(
                [probe, "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type", "-of", "csv=p=0",
                 str(video_path)],
                capture_output=True, text=True, check=False)
            if proc.returncode == 0:
                return bool(proc.stdout.strip())
        except OSError:
            pass
    try:
        out = _run([_ffmpeg(), "-hide_banner", "-i", str(video_path)])
    except RuntimeError as e:
        out = str(e)
    # ffmpeg's -i probe prints a "Stream #0:N: Audio:" line for every
    # audio track. No such line → no audio.
    return "Audio:" in out


def _extract_audio(video_path: Path) -> Path:
    """Pull the audio track out as 16kHz mono wav (Whisper's preferred input).

    Raises RuntimeError if the input has no audio stream — callers should
    probe with `_has_audio_stream` first if they want to handle that case.
    """
    audio_path = video_path.with_suffix(".audio.wav")
    _run([
        _ffmpeg(), "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        str(audio_path),
    ])
    return audio_path


def transcribe_words(video_path: Path, *, job_id: str | None = None,
                     script_hint: str | None = None) -> list[Word]:
    """Run OpenAI Whisper on the video's audio. Returns word-level timestamps.

    Uses `whisper-1` (current OpenAI Whisper API model) with
    response_format=verbose_json + timestamp_granularities=['word'].

    `script_hint` (backlog #20, 2026-06-12): when the EXACT spoken script is
    already known (Reengineer dialogue, Step-6 movement-prompt lines), it is
    passed as Whisper's `prompt` so the transcription is biased toward the
    real wording — mis-hearings used to be burned into captions verbatim.
    Whisper only reads the prompt's final ~224 tokens, hence the char cap.

    Returns `[]` (empty word list) for video-only inputs with no audio track
    — Higgsfield Supercomputer clips are typically silent. Callers that fan
    out across N clips get `match_clips_by_transcript` to fall back to
    upload-order placement when transcripts are empty.
    """
    if not _has_audio_stream(video_path):
        return []
    audio_path = _extract_audio(video_path)
    client = openai_image._client()  # reuses settings.openai_api_key + auth
    extra: dict = {}
    if script_hint and script_hint.strip():
        extra["prompt"] = script_hint.strip()[-800:]
    with record(phase="editor_transcribe", model="whisper-1",
                character="editor", job_id=job_id):
        with audio_path.open("rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["word"],
                **extra,
            )
    # The SDK returns an object; words live on `result.words` (list of dicts).
    words: list[Word] = []
    raw = getattr(result, "words", None) or []
    for w in raw:
        # `w` can be a dict or object; handle both.
        if isinstance(w, dict):
            words.append(Word(text=w.get("word", ""),
                              start=float(w.get("start", 0)),
                              end=float(w.get("end", 0))))
        else:
            words.append(Word(text=getattr(w, "word", ""),
                              start=float(getattr(w, "start", 0)),
                              end=float(getattr(w, "end", 0))))
    audio_path.unlink(missing_ok=True)
    return words


# --- 3. Caption templates + ASS rendering ---------------------------------------------

TemplateName = Literal["mrbeast", "tiktok", "karaoke", "minimal", "subtitle"]


@dataclass
class CaptionStyle:
    """Subset of ASS style fields we expose to the user."""
    font: str = "Arial"
    size: int = 90
    primary_color: str = "&H00FFFFFF"   # white (ASS uses BGR + alpha, &HAABBGGRR)
    outline_color: str = "&H00000000"   # black
    back_color: str = "&H80000000"      # half-transparent black for boxed
    bold: bool = True
    outline: int = 4
    shadow: int = 0
    margin_v: int = 80                  # vertical margin from bottom (in 1920-tall video coords)
    margin_h: int = 0                   # horizontal offset from center (in 1080-wide coords). Triggers \pos() override.
    alignment: int = 2                  # 2 = bottom-center (ASS conventions)
    box: bool = False                   # background box behind text
    words_per_card: int = 3             # word-by-word grouping size
    highlight_color: str | None = None  # color used to highlight the active word
    all_caps: bool = False              # force-uppercase the rendered text (Submagic/TikTok aesthetic)
    # New tunables (Hugo, May 2026): give the user direct control over
    # font weight, opacity, and the two separate shadow components so the
    # Remotion templates can be dialed in per-job from the Style tab
    # rather than rebuilding compositions for every variant. Default
    # values preserve the prior look of each template.
    #
    # `font_weight = None` means "use `font` as-is" (the template's choice).
    # When the user moves the Style-tab slider, an int 100-900 lands here
    # and BOTH the Remotion path (CSS fontWeight) AND the ASS path
    # (auto-swap to a heavier installed variant in the same family —
    # e.g. TikTok Sans ExtraBold → TikTok Sans Black at weight=900) honor it.
    font_weight: int | None = None
    opacity: float = 1.0                # text opacity 0.0-1.0
    shadow_blur: int | None = None      # CSS text-shadow blur radius in px; None = derive from `shadow` value
    shadow_distance: int | None = None  # CSS text-shadow offset in px; None = derive from `shadow` value
    # Rendering engine.
    #   "ass"      → existing ffmpeg+ASS path (cheap, no animation)
    #   "remotion" → React composition rendered via `npx remotion render`
    #   "veed"     → cloud render via fal.ai's VEED Subtitle Styling endpoint
    #                (~$0.10/min). Templates with engine="veed" carry their
    #                preset params in `veed_params` — see TEMPLATES.
    engine: Literal["ass", "remotion", "veed"] = "ass"
    # When engine="remotion", the id of the React composition to render.
    # Must match an entry in `remotion/src/Root.tsx`.
    composition_id: str | None = None
    # When engine="veed", params sent verbatim to fal.ai's auto-subtitle
    # endpoint (minus video_url). See clients/fal_veed.py for the schema.
    veed_params: dict | None = None

    def to_remotion_props(self) -> dict:
        """Map ASS-style fields onto the typed props the Remotion
        compositions consume. Position fields assume a 1080×1920 canvas
        (the same assumption ASS makes via PlayResX/Y)."""
        accent = _ass_color_to_hex(self.highlight_color or self.primary_color, default="#FFD400")
        sizeScale = max(0.4, min(2.5, self.size / 115.2))
        margin_v_clamped = max(0, min(1900, self.margin_v))
        margin_h_clamped = max(-540, min(540, self.margin_h))
        # Vertical position depends on alignment (ASS numpad layout):
        # 1-3 bottom, 4-6 middle, 7-9 top. For middle, ignore margin_v entirely
        # (matches libass behavior); for top, margin_v is distance from top.
        if self.alignment in (4, 5, 6):
            y_pct = 0.5
        elif self.alignment in (7, 8, 9):
            y_pct = max(0.05, min(0.95, margin_v_clamped / 1920.0))
        else:
            y_pct = max(0.05, min(0.95, 1.0 - margin_v_clamped / 1920.0))
        x_pct = max(0.05, min(0.95, 0.5 + margin_h_clamped / 1080.0))
        # When the user doesn't set them explicitly, derive shadow blur +
        # distance from the legacy single `shadow` value so existing templates
        # keep their feel. Distance defaults to shadow, blur to ~2× shadow.
        sd = self.shadow_distance if self.shadow_distance is not None else self.shadow
        sb = self.shadow_blur if self.shadow_blur is not None else max(self.shadow * 2, 0)
        outline_color_hex = _ass_color_to_hex(self.outline_color, default="#000000")
        return {
            "accent": accent,
            "fontFamily": self.font,
            "sizeScale": sizeScale,
            "positionPct": {"x": x_pct, "y": y_pct},
            "allCaps": self.all_caps,
            "wordsPerCard": self.words_per_card,
            # New tunables — every Remotion composition reads these via
            # `BaseCaptionProps` and applies them to text-shadow + element
            # styles. Defaults are chosen to preserve each composition's
            # original look when the user hasn't touched the new sliders.
            "fontWeight": max(100, min(900, int(self.font_weight if self.font_weight is not None else 900))),
            "opacity": max(0.0, min(1.0, float(self.opacity))),
            "shadowDistance": max(0, min(50, int(sd))),
            "shadowBlur": max(0, min(60, int(sb))),
            "outlinePx": max(0, min(20, int(self.outline))),
            "outlineColor": outline_color_hex,
        }


def _ass_color_to_hex(ass: str | None, *, default: str = "#FFFFFF") -> str:
    """Convert ASS &HAABBGGRR (or &HBBGGRR) to a #RRGGBB CSS hex color.
    Returns `default` when the input is None or unparseable."""
    if not ass:
        return default
    s = ass.strip().lstrip("&").lstrip("Hh")
    # Strip a leading "00" alpha if present (ASS uses AABBGGRR where AA=00 is opaque)
    if len(s) == 8:
        s = s[2:]
    if len(s) != 6:
        return default
    try:
        bb, gg, rr = s[0:2], s[2:4], s[4:6]
        int(bb, 16); int(gg, 16); int(rr, 16)
        return f"#{rr}{gg}{bb}".upper()
    except ValueError:
        return default


TEMPLATES: dict[str, CaptionStyle] = {
    "mrbeast":  CaptionStyle(font="Impact", size=110, primary_color="&H00FFFFFF",
                              outline_color="&H00000000", outline=6, bold=True,
                              words_per_card=3, highlight_color="&H0000FFFF",  # cyan
                              margin_v=120),
    "tiktok":   CaptionStyle(font="Arial", size=90, primary_color="&H00FFFFFF",
                              back_color="&HC0000000", box=True, outline=2,
                              words_per_card=4, margin_v=100),
    "karaoke":  CaptionStyle(font="Arial", size=80, primary_color="&H00FFFFFF",
                              outline_color="&H00000000", outline=3,
                              words_per_card=6, highlight_color="&H0000A5FF",  # orange
                              margin_v=80),
    "minimal":  CaptionStyle(font="Helvetica", size=64, primary_color="&H00FFFFFF",
                              outline_color="&H80000000", outline=2,
                              words_per_card=8, margin_v=60),
    "subtitle": CaptionStyle(font="Arial", size=54, primary_color="&H00FFFFFF",
                              outline_color="&H00000000", outline=2, shadow=1,
                              words_per_card=10, margin_v=40),
    # Submagic/TikTok-style word-by-word yellow popout. Matches the "NEVER BUY
    # HONEY" + "IT'S STRIPS OF" CapCut-style screenshots: bold display font,
    # all-caps, white default, yellow active word, thick black outline +
    # drop-shadow for the "pops off the screen" look.
    "popout-yellow": CaptionStyle(font="Anton", size=120,
                              primary_color="&H00FFFFFF",      # white
                              outline_color="&H00000000",      # black
                              back_color="&HC0000000",         # half-transparent black (for shadow tint)
                              outline=6, shadow=3,             # thinner outline + visible drop shadow
                              bold=True, box=False,
                              words_per_card=3,
                              highlight_color="&H0000FFFF",    # yellow (ASS BGR = RGB 255,255,0)
                              margin_v=400, all_caps=True),

    # --- 8 new modern templates with strong shadow ---

    # Same look as popout-yellow but no colored highlight — pure punch.
    "popout-white":  CaptionStyle(font="Anton", size=120,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=5, shadow=5, bold=True, box=False,
                              words_per_card=3, margin_v=400, all_caps=True),

    # Submagic-pink highlight.
    "popout-pink":   CaptionStyle(font="Anton", size=120,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=6, shadow=3, bold=True, box=False,
                              words_per_card=3,
                              highlight_color="&H00B56BFF",    # RGB 255,107,181 → BGR B5 6B FF
                              margin_v=400, all_caps=True),

    # Captions-hype lime green highlight.
    "popout-green":  CaptionStyle(font="Anton", size=120,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=6, shadow=3, bold=True, box=False,
                              words_per_card=3,
                              highlight_color="&H0000FFC6",    # RGB 198,255,0 → BGR 00 FF C6
                              margin_v=400, all_caps=True),

    # Modern + clean: white text, NO outline, just a big soft drop shadow.
    "clean-shadow":  CaptionStyle(font="Helvetica", size=72,
                              primary_color="&H00FFFFFF",
                              outline_color="&H40000000",      # very faint outline (mostly transparent)
                              outline=1, shadow=8, bold=True, box=False,
                              words_per_card=5, margin_v=300, all_caps=False),

    # Bold typography focus — Montserrat Black, mixed case, soft shadow.
    "bold-shadow":   CaptionStyle(font="Montserrat Black", size=90,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=2, shadow=6, bold=True, box=False,
                              words_per_card=4, margin_v=350, all_caps=False),

    # Retro monospace look in a soft black box.
    "typewriter":    CaptionStyle(font="Courier", size=64,
                              primary_color="&H00FFFFFF",
                              back_color="&HC0000000",         # semi-transparent black
                              outline=0, shadow=2, bold=True, box=True,
                              words_per_card=6, margin_v=200, all_caps=False),

    # Kinetic / single-word-at-a-time, huge text. Bebas Neue tall caps.
    "kinetic":       CaptionStyle(font="Bebas Neue", size=160,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=5, shadow=4, bold=True, box=False,
                              words_per_card=1,                # one word per card → fast cuts
                              margin_v=500, all_caps=True),

    # Classic broadcast lower-third — small, clean, light shadow, sits near edge.
    "bottom-third":  CaptionStyle(font="Helvetica", size=48,
                              primary_color="&H00FFFFFF",
                              outline_color="&H80000000",
                              outline=1, shadow=4, bold=True, box=False,
                              words_per_card=8, margin_v=80, all_caps=False),

    # Submagic-style: white Montserrat Bold, mixed case, very subtle outline,
    # noticeable but soft drop shadow — matches the app.submagic.co default look.
    "submagic":      CaptionStyle(font="Montserrat", size=80,
                              primary_color="&H00FFFFFF",
                              outline_color="&H60000000",       # mostly transparent black, barely-there edge
                              outline=1, shadow=4, bold=True, box=False,
                              words_per_card=3, margin_v=400, all_caps=False),

    # The Bold Font / "but jewelry" CapCut look — heavy rounded sans, mixed
    # case, no outline, just a clean drop shadow. Uses Poppins ExtraBold as
    # the free stand-in for the (paid) The Bold Font.
    "modern-bold":   CaptionStyle(font="Poppins ExtraBold", size=95,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=0, shadow=4, bold=True, box=False,
                              words_per_card=3, margin_v=400, all_caps=False),

    # Soft & friendly: Arial Rounded MT Bold (locally installed). Mixed case,
    # gentle drop shadow, barely-there outline. Reads as warm/lifestyle/podcast
    # rather than punchy/TikTok. Hugo dropped the .ttf into state/fonts/.
    "rounded-soft":  CaptionStyle(font="Arial Rounded MT Bold", size=88,
                              primary_color="&H00FFFFFF",
                              outline_color="&H80000000",       # mostly-transparent edge
                              outline=2, shadow=5, bold=True, box=False,
                              words_per_card=3, margin_v=400, all_caps=False),

    # Same rounded font, but pop the active word in soft yellow — keeps the
    # friendly read while adding a TikTok-style emphasis beat.
    "rounded-pop":   CaptionStyle(font="Arial Rounded MT Bold", size=92,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=3, shadow=4, bold=True, box=False,
                              words_per_card=3,
                              highlight_color="&H0000F4FF",       # warm yellow
                              margin_v=400, all_caps=False),

    # Instagram Sans Bold — the official IG caption font. Slightly wider /
    # warmer than Helvetica, mid-weight bold, designed for legibility on
    # photo backgrounds. Mixed case + soft shadow gives the polished
    # editorial look you see on most Reels captions.
    "instagram":     CaptionStyle(font="Instagram Sans Bold", size=82,
                              primary_color="&H00FFFFFF",
                              outline_color="&H60000000",       # very subtle edge
                              outline=2, shadow=5, bold=True, box=False,
                              words_per_card=3, margin_v=400, all_caps=False),

    # Same Instagram Sans but with a magenta-pink active word highlight —
    # IG-feed flavor for emphasis-driven cuts.
    "instagram-pop": CaptionStyle(font="Instagram Sans Bold", size=88,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=2, shadow=4, bold=True, box=False,
                              words_per_card=3,
                              highlight_color="&H00B56BFF",       # RGB 255,107,181
                              margin_v=400, all_caps=False),

    # Instagram Sans Bold, centered in the middle of the screen — for
    # talking-head reels where you want eye-level captions instead of
    # the usual lower-third position. ASS alignment=5 = middle-center;
    # libass ignores margin_v at middle alignments so the text sits
    # exactly at 50% Y.
    "instagram-center": CaptionStyle(font="Instagram Sans Bold", size=92,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=3, shadow=6, bold=True, box=False,
                              words_per_card=3,
                              alignment=5,                        # middle-center
                              margin_v=0, all_caps=False),

    # TikTok Sans ExtraBold — the official TikTok-platform font. Very heavy,
    # condensed, designed for vertical mobile. Pairs with all-caps + cyan
    # active-word highlight to match the TikTok caption-popout aesthetic.
    "tiktok-pop":    CaptionStyle(font="TikTok Sans ExtraBold", size=110,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=5, shadow=3, bold=True, box=False,
                              words_per_card=3,
                              highlight_color="&H0000FFFF",       # yellow (BGR FFFF00 = RGB 255,255,0)
                              margin_v=400, all_caps=True),

    # TikTok Sans Black — heaviest weight, mixed case, no highlight. Clean
    # but commands attention. The "premium product launch" feel rather
    # than the popout-yellow viral look.
    "tiktok-black":  CaptionStyle(font="TikTok Sans Black", size=100,
                              primary_color="&H00FFFFFF",
                              outline_color="&H00000000",
                              outline=4, shadow=5, bold=True, box=False,
                              words_per_card=3, margin_v=400, all_caps=False),

    # --- Remotion-rendered templates (engine="remotion") ---
    # These cannot be reproduced in ASS — they rely on spring physics,
    # multi-layer glow, per-word entrance animation. Rendered via the
    # React project at `<repo>/remotion/`.

    # PRO premium default — combines Submagic's random-keyword color
    # emphasis + CapCut's accent glow + MrBeast's italic ALLCAPS punch.
    # 22% active-word scale boost, 16% larger size than `submagic-pop`,
    # Montserrat 900 italic. This is the recommended caption look.
    "submagic-pro":  CaptionStyle(font="Montserrat", size=130,
                              highlight_color="&H0000D4FF",   # #FFD400 yellow primary accent
                              words_per_card=3, margin_v=460, all_caps=True,
                              engine="remotion", composition_id="SubmagicPro"),

    # Submagic-style word-by-word pop with spring entrance, yellow active.
    "submagic-pop":  CaptionStyle(font="Inter", size=120,
                              highlight_color="&H0000D4FF",   # #FFD400 yellow (BGR &H00D4FF)
                              words_per_card=3, margin_v=420, all_caps=True,
                              engine="remotion", composition_id="SubmagicPop"),

    # MrBeast / Hormozi-style: ALLCAPS, no entry animation, single keyword
    # in the card popped yellow.
    "mrbeast-bold": CaptionStyle(font="Anton", size=140,
                              highlight_color="&H0000FFFF",   # #FFFF00 yellow
                              words_per_card=3, margin_v=480, all_caps=True,
                              engine="remotion", composition_id="MrBeastBold"),

    # CapCut-style cyan-glow lines with phrase entrance.
    "capcut-glow":  CaptionStyle(font="Poppins", size=100,
                              highlight_color="&H00FFE500",   # #00E5FF cyan (BGR &HFFE500)
                              words_per_card=5, margin_v=380, all_caps=False,
                              engine="remotion", composition_id="CapCutGlow"),

    # CapCut "purple pill on active word" — pixel-match for the reference
    # video Hugo brought (33.mov). White ALLCAPS Montserrat Black, vibrant
    # violet pill follows the currently-spoken word, no outline, mid-screen.
    # Renders locally via Remotion because VEED's fal.ai API applies
    # background_color to the WHOLE card, not per-active-word.
    #
    # Defaults below match the Style-tab settings Hugo dialed in after the
    # first round of test renders (74pt / margin_v=840 / shadow distance 3px
    # / shadow blur 20px / font weight 900 / #7800f0 purple). Bake those
    # into the template so a fresh render produces the look immediately
    # without anyone touching the overrides panel.
    # CapCut "yellow karaoke" — exact replica of Hugo's reference video
    # "silas ears 11.mov" (frame-decoded 2026-06-10). Poppins Black ALLCAPS,
    # near-white text with THICK black outline + soft shadow; the currently
    # spoken word's FILL turns yellow (#F8F800, sampled) — instant karaoke
    # hop, no entrance animation, mid-screen cards of ~4 words.
    "capcut-yellow":  CaptionStyle(font="Poppins", size=85,
                              highlight_color="&H0000F8F8",   # #F8F800 yellow (ASS BGR)
                              outline_color="&H00000000",
                              outline=0,                      # 0 → comp default (~9.5% of font)
                              shadow=0,                       # 0 → comp default soft shadow
                              font_weight=900,
                              words_per_card=4, margin_v=920, # ≈52% down on 1920
                              all_caps=True,
                              engine="remotion",
                              composition_id="CapCutYellowKaraoke"),

    # CapCut "blue box" — exact replica of Hugo's reference video
    # "Silas ears 10.mov" (frame-decoded 2026-06-10). Same Poppins Black
    # ALLCAPS base with a thinner outline; the spoken word gets a vivid blue
    # (#0070F8, sampled) rounded box behind it, hopping word-to-word
    # instantly. No entrance animation, mid-screen.
    "capcut-bluebox": CaptionStyle(font="Poppins", size=85,
                              highlight_color="&H00F87000",   # #0070F8 blue (ASS BGR)
                              outline_color="&H00000000",
                              outline=0,
                              shadow=0,
                              font_weight=900,
                              words_per_card=4, margin_v=920,
                              all_caps=True,
                              engine="remotion",
                              composition_id="CapCutBlueBox"),

    "capcut-purple-pill": CaptionStyle(font="Montserrat", size=74,
                              highlight_color="&H00F00078",   # #7800f0 vivid violet (ASS BGR)
                              outline_color="&H00000000",     # black stroke
                              outline=0,
                              shadow=3,                       # text-shadow OFFSET in px
                              shadow_blur=20,                 # text-shadow BLUR radius in px
                              font_weight=900,
                              opacity=1.0,
                              words_per_card=3, margin_v=840,
                              all_caps=True,
                              engine="remotion",
                              composition_id="CapCutPurplePill"),

    # --- VEED Subtitle Styling templates (engine="veed") ----------------------
    # Cloud-rendered captions via fal.ai's `fal-ai/workflow-utilities/auto-subtitle`
    # endpoint. Higher visual quality than our local ASS/Remotion paths (matches
    # Submagic/CapCut aesthetic). Costs ~$0.10/min. Requires FAL_API_KEY.
    # The visible fields below are placeholders for the picker grid; the
    # actual fal request params live in `veed_params`.

    # Submagic-style: bold white + yellow active-word highlight, bottom position.
    "veed-yellow":  CaptionStyle(font="Montserrat", size=100,
                              primary_color="&H00FFFFFF",
                              highlight_color="&H0000D4FF",   # #FFD400 yellow
                              words_per_card=3, margin_v=200, all_caps=False,
                              engine="veed",
                              veed_params={
                                  "font_name": "Montserrat",
                                  "font_size": 100,
                                  "font_weight": "bold",
                                  "font_color": "white",
                                  "highlight_color": "yellow",
                                  "stroke_width": 3,
                                  "stroke_color": "black",
                                  "background_color": "none",
                                  "position": "bottom",
                                  "y_offset": 200,
                                  "words_per_subtitle": 3,
                                  "enable_animation": True,
                              }),

    # Purple/pink highlight — more editorial / IG-reels feel.
    "veed-purple": CaptionStyle(font="Inter", size=100,
                              primary_color="&H00FFFFFF",
                              highlight_color="&H00FF00FF",   # magenta-ish
                              words_per_card=3, margin_v=200, all_caps=False,
                              engine="veed",
                              veed_params={
                                  "font_name": "Inter",
                                  "font_size": 100,
                                  "font_weight": "black",
                                  "font_color": "white",
                                  "highlight_color": "purple",
                                  "stroke_width": 3,
                                  "stroke_color": "black",
                                  "background_color": "none",
                                  "position": "bottom",
                                  "y_offset": 200,
                                  "words_per_subtitle": 3,
                                  "enable_animation": True,
                              }),

    # Center-screen variant — talking-head reels where captions sit at eye level.
    "veed-center": CaptionStyle(font="Montserrat", size=110,
                              primary_color="&H00FFFFFF",
                              highlight_color="&H0000D4FF",
                              words_per_card=3, margin_v=960,
                              alignment=5,                     # middle-center
                              all_caps=False,
                              engine="veed",
                              veed_params={
                                  "font_name": "Montserrat",
                                  "font_size": 110,
                                  "font_weight": "black",
                                  "font_color": "white",
                                  "highlight_color": "yellow",
                                  "stroke_width": 4,
                                  "stroke_color": "black",
                                  "background_color": "none",
                                  "position": "center",
                                  "y_offset": 0,
                                  "words_per_subtitle": 3,
                                  "enable_animation": True,
                              }),

    # CapCut "purple pill on active word" template — white ALLCAPS text with
    # NO stroke, active word gets a vibrant purple background pill while
    # inactive words stay transparent. Matches a common TikTok/IG-reels style
    # Hugo brought a sample for (the "33.mov" reference in his CapCut library).
    # Whether `background_color` applies per-active-word OR per-card depends
    # on VEED's animation engine — first render against a real clip confirms.
    "veed-purple-pill": CaptionStyle(font="Montserrat", size=110,
                              primary_color="&H00FFFFFF",
                              highlight_color="&H00F65C8B",       # purple-ish for ASS preview
                              words_per_card=3, margin_v=900,     # ~50% down on 1920
                              alignment=5,                         # middle-center
                              all_caps=True,
                              engine="veed",
                              veed_params={
                                  "font_name": "Montserrat",
                                  "font_size": 110,
                                  "font_weight": "black",
                                  "font_color": "white",
                                  "highlight_color": "white",     # text stays white when active
                                  "stroke_width": 0,
                                  "stroke_color": "black",
                                  "background_color": "purple",   # pill behind active word
                                  "background_opacity": 1.0,
                                  "position": "center",
                                  "y_offset": 50,                  # slight below center
                                  "words_per_subtitle": 3,
                                  "enable_animation": True,
                              }),

    # MrBeast/Hormozi-style ALLCAPS with heavy stroke and yellow active word.
    "veed-mrbeast": CaptionStyle(font="Anton", size=120,
                              primary_color="&H00FFFFFF",
                              highlight_color="&H0000D4FF",
                              words_per_card=3, margin_v=240, all_caps=True,
                              engine="veed",
                              veed_params={
                                  "font_name": "Anton",
                                  "font_size": 120,
                                  "font_weight": "black",
                                  "font_color": "white",
                                  "highlight_color": "yellow",
                                  "stroke_width": 5,
                                  "stroke_color": "black",
                                  "background_color": "none",
                                  "position": "bottom",
                                  "y_offset": 240,
                                  "words_per_subtitle": 3,
                                  "enable_animation": True,
                              }),
}


def _ass_header(style: CaptionStyle) -> str:
    """Build the [Script Info] + [V4+ Styles] block of an ASS file."""
    fields = [
        "Default", style.font, str(style.size),
        style.primary_color, "&H000000FF", style.outline_color, style.back_color,
        "-1" if style.bold else "0", "0", "0", "0", "100", "100", "0", "0",
        "3" if style.box else "1",                # BorderStyle: 1=outline, 3=opaque box
        str(style.outline), str(style.shadow),
        str(style.alignment), "20", "20", str(style.margin_v), "1",
    ]
    style_line = "Style: " + ",".join(fields)
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1080\nPlayResY: 1920\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"{style_line}\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _format_ts(t: float) -> str:
    """ASS timestamp format: H:MM:SS.CS (centiseconds)."""
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t - h * 3600 - m * 60
    return f"{h}:{m:02d}:{s:05.2f}"


# Breathing room at scene joins (backlog #30, 2026-06-12): Kling masters end
# ≤0.14s after the last word and the onset trim cut the next clip to the
# first phoneme — every join felt like a hard jump cut. Clips after the
# first keep a short pre-roll before the audio onset, and every clip keeps a
# short tail after its last sound (bounded by the source material).
_JOIN_HEAD_SECS = 0.12
_JOIN_TAIL_SECS = 0.25

# A caption card never spans a real pause or scene join (backlog #21,
# 2026-06-12): a gap longer than this between consecutive words starts a new
# card — otherwise the next scene's words sat on screen seconds early.
# Mirrored in remotion/src/lib/useCurrentWord.ts (GAP_BREAK_SECS) and
# app.js captionCards(); a pytest keeps the three in sync.
CARD_GAP_BREAK_SECS = 0.8


def _group_words(words: list[Word], per_card: int) -> list[tuple[float, float, list[Word]]]:
    """Group words into up-to-N-word cards, breaking EARLY at real pauses
    (> CARD_GAP_BREAK_SECS). Returns (card_start, card_end, words)."""
    cards: list[tuple[float, float, list[Word]]] = []
    current: list[Word] = []
    for w in words:
        if current and (len(current) >= per_card
                        or w.start - current[-1].end > CARD_GAP_BREAK_SECS):
            cards.append((current[0].start, current[-1].end, current))
            current = []
        current.append(w)
    if current:
        cards.append((current[0].start, current[-1].end, current))
    return cards


def _ass_events(words: list[Word], style: CaptionStyle) -> str:
    """Emit one ASS Dialogue line per card. If `highlight_color` is set, also
    emit per-word overrides so the spoken word pops in the highlight color.

    When `margin_h` is non-zero, prepends a `\\pos(x, y)` override per event so
    the caption lands at a custom point on the 1080×1920 canvas (overrides the
    Style's MarginV + alignment-based placement)."""
    def _case(w: str) -> str:
        return w.upper() if style.all_caps else w

    # Free-position override — used when user has dragged the text off-center.
    pos_prefix = ""
    if style.margin_h != 0:
        x = 540 + int(style.margin_h)
        y = 1920 - int(style.margin_v)
        pos_prefix = f"{{\\pos({x},{y})}}"

    out_lines: list[str] = []
    cards = _group_words(words, style.words_per_card)
    for card_start, card_end, chunk in cards:
        if style.highlight_color:
            # Word-by-word karaoke: each word is highlighted only during its own
            # timestamp range. We render one dialogue per word with the full card
            # text but only one word colored. Cheap and reliable.
            for active_idx, active in enumerate(chunk):
                parts: list[str] = []
                for j, w in enumerate(chunk):
                    word = _case(w.text.strip())
                    if j == active_idx:
                        parts.append(f"{{\\c{style.highlight_color}}}{word}{{\\c{style.primary_color}}}")
                    else:
                        parts.append(word)
                text = pos_prefix + " ".join(parts)
                out_lines.append(
                    f"Dialogue: 0,{_format_ts(active.start)},{_format_ts(active.end)},"
                    f"Default,,0,0,0,,{text}"
                )
        else:
            text = pos_prefix + " ".join(_case(w.text.strip()) for w in chunk)
            out_lines.append(
                f"Dialogue: 0,{_format_ts(card_start)},{_format_ts(card_end)},"
                f"Default,,0,0,0,,{text}"
            )
    return "\n".join(out_lines)


def _write_ass(words: list[Word], style: CaptionStyle, dest: Path) -> Path:
    dest.write_text(_ass_header(style) + _ass_events(words, style), encoding="utf-8")
    return dest


def render_captions(input_video: Path, output_video: Path, *,
                    words: list[Word], style: CaptionStyle,
                    job_id: str | None = None) -> dict:
    """Burn captions into `input_video`. Routes between three engines based on
    `style.engine`:
      - "ass"      → legacy ffmpeg+ASS (no animation, fast)
      - "remotion" → React composition via `npx remotion render`
      - "veed"     → cloud render on fal.ai's VEED Subtitle Styling endpoint
                     (highest quality, ~$0.10/min, requires FAL_API_KEY).
                     For this engine `words` is ignored — VEED runs its own
                     transcription internally so we hand off the raw MP4.
    Returns a summary dict."""
    if style.engine == "veed":
        from character_swap.clients import fal_veed
        params = dict(style.veed_params or {})
        # Drop our explicit keyword-only fields from the dict so kwargs match.
        summary = fal_veed.render_captions(
            input_video, output_video,
            font_name=params.pop("font_name", style.font),
            font_size=params.pop("font_size", style.size),
            font_weight=params.pop("font_weight", "bold"),
            font_color=params.pop("font_color", "white"),
            highlight_color=params.pop("highlight_color", "yellow"),
            stroke_width=params.pop("stroke_width", 3),
            stroke_color=params.pop("stroke_color", "black"),
            background_color=params.pop("background_color", "none"),
            background_opacity=params.pop("background_opacity", None),
            position=params.pop("position", "bottom"),
            y_offset=params.pop("y_offset", 75),
            words_per_subtitle=params.pop("words_per_subtitle",
                                          style.words_per_card),
            enable_animation=params.pop("enable_animation", True),
            language=params.pop("language", "en"),
            extra_params=params or None,
            job_id=job_id,
        )
        return {**summary,
                "n_words": summary.get("n_words", 0),
                "template": "veed"}
    if style.engine == "remotion":
        from character_swap import remotion_render
        if not style.composition_id:
            raise ValueError(
                f"CaptionStyle.engine='remotion' but no composition_id set "
                f"(font={style.font})"
            )
        word_dicts = [{"text": w.text, "start": w.start, "end": w.end} for w in words]
        return remotion_render.render_remotion(
            input_video, output_video,
            composition_id=style.composition_id,
            props=style.to_remotion_props(),
            words=word_dicts,
            job_id=job_id,
        )
    with record(phase="editor_captions", model="ffmpeg-subtitles",
                character="editor", job_id=job_id):
        # When the user has set font_weight via the Style tab, swap the
        # template's font for a heavier installed variant in the same
        # family (ASS engine has no synthetic weight — heaviness comes
        # from the font file). When font_weight is None, this is a no-op.
        import dataclasses as _dc
        resolved_font_name = _resolve_font_for_weight(style.font, style.font_weight)
        if resolved_font_name != style.font:
            style = _dc.replace(style, font=resolved_font_name)
        # Ensure the chosen font is available (downloads on first use).
        _ensure_font(style.font)
        ass_path = input_video.with_suffix(".captions.ass")
        _write_ass(words, style, ass_path)
        output_video.parent.mkdir(parents=True, exist_ok=True)
        # `subtitles` filter doesn't escape special chars in the path
        # gracefully — escape colons + commas + brackets.
        ass_arg = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", r"\'")
        fonts_arg = str(_fonts_dir()).replace("\\", "/").replace(":", "\\:")
        _run([
            _ffmpeg(), "-y", "-i", str(input_video),
            "-vf", f"subtitles='{ass_arg}':fontsdir='{fonts_arg}'",
            *_enc_v(),
            "-c:a", "copy",
            str(output_video),
        ])
        ass_path.unlink(missing_ok=True)
        return {"n_words": len(words), "template": style.font + f"/{style.size}"}


def compute_wpm(words: list[Word], *,
                ignore_silence_above_secs: float = 0.4) -> float:
    """Words per minute, robust to Whisper's quirks.

    OpenAI's whisper-1 API returns word-level timestamps that are
    typically interpolated INSIDE each segment: word[i].end is set to
    word[i+1].start (no gap). So summing per-word durations is
    equivalent to using the span from first-word.start to
    last-word.end — both fold inter-word silences into "speaking time".

    The real talking-pace measure is:

        active_speaking_secs = span − sum(inter-word gaps > threshold)

    i.e. the time minus REAL pauses (between phrases, mid-sentence
    breaths, etc.). This gives a number that answers "if you silence-
    trimmed this clip first, what would its WPM be?" — which is what
    "talking speed" actually means perceptually.

    A clip where the speaker delivers 5 words then pauses 2 seconds
    then delivers 5 more words ends up with the SAME WPM as a clip
    where they deliver 10 words back-to-back at the same mouth-speed.
    Stretching both to the same target gives them the same perceived
    talking pace while preserving their original pause structures.

    Returns 0.0 for empty / too-short / zero-duration lists.
    """
    if len(words) < 2:
        return 0.0
    total_span = words[-1].end - words[0].start
    if total_span <= 0.1:
        return 0.0
    # Subtract LONG inter-word gaps (real pauses). Short gaps are
    # Whisper segmentation noise / breathing / micro-pauses and should
    # stay counted as part of speaking time.
    long_pause_secs = 0.0
    for i in range(1, len(words)):
        gap = words[i].start - words[i - 1].end
        if gap > ignore_silence_above_secs:
            long_pause_secs += gap
    active_secs = total_span - long_pause_secs
    if active_secs <= 0.1:
        return 0.0
    return (len(words) / active_secs) * 60.0


def compute_speed_factor(words: list[Word], *,
                         target_wpm: float = 190.0,
                         min_factor: float = 0.5,
                         max_factor: float = 2.0,
                         dead_zone: float = 0.03) -> float:
    """The ffmpeg `atempo` factor that brings the clip's spoken pace to
    target_wpm.

    Key insight: ffmpeg's `atempo=X` makes audio play X times AS FAST.
    So if the source is 252 WPM (too fast) and target is 190, we want
    to SLOW DOWN — atempo should be < 1 (specifically 190/252 = 0.754).
    Conversely if the source is 130 WPM (too slow), atempo > 1 to speed
    up.

    Formula: `target_wpm / current_wpm`.

    After playback at this factor, new spoken pace = current × factor =
    current × (target / current) = target. ✓

    Returns 1.0 (no change) when:
      - the transcript is empty / too short to measure (`compute_wpm` == 0)
      - the current pace is within `dead_zone` of target (±3% by default)
        — no point burning an ffmpeg pass for a negligible adjustment

    Clamped to `[min_factor, max_factor]` so we stay inside ffmpeg
    `atempo`'s pitch-preserving single-pass range. Pace outside this
    range still gets *some* normalization at the boundary.
    """
    current = compute_wpm(words)
    if current <= 0:
        return 1.0
    factor = target_wpm / current
    if abs(factor - 1.0) < dead_zone:
        return 1.0
    return max(min_factor, min(max_factor, factor))


def time_stretch(input_path: Path, output_path: Path, *,
                 speed_factor: float, job_id: str | None = None) -> Path:
    """Time-stretch a video by `speed_factor` (>1 speeds up, <1 slows
    down). Audio pitch is preserved via ffmpeg's `atempo` filter; video
    PTS is scaled in lockstep via `setpts` so A/V stay in sync.

    speed_factor very close to 1.0 takes a fast path — we still re-encode
    so the output has a consistent codec for downstream `concat_videos`,
    but skip the atempo/setpts filters.

    speed_factor outside [0.5, 2.0] raises ValueError — callers should
    clamp via `compute_speed_factor` first.
    """
    if speed_factor < 0.5 or speed_factor > 2.0:
        raise ValueError(
            f"speed_factor {speed_factor} outside safe atempo range [0.5, 2.0]"
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    has_audio = _has_audio_stream(input_path)
    with record(phase="editor_time_stretch", model="ffmpeg-atempo",
                character="editor", job_id=job_id) as entry:
        entry["speed_factor"] = round(speed_factor, 4)
        entry["has_audio"] = has_audio
        if abs(speed_factor - 1.0) < 1e-3:
            # Passthrough: still re-encode to clean h264/aac so downstream
            # concat sees the same codec as a stretched clip would have.
            cmd = [_ffmpeg(), "-y", "-i", str(input_path),
                   *_enc_v()]
            if has_audio:
                cmd += ["-c:a", "aac", "-b:a", "192k"]
            else:
                cmd += ["-an"]
            cmd += [str(output_path)]
            _run(cmd)
            return output_path
        # `atempo` is pitch-preserving for audio; `setpts=PTS/factor`
        # scales the video time-base by the same factor so the two
        # streams stay locked. For audio-less inputs (Higgsfield clips),
        # we skip the [0:a]atempo branch entirely.
        if has_audio:
            cmd = [_ffmpeg(), "-y", "-i", str(input_path),
                   "-filter_complex",
                   f"[0:v]setpts=PTS/{speed_factor:.4f}[v];"
                   f"[0:a]atempo={speed_factor:.4f}[a]",
                   "-map", "[v]", "-map", "[a]",
                   *_enc_v(),
                   "-c:a", "aac", "-b:a", "192k",
                   str(output_path)]
        else:
            cmd = [_ffmpeg(), "-y", "-i", str(input_path),
                   "-filter_complex",
                   f"[0:v]setpts=PTS/{speed_factor:.4f}[v]",
                   "-map", "[v]",
                   *_enc_v(),
                   "-an",
                   str(output_path)]
        _run(cmd)
    return output_path


def shift_word_timestamps(words: list[Word], offset: float) -> list[Word]:
    """Subtract `offset` from every word's start/end timestamps (clamped to 0).
    Used after `trim_to_first_word` re-cuts the start of a clip so the
    transcript still lines up with the new file's timeline.
    """
    if offset <= 0:
        return list(words)
    out: list[Word] = []
    for w in words:
        new_start = max(0.0, w.start - offset)
        new_end = max(new_start + 0.001, w.end - offset)
        out.append(Word(text=w.text, start=new_start, end=new_end))
    return out


def scale_word_timestamps(words: list[Word], speed_factor: float) -> list[Word]:
    """After a clip has been time-stretched by `speed_factor`, every
    Word's start/end timestamps must be divided by `speed_factor` to
    align with the new (shorter or longer) clip. Higher speed → shorter
    output → smaller timestamps.

    Returns a new list — `words` is not mutated.
    """
    if abs(speed_factor - 1.0) < 1e-3:
        return list(words)
    return [
        Word(text=w.text,
             start=w.start / speed_factor,
             end=w.end / speed_factor)
        for w in words
    ]


def extract_last_frame(video_path: Path, dest_png: Path) -> Path | None:
    """Pull the last frame of a video out as a PNG. Returns the dest path
    on success, or None if ffmpeg fails (corrupt clip, zero-duration,
    codec mismatch, etc.) so callers can fall back gracefully.

    Used by the B-roll runner to chain scene-group clips: clip N+1's
    video gen starts from clip N's last frame, so the same physical
    scene with cumulative state carries forward.

    `-sseof -1.0` seeks 1.0s before EOF and decodes one frame from
    there. For clips shorter than 1s, ffmpeg gracefully clamps to the
    available range and still emits the last available frame.
    `-q:v 2` keeps the JPEG-equivalent quality high (libavformat uses
    this for PNG-via-mjpeg edge cases on some platforms).
    """
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        _run([
            _ffmpeg(), "-y", "-sseof", "-1.0",
            "-i", str(video_path),
            "-vframes", "1", "-q:v", "2",
            str(dest_png),
        ])
    except RuntimeError:
        return None
    return dest_png if dest_png.exists() and dest_png.stat().st_size > 0 else None


def trim_range(input_path: Path, output_path: Path, *,
               start_secs: float, end_secs: float) -> Path:
    """Cut a video to [start, end] seconds (re-encode for clean frames).
    `end_secs` <= 0 means 'until end of file'."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [_ffmpeg(), "-y", "-ss", f"{max(0.0, start_secs):.3f}"]
    if end_secs and end_secs > start_secs:
        args += ["-to", f"{end_secs:.3f}"]
    args += [
        "-i", str(input_path),
        *_enc_v(),
        "-c:a", "aac", "-b:a", "192k",
        str(output_path),
    ]
    _run(args)
    return output_path


def words_to_json(words: list[Word]) -> str:
    return json.dumps([{"text": w.text, "start": w.start, "end": w.end} for w in words])


def words_from_json(raw: str) -> list[Word]:
    return [Word(text=w["text"], start=float(w["start"]), end=float(w["end"]))
            for w in json.loads(raw)]


def filter_and_shift_words(words: list[Word], *, start: float, end: float) -> list[Word]:
    """Keep words inside [start, end] and shift timestamps so the trimmed clip
    starts at 0. Used when re-rendering after a manual trim."""
    out: list[Word] = []
    eff_end = end if end and end > 0 else float("inf")
    for w in words:
        if w.end <= start or w.start >= eff_end:
            continue
        out.append(Word(
            text=w.text,
            start=max(0.0, w.start - start),
            end=max(0.0, min(w.end, eff_end) - start),
        ))
    return out


def _target_resolution(aspect_ratio: str) -> tuple[int, int]:
    """Map an aspect ratio string ("9:16", "1:1", "16:9") to a target
    pixel resolution. All output sizes target a 1080-pixel short edge
    to match Grok/Veo/Kling defaults."""
    a = (aspect_ratio or "9:16").strip()
    return {
        "9:16": (1080, 1920),   # vertical (TikTok / Reels / Shorts)
        "1:1":  (1080, 1080),   # square (Instagram feed)
        "16:9": (1920, 1080),   # landscape (YouTube / web)
    }.get(a, (1080, 1920))


def concat_videos(video_paths: list[Path], output_path: Path,
                  *, aspect_ratio: str = "9:16") -> Path:
    """Concatenate N videos into one. Re-encodes (rather than concat demuxer)
    so clips with different codecs/resolutions still play back cleanly.

    `aspect_ratio` determines the target canvas — clips with different
    aspect ratios get letterboxed/pillarboxed onto the chosen canvas.

    Audio handling:
      - If ALL inputs have audio: standard concat with audio output.
      - If NONE have audio (Higgsfield Supercomputer clips are video-only):
        concat produces a video-only output.
      - If MIXED: synthesizes silent audio (anullsrc) for the video-only
        inputs so the concat filter sees a uniform a/v stream layout, then
        outputs a normal video+audio file. The user can voice-swap or add
        a voiceover afterwards.
    """
    if not video_paths:
        raise ValueError("concat_videos: no inputs")
    has_audio_flags = [_has_audio_stream(p) for p in video_paths]
    any_audio = any(has_audio_flags)
    all_audio = all(has_audio_flags)

    if len(video_paths) == 1:
        # Just copy through; preserve or drop audio based on the single input.
        cmd = [_ffmpeg(), "-y", "-i", str(video_paths[0]),
               *_enc_v()]
        if has_audio_flags[0]:
            cmd += ["-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-an"]
        cmd += [str(output_path)]
        _run(cmd)
        return output_path

    # Use the concat filter (handles different fps / size) with auto-scaling
    # to the chosen target resolution. Landscape sources get letterboxed if
    # we're outputting vertical, vertical sources get pillarboxed if we're
    # outputting landscape. Audio normalized to 44.1kHz stereo when present.
    inputs: list[str] = []
    for p in video_paths:
        inputs += ["-i", str(p)]
    # When MIXED, append one anullsrc input per video-only clip; the audio
    # branch in the filter graph maps from those synthetic inputs instead.
    synth_indices: dict[int, int] = {}  # clip_idx → input_idx in cmd
    if any_audio and not all_audio:
        for i, has in enumerate(has_audio_flags):
            if not has:
                synth_indices[i] = len(video_paths) + len(synth_indices)
                inputs += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

    target_w, target_h = _target_resolution(aspect_ratio)
    parts: list[str] = []
    for i in range(len(video_paths)):
        parts.append(
            f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]"
        )
        if any_audio:
            # Pick the audio source for this clip: real audio from the clip
            # itself if it has one, else the synth-silent anullsrc input.
            audio_src_idx = i if has_audio_flags[i] else synth_indices[i]
            parts.append(
                f"[{audio_src_idx}:a]aresample=44100,aformat=channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{i}]"
            )

    if any_audio:
        labels = "".join(f"[v{i}][a{i}]" for i in range(len(video_paths)))
        parts.append(f"{labels}concat=n={len(video_paths)}:v=1:a=1[outv][outa]")
    else:
        labels = "".join(f"[v{i}]" for i in range(len(video_paths)))
        parts.append(f"{labels}concat=n={len(video_paths)}:v=1:a=0[outv]")
    filter_complex = ";".join(parts)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [_ffmpeg(), "-y", *inputs,
           "-filter_complex", filter_complex,
           "-map", "[outv]"]
    if any_audio:
        cmd += ["-map", "[outa]",
                *_enc_v(),
                "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += [*_enc_v(),
                "-an"]
    cmd += [str(output_path)]
    _run(cmd)
    return output_path


def assemble_clips(video_paths: list[Path], output_path: Path, *,
                   aspect_ratio: str = "9:16",
                   enable_interior_trim: bool = True,
                   threshold_db: float = -30.0,
                   min_silence_secs: float = 0.30,
                   pad_secs: float = 0.03,
                   job_id: str | None = None) -> dict:
    """Audio-onset trim + interior/trailing silence trim + scale + concat of
    N clips in ONE libx264 generation (2026-06-12).

    Replaces the old three-step chain (per-clip trim_leading_silence →
    concat_videos → trim_silences) in the shared Editor pipeline — each step
    was its own full re-encode, so a Kling master lost three CRF generations
    before captions. Here all cutting happens as analysis (silencedetect,
    no encode) and ONE filter_complex performs every trim, the scale/pad to
    the target canvas, and the concat in a single encode.

    Semantics match the old chain:
      - Leading silence is ALWAYS cut per clip (audio onset, ≥0.05s lead) —
        Hugo's universal entry-trim rule, independent of `enable_interior_trim`.
      - `enable_interior_trim` collapses interior pauses ≥ `min_silence_secs`
        (with `pad_secs` kept around each cut) and drops trailing silence —
        the "Trim silences" toggle.
      - Audio-less clips pass through whole; mixed sets get synth silence so
        the concat layout stays uniform (same as concat_videos).
    Boundary nuance vs the old chain: a pause spanning a clip boundary used
    to be ONE detected silence in the merged file; here it's cut on each
    side independently — same content removed, pad kept on both sides.
    """
    if not video_paths:
        raise ValueError("assemble_clips: no inputs")

    with record(phase="editor_assemble_clips", model="ffmpeg-single-encode",
                character="editor", job_id=job_id) as entry:
        has_audio_flags = [_has_audio_stream(p) for p in video_paths]
        any_audio = any(has_audio_flags)

        # ---- one loudness measurement per clip (analysis only) ------------
        # Feeds BOTH the static equalization gain (backlog #10) and the
        # per-clip adaptive silence threshold (backlog #37).
        measured: list[tuple[float, float] | None] = [None] * len(video_paths)
        if any_audio and (settings.loudnorm_enabled
                          or settings.adaptive_silence_threshold):
            measured = [_measure_clip_loudness(p) if has_audio else None
                        for p, has_audio in zip(video_paths, has_audio_flags)]

        # ---- analysis only (no encodes): keep-ranges per clip -------------
        # One silencedetect pass per clip at d=0.05 catches both the onset
        # lead AND every interior pause; interior cutting only honors pauses
        # ≥ min_silence_secs (filtered by length below).
        keeps: list[list[tuple[float, float]]] = []
        removed_total = 0.0
        for ci, (p, has_audio) in enumerate(zip(video_paths, has_audio_flags)):
            duration = _probe_duration(p)
            if duration <= 0:
                # Probe failure (0.0 is _probe_duration's failure sentinel) —
                # raising here engages run_editor_pipeline's legacy-chain
                # fallback instead of silently trimming the clip to nothing.
                raise RuntimeError(
                    f"assemble_clips: could not probe duration of {p}")
            if not has_audio:
                keeps.append([(0.0, duration)])
                continue
            # Fixed thresholds ate quiet speech on low-level clips and
            # missed pauses on hot ones (backlog #37) — when the clip's
            # loudness is known, the threshold tracks it instead.
            thr = threshold_db
            if (settings.adaptive_silence_threshold
                    and measured[ci] is not None):
                thr = _adaptive_silence_threshold(measured[ci][0])
            silences = _detect_silences(p, thr, 0.05)
            onset = (silences[0][1]
                     if silences and silences[0][0] <= 0.05 else 0.0)
            onset = min(onset, max(0.0, duration - 0.05))
            # Breathing room (backlog #30): clips after the first keep a
            # short pre-roll before the onset so the join doesn't slam into
            # the first phoneme. The final's FIRST clip stays tight (the
            # hook), matching the exact-start contract.
            if ci > 0:
                onset = max(0.0, onset - _JOIN_HEAD_SECS)
            if enable_interior_trim:
                interior = [s for s in silences
                            if (s[1] - s[0]) >= min_silence_secs]
                keep = _invert_silences(interior, duration, pad_secs)
                # The 0.05s onset pass catches short leads the interior
                # filter ignores. Drop keeps that END before the onset (a
                # sub-50ms head click + its pad would otherwise survive as
                # the output's first segment), then clamp the first
                # survivor's start — leading content before the onset is
                # ALWAYS cut, matching trim_leading_silence's contract.
                while keep and keep[0][1] <= onset:
                    keep.pop(0)
                if keep:
                    # First keep starts exactly at the (head-adjusted) onset:
                    # clamps pre-onset pad on the first clip (exact-start
                    # contract) AND grants later clips their #30 pre-roll.
                    keep[0] = (onset, keep[0][1])
                if not keep:   # all-silent clip: keep a 0.5s sliver
                    keep = ([(onset, duration)]
                            if duration - onset > 0.05
                            else [(0.0, min(duration, 0.5))])
                # Breathing room (backlog #30): a short natural tail after
                # the clip's last sound — never cut at last-word + pad.
                last_s, last_e = keep[-1]
                keep[-1] = (last_s, min(duration, last_e + _JOIN_TAIL_SECS))
            else:
                keep = [(onset, duration)]
            keeps.append(keep)
            removed_total += duration - sum(e - s for s, e in keep)

        n_segments = sum(len(k) for k in keeps)
        entry["n_clips"] = len(video_paths)
        entry["n_segments"] = n_segments
        entry["removed_secs"] = round(removed_total, 2)

        # ---- per-clip loudness equalization (backlog #10, 2026-06-12) -----
        # Finals measured -20 LUFS with 3 dB jumps between Kling clips. One
        # static gain per clip (analysis pass only — applied inside the same
        # single encode below, so it costs zero extra generations), true-peak
        # capped at -1 dBTP. LOUDNORM_ENABLED=0 restores untouched audio.
        gains: list[float] = [0.0] * len(video_paths)
        if settings.loudnorm_enabled and any_audio:
            for gi, m in enumerate(measured):
                if m is None:
                    continue
                gains[gi] = round(_clip_gain_db(
                    m[0], m[1], settings.loudnorm_target_lufs), 2)
            entry["loudnorm_gains_db"] = list(gains)

        # ---- one filter_complex: trims + scale/pad + concat ----------------
        inputs: list[str] = []
        for p in video_paths:
            inputs += ["-i", str(p)]
        synth_idx: int | None = None
        if any_audio and not all(has_audio_flags):
            synth_idx = len(video_paths)
            inputs += ["-f", "lavfi", "-i",
                       "anullsrc=channel_layout=stereo:sample_rate=44100"]

        target_w, target_h = _target_resolution(aspect_ratio)
        # Backlog #19 (2026-06-12): fps was hardcoded to 30, so an all-24fps
        # Kling set got ~20% duplicated frames (visible judder). The concat
        # still needs ONE uniform rate — use the highest measured input rate
        # (a minority-low-fps clip gets dups either way; an all-equal set
        # passes through at its native rate). Probe failure → legacy 30.
        measured_fps = [f for f in (_probe_fps(p) for p in video_paths) if f]
        fps_target = max(10.0, min(60.0, max(measured_fps))) if measured_fps else 30.0
        entry["fps"] = round(fps_target, 3)

        parts: list[str] = []
        seg_labels: list[str] = []
        for i, keep in enumerate(keeps):
            for j, (s, e) in enumerate(keep):
                vlab, alab = f"v{i}_{j}", f"a{i}_{j}"
                parts.append(
                    f"[{i}:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS,"
                    f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease,"
                    f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,"
                    f"setsar=1,fps={fps_target:g},format=yuv420p[{vlab}]"
                )
                if any_audio:
                    if has_audio_flags[i]:
                        vol = (f"volume={gains[i]:.2f}dB,"
                               if abs(gains[i]) >= 0.25 else "")
                        parts.append(
                            f"[{i}:a]atrim=start={s:.3f}:end={e:.3f},"
                            f"asetpts=PTS-STARTPTS,{vol}aresample=44100,"
                            f"aformat=channel_layouts=stereo[{alab}]"
                        )
                    else:
                        parts.append(
                            f"[{synth_idx}:a]atrim=start=0:end={e - s:.3f},"
                            f"asetpts=PTS-STARTPTS[{alab}]"
                        )
                    seg_labels.append(f"[{vlab}][{alab}]")
                else:
                    seg_labels.append(f"[{vlab}]")

        av = "v=1:a=1[outv][outa]" if any_audio else "v=1:a=0[outv]"
        parts.append(f"{''.join(seg_labels)}concat=n={n_segments}:{av}")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [_ffmpeg(), "-y", *inputs,
               "-filter_complex", ";".join(parts),
               "-map", "[outv]"]
        if any_audio:
            cmd += ["-map", "[outa]", *_enc_v(), "-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += [*_enc_v(), "-an"]
        cmd += [str(output_path)]
        _run(cmd)

        out_duration = _probe_duration(output_path)
        entry["duration"] = round(out_duration, 2)
        return {"n_clips": len(video_paths), "n_segments": n_segments,
                "duration": round(out_duration, 2),
                "removed_secs": round(removed_total, 2)}


def apply_timeline(input_path: Path, output_path: Path, *,
                   segments: list[tuple[float, float]],
                   job_id: str | None = None) -> dict:
    """Cut `input_path` into the given `segments` (each `(start, end)` in
    seconds, all relative to the source) and concatenate them, in order, to
    `output_path`.

    Powers the CapCut-style timeline editor: the user drags trim handles and
    drops split markers in the UI; the resulting segment list is sent here.
    Segments may be reordered relative to the source — they're concatenated
    in the supplied list order, so segments[0] is the new clip's opening.

    Implemented as a single `filter_complex` with N trims + concat so we
    don't write intermediate files. Re-encodes (libx264/aac) to guarantee
    clean cuts on non-keyframe boundaries.
    """
    if not segments:
        raise ValueError("apply_timeline: no segments provided")
    # Drop degenerate ranges (end <= start, or near-zero length) — they'd
    # produce empty streams that crash the concat filter.
    clean = [(float(s), float(e)) for s, e in segments if float(e) - float(s) > 0.02]
    if not clean:
        raise ValueError("apply_timeline: all segments are degenerate (length <= 0)")

    with record(phase="editor_timeline", model="ffmpeg-trim-concat",
                character="editor", job_id=job_id) as entry:
        entry["n_segments"] = len(clean)
        entry["total_in_secs"] = round(sum(e - s for s, e in clean), 2)

        # Single-segment shortcut: just trim and re-encode.
        if len(clean) == 1:
            start, end = clean[0]
            trim_range(input_path, output_path, start_secs=start, end_secs=end)
            return {
                "n_segments": 1,
                "duration": round(end - start, 2),
                "segments": [{"start": round(start, 3), "end": round(end, 3)}],
            }

        parts: list[str] = []
        for i, (start, end) in enumerate(clean):
            parts.append(
                f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS,"
                f"fps=30,format=yuv420p[v{i}]"
            )
            parts.append(
                f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS,"
                f"aresample=44100,aformat=channel_layouts=stereo[a{i}]"
            )
        labels = "".join(f"[v{i}][a{i}]" for i in range(len(clean)))
        parts.append(f"{labels}concat=n={len(clean)}:v=1:a=1[outv][outa]")
        filter_complex = ";".join(parts)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        _run([
            _ffmpeg(), "-y", "-i", str(input_path),
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "[outa]",
            *_enc_v(),
            "-c:a", "aac", "-b:a", "192k",
            str(output_path),
        ])
        return {
            "n_segments": len(clean),
            "duration": round(_probe_duration(output_path), 2),
            "segments": [{"start": round(s, 3), "end": round(e, 3)} for s, e in clean],
        }


def _normalize_text(s: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — used for matching."""
    import re
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def match_clips_by_transcript(clip_transcripts: list[str], script: str) -> list[dict]:
    """Order clips so their transcripts line up with the script.

    For each clip, finds where its content appears in the script using a
    longest-common-substring search (difflib). Clips with very weak matches
    are appended at the end and flagged `unmatched`.

    Returns a list of dicts in script order, each with `{idx, position,
    score, unmatched}`.
    """
    from difflib import SequenceMatcher
    norm_script = _normalize_text(script)
    if not norm_script:
        return [{"idx": i, "position": i, "score": 0, "unmatched": True}
                for i in range(len(clip_transcripts))]

    placements: list[dict] = []
    for i, transcript in enumerate(clip_transcripts):
        norm_t = _normalize_text(transcript)
        if not norm_t:
            placements.append({"idx": i, "position": len(norm_script) + 1,
                               "score": 0, "unmatched": True})
            continue
        matcher = SequenceMatcher(None, norm_script, norm_t, autojunk=False)
        # find_longest_match returns (a, b, size) where `a` is the start in
        # `norm_script`. Position the clip at that point.
        match = matcher.find_longest_match(0, len(norm_script), 0, len(norm_t))
        # Score = how much of the clip transcript was found, capped at 1.
        score = match.size / max(1, len(norm_t))
        unmatched = match.size < 12 or score < 0.15
        placements.append({
            "idx": i,
            "position": match.a if not unmatched else len(norm_script) + 1 + i,
            "score": round(score, 3),
            "unmatched": unmatched,
        })
    placements.sort(key=lambda x: (x["unmatched"], x["position"]))
    return placements


def replace_audio(video_path: Path, audio_path: Path, output_path: Path) -> Path:
    """Replace the audio track of a video with a new audio file. The video
    stream is copied (no re-encode) and the new audio is encoded as AAC.
    Output duration is clipped to the shorter of (video, audio)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run([
        _ffmpeg(), "-y",
        "-i", str(video_path),
        "-i", str(audio_path),
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path),
    ])
    return output_path


def style_from_params(template: str | None,
                      overrides: dict | None = None) -> CaptionStyle:
    """Look up a template, then apply user overrides (font, size, color, etc.)."""
    base = TEMPLATES.get(template or "tiktok", TEMPLATES["tiktok"])
    if not overrides:
        return base
    # Pydantic-style merge: start from dataclass dict, overlay overrides.
    from dataclasses import asdict, fields
    valid = {f.name for f in fields(CaptionStyle)}
    merged = asdict(base) | {k: v for k, v in overrides.items() if k in valid}
    return CaptionStyle(**merged)
