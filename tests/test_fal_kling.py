"""Tests for routing Kling 3.0 (kling-v3) through fal.ai for 3-15s durations.

The official Kling API only generates 5s/10s, so `kling-v3` is routed to
fal.ai's Kling v3 endpoint (clients/fal_kling.py) which accepts whole-second
durations 3-15. These tests cover the duration clamp + that pipeline's
submit/wait dispatch sends kling-v3 to fal_kling (not the official Kling
client), without hitting any network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap import pipeline, runner_media
from character_swap.clients import fal_kling, kling


# --- duration clamp -------------------------------------------------------

@pytest.mark.parametrize("inp,expected", [
    (3, 3), (7, 7), (15, 15),
    (2, 3),          # below min → 3
    (20, 15),        # above max → 15
    (None, 5),       # default
    ("abc", 5),      # garbage → default
])
def test_clamp_duration(inp, expected):
    assert fal_kling.clamp_duration(inp) == expected


# --- routing --------------------------------------------------------------

def test_kling_v3_removed_from_official_kling_models():
    # The official Kling client must NOT claim kling-v3 anymore.
    assert "kling-v3" not in kling.KLING_MODELS
    assert "kling-v3" not in kling.LEGACY_ALIASES


def test_registry_kling_v3_routes_to_fal_with_full_range():
    entry = runner_media.VIDEO_MODELS["kling-v3"]
    assert entry["provider"] == "fal"
    assert entry["duration_options"] == list(range(3, 16))   # 3..15
    assert entry["label"] == "Kling 3.0"


def test_submit_video_routes_kling_v3_to_fal(monkeypatch):
    captured = {}
    def fake_submit(**kw):
        captured.update(kw)
        return "fal_req_abc"
    monkeypatch.setattr(fal_kling, "submit_image_to_video", fake_submit)
    # Guard: if it wrongly hit the official Kling client, fail loudly.
    monkeypatch.setattr(kling, "submit_kling",
                        lambda **kw: pytest.fail("routed to official Kling, not fal"))

    rid = pipeline.submit_video(
        image=Path("/frame.png"), movement_prompt="he waves",
        character_name="X", model="kling-v3", duration_secs=7,
    )
    assert rid == "fal_req_abc"
    assert captured["duration_secs"] == 7         # per-second duration passed through
    assert captured["prompt"] == "he waves"


def test_wait_for_video_routes_kling_v3_to_fal(monkeypatch, tmp_path):
    captured = {}
    def fake_wait(**kw):
        captured.update(kw)
        return kw["dest"]
    monkeypatch.setattr(fal_kling, "wait_for_video", fake_wait)
    monkeypatch.setattr(kling, "wait_for_kling",
                        lambda **kw: pytest.fail("routed to official Kling, not fal"))

    dest = tmp_path / "out.mp4"
    pipeline.wait_for_video(
        job_id="fal_req_abc", character_name="X", dest=dest, model="kling-v3",
    )
    assert captured["request_id"] == "fal_req_abc"
    assert captured["dest"] == dest
