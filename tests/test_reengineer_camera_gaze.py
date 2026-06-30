"""Reengineer gaze policy (Hugo 2026-06-13): EVERY image generated in the
Reengineer flow has the person looking directly into the camera.

Three layers: (1) the Director systems are instructed to include the
sentence verbatim, (2) ensure_camera_gaze() is the code-level guarantee on
every Director-written prompt, (3) the QC judge receives camera_gaze=true
for Reengineer jobs and ENFORCES camera gaze instead of failing it as a
scene mismatch. The static templates already contain the sentence.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap import prompt_director, swap_qc
from character_swap.config import settings
from character_swap.prompt_director import (
    CAMERA_GAZE_SENTENCE,
    ensure_camera_gaze,
)


# ------------------------------------------------------------ ensure helper

def test_ensure_appends_when_missing():
    out = ensure_camera_gaze("the person holds a glass")
    assert out == "the person holds a glass " + CAMERA_GAZE_SENTENCE


def test_ensure_noop_when_already_compliant():
    p = "intro. " + CAMERA_GAZE_SENTENCE + " more."
    assert ensure_camera_gaze(p) == p


def test_ensure_old_away_anchor_does_not_count_as_compliance():
    # The OLD Director style anchored the original gaze ("NOT at camera") —
    # that phrasing must not satisfy the policy check.
    p = "eyes down on the glass, NOT at camera"
    assert CAMERA_GAZE_SENTENCE in ensure_camera_gaze(p)


# ------------------------------------------------------- system prompt policy

def _flat(s: str) -> str:
    return " ".join(s.split())


def test_reengineer_director_system_mandates_camera_gaze():
    assert _flat(CAMERA_GAZE_SENTENCE) in _flat(
        prompt_director.REENGINEER_SWAP_DIRECTOR_SYSTEM)
    assert "NOT at camera" not in prompt_director.REENGINEER_SWAP_DIRECTOR_SYSTEM


def test_scene_rewrite_system_mandates_camera_gaze():
    assert _flat(CAMERA_GAZE_SENTENCE) in _flat(
        prompt_director.SCENE_REWRITE_DIRECTOR_SYSTEM)


def test_static_templates_already_demand_camera_gaze():
    from character_swap import pipeline
    assert "directly into the camera" in pipeline.build_edit_swap_prompt("scene")
    assert "directly into the camera" in pipeline.build_gpt_id_swap_prompt("scene")


# ------------------------------------------- code-level guarantee on outputs

def _stub_director(monkeypatch, tool_payload):
    monkeypatch.setattr(prompt_director.anthropic_client,
                        "messages_with_tools", lambda **kw: object())
    monkeypatch.setattr(prompt_director.anthropic_client, "extract_tool_call",
                        lambda resp, name: tool_payload)
    monkeypatch.setattr(prompt_director.anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "text", "text": str(p)})


def test_direct_reengineer_swap_enforces_gaze(monkeypatch, tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    _stub_director(monkeypatch, {
        "intent": "i",
        "scenes": [{"scene_id": "s1", "prompt": "no gaze written here"}],
    })
    result = prompt_director.direct_reengineer_swap(scenes=[("s1", frame)])
    assert result is not None
    _, prompts, _meta = result
    assert CAMERA_GAZE_SENTENCE in prompts["s1"]
    # Ordering: gaze sentence belongs to the scene part, before the style clause.
    assert prompts["s1"].index(CAMERA_GAZE_SENTENCE) < \
        prompts["s1"].index(prompt_director.ORGANIC_STYLE_CLAUSE.strip())


def test_direct_scene_prompt_rewrite_enforces_gaze(monkeypatch, tmp_path):
    frame = tmp_path / "f.png"
    frame.write_bytes(b"x")
    _stub_director(monkeypatch, {"prompt": "swap the mug for a glass"})
    out = prompt_director.direct_scene_prompt_rewrite(
        scene_id="s1", frame_path=frame, current_prompt="p",
        change_request="byt mugg")
    assert CAMERA_GAZE_SENTENCE in out


# --------------------------------------------------------------- QC flag

def _stub_qc(monkeypatch, captured):
    from character_swap.clients import anthropic_client
    monkeypatch.setattr(settings, "swap_qc_enabled", True)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")

    def fake_messages(**kw):
        captured.update(kw)
        return object()
    monkeypatch.setattr(anthropic_client, "messages_with_tools", fake_messages)
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: {"passed": True, "reason": "",
                                            "corrective_hint": ""})
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "text", "text": str(p)})


def _qc_flags_text(captured):
    return " ".join(b.get("text", "")
                    for b in captured["messages"][0]["content"])


def test_inspect_variant_passes_camera_gaze_flag(monkeypatch, tmp_path):
    img = tmp_path / "i.png"
    img.write_bytes(b"x")
    captured = {}
    _stub_qc(monkeypatch, captured)
    v = swap_qc.inspect_variant(scene_image=img, character_image=img,
                                result_image=img, camera_gaze=True)
    assert v is not None and v.passed
    assert "camera_gaze=true" in _qc_flags_text(captured)


def test_inspect_variant_omits_flag_for_plain_swap(monkeypatch, tmp_path):
    img = tmp_path / "i.png"
    img.write_bytes(b"x")
    captured = {}
    _stub_qc(monkeypatch, captured)
    swap_qc.inspect_variant(scene_image=img, character_image=img,
                            result_image=img)
    assert "camera_gaze" not in _qc_flags_text(captured)


def test_qc_system_treats_gaze_as_informational():
    # Hugo 2026-06-30: image QC was loosened to catastrophe-only. Gaze is no
    # longer judged at all — the inspect_variant call still PASSES the
    # camera_gaze flag (locked by test_inspect_variant_passes_camera_gaze_flag),
    # but the system prompt must declare context flags informational and must
    # NOT fail an image for gaze.
    low = swap_qc.QC_SYSTEM.lower()
    assert "informational only" in low
    assert "gaze" in low
    assert "WRONG GAZE" not in swap_qc.QC_SYSTEM


def test_runner_qc_call_passes_from_reengineer():
    # The call site wires camera_gaze to the job's reengineer origin —
    # source-level guard so a refactor can't silently drop the flag.
    import inspect
    from character_swap import runner
    src = inspect.getsource(runner)
    assert "camera_gaze=job.from_reengineer" in src
