"""Backlog #28 (2026-06-12): direct_movement payload is bounded.

calls.jsonl forensics: 64% of director_movement calls (7/11) died with
RequestTooLargeError — the payload included the scene ref plus EVERY
approved variant per scene, unbounded on multi-character jobs. The prompt
is per-SCENE, so one approved variant (the actual start frame) per scene
carries the signal; a global image budget sheds variant images before
scene refs on very large runs.
"""
from __future__ import annotations

from pathlib import Path

from character_swap import prompt_director
from character_swap.clients import anthropic_client


def _wire(monkeypatch, scene_ids):
    payload = {"intent": "x", "scenes": [
        {"scene_id": sid, "prompt": f"move {sid}"} for sid in scene_ids]}
    seen: dict = {}

    def fake_call(**kw):
        seen.update(kw)
        return object()
    monkeypatch.setattr(anthropic_client, "messages_with_tools", fake_call)
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: payload)
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "image_stub", "path": str(p)})
    return seen


def _stubs(seen):
    return [b for b in seen["messages"][0]["content"]
            if b.get("type") == "image_stub"]


def test_one_approved_variant_per_scene(monkeypatch, tmp_path):
    scene_ids = ["s0", "s1"]
    seen = _wire(monkeypatch, scene_ids)
    scenes = [
        (sid, tmp_path / f"{sid}.png",
         [tmp_path / f"{sid}-v{k}.png" for k in range(4)],   # 4 approved
         "walk")
        for sid in scene_ids
    ]
    plan = prompt_director.direct_movement(scenes=scenes)
    assert plan is not None
    stubs = _stubs(seen)
    # 2 scene refs + exactly 1 approved variant each — not 4.
    assert len(stubs) == 4
    assert sum("-v0" in b["path"] for b in stubs) == 2
    assert not any("-v1" in b["path"] for b in stubs)


def test_global_budget_sheds_variants_before_scene_refs(monkeypatch, tmp_path):
    scene_ids = [f"s{i}" for i in range(70)]
    seen = _wire(monkeypatch, scene_ids)
    scenes = [(sid, tmp_path / f"{sid}.png",
               [tmp_path / f"{sid}-v0.png"], "walk") for sid in scene_ids]
    plan = prompt_director.direct_movement(scenes=scenes)
    assert plan is not None
    stubs = _stubs(seen)
    assert len(stubs) == 70                       # every scene ref kept
    assert not any("-v0" in b["path"] for b in stubs)   # variants shed
