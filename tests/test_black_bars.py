"""Automatic black-bar removal in the final builds (Hugo 2026-07-24).

Every clip entering a final is cropdetect-scanned; clips carrying baked-in
letterbox/pillarbox bars — or slightly off-aspect frames the pipeline would
otherwise pad — get the LARGEST target-aspect window inside their real
content box (minimal zoom by construction), capped at
BLACKBAR_MAX_CROP_FRAC (5%) of the frame per axis. Beyond the cap, on any
detection failure, or with BLACKBAR_FIX=0 the clip keeps the legacy
scale+pad behavior — the fix never blocks a build.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from character_swap import video_edit


# ------------------------------------------------------------ geometry (pure)

def test_clean_on_aspect_clip_is_noop():
    """Exact 9:16 clip with a full-frame content box → no crop at all."""
    assert video_edit.compute_bar_crop(
        1080, 1920, (1080, 1920, 0, 0)) is None
    assert video_edit.compute_bar_crop(
        720, 1280, (720, 1280, 0, 0)) is None


def test_thin_pillarbox_cropped_within_cap():
    """Thin side bars (content 1040 wide of 1080): crop lands inside the
    content box, target-aspect, centered — and every value is even."""
    crop = video_edit.compute_bar_crop(1080, 1920, (1040, 1920, 20, 0))
    assert crop is not None
    w, h, x, y = crop
    assert all(v % 2 == 0 for v in crop)
    # Window fits inside the content box…
    assert x >= 20 and x + w <= 20 + 1040
    assert y >= 0 and y + h <= 1920
    # …at (near-)target aspect, within the 5% cap on both axes.
    assert abs(w / h - 9 / 16) < 0.01
    assert (1080 - w) / 1080 <= 0.05
    assert (1920 - h) / 1920 <= 0.05


def test_thin_letterbox_cropped_within_cap():
    crop = video_edit.compute_bar_crop(1080, 1920, (1080, 1840, 0, 40))
    assert crop is not None
    w, h, x, y = crop
    assert y >= 40 and y + h <= 40 + 1840
    assert abs(w / h - 9 / 16) < 0.01
    assert (1080 - w) / 1080 <= 0.05 and (1920 - h) / 1920 <= 0.05


def test_big_bars_hit_the_cap_and_keep_legacy_pad():
    """Content 960 of 1080 wide → eliminating needs an 11% crop → None
    (Hugo's 5% cap: keep today's padded behavior for that clip)."""
    assert video_edit.compute_bar_crop(
        1080, 1920, (960, 1920, 60, 0)) is None


def test_cap_is_tunable():
    """The same big-bar clip IS cropped when the cap allows it."""
    crop = video_edit.compute_bar_crop(
        1080, 1920, (960, 1920, 60, 0), max_crop_frac=0.15)
    assert crop is not None
    w, h, x, y = crop
    assert x >= 60 and x + w <= 60 + 960


def test_off_aspect_frame_without_bars_is_cropped_not_padded():
    """A clip slightly wider than 9:16 with NO baked bars used to get
    pipeline pad bars — now it gets a tiny crop instead."""
    crop = video_edit.compute_bar_crop(1080, 1900, (1080, 1900, 0, 0))
    assert crop is not None
    w, h, _x, _y = crop
    assert w < 1080 and (1080 - w) / 1080 <= 0.05
    assert abs(w / h - 9 / 16) < 0.01


def test_out_of_frame_box_is_clamped():
    """cropdetect output must never produce an out-of-frame crop."""
    crop = video_edit.compute_bar_crop(1080, 1920, (2000, 3000, -50, -50))
    assert crop is None or (
        crop[2] >= 0 and crop[3] >= 0
        and crop[2] + crop[0] <= 1080 and crop[3] + crop[1] <= 1920)


def test_degenerate_box_is_noop():
    assert video_edit.compute_bar_crop(1080, 1920, (0, 0, 0, 0)) is None
    assert video_edit.compute_bar_crop(0, 0, (100, 100, 0, 0)) is None


def test_hairline_noise_box_hits_dead_band():
    """A ≤2px-per-side 'bar' is cropdetect/encode noise on a clean clip —
    not worth an extra encode generation (dead-band < 4px per axis)."""
    assert video_edit.compute_bar_crop(
        1080, 1920, (1078, 1918, 2, 2)) is None


# --------------------------------------------------- filter-graph shape (fast)

def test_concat_prepends_crop_and_covers_canvas(tmp_path, monkeypatch):
    """A clip with a detected bar crop gets crop → scale-to-COVER → crop
    (never pad); a clean clip keeps the legacy scale+pad chain."""
    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        captured["cmd"] = args
        return ""

    monkeypatch.setattr(video_edit, "_run", fake_run)
    monkeypatch.setattr(video_edit, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(
        video_edit, "bar_crop_for_clip",
        lambda p, ar="9:16": (706, 1256, 6, 12) if "barred" in p.name else None)

    video_edit.concat_videos(
        [tmp_path / "barred.mp4", tmp_path / "clean.mp4"],
        tmp_path / "out.mp4")

    fc = captured["cmd"][captured["cmd"].index("-filter_complex") + 1]
    assert ("[0:v]crop=706:1256:6:12,"
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920") in fc
    assert ("[1:v]scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920") in fc


def test_concat_single_input_gets_vf_crop(tmp_path, monkeypatch):
    captured: dict[str, list[str]] = {}

    def fake_run(args, **kwargs):
        captured["cmd"] = args
        return ""

    monkeypatch.setattr(video_edit, "_run", fake_run)
    monkeypatch.setattr(video_edit, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(video_edit, "bar_crop_for_clip",
                        lambda p, ar="9:16": (706, 1256, 6, 12))

    video_edit.concat_videos([tmp_path / "one.mp4"], tmp_path / "out.mp4")
    cmd = captured["cmd"]
    assert cmd[cmd.index("-vf") + 1] == "crop=706:1256:6:12"


def test_kill_switch_disables_detection(tmp_path, monkeypatch):
    """BLACKBAR_FIX=0 → bar_crop_for_clip is a no-op before any probing."""
    monkeypatch.setattr(video_edit.settings, "blackbar_fix", False)
    monkeypatch.setattr(video_edit, "_probe_dims",
                        lambda p: pytest.fail("probed despite kill switch"))
    assert video_edit.bar_crop_for_clip(tmp_path / "x.mp4") is None


def test_detection_failure_never_raises(tmp_path):
    """Nonexistent file → probe fails → None (the fix never blocks)."""
    assert video_edit.bar_crop_for_clip(tmp_path / "missing.mp4") is None


# ------------------------------------------- Editor single-clip flows (fast)

def test_maybe_crop_black_bars_helper(tmp_path, monkeypatch):
    """The shared Editor Step-0b helper: crop found → cropped path; no crop
    or any failure → the input passes through untouched."""
    import asyncio
    from character_swap import api

    src = tmp_path / "src.mp4"

    monkeypatch.setattr(video_edit, "bar_crop_for_clip",
                        lambda p, ar="9:16": (706, 1256, 6, 12))
    called: dict = {}

    def fake_crop(inp, outp, crop, *, job_id=None):
        called["crop"] = crop
        return outp

    monkeypatch.setattr(video_edit, "crop_video", fake_crop)
    out = asyncio.run(api._maybe_crop_black_bars(src, tmp_path, "ed_test"))
    assert out == tmp_path / "00b-crop.mp4"
    assert called["crop"] == (706, 1256, 6, 12)

    # No bars → passthrough, no encode.
    monkeypatch.setattr(video_edit, "bar_crop_for_clip",
                        lambda p, ar="9:16": None)
    assert asyncio.run(
        api._maybe_crop_black_bars(src, tmp_path, "ed_test")) == src

    # Encode failure → passthrough (never blocks a render).
    monkeypatch.setattr(video_edit, "bar_crop_for_clip",
                        lambda p, ar="9:16": (706, 1256, 6, 12))

    def boom(*a, **k):
        raise RuntimeError("ffmpeg died")

    monkeypatch.setattr(video_edit, "crop_video", boom)
    assert asyncio.run(
        api._maybe_crop_black_bars(src, tmp_path, "ed_test")) == src


def test_all_single_clip_editor_endpoints_run_step_0b():
    """auto_edit AND the sibling one-shot trim_silences / captions endpoints
    (all producing pushable finals that never scale to a canvas) must run
    the shared bar-removal step."""
    import inspect
    from character_swap import api

    for fn in (api.editor_auto_edit, api.editor_trim_silences,
               api.editor_captions):
        assert "_maybe_crop_black_bars" in inspect.getsource(fn), fn.__name__


# ------------------------------------------------- real ffmpeg (integration)

def _barred_clip(dest: Path, *, secs: float = 1.0,
                 content: str = "720x1256", canvas: str = "720x1280",
                 color: str = "red") -> Path:
    """Solid-color content letterboxed/pillarboxed onto a black canvas —
    a clip with baked-in bars, like a model rendering letterboxed output."""
    cw, ch = (int(v) for v in canvas.split("x"))
    args = ["ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s={content}:d={secs}:r=12",
            "-f", "lavfi", "-i",
            f"sine=frequency=440:sample_rate=44100:duration={secs}",
            "-vf", f"pad={cw}:{ch}:(ow-iw)/2:(oh-ih)/2",
            "-map", "0:v", "-map", "1:a",
            "-pix_fmt", "yuv420p", "-shortest", str(dest)]
    subprocess.run(args, check=True, capture_output=True)
    return dest


def test_detect_content_box_finds_letterbox(tmp_path):
    clip = _barred_clip(tmp_path / "barred.mp4")
    box = video_edit.detect_content_box(clip)
    assert box is not None
    w, h, x, y = box
    # Content is 720x1256 at y=12; allow ±4px for encode/round=2 wobble.
    assert abs(w - 720) <= 4 and abs(h - 1256) <= 4
    assert abs(x - 0) <= 4 and abs(y - 12) <= 4


def test_assemble_clips_output_has_no_bars(tmp_path):
    """End-to-end: two thin-letterboxed clips through assemble_clips → the
    1080x1920 output's own content box is (near) the full frame."""
    a = _barred_clip(tmp_path / "a.mp4", color="red")
    b = _barred_clip(tmp_path / "b.mp4", color="blue")
    out = tmp_path / "out.mp4"
    video_edit.assemble_clips([a, b], out, enable_interior_trim=False)
    assert out.exists()
    dims = video_edit._probe_dims(out)
    assert dims == (1080, 1920)
    box = video_edit.detect_content_box(out)
    assert box is not None
    assert box[0] >= 1080 - 8 and box[1] >= 1920 - 8


def test_big_bars_survive_the_cap_end_to_end(tmp_path):
    """A clip whose bars need >5% crop keeps them (cap respected)."""
    clip = _barred_clip(tmp_path / "big.mp4",
                        content="720x1080", canvas="720x1280")
    out = tmp_path / "out.mp4"
    video_edit.concat_videos([clip, clip], out)
    box = video_edit.detect_content_box(out)
    assert box is not None
    # Output still letterboxed: content height well below the canvas.
    assert box[1] <= 1920 * 0.9
