"""Reengineer pipeline — hermetic tests.

Scene detection runs against tiny ffmpeg-synthesized videos (real ffmpeg, no
network). Everything provider-shaped (Whisper, Claude, swap/video gen) is out
of scope here — covered by the fallback-plan and state-machine tests.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from character_swap import reengineer
from character_swap.runner_reengineer import _clamp_kling
from character_swap.video_edit import Word


def _make_video(dest: Path, segments: list[str], secs_each: float = 3.0) -> Path:
    """Synthesize a tiny video with one solid-color segment per entry —
    hard cuts between segments trip ffmpeg's scene detector."""
    parts = []
    for i, color in enumerate(segments):
        p = dest.parent / f"seg{i}.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-f", "lavfi",
             "-i", f"color=c={color}:s=160x284:d={secs_each}:r=12",
             "-pix_fmt", "yuv420p", str(p)],
            check=True, capture_output=True)
        parts.append(p)
    listfile = dest.parent / "concat.txt"
    listfile.write_text("".join(f"file '{p}'\n" for p in parts))
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-f", "concat", "-safe", "0",
         "-i", str(listfile), "-c", "copy", str(dest)],
        check=True, capture_output=True)
    return dest


def test_detect_scenes_finds_hard_cuts(tmp_path):
    video = _make_video(tmp_path / "v.mp4", ["red", "blue", "green"])
    spans = reengineer.detect_scenes(video)
    assert len(spans) == 3
    # Contiguous coverage from 0 to the end.
    assert spans[0][0] == 0.0
    for (a1, b1), (a2, b2) in zip(spans, spans[1:]):
        assert abs(b1 - a2) < 0.01
    assert spans[-1][1] == pytest.approx(9.0, abs=0.5)


def test_detect_scenes_subdivides_long_single_shot(tmp_path):
    video = _make_video(tmp_path / "v.mp4", ["red"], secs_each=24.0)
    spans = reengineer.detect_scenes(video)
    # 24s single shot must split into chunks within Kling's 3-15s window.
    assert len(spans) >= 2
    for a, b in spans:
        assert (b - a) <= reengineer.MAX_SCENE_SECS + 0.01


def test_extract_frame(tmp_path):
    video = _make_video(tmp_path / "v.mp4", ["red"], secs_each=2.0)
    frame = reengineer.extract_frame(video, 1.0, tmp_path / "f.png")
    assert frame.exists() and frame.stat().st_size > 0


def test_words_in_span_uses_midpoints():
    words = [Word("hello", 0.0, 1.0), Word("world", 1.0, 2.0), Word("bye", 5.0, 6.0)]
    assert reengineer.words_in_span(words, 0.0, 2.5) == "hello world"
    assert reengineer.words_in_span(words, 4.0, 7.0) == "bye"
    assert reengineer.words_in_span(words, 2.5, 4.0) == ""


def test_fallback_plans_carry_verbatim_dialogue():
    words = [Word("drink", 0.2, 0.5), Word("this", 0.5, 0.9)]
    plans = reengineer.fallback_plans([(0.0, 2.0), (2.0, 4.0)], words)
    assert len(plans) == 2
    assert 'The person says: "drink this"' in plans[0].motion_prompt
    assert plans[0].speech == "drink this"
    assert plans[1].speech == ""           # silent scene → no dialogue clause
    assert "says:" not in plans[1].motion_prompt


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(type(reengineer.settings), "output_dir",
                        property(lambda self: tmp_path), raising=False)
    state = {"re_id": "re_test1", "status": "queued", "scenes": []}
    reengineer.save_state(state)
    loaded = reengineer.load_state("re_test1")
    assert loaded == state
    assert any(s["re_id"] == "re_test1" for s in reengineer.list_states())


def test_clamp_kling_durations():
    assert _clamp_kling(1.2) == 3      # floor
    assert _clamp_kling(7.4) == 7      # rounds
    assert _clamp_kling(7.6) == 8
    assert _clamp_kling(40.0) == 15    # ceiling


def test_video_audio_override_threads_to_fal_kling(monkeypatch, tmp_path):
    """Reengineer jobs set Job.video_audio=True → submit_video must pass
    generate_audio=True to fal Kling regardless of the global setting."""
    from character_swap import pipeline
    from character_swap.clients import fal_kling

    seen = {}
    monkeypatch.setattr(fal_kling, "submit_image_to_video",
                        lambda **kw: seen.update(kw) or "req-1")
    monkeypatch.setattr(type(pipeline.settings), "kling_generate_audio",
                        property(lambda self: False), raising=False)
    img = tmp_path / "f.png"
    img.write_bytes(b"x")

    pipeline.submit_video(image=img, movement_prompt="p", character_name="X",
                          model="kling-v3", duration_secs=5, generate_audio=True)
    assert seen["generate_audio"] is True
    # None → falls back to the (False) global setting.
    pipeline.submit_video(image=img, movement_prompt="p", character_name="X",
                          model="kling-v3", duration_secs=5)
    assert seen["generate_audio"] is False


def test_kling_audio_on_by_default(monkeypatch, tmp_path):
    """2026-06-10 decision: ALL videos (Swap Step 4 included) generate with
    sound. Job.video_audio=None must resolve to generate_audio=True via the
    global default — no per-job opt-in needed."""
    from character_swap import pipeline
    from character_swap.clients import fal_kling
    from character_swap.config import Settings

    assert Settings.model_fields["kling_generate_audio"].default is True

    seen = {}
    monkeypatch.setattr(fal_kling, "submit_image_to_video",
                        lambda **kw: seen.update(kw) or "req-1")
    img = tmp_path / "f.png"
    img.write_bytes(b"x")
    # No generate_audio kwarg (a plain Swap job, video_audio=None).
    pipeline.submit_video(image=img, movement_prompt="p", character_name="X",
                          model="kling-v3", duration_secs=5)
    assert seen["generate_audio"] is True


def test_sensitivity_thresholds_mapping():
    """UI sensitivity choices map to ffmpeg scene-score thresholds; default
    SCENE_THRESHOLD is 'high' (0.12 — catches cuts between similar shots)."""
    assert reengineer.SENSITIVITY_THRESHOLDS == {
        "normal": 0.20, "high": 0.12, "max": 0.06}
    assert reengineer.SCENE_THRESHOLD == reengineer.SENSITIVITY_THRESHOLDS["high"]


def test_wallet_guard_merges_shortest_keeping_cut_boundaries(tmp_path):
    """Above max_scenes the SHORTEST neighbors merge — surviving boundaries
    stay aligned with real cuts (no even re-split)."""
    video = _make_video(tmp_path / "v.mp4", ["red", "blue", "green", "yellow"])
    spans = reengineer.detect_scenes(video, max_scenes=2)
    assert len(spans) == 2
    # Full coverage and at least one boundary on a real 3s-multiple cut.
    assert spans[0][0] == 0.0
    assert spans[-1][1] == pytest.approx(12.0, abs=0.5)
    inner = spans[0][1]
    assert any(abs(inner - cut) < 0.5 for cut in (3.0, 6.0, 9.0))


def test_build_edit_swap_prompt_outfit_modes():
    from character_swap import pipeline
    # scene mode IS the validated default prompt.
    assert pipeline.build_edit_swap_prompt("scene") == pipeline.EDIT_SWAP_PROMPT
    char = pipeline.build_edit_swap_prompt("character")
    assert "wears their own outfit from Image 2" in char
    assert "do not keep the original person's clothing from Image 1" in char
    custom = pipeline.build_edit_swap_prompt("custom", "a red hoodie")
    assert "wears: a red hoodie" in custom
    with pytest.raises(ValueError):
        pipeline.build_edit_swap_prompt("custom", "")
    with pytest.raises(ValueError):
        pipeline.build_edit_swap_prompt("nope")
