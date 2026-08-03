"""ffprobe is optional — every stream fact must survive without it.

2026-08-04, found while packaging the app for a second Mac: imageio-ffmpeg
bundles ONLY ffmpeg, so a machine without a system ffmpeg install (no
Homebrew) has no `ffprobe` at all. `_probe_fps` and `_probe_dims` returned
None there, and both callers treat None as "carry on":

  * assemble_clips pinned EVERY concat to fps_target = 30.0 regardless of
    the source clips, and
  * bar_crop_for_clip returned None for every clip, switching the automatic
    black-bar removal off machine-wide.

Neither said a word to the user — exactly the silent partial result this
project refuses. Both now fall back to parsing ffmpeg's own header output,
the same trick _probe_duration has used for duration since 2026-06-12.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from character_swap import video_edit


def _clip(dest: Path, *, size: str = "320x568", fps: int = 12,
          secs: float = 1.0, content: str | None = None) -> Path:
    """A real encoded clip. With `content` set, that smaller frame is padded
    onto the `size` canvas — i.e. a clip with baked-in black bars."""
    args = [video_edit._ffmpeg(), "-hide_banner", "-y",
            "-f", "lavfi", "-i",
            f"color=c=red:s={content or size}:d={secs}:r={fps}"]
    if content:
        cw, ch = (int(v) for v in size.split("x"))
        args += ["-vf", f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2"]
    args += ["-pix_fmt", "yuv420p", str(dest)]
    subprocess.run(args, check=True, capture_output=True)
    return dest


@pytest.fixture()
def no_ffprobe(monkeypatch):
    """Simulate a Mac with no system ffmpeg install."""
    monkeypatch.setattr(video_edit, "_ffprobe", lambda: None)


def test_probe_dims_without_ffprobe(tmp_path, no_ffprobe):
    assert video_edit._probe_dims(_clip(tmp_path / "a.mp4")) == (320, 568)


def test_probe_fps_without_ffprobe(tmp_path, no_ffprobe):
    fps = video_edit._probe_fps(_clip(tmp_path / "b.mp4", fps=24))
    assert fps is not None
    assert abs(fps - 24.0) < 0.1


def test_fps_fallback_preserves_a_non_default_rate(tmp_path, no_ffprobe):
    """The whole point: without the fallback this clip's 15 fps was silently
    replaced by assemble_clips' 30.0 default."""
    fps = video_edit._probe_fps(_clip(tmp_path / "c.mp4", fps=15))
    assert fps is not None and abs(fps - 15.0) < 0.1


def test_black_bar_fix_still_fires_without_ffprobe(tmp_path, no_ffprobe):
    """bar_crop_for_clip must still find the bars — it returned None for
    every clip when _probe_dims gave up."""
    clip = _clip(tmp_path / "barred.mp4", size="720x1280", content="720x1256")
    assert video_edit.bar_crop_for_clip(clip, "9:16") is not None


def test_fallback_fires_when_ffprobe_exists_but_answers_garbage(tmp_path, monkeypatch):
    """A broken/foreign ffprobe on PATH must not disable the feature either."""
    clip = _clip(tmp_path / "d.mp4")
    monkeypatch.setattr(video_edit, "_ffprobe", lambda: "/bin/false")
    assert video_edit._probe_dims(clip) == (320, 568)
    assert video_edit._probe_fps(clip) is not None


# --- parsing, without spawning anything -----------------------------------

_REAL_LINE = (
    "  Stream #0:0[0x1](und): Video: h264 (avc1 / 0x31637661), "
    "yuv420p(tv, bt709), 1080x1920 [SAR 1:1 DAR 9:16], 4989 kb/s, "
    "29.97 fps, 30 tbr, 15360 tbn (default)"
)


def test_dims_regex_never_reads_the_hex_fourcc_as_a_resolution(monkeypatch):
    monkeypatch.setattr(video_edit, "_video_stream_line", lambda _p: _REAL_LINE)
    assert video_edit._probe_dims_via_ffmpeg(Path("x.mp4")) == (1080, 1920)


def test_fps_regex_reads_fractional_rates(monkeypatch):
    monkeypatch.setattr(video_edit, "_video_stream_line", lambda _p: _REAL_LINE)
    assert video_edit._probe_fps_via_ffmpeg(Path("x.mp4")) == pytest.approx(29.97)


def test_audio_only_input_yields_none_not_a_crash(monkeypatch):
    monkeypatch.setattr(video_edit, "_video_stream_line", lambda _p: None)
    assert video_edit._probe_dims_via_ffmpeg(Path("x.mp4")) is None
    assert video_edit._probe_fps_via_ffmpeg(Path("x.mp4")) is None
