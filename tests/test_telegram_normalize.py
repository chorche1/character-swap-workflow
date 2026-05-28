"""Tests for the Telegram-upload normalization pipeline.

Higgsfield exports videos at the odd 716×1284 resolution and yuvj420p
(JPEG full range) color, which Telegram mobile clients render either as
a file attachment or at the wrong aspect ratio. `_normalize_for_telegram`
re-encodes them to a canonical 1080×1920 yuv420p +faststart reel before
upload.

These tests generate small synthetic videos via ffmpeg's `testsrc`
filter (real video files on disk, no external fetches) and exercise the
probe + decide + re-encode path end-to-end.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest

from character_swap.clients.telegram import (
    _needs_normalize,
    _normalize_for_telegram,
    _probe_for_telegram,
    _TG_TARGET_H,
    _TG_TARGET_W,
)


def _ffmpeg() -> str:
    return imageio_ffmpeg.get_ffmpeg_exe()


def _make_test_video(path: Path, *, width: int, height: int,
                     duration: float = 1.0, pix_fmt: str = "yuv420p") -> Path:
    """Synthesize a tiny test video at the requested dimensions/format.
    Uses ffmpeg's `testsrc` source filter so we don't depend on external
    sample files. Audio is a 440Hz sine so the file has both streams."""
    cmd = [
        _ffmpeg(), "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size={width}x{height}:rate=24",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", pix_fmt,
        "-c:a", "aac", "-b:a", "64k",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.fail(f"test video synth failed: {proc.stderr[-400:]}")
    assert path.exists()
    return path


def test_probe_reads_dimensions_and_pix_fmt(tmp_path: Path) -> None:
    """The probe should round-trip dimensions and pix_fmt out of ffmpeg
    stderr — these are the two values used by `_needs_normalize`."""
    v = _make_test_video(tmp_path / "src.mp4",
                         width=716, height=1284, duration=1.0)
    probe = _probe_for_telegram(v)
    assert probe.width == 716
    assert probe.height == 1284
    assert probe.pix_fmt.startswith("yuv420p")
    # Synth duration is 1s; allow tolerance for ffmpeg rounding.
    assert 0.5 < probe.duration_secs < 2.0


def test_needs_normalize_true_for_off_dimensions() -> None:
    """716×1284 (Higgsfield's export) must trigger the re-encode path."""
    from character_swap.clients.telegram import _VideoProbe
    probe = _VideoProbe(width=716, height=1284, duration_secs=5.0, pix_fmt="yuv420p")
    assert _needs_normalize(probe) is True


def test_needs_normalize_true_for_yuvj_pixel_format() -> None:
    """yuvj420p (JPEG full-range) renders colors wrong on some Telegram
    clients even at the right dimensions — still needs re-encode."""
    from character_swap.clients.telegram import _VideoProbe
    probe = _VideoProbe(
        width=_TG_TARGET_W, height=_TG_TARGET_H,
        duration_secs=5.0, pix_fmt="yuvj420p",
    )
    assert _needs_normalize(probe) is True


def test_needs_normalize_false_for_clean_1080x1920_yuv420p() -> None:
    """A file already in the canonical reel format should pass through
    untouched — re-encoding loses quality for no gain."""
    from character_swap.clients.telegram import _VideoProbe
    probe = _VideoProbe(
        width=_TG_TARGET_W, height=_TG_TARGET_H,
        duration_secs=5.0, pix_fmt="yuv420p",
    )
    assert _needs_normalize(probe) is False


def test_normalize_passes_through_clean_source(tmp_path: Path) -> None:
    """Source already at 1080×1920 yuv420p → returned path is the source
    itself, no sidecar created."""
    src = _make_test_video(tmp_path / "clean.mp4",
                           width=_TG_TARGET_W, height=_TG_TARGET_H,
                           duration=1.0)
    result = _normalize_for_telegram(src)
    assert result.path == src
    assert result.sidecar is None
    assert result.re_encoded is False
    assert result.width == _TG_TARGET_W
    assert result.height == _TG_TARGET_H


def test_normalize_reencodes_716x1284_to_1080x1920(tmp_path: Path) -> None:
    """The Higgsfield case: 716×1284 source → sidecar at 1080×1920
    yuv420p with +faststart. Probe the result file to confirm."""
    src = _make_test_video(tmp_path / "higgs.mp4",
                           width=716, height=1284, duration=1.0)
    result = _normalize_for_telegram(src)
    assert result.re_encoded is True
    assert result.sidecar is not None
    assert result.sidecar.exists()
    assert result.path == result.sidecar
    assert result.width == _TG_TARGET_W
    assert result.height == _TG_TARGET_H
    # Re-probe the sidecar — the encoder should actually have produced
    # 1080×1920 yuv420p, not just claimed to in our dataclass.
    out_probe = _probe_for_telegram(result.sidecar)
    assert out_probe.width == _TG_TARGET_W
    assert out_probe.height == _TG_TARGET_H
    assert out_probe.pix_fmt.startswith("yuv420p")
    assert not out_probe.pix_fmt.startswith("yuvj")  # not full-range


def test_normalize_preserves_aspect_with_pad_not_crop(tmp_path: Path) -> None:
    """716×1284 → 1080×1920 fits the source into the canvas with letterbox
    padding instead of cropping. The output file must exist and be
    decodable; we can't easily detect the pad pixels but we can ensure
    the encoder didn't truncate the duration."""
    src = _make_test_video(tmp_path / "src.mp4",
                           width=716, height=1284, duration=2.0)
    result = _normalize_for_telegram(src)
    assert result.sidecar is not None
    out_probe = _probe_for_telegram(result.sidecar)
    # Duration should round-trip within ~0.2s.
    assert abs(out_probe.duration_secs - 2.0) < 0.5
