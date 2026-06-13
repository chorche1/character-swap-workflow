"""Hugo 2026-06-12 ('fix those 2 things for every future generation'):

1. MOTION ACCURACY — the analyst saw ONE mid-frame per scene and reduced
   dynamic actions ('pours baking soda over the kiwis') to static poses
   ('holds kiwis'). It now gets THREE chronological frames per scene and a
   system prompt that demands the physical action arc.
2. BACKGROUND CORRECTNESS — with a replacement background, the swap-QC judge
   never saw the replacement image, so a result that KEPT the original
   scene background passed QC (re_10fe66db8b scene 1). The judge now
   receives the BACKGROUND image and fails original-background results.
   (The Director-side fix is covered in test_reengineer_director.py.)

Hermetic: anthropic client stubbed.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import reengineer, runner_reengineer, swap_qc
from character_swap.video_edit import Word


def _stub_anthropic(monkeypatch, module, payload):
    seen: dict = {}

    def fake_messages(**kw):
        seen.update(kw)
        return "RESP"
    monkeypatch.setattr(module.anthropic_client if hasattr(module, "anthropic_client")
                        else None, "messages_with_tools", fake_messages,
                        raising=False)
    return seen


def test_analyst_sees_timestamped_frame_sequence(monkeypatch, tmp_path):
    """The analyst reads each scene as a low-fps VIDEO: a chronological
    frame sequence labeled with seconds-into-the-scene timestamps."""
    from character_swap.clients import anthropic_client
    seen: dict = {}

    def fake_messages(**kw):
        seen.update(kw)
        return "RESP"
    monkeypatch.setattr(anthropic_client, "messages_with_tools", fake_messages)
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: {"scenes": [
                            {"idx": 0, "motion_prompt": "m", "speech": "",
                             "summary": "s"}]})
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p, **k: {"type": "text", "text": f"IMG:{p}"})

    seq = [(tmp_path / f"f{j}.png", j * 0.4) for j in range(5)]
    out = reengineer.analyze_scenes(
        frames=[seq[2][0]], spans=[(0.0, 2.0)],
        words=[Word(text="hi", start=0.1, end=0.3)], re_id="re_t",
        motion_frames=[seq],
    )
    assert out is not None
    texts = [b.get("text", "") for b in seen["messages"][0]["content"]]
    # Every frame present, chronological, timestamp-labeled.
    idxs = [texts.index(f"IMG:{p}") for p, _ in seq]
    assert idxs == sorted(idxs)
    assert "t=+0.0s:" in texts and "t=+1.6s:" in texts
    assert any("read it as a low-fps VIDEO" in t for t in texts)
    # The system prompt demands the action arc, not a static pose.
    assert "PHYSICAL ACTION" in seen["system"]
    flat = " ".join(seen["system"].split())
    assert "never reduced to a static pose" in flat
    assert "READ EACH SEQUENCE AS A VIDEO" in seen["system"]
    assert "between two samples" in flat     # actions hiding between frames


def test_analyst_single_frame_fallback(monkeypatch, tmp_path):
    """Without motion_frames (older callers), one frame per scene, no labels."""
    from character_swap.clients import anthropic_client
    seen: dict = {}
    monkeypatch.setattr(anthropic_client, "messages_with_tools",
                        lambda **kw: seen.update(kw) or "RESP")
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: {"scenes": [
                            {"idx": 0, "motion_prompt": "m", "speech": "",
                             "summary": "s"}]})
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p, **k: {"type": "text", "text": f"IMG:{p}"})
    out = reengineer.analyze_scenes(
        frames=[tmp_path / "m.png"], spans=[(0.0, 3.0)], words=[],
        re_id="re_t")
    assert out is not None
    texts = [b.get("text", "") for b in seen["messages"][0]["content"]]
    assert "early:" not in texts and "late:" not in texts


def _wire_analyze(monkeypatch, tmp_path, spans):
    extracted: list[tuple[float, str]] = []

    def fake_extract(video, at, dest):
        extracted.append((round(at, 2), Path(dest).name))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"png")
        return Path(dest)
    monkeypatch.setattr(runner_reengineer.reengineer, "extract_frame",
                        fake_extract)
    monkeypatch.setattr(runner_reengineer.reengineer, "detect_scenes",
                        lambda *a, **kw: spans)
    captured: dict = {}

    def fake_analyze(**kw):
        captured.update(kw)
        return None                       # → fallback_plans
    monkeypatch.setattr(runner_reengineer.reengineer, "analyze_scenes",
                        fake_analyze)
    monkeypatch.setattr(runner_reengineer, "_register_frame_as_scene",
                        lambda f: ("sc_x", f))
    words_file = tmp_path / "words.json"
    words_file.write_text('[{"text": "hi", "start": 0.1, "end": 0.3}]')
    return extracted, captured


def test_analyze_samples_dense_timestamped_sequence(monkeypatch, tmp_path):
    """_analyze samples ~2.5 fps per scene (min 3, max 8 frames), keeps the
    frame nearest the midpoint as the canonical scene-XX.png asset, and
    passes (path, offset) sequences to the analyst."""
    extracted, captured = _wire_analyze(monkeypatch, tmp_path,
                                        [(0.0, 2.0), (2.0, 9.0)])
    asyncio.run(runner_reengineer._analyze(
        "re_t", {"scene_sensitivity": "high"}, tmp_path / "src.mp4", tmp_path))

    mf = captured["motion_frames"]
    # 2.0s scene → ceil(2*2.5)=5 frames; 7.0s scene → ceil(17.5)→capped 8.
    assert [len(seq) for seq in mf] == [5, 8]
    # Offsets are chronological midpoints of equal slices.
    offs0 = [off for _, off in mf[0]]
    assert offs0 == sorted(offs0) and offs0[0] == pytest.approx(0.2)
    # Exactly one canonical asset per scene, at the slot nearest 50%.
    names0 = [p.name for p, _ in mf[0]]
    assert names0.count("scene-00.png") == 1
    assert names0[2] == "scene-00.png"              # middle of 5
    assert any(p.name == "scene-01.png" for p, _ in mf[1])
    # The canonical frames list is exactly the scene-XX.png slots.
    # (fallback_plans path → entries registered from those frames)


def test_analyze_frame_budget_scales_down_for_many_scenes(monkeypatch, tmp_path):
    """20 scenes → per-scene cap drops (90-image budget / Anthropic's
    100-images-per-request limit) instead of 20×8=160 frames."""
    spans = [(float(i), float(i) + 6.0) for i in range(20)]
    extracted, captured = _wire_analyze(monkeypatch, tmp_path, spans)
    asyncio.run(runner_reengineer._analyze(
        "re_t", {"scene_sensitivity": "high"}, tmp_path / "src.mp4", tmp_path))
    mf = captured["motion_frames"]
    assert len(mf) == 20
    assert all(len(seq) == 4 for seq in mf)          # 90 // 20 = 4
    assert sum(len(seq) for seq in mf) <= 90


def test_qc_receives_replacement_background(monkeypatch, tmp_path):
    """REGRESSION: with background_replaced + background_image, the judge
    gets the BACKGROUND block and the system text that fails kept-original
    backgrounds. Without the image, content is unchanged."""
    from character_swap.clients import anthropic_client
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "swap_qc_enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(settings), "anthropic_api_key",
                        property(lambda self: "k"), raising=False)
    seen: dict = {}
    monkeypatch.setattr(anthropic_client, "messages_with_tools",
                        lambda **kw: seen.update(kw) or "RESP")
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: {"passed": False,
                                            "reason": "wrong background",
                                            "corrective_hint": "use bg"})
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p, **k: {"type": "text", "text": f"IMG:{p}"})

    v = swap_qc.inspect_variant(
        scene_image=tmp_path / "s.png", character_image=tmp_path / "c.png",
        result_image=tmp_path / "r.png", background_replaced=True,
        background_image=tmp_path / "bg.png")
    assert v is not None and v.passed is False
    texts = [b.get("text", "") for b in seen["messages"][0]["content"]]
    assert any("BACKGROUND (the requested replacement" in t for t in texts)
    assert f"IMG:{tmp_path / 'bg.png'}" in texts
    assert "WRONG BACKGROUND" in seen["system"]

    seen.clear()
    swap_qc.inspect_variant(
        scene_image=tmp_path / "s.png", character_image=tmp_path / "c.png",
        result_image=tmp_path / "r.png", background_replaced=False)
    texts = [b.get("text", "") for b in seen["messages"][0]["content"]]
    assert not any("BACKGROUND (the requested" in t for t in texts)
