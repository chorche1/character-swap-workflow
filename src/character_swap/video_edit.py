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
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import imageio_ffmpeg

from character_swap.call_log import record
from character_swap.clients import openai_image  # reuse _client() for OpenAI auth
from character_swap.config import settings


def _ffmpeg() -> str:
    """Path to the ffmpeg binary (bundled by imageio-ffmpeg, no system install)."""
    return imageio_ffmpeg.get_ffmpeg_exe()


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

def _probe_duration(input_path: Path) -> float:
    """Total duration in seconds."""
    out = _run([
        _ffmpeg(), "-hide_banner", "-i", str(input_path), "-f", "null", "-",
    ])
    # ffmpeg prints "Duration: HH:MM:SS.ms," — parse it
    for line in out.splitlines():
        if "Duration:" in line:
            dur = line.split("Duration:")[1].split(",")[0].strip()
            h, m, s = dur.split(":")
            return int(h) * 3600 + int(m) * 60 + float(s)
    return 0.0


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
    Adds a tiny `pad_secs` either side of each keep range so words don't get clipped."""
    keep: list[tuple[float, float]] = []
    cursor = 0.0
    for s_start, s_end in silences:
        if s_start > cursor:
            keep.append((max(0.0, cursor - pad_secs), min(total_duration, s_start + pad_secs)))
        cursor = s_end
    if cursor < total_duration:
        keep.append((max(0.0, cursor - pad_secs), total_duration))
    return [(a, b) for a, b in keep if b - a > 0.05]  # drop microscopic slivers


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
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
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


def _extract_audio(video_path: Path) -> Path:
    """Pull the audio track out as 16kHz mono wav (Whisper's preferred input)."""
    audio_path = video_path.with_suffix(".audio.wav")
    _run([
        _ffmpeg(), "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        str(audio_path),
    ])
    return audio_path


def transcribe_words(video_path: Path, *, job_id: str | None = None) -> list[Word]:
    """Run OpenAI Whisper on the video's audio. Returns word-level timestamps.

    Uses `whisper-1` (current OpenAI Whisper API model) with
    response_format=verbose_json + timestamp_granularities=['word'].
    """
    audio_path = _extract_audio(video_path)
    client = openai_image._client()  # reuses settings.openai_api_key + auth
    with record(phase="editor_transcribe", model="whisper-1",
                character="editor", job_id=job_id):
        with audio_path.open("rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
                timestamp_granularities=["word"],
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
    margin_v: int = 80                  # vertical margin from bottom
    alignment: int = 2                  # 2 = bottom-center (ASS conventions)
    box: bool = False                   # background box behind text
    words_per_card: int = 3             # word-by-word grouping size
    highlight_color: str | None = None  # color used to highlight the active word
    all_caps: bool = False              # force-uppercase the rendered text (Submagic/TikTok aesthetic)


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
    # HONEY" honey-store screenshot Hugo referenced: bold condensed all-caps,
    # white default, yellow active word, thick black outline, no background.
    "popout-yellow": CaptionStyle(font="Impact", size=110,
                              primary_color="&H00FFFFFF",      # white
                              outline_color="&H00000000",      # black
                              outline=8, bold=True, shadow=0, box=False,
                              words_per_card=3,
                              highlight_color="&H0000FFFF",    # yellow (ASS BGR = RGB 255,255,0)
                              margin_v=200, all_caps=True),
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


def _group_words(words: list[Word], per_card: int) -> list[tuple[float, float, list[Word]]]:
    """Group words into N-word cards. Returns (card_start, card_end, words)."""
    cards: list[tuple[float, float, list[Word]]] = []
    for i in range(0, len(words), per_card):
        chunk = words[i:i + per_card]
        if not chunk:
            continue
        cards.append((chunk[0].start, chunk[-1].end, chunk))
    return cards


def _ass_events(words: list[Word], style: CaptionStyle) -> str:
    """Emit one ASS Dialogue line per card. If `highlight_color` is set, also
    emit per-word overrides so the spoken word pops in the highlight color."""
    def _case(w: str) -> str:
        return w.upper() if style.all_caps else w

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
                text = " ".join(parts)
                out_lines.append(
                    f"Dialogue: 0,{_format_ts(active.start)},{_format_ts(active.end)},"
                    f"Default,,0,0,0,,{text}"
                )
        else:
            text = " ".join(_case(w.text.strip()) for w in chunk)
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
    """Burn captions into `input_video`. Uses ffmpeg's `subtitles` filter on a
    generated ASS file. Returns a summary."""
    with record(phase="editor_captions", model="ffmpeg-subtitles",
                character="editor", job_id=job_id):
        ass_path = input_video.with_suffix(".captions.ass")
        _write_ass(words, style, ass_path)
        output_video.parent.mkdir(parents=True, exist_ok=True)
        # `subtitles` filter doesn't escape special chars in the path
        # gracefully — escape colons + commas + brackets.
        ass_arg = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", r"\'")
        _run([
            _ffmpeg(), "-y", "-i", str(input_video),
            "-vf", f"subtitles='{ass_arg}'",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "copy",
            str(output_video),
        ])
        ass_path.unlink(missing_ok=True)
        return {"n_words": len(words), "template": style.font + f"/{style.size}"}


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
