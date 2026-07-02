"""apply_timeline on audio-less videos (2026-07-02 reliability audit).

The CapCut-style timeline editor's multi-segment path unconditionally mapped
`[0:a]` — ffmpeg failed with "Stream specifier ':a' ... matches no streams"
on silent videos (Higgsfield Supercomputer clips), an input class every other
Editor primitive supports. The fix mirrors concat_videos: probe with
`_has_audio_stream` once and build a video-only filter graph when needed.
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


# ------------------------------------------------- filter-graph shape (fast)

def test_no_audio_timeline_builds_video_only_graph(tmp_path, monkeypatch):
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(video_edit, "_run",
                        lambda args, **kw: captured.setdefault("cmd", args) and "")
    monkeypatch.setattr(video_edit, "_has_audio_stream", lambda p: False)
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 1.2)

    video_edit.apply_timeline(tmp_path / "in.mp4", tmp_path / "out.mp4",
                              segments=[(0.0, 0.6), (1.0, 1.6)])

    cmd = captured["cmd"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a]" not in fc                    # no audio chain at all
    assert "a=0[outv]" in fc                    # video-only concat
    assert "[outa]" not in " ".join(cmd)
    assert "-an" in cmd                         # explicit no-audio output


def test_with_audio_timeline_keeps_audio_graph(tmp_path, monkeypatch):
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(video_edit, "_run",
                        lambda args, **kw: captured.setdefault("cmd", args) and "")
    monkeypatch.setattr(video_edit, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 1.2)

    video_edit.apply_timeline(tmp_path / "in.mp4", tmp_path / "out.mp4",
                              segments=[(0.0, 0.6), (1.0, 1.6)])

    cmd = captured["cmd"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[0:a]atrim" in fc
    assert "v=1:a=1[outv][outa]" in fc
    assert "[outa]" in cmd and "-an" not in cmd


# ------------------------------------------- real ffmpeg end-to-end (~secs)

def test_timeline_renders_silent_video_with_real_ffmpeg(tmp_path):
    """2-segment cut of an audio-less clip used to crash ffmpeg outright."""
    src = _clip(tmp_path / "silent.mp4", secs=2.0, no_audio=True)
    out = tmp_path / "out.mp4"

    info = video_edit.apply_timeline(src, out,
                                     segments=[(0.2, 0.8), (1.2, 1.8)])

    assert out.exists()
    assert info["n_segments"] == 2
    assert video_edit._probe_duration(out) == pytest.approx(1.2, abs=0.3)
    assert not video_edit._has_audio_stream(out)


def test_timeline_still_carries_audio_with_real_ffmpeg(tmp_path):
    src = _clip(tmp_path / "talkie.mp4", secs=2.0)
    out = tmp_path / "out.mp4"

    info = video_edit.apply_timeline(src, out,
                                     segments=[(0.0, 0.5), (1.0, 1.5)])

    assert out.exists()
    assert info["n_segments"] == 2
    assert video_edit._has_audio_stream(out)
    assert video_edit._probe_duration(out) == pytest.approx(1.0, abs=0.3)
