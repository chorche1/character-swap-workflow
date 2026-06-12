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


def test_analyst_sees_three_chronological_frames(monkeypatch, tmp_path):
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

    early, mid, late = (tmp_path / "e.png", tmp_path / "m.png",
                        tmp_path / "l.png")
    out = reengineer.analyze_scenes(
        frames=[mid], spans=[(0.0, 3.0)],
        words=[Word(text="hi", start=0.1, end=0.3)], re_id="re_t",
        motion_frames=[[early, mid, late]],
    )
    assert out is not None
    texts = [b.get("text", "") for b in seen["messages"][0]["content"]]
    # All three frames, chronologically labeled.
    assert "early:" in texts and "middle:" in texts and "late:" in texts
    e_i, m_i, l_i = (texts.index(f"IMG:{p}") for p in (early, mid, late))
    assert e_i < m_i < l_i
    # The system prompt demands the action arc, not a static pose.
    assert "PHYSICAL ACTION" in seen["system"]
    flat = " ".join(seen["system"].split())
    assert "reduce a dynamic action to a static pose" in flat


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


def test_analyze_extracts_triplet_per_scene(monkeypatch, tmp_path):
    """_analyze pulls early/mid/late frames at 15/50/85% of each span and
    keeps the MID frame as the canonical scene asset."""
    extracted: list[tuple[float, str]] = []

    def fake_extract(video, at, dest):
        extracted.append((round(at, 2), Path(dest).name))
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"png")
        return Path(dest)
    monkeypatch.setattr(runner_reengineer.reengineer, "extract_frame",
                        fake_extract)
    monkeypatch.setattr(runner_reengineer.reengineer, "detect_scenes",
                        lambda *a, **kw: [(0.0, 2.0)])
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

    asyncio.run(runner_reengineer._analyze(
        "re_t", {"scene_sensitivity": "high"}, tmp_path / "src.mp4", tmp_path))

    times = sorted(t for t, _ in extracted)
    assert times == [0.3, 1.0, 1.7]                  # 15% / 50% / 85%
    mf = captured["motion_frames"]
    assert len(mf) == 1 and len(mf[0]) == 3
    assert mf[0][1].name == "scene-00.png"           # mid = canonical asset
    assert {p.name for p in mf[0]} == {"scene-00-early.png", "scene-00.png",
                                       "scene-00-late.png"}


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
