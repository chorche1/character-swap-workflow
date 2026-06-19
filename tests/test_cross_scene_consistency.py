"""Backlog #13 (2026-06-12): cross-scene appearance consistency.

Every variant passes SOLO QC, but nothing compared a character's results
ACROSS scenes — sleeves/gloves/glasses wobbled scene-to-scene within the
same final (observed on re_10fe66db8b's henley sleeves). One advisory
vision call per character now runs when the swap phase finishes; findings
land in state.consistency_warnings and render as amber chips at the gate.
Advisory only — never blocks, never fails variants.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from character_swap import runner_reengineer, swap_qc
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


def _wire_qc(monkeypatch, payload):
    from character_swap.clients import anthropic_client
    monkeypatch.setattr(settings, "swap_qc_enabled", True, raising=False)
    monkeypatch.setattr(settings, "anthropic_api_key", "k", raising=False)
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


def test_inspect_consistency_returns_issues(monkeypatch, tmp_path):
    seen = _wire_qc(monkeypatch, {"issues": [
        {"scene_id": "s3", "issue": "blue gloves appear only here"}]})
    issues = swap_qc.inspect_consistency(
        variants=[("s1", tmp_path / "a.png"), ("s3", tmp_path / "b.png")],
        character_image=tmp_path / "c.png")
    assert issues == [{"scene_id": "s3",
                       "issue": "blue gloves appear only here"}]
    stubs = [b for b in seen["messages"][0]["content"]
             if b.get("type") == "image_stub"]
    assert len(stubs) == 3                  # char ref + 2 scene variants
    assert seen["system"] == swap_qc.CONSISTENCY_SYSTEM


def test_inspect_consistency_single_scene_is_trivially_consistent(monkeypatch, tmp_path):
    _wire_qc(monkeypatch, {"issues": []})
    assert swap_qc.inspect_consistency(
        variants=[("s1", tmp_path / "a.png")],
        character_image=tmp_path / "c.png") == []


def test_inspect_consistency_unavailable_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "swap_qc_enabled", False, raising=False)
    assert swap_qc.inspect_consistency(
        variants=[("s1", tmp_path / "a.png"), ("s2", tmp_path / "b.png")],
        character_image=tmp_path / "c.png") is None


def test_consistency_warnings_per_character(monkeypatch, tmp_path):
    img1 = tmp_path / "v1.png"; img1.write_bytes(b"x")
    img2 = tmp_path / "v2.png"; img2.write_bytes(b"x")
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path=str(tmp_path / "ref.png"),
        status=CharStatus.AWAITING_APPROVAL,
        images=[
            GeneratedImage(variant_id="v1", path=str(img1), prompt="p",
                           scene_id="s1", status=VariantStatus.READY),
            GeneratedImage(variant_id="v2", path=str(img2), prompt="p",
                           scene_id="s2", status=VariantStatus.READY),
            GeneratedImage(variant_id="v3", path="/missing.png", prompt="p",
                           scene_id="s3", status=VariantStatus.FAILED),
        ])
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path="/s.png", scene_ids=["s1", "s2", "s3"],
              scene_image_paths=["/s.png"] * 3, characters={"cA": jc})

    captured: dict = {}

    def fake_inspect(**kw):
        captured.update(kw)
        return [{"scene_id": "s2", "issue": "sleeves rolled up only here"}]
    monkeypatch.setattr(runner_reengineer.swap_qc, "inspect_consistency",
                        fake_inspect)

    out = asyncio.run(runner_reengineer._consistency_warnings(job))
    assert out == {"cA": [{"scene_id": "s2",
                           "issue": "sleeves rolled up only here"}]}
    # Only READY variants with existing files were compared.
    assert [sid for sid, _ in captured["variants"]] == ["s1", "s2"]


# The amber cross-scene-consistency BANNER was removed from the gate UI at
# Hugo's request (2026-06-19, along with the end-pose / analyst-fallback /
# old-pipeline disclaimers). The backend still computes consistency_warnings
# (test above); there is just no banner to assert anymore.
