"""Video-quality chain (Hugo 2026-06-12, 'gör 1-3' from the quality audit):

1. Kling v3 routes to the PRO tier (1080p) by default, env-overridable.
2. Every local re-encode uses settings-driven quality (FFMPEG_CRF/PRESET,
   default 16/medium — was hardcoded veryfast/CRF-20); Remotion renders get
   --crf/--jpeg-quality and the render cache key includes them.
3. assemble_clips does onset-trim + interior-silence trim + scale + concat
   in ONE encode; run_editor_pipeline falls back to the legacy 3-encode
   chain if the combined pass fails.

ffmpeg-real tests reuse the synthesized-clip helper pattern from
test_leading_silence_trim.py.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from character_swap import runner_compile, video_edit
from character_swap.clients import fal_kling
from character_swap.config import settings
from character_swap.remotion_render import _hash_render_inputs


def _clip(dest: Path, *, lead_silence: float = 0.0, tone_secs: float = 2.0,
          silent: bool = False, size: str = "160x284") -> Path:
    """Color clip: [lead_silence of digital silence][440Hz tone]. With
    `silent=True` the whole clip is silence (anullsrc audio track)."""
    total = lead_silence + tone_secs
    if silent:
        args = ["ffmpeg", "-hide_banner", "-y",
                "-f", "lavfi", "-i", f"color=c=red:s={size}:d={total}:r=12",
                "-f", "lavfi", "-i",
                "anullsrc=channel_layout=stereo:sample_rate=44100",
                "-t", str(total),
                "-c:v", "libx264", "-c:a", "aac", "-shortest", str(dest)]
    else:
        af = (f"volume=0dB,adelay={int(lead_silence * 1000)}:all=1,"
              f"apad=whole_dur={total}")
        args = ["ffmpeg", "-hide_banner", "-y",
                "-f", "lavfi", "-i", f"color=c=red:s={size}:d={total}:r=12",
                "-f", "lavfi", "-i", f"sine=frequency=440:duration={tone_secs}",
                "-af", af, "-c:v", "libx264", "-c:a", "aac",
                "-shortest", str(dest)]
    subprocess.run(args, check=True, capture_output=True)
    return dest


def _clip_with_pause(tmp: Path, *, tone_secs: float, pause_secs: float) -> Path:
    """[tone][pause][tone] in one file, via concat of three synthesized parts."""
    t1 = _clip(tmp / "part1.mp4", tone_secs=tone_secs)
    gap = _clip(tmp / "gap.mp4", tone_secs=pause_secs, silent=True)
    t2 = _clip(tmp / "part2.mp4", tone_secs=tone_secs)
    listfile = tmp / "concat.txt"
    listfile.write_text(f"file '{t1}'\nfile '{gap}'\nfile '{t2}'\n")
    out = tmp / "paused.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-y", "-f", "concat",
                    "-safe", "0", "-i", str(listfile),
                    "-c:v", "libx264", "-c:a", "aac", str(out)],
                   check=True, capture_output=True)
    return out


def _dims(p: Path) -> tuple[int, int]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
        check=True, capture_output=True, text=True).stdout.strip()
    w, h = out.split(",")[:2]
    return int(w), int(h)


# ---------------------------------------------------------------- 1. Kling tier

def test_kling_v3_defaults_to_pro_tier(monkeypatch):
    """PRO = the 1080p tier; standard (720p) was being upscaled 1.5× into
    fake-1080p finals. Env-overridable, junk values fall back to pro."""
    monkeypatch.setattr(type(settings), "kling_v3_tier",
                        property(lambda self: "pro"), raising=False)
    assert fal_kling._endpoint() == "fal-ai/kling-video/v3/pro/image-to-video"
    monkeypatch.setattr(type(settings), "kling_v3_tier",
                        property(lambda self: "standard"), raising=False)
    assert fal_kling._endpoint() == "fal-ai/kling-video/v3/standard/image-to-video"
    monkeypatch.setattr(type(settings), "kling_v3_tier",
                        property(lambda self: "ultra-mega"), raising=False)
    assert fal_kling._endpoint() == "fal-ai/kling-video/v3/pro/image-to-video"


def test_kling_submit_sends_talking_head_negative_prompt(monkeypatch, tmp_path):
    """Research 2026-06-12: a 5-8-term talking-head negative_prompt is sent
    with every Kling submit; empty setting → field omitted (fal's own
    default applies)."""

    class _Handler:
        request_id = "req-1"

    class _Fal:
        def upload_file(self, p):
            return "https://fal/u.png"

        def submit(self, endpoint, arguments):
            seen.update(arguments)
            return _Handler()

    seen: dict = {}
    monkeypatch.setattr(fal_kling, "_client", lambda: _Fal())
    img = tmp_path / "f.png"
    img.write_bytes(b"x")

    fal_kling.submit_image_to_video(image=img, prompt="p", duration_secs=5)
    assert seen["negative_prompt"] == settings.kling_negative_prompt
    assert "morphing face" in seen["negative_prompt"]
    assert "frozen lips" in seen["negative_prompt"]

    seen.clear()
    monkeypatch.setattr(type(settings), "kling_negative_prompt",
                        property(lambda self: "  "), raising=False)
    fal_kling.submit_image_to_video(image=img, prompt="p", duration_secs=5)
    assert "negative_prompt" not in seen        # fal default applies


def test_kling_v3_tier_default_is_pro():
    # Assert the FIELD default, not the live instance — Hugo's .env may
    # legitimately override KLING_V3_TIER, and the suite must stay green.
    from character_swap.config import Settings
    assert Settings.model_fields["kling_v3_tier"].default == "pro"


# ----------------------------------------------------------- 2. encode quality

def test_enc_v_is_settings_driven(monkeypatch):
    # Field defaults (env-independent):
    from character_swap.config import Settings
    assert Settings.model_fields["ffmpeg_crf"].default == 16
    assert Settings.model_fields["ffmpeg_preset"].default == "medium"
    # _enc_v reads the live settings at call time:
    monkeypatch.setattr(type(settings), "ffmpeg_crf",
                        property(lambda self: 18), raising=False)
    monkeypatch.setattr(type(settings), "ffmpeg_preset",
                        property(lambda self: "slow"), raising=False)
    assert video_edit._enc_v() == ["-c:v", "libx264",
                                   "-preset", "slow", "-crf", "18"]


def test_no_hardcoded_low_quality_encodes_left():
    """The old hardcoded triplet must never come back — every local encode
    goes through _enc_v()."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "character_swap" / "video_edit.py").read_text()
    assert '"-preset", "veryfast", "-crf", "20"' not in src


def test_remotion_cache_key_includes_encode_quality(monkeypatch, tmp_path):
    """A quality bump must MISS the render cache — same inputs, different
    crf/jpeg-quality, different key."""
    f = tmp_path / "in.mp4"
    f.write_bytes(b"x")
    props = {"videoSrc": "local://in.mp4", "words": []}
    monkeypatch.setattr(type(settings), "remotion_crf",
                        property(lambda self: 16), raising=False)
    k1 = _hash_render_inputs("CapCutPurplePill", props, f)
    monkeypatch.setattr(type(settings), "remotion_crf",
                        property(lambda self: 23), raising=False)
    k2 = _hash_render_inputs("CapCutPurplePill", props, f)
    assert k1 != k2


def test_clip_gain_math():
    """Backlog #10: static per-clip gain — target-seeking, true-peak-capped,
    clamped to ±12 dB."""
    g = video_edit._clip_gain_db
    assert g(-20.0, -8.0, -14.0) == 6.0          # plain boost to target
    assert g(-20.0, -2.0, -14.0) == 1.0          # capped by -1 dBTP ceiling
    assert g(-8.0, -1.0, -14.0) == -6.0          # attenuation, no tp cap
    assert g(-40.0, -30.0, -14.0) == 12.0        # clamped boost
    assert g(-2.0, -0.1, -30.0) == -12.0         # clamped attenuation


def test_measure_clip_loudness_on_real_tone(tmp_path):
    clip = _clip(tmp_path / "tone.mp4", tone_secs=3.0)
    measured = video_edit._measure_clip_loudness(clip)
    assert measured is not None
    loudness, true_peak = measured
    assert -60.0 < loudness < -1.0
    assert true_peak <= 0.5


def test_measure_clip_loudness_silent_clip_returns_none(tmp_path):
    clip = _clip(tmp_path / "sil.mp4", tone_secs=2.0, silent=True)
    assert video_edit._measure_clip_loudness(clip) is None


def test_assemble_equalizes_clip_loudness(tmp_path, monkeypatch):
    """Two clips ~12 dB apart must land near the shared target after
    assemble (backlog #10: -20 LUFS finals with 3 dB jumps between scenes).
    The gain rides inside the single encode — no extra generation."""
    quiet = tmp_path / "quiet.mp4"
    loud = tmp_path / "loud.mp4"
    _clip(loud, tone_secs=3.0)
    subprocess.run(  # same tone, attenuated 12 dB
        ["ffmpeg", "-hide_banner", "-y", "-i", str(loud),
         "-af", "volume=-12dB", "-c:v", "copy", "-c:a", "aac", str(quiet)],
        check=True, capture_output=True)
    monkeypatch.setattr(settings, "loudnorm_enabled", True, raising=False)
    monkeypatch.setattr(settings, "loudnorm_target_lufs", -14.0, raising=False)

    out = tmp_path / "out.mp4"
    video_edit.assemble_clips([quiet, loud], out,
                              enable_interior_trim=False)
    measured = video_edit._measure_clip_loudness(out)
    assert measured is not None
    # Both halves pulled toward -14 → integrated lands well inside ±4 LU
    # (raw concat of -14 and -26 LUFS halves would integrate near -16.5
    # and, more importantly, keep the 12 dB step between halves).
    assert -18.0 < measured[0] < -10.0


def test_assemble_loudnorm_disabled_keeps_audio_untouched(tmp_path, monkeypatch):
    quiet = tmp_path / "q.mp4"
    _clip(quiet, tone_secs=2.0)
    sub = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-i", str(quiet),
         "-af", "volume=-18dB", "-c:v", "copy", "-c:a", "aac",
         str(tmp_path / "q2.mp4")], check=True, capture_output=True)
    assert sub.returncode == 0
    monkeypatch.setattr(settings, "loudnorm_enabled", False, raising=False)
    calls = []
    real_measure = video_edit._measure_clip_loudness
    monkeypatch.setattr(video_edit, "_measure_clip_loudness",
                        lambda p: calls.append(p) or real_measure(p))

    video_edit.assemble_clips([tmp_path / "q2.mp4"], tmp_path / "out.mp4",
                              enable_interior_trim=False)
    assert calls == []                  # opt-out: no analysis, no gain


def test_assemble_clips_raises_on_probe_failure(tmp_path, monkeypatch):
    """REGRESSION (review 2026-06-12): a failed duration probe (0.0 sentinel)
    must RAISE — engaging run_editor_pipeline's legacy fallback — never
    silently trim the clip to a single frame."""
    clip = _clip(tmp_path / "a.mp4", tone_secs=1.0)
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 0.0)
    with pytest.raises(RuntimeError, match="could not probe duration"):
        video_edit.assemble_clips([clip], tmp_path / "out.mp4")


def test_assemble_clips_drops_pre_onset_click_sliver(tmp_path, monkeypatch):
    """REGRESSION (review 2026-06-12): a sub-50ms head click followed by a
    long leading pause must NOT survive as the output's first segment — the
    keep ending before the onset is dropped and the next keep is clamped to
    the onset."""
    clip = _clip(tmp_path / "a.mp4", tone_secs=2.5)
    # Crafted analysis: click at 0.0-0.04, silence (0.04, 0.54), tone after.
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 2.5)
    monkeypatch.setattr(video_edit, "_has_audio_stream", lambda p: True)
    monkeypatch.setattr(video_edit, "_detect_silences",
                        lambda p, db, d: [(0.04, 0.54)])
    captured: dict = {}

    def fake_run(cmd):
        for i, a in enumerate(cmd):
            if a == "-filter_complex":
                captured["fc"] = cmd[i + 1]
        Path(cmd[-1]).write_bytes(b"mp4")
        return ""
    monkeypatch.setattr(video_edit, "_run", fake_run)

    video_edit.assemble_clips([clip], tmp_path / "out.mp4",
                              enable_interior_trim=True,
                              min_silence_secs=0.30, pad_secs=0.03)
    fc = captured["fc"]
    assert "trim=start=0.540" in fc          # starts at the true onset
    assert "end=0.070" not in fc             # click+pad sliver is gone


# ------------------------------------------------- 3. single-encode assemble

def test_assemble_clips_one_encode_trims_and_scales(tmp_path):
    """Two clips with 1s dead air each → onset cut per clip, output on the
    1080x1920 canvas, all in one pass."""
    a = _clip(tmp_path / "a.mp4", lead_silence=1.0, tone_secs=2.0)
    b = _clip(tmp_path / "b.mp4", lead_silence=1.0, tone_secs=2.0)
    out = tmp_path / "out.mp4"
    info = video_edit.assemble_clips([a, b], out, aspect_ratio="9:16",
                                     enable_interior_trim=True,
                                     min_silence_secs=0.30, pad_secs=0.03)
    assert out.exists()
    assert _dims(out) == (1080, 1920)
    # ~2s of tone per clip survives; 1s lead cut from each.
    assert video_edit._probe_duration(out) == pytest.approx(4.0, abs=0.8)
    assert info["removed_secs"] == pytest.approx(2.0, abs=0.8)


def test_assemble_clips_collapses_interior_pause_only_when_enabled(tmp_path):
    """A 1.5s mid-clip pause is cut with the toggle ON and kept with it OFF
    (onset trim runs in both cases)."""
    clip = _clip_with_pause(tmp_path, tone_secs=1.5, pause_secs=1.5)
    on = tmp_path / "on.mp4"
    off = tmp_path / "off.mp4"
    video_edit.assemble_clips([clip], on, enable_interior_trim=True,
                              min_silence_secs=0.4, pad_secs=0.03)
    video_edit.assemble_clips([clip], off, enable_interior_trim=False)
    d_on = video_edit._probe_duration(on)
    d_off = video_edit._probe_duration(off)
    assert d_off == pytest.approx(4.5, abs=0.5)      # pause kept
    assert d_off - d_on == pytest.approx(1.5, abs=0.5)  # pause cut when ON


def test_run_editor_pipeline_falls_back_to_legacy_chain(tmp_path, monkeypatch):
    """If the combined pass blows up, the proven 3-step chain still builds
    the video — reliability first."""
    a = _clip(tmp_path / "a.mp4", lead_silence=1.0, tone_secs=2.0)
    edit_dir = tmp_path / "ed"
    edit_dir.mkdir()

    def boom(*args, **kw):
        raise RuntimeError("combined pass failed")
    monkeypatch.setattr(video_edit, "assemble_clips", boom)
    legacy_calls: list[str] = []
    real_trim = video_edit.trim_leading_silence
    real_concat = video_edit.concat_videos
    monkeypatch.setattr(video_edit, "trim_leading_silence",
                        lambda *a, **kw: (legacy_calls.append("trim"),
                                          real_trim(*a, **kw))[1])
    monkeypatch.setattr(video_edit, "concat_videos",
                        lambda *a, **kw: (legacy_calls.append("concat"),
                                          real_concat(*a, **kw))[1])
    monkeypatch.setattr(
        video_edit, "transcribe_words",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no whisper")))

    result = asyncio.run(runner_compile.run_editor_pipeline(
        [a], edit_id="ed_t", edit_dir=edit_dir,
        template="minimal", overrides=None,
        enable_trim=False, enable_captions=False,
        enable_wpm_normalize=False, target_wpm=190.0,
        threshold_db=-30.0, min_silence_secs=0.30, pad_secs=0.03,
        voice_id=None, enable_transcribe=False,
    ))
    assert "trim" in legacy_calls and "concat" in legacy_calls
    assert result.final.exists()
