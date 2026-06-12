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
    assert ('The person says, in a casual conversational tone with a '
            'natural American accent: "drink this"') in plans[0].motion_prompt
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
    """Whole seconds, always rounded UP (Hugo 2026-06-12), inside [3, 15]."""
    assert _clamp_kling(1.2) == 3      # floor
    assert _clamp_kling(7.4) == 8      # rounds UP — never down
    assert _clamp_kling(7.6) == 8
    assert _clamp_kling(8.0) == 8      # exact integers stay put
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


def test_background_prompt_mode():
    """Background mode: Image 3 environment + relight directives; non-background
    output stays byte-identical to the bake-off-validated prompt."""
    from character_swap import pipeline
    p = pipeline.build_edit_swap_prompt("scene", background=True)
    assert "Image 3 is the NEW ENVIRONMENT" in p
    assert "relight" in p.lower()
    assert "do not keep Image 1's background" in p
    # The kept-objects guarantee (products must survive the background swap).
    assert "every object, product and prop the person touches" in p
    # character outfit + background combine without contradiction
    pc = pipeline.build_edit_swap_prompt("character", background=True)
    assert "wears their own outfit from Image 2" in pc
    # default stays the validated prompt
    assert pipeline.build_edit_swap_prompt("scene") == pipeline.EDIT_SWAP_PROMPT


def test_fal_payload_includes_background_as_third_image(monkeypatch, tmp_path):
    from character_swap.clients import fal_image
    monkeypatch.setattr(fal_image, "_hosted_url", lambda p: f"https://cdn.fake/{p.name}")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    char = tmp_path / "c.png"; char.write_bytes(b"c")
    bg = tmp_path / "bg.png"; bg.write_bytes(b"b")
    payload = fal_image._payload_for(
        "fal-ai/nano-banana-pro/edit", prompt="p", scene_image=scene,
        character_image=char, aspect_ratio="9:16", extra_reference_image=bg)
    assert payload["image_urls"] == [
        "https://cdn.fake/s.png", "https://cdn.fake/c.png", "https://cdn.fake/bg.png"]
    # without background: two images, unchanged behavior
    p2 = fal_image._payload_for(
        "fal-ai/nano-banana-pro/edit", prompt="p", scene_image=scene,
        character_image=char, aspect_ratio="9:16")
    assert len(p2["image_urls"]) == 2


def test_dispatch_threads_extra_reference_to_fal(monkeypatch, tmp_path):
    from character_swap import pipeline
    from character_swap.clients import fal_image
    seen = {}
    monkeypatch.setattr(fal_image, "swap_image",
                        lambda **kw: seen.update(kw) or b"img")
    bg = tmp_path / "bg.png"; bg.write_bytes(b"b")
    pipeline._dispatch_variant(
        model="nbp-swap", scene_image=Path("/s.png"),
        character_image=Path("/c.png"), character_name="X",
        prompt="custom", dest=tmp_path / "o.png", job_id=None,
        extra_reference_image=bg)
    assert seen["extra_reference_image"] == bg


def test_with_accent_appends_once():
    from character_swap.runner_reengineer import _with_accent
    p = _with_accent("The person pours oil on their foot.")
    assert "American accent" in p
    # idempotent — never doubles up (agent prompts already carry it)
    assert _with_accent(p) == p


def test_fallback_plans_speak_american():
    words = [Word("drink", 0.2, 0.5), Word("this", 0.5, 0.9)]
    plans = reengineer.fallback_plans([(0.0, 2.0)], words)
    assert "American accent" in plans[0].motion_prompt


def test_analyst_system_demands_american_accent():
    assert "American accent" in reengineer.REENGINEER_ANALYST_SYSTEM


def test_resolve_source_filename():
    from character_swap.models import CharacterAsset, CharacterImage
    ch = CharacterAsset(
        char_id="c1", name="N", filename="primary.png",
        primary_image_id="im_a",
        images=[CharacterImage(image_id="im_a", filename="primary.png"),
                CharacterImage(image_id="im_b", filename="outfit2.png")])
    assert ch.resolve_source_filename(None) == "primary.png"
    assert ch.resolve_source_filename("im_b") == "outfit2.png"   # outfit pick
    assert ch.resolve_source_filename("im_gone") == "primary.png"  # stale id


# --- analyst failure is never silent (backlog #23, 2026-06-12) ---------------


def _analyze_with(monkeypatch, tmp_path, analyze_result):
    """Run runner_reengineer._analyze on a tiny real video with the
    provider-shaped pieces (Whisper, Claude analyst, store) stubbed."""
    import asyncio
    from character_swap import runner_reengineer

    video = _make_video(tmp_path / "v.mp4", ["red", "blue"])
    run_dir = tmp_path / "run"
    (run_dir / "scenes").mkdir(parents=True)
    monkeypatch.setattr(runner_reengineer.video_edit, "transcribe_words",
                        lambda src, job_id=None: [])
    monkeypatch.setattr(runner_reengineer.reengineer, "analyze_scenes",
                        lambda **kw: analyze_result(**kw)
                        if callable(analyze_result) else analyze_result)
    monkeypatch.setattr(runner_reengineer, "_register_frame_as_scene",
                        lambda f: (f"sc_{f.stem}", f))
    state = {"re_id": "re_t", "scene_sensitivity": "high"}
    entries = asyncio.run(
        runner_reengineer._analyze("re_t", state, video, run_dir))
    return state, entries


def test_analyst_failure_sets_fallback_flag(tmp_path, monkeypatch):
    """analyze_scenes -> None used to be invisible: generic prompts appeared
    at the gate with no hint. The state must carry analyst_fallback=True so
    the UI renders the review-before-animating banner."""
    state, entries = _analyze_with(monkeypatch, tmp_path, None)
    assert state.get("analyst_fallback") is True
    assert entries and all(e["motion_prompt"] for e in entries)


def test_analyst_success_does_not_set_fallback_flag(tmp_path, monkeypatch):
    def fake_analyze(*, frames, spans, words, re_id, motion_frames):
        return [reengineer.ScenePlan(idx=i, motion_prompt=f"plan {i}",
                                     speech="", summary="")
                for i in range(len(frames))]
    state, entries = _analyze_with(monkeypatch, tmp_path, fake_analyze)
    assert "analyst_fallback" not in state
    assert [e["motion_prompt"] for e in entries] == \
        [f"plan {i}" for i in range(len(entries))]


# --- recovered runs drop stale error banners (backlog #36, 2026-06-12) -------


def test_update_clears_error_on_non_failed_status(tmp_path, monkeypatch):
    from character_swap import runner_reengineer
    from character_swap.config import settings
    monkeypatch.setattr(settings, "output_dir", tmp_path, raising=False)
    (tmp_path / "reengineer" / "re_e").mkdir(parents=True)

    runner_reengineer._update("re_e", status="failed", error="kling exploded")
    assert reengineer.load_state("re_e")["error"] == "kling exploded"

    # Recovery: a later non-failed transition clears the stale banner.
    runner_reengineer._update("re_e", status="done")
    state = reengineer.load_state("re_e")
    assert state["status"] == "done"
    assert state["error"] is None

    # Non-status updates never touch the error field.
    runner_reengineer._update("re_e", status="failed", error="boom")
    runner_reengineer._update("re_e", finals_stale=True)
    assert reengineer.load_state("re_e")["error"] == "boom"


def test_pre_v2_finals_rebuild_hint_in_ui():
    """Backlog #38: finals built by the old assemble (no edit_id — the
    duration-cap era with truncated dialogue) must surface a rebuild hint."""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(
        encoding="utf-8")
    assert "f.status === 'done' && !f.edit_id" in html
    assert "Bygg ihop igen" in html
