"""concat_videos on MIXED audio/no-audio clip sets must terminate (2026-07-02).

The mixed branch synthesizes silent audio via anullsrc — an INFINITE lavfi
stream. Without an atrim bound the concat filter's audio leg never reaches
EOF and ffmpeg encodes silence forever while the output file grows until the
disk fills (reproduced live during the 2026-07-01 reliability audit; the
sibling assemble_clips had the atrim fix, concat_videos didn't). These tests
lock (1) the bounded filter-graph shape, (2) real-ffmpeg termination on a
mixed set, and (3) the global _run timeout that backstops every ffmpeg call.
"""
from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from character_swap import video_edit


def _clip(dest: Path, *, secs: float = 1.0, no_audio: bool = False) -> Path:
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

def test_mixed_concat_bounds_synth_audio_with_atrim(tmp_path, monkeypatch):
    """The anullsrc chain for an audio-less clip must carry an atrim bound
    to that clip's duration — an unbounded [N:a] chain is the hang."""
    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        captured["cmd"] = args
        return ""

    monkeypatch.setattr(video_edit, "_run", fake_run)
    monkeypatch.setattr(video_edit, "_has_audio_stream",
                        lambda p: "withaudio" in p.name)
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 1.5)

    video_edit.concat_videos(
        [tmp_path / "withaudio.mp4", tmp_path / "silent.mp4"],
        tmp_path / "out.mp4")

    cmd = captured["cmd"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    # Exactly one anullsrc input, and its chain is trimmed to the clip length.
    assert " ".join(cmd).count("anullsrc") == 1
    assert "[2:a]atrim=start=0:end=1.500" in fc
    # The real-audio clip's chain stays untrimmed.
    assert "[0:a]aresample=44100" in fc


def test_mixed_concat_refuses_unprobeable_silent_clip(tmp_path, monkeypatch):
    """If the silent clip's duration can't be probed we must fail loudly,
    never emit an unbounded anullsrc."""
    monkeypatch.setattr(video_edit, "_run", lambda args, **kw: "")
    monkeypatch.setattr(video_edit, "_has_audio_stream",
                        lambda p: "withaudio" in p.name)
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 0.0)

    with pytest.raises(RuntimeError, match="could not probe duration"):
        video_edit.concat_videos(
            [tmp_path / "withaudio.mp4", tmp_path / "silent.mp4"],
            tmp_path / "out.mp4")


# ------------------------------------------- real ffmpeg end-to-end (~secs)

def test_mixed_concat_terminates_with_real_ffmpeg(tmp_path):
    """1s a/v clip + 1s video-only clip: before the fix this encoded silent
    audio forever. Run in a worker thread so a regression FAILS instead of
    hanging the suite."""
    a = _clip(tmp_path / "a.mp4", secs=1.0)
    b = _clip(tmp_path / "b.mp4", secs=1.0, no_audio=True)
    out = tmp_path / "out.mp4"

    result: dict[str, BaseException | Path] = {}

    def work():
        try:
            result["ok"] = video_edit.concat_videos([a, b], out)
        except BaseException as exc:  # surfaced below
            result["err"] = exc

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout=120)
    assert not t.is_alive(), "concat_videos did not terminate on a mixed set"
    if "err" in result:
        raise AssertionError(f"concat_videos failed: {result['err']}")

    assert out.exists()
    assert video_edit._has_audio_stream(out)
    assert video_edit._probe_duration(out) == pytest.approx(2.0, abs=0.5)


# --------------------------------------------------- the _run global timeout

def test_run_kills_wedged_ffmpeg_and_fails_loudly():
    """An encode reading an unbounded lavfi source must be killed at the
    timeout and raise, not hang forever."""
    with pytest.raises(RuntimeError, match="timed out"):
        video_edit._run(
            ["ffmpeg", "-hide_banner", "-y",
             "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
             "-f", "null", "-"],
            timeout_secs=2)
