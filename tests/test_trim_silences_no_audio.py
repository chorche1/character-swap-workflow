"""trim_silences on audio-less videos (2026-07-03 re-audit).

trim_silences was the LAST ffmpeg primitive without the `_has_audio_stream`
guard — its filter_complex unconditionally mapped `[0:a]`, so a video-only
input (Higgsfield Supercomputer export, muted screen recording) crashed
ffmpeg with "Stream specifier ':a' ... matches no streams" and 500'd the
whole Editor auto_edit / multi_auto_edit / standalone-trim request, even
though every sibling primitive (trim_leading_silence, time_stretch,
concat_videos, apply_timeline) already passes such clips through. The fix
mirrors trim_leading_silence: probe once, copy through untouched.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from character_swap import video_edit


def _clip(dest: Path, *, secs: float = 2.0, no_audio: bool = False) -> Path:
    """Tiny solid-color test clip, optionally with a 440Hz tone track."""
    args = ["ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", f"color=c=red:s=160x284:d={secs}:r=12"]
    if not no_audio:
        args += ["-f", "lavfi", "-i",
                 f"sine=frequency=440:sample_rate=44100:duration={secs}",
                 "-map", "0:v", "-map", "1:a"]
    args += ["-pix_fmt", "yuv420p", "-shortest", str(dest)]
    subprocess.run(args, check=True, capture_output=True)
    return dest


# ------------------------------------------------------ pass-through (fast)

def test_no_audio_input_passes_through_without_ffmpeg(tmp_path, monkeypatch):
    """Video-only input → copied untouched; the [0:a] filter graph (or any
    ffmpeg invocation) must never be built."""
    src = tmp_path / "silent.mp4"
    src.write_bytes(b"fake-video-bytes")
    out = tmp_path / "out" / "trimmed.mp4"

    ran: list[list[str]] = []
    monkeypatch.setattr(video_edit, "_run",
                        lambda args, **kw: ran.append(args) and "")
    monkeypatch.setattr(video_edit, "_has_audio_stream", lambda p: False)
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 3.0)

    summary = video_edit.trim_silences(src, out)

    assert ran == [], "no ffmpeg call on an audio-less pass-through"
    assert out.read_bytes() == b"fake-video-bytes"
    assert summary == {"original_duration": 3.0, "trimmed_duration": 3.0,
                       "n_cuts": 0, "saved_secs": 0.0}


# ------------------------------------------- real ffmpeg end-to-end (~secs)

def test_trim_silences_survives_silent_video_with_real_ffmpeg(tmp_path):
    """The exact crash: an audio-less clip used to fail with 'matches no
    streams' → RuntimeError → HTTP 500 on the whole edit."""
    src = _clip(tmp_path / "silent.mp4", secs=2.0, no_audio=True)
    out = tmp_path / "out.mp4"

    summary = video_edit.trim_silences(src, out)

    assert out.exists()
    assert summary["n_cuts"] == 0
    assert summary["trimmed_duration"] == pytest.approx(2.0, abs=0.3)
    assert not video_edit._has_audio_stream(out)


def test_trim_silences_still_processes_audio_clip_with_real_ffmpeg(tmp_path):
    """Counterfactual: a clip WITH audio still goes through the real
    silence-detect + filter_complex path and keeps its audio track."""
    src = _clip(tmp_path / "talkie.mp4", secs=2.0)
    out = tmp_path / "out.mp4"

    summary = video_edit.trim_silences(src, out)

    assert out.exists()
    assert summary["n_cuts"] >= 1              # whole-clip keep at minimum
    assert video_edit._has_audio_stream(out)
    assert summary["trimmed_duration"] == pytest.approx(2.0, abs=0.4)
