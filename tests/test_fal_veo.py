"""Tests for routing Veo 3.1 Fast (veo-3.1-fast) through fal.ai.

The Gemini path only carries Veo 3 / Veo 3 Fast; `veo-3.1-fast` is routed to
fal.ai's Veo 3.1 Fast image-to-video endpoint (clients/fal_veo.py). These tests
cover the duration clamp + "<n>s" formatting, resolution/aspect resolution, the
registry entry, and that pipeline's submit/wait dispatch sends the model to
fal_veo (not the Gemini Veo client), without hitting any network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap import pipeline, runner_media
from character_swap.clients import fal_veo, google_genai


# --- duration clamp (nearest of 4/6/8) ------------------------------------

@pytest.mark.parametrize("inp,expected", [
    (4, 4), (6, 6), (8, 8),
    (5, 4),          # 5 → nearest is 4 (tie-break low)
    (7, 6),          # 7 → nearest is 6 (tie-break low)
    (3, 4),          # below → 4
    (12, 8),         # above → 8
    (None, 8),       # default
    ("abc", 8),      # garbage → default
])
def test_clamp_duration(inp, expected):
    assert fal_veo.clamp_duration(inp) == expected


# --- resolution + aspect resolution ---------------------------------------

def test_resolution_defaults_and_clamps(monkeypatch):
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "1080p")
    assert fal_veo._resolution() == "1080p"
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "720p")
    assert fal_veo._resolution() == "720p"
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "bogus")
    assert fal_veo._resolution() == "1080p"   # invalid → default


def test_aspect_ratio_passthrough_else_auto():
    assert fal_veo._aspect_ratio("9:16") == "9:16"
    assert fal_veo._aspect_ratio("16:9") == "16:9"
    assert fal_veo._aspect_ratio("1:1") == "auto"   # unsupported → auto
    assert fal_veo._aspect_ratio(None) == "auto"


# --- registry -------------------------------------------------------------

def test_registry_veo_31_fast_routes_to_fal():
    entry = runner_media.VIDEO_MODELS["veo-3.1-fast"]
    assert entry["provider"] == "fal"
    assert entry["duration_options"] == [4, 6, 8]
    assert entry["duration_default"] == 8
    assert "Veo 3.1 Fast" in entry["label"]


# --- routing --------------------------------------------------------------

def test_submit_video_routes_veo_31_fast_to_fal(monkeypatch):
    captured = {}
    monkeypatch.setattr(fal_veo, "submit_image_to_video",
                        lambda **kw: (captured.update(kw), "fal_req_veo")[1])
    # Guard: must NOT hit the Gemini Veo client.
    monkeypatch.setattr(google_genai, "submit_veo",
                        lambda **kw: pytest.fail("routed to Gemini Veo, not fal"))

    rid = pipeline.submit_video(
        image=Path("/frame.png"), movement_prompt="he waves",
        character_name="X", model="veo-3.1-fast", duration_secs=6,
        aspect_ratio="9:16",
    )
    assert rid == "fal_req_veo"
    assert captured["duration_secs"] == 6
    assert captured["aspect_ratio"] == "9:16"
    assert captured["prompt"] == "he waves"
    assert captured["generate_audio"] is True   # default ON for Veo


def test_wait_for_video_routes_veo_31_fast_to_fal(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(fal_veo, "wait_for_video",
                        lambda **kw: (captured.update(kw), kw["dest"])[1])
    monkeypatch.setattr(google_genai, "wait_for_veo",
                        lambda **kw: pytest.fail("routed to Gemini Veo, not fal"))

    dest = tmp_path / "out.mp4"
    pipeline.wait_for_video(
        job_id="fal_req_veo", character_name="X", dest=dest, model="veo-3.1-fast",
    )
    assert captured["request_id"] == "fal_req_veo"
    assert captured["dest"] == dest


# --- argument shaping (duration -> "<n>s", no network) --------------------

def test_submit_builds_fal_arguments(monkeypatch):
    """The fal `arguments` dict must use image_url + duration '<n>s' + the
    configured resolution. Stub fal_client so nothing hits the network."""
    captured = {}

    class _Handler:
        request_id = "rid123"

    class _FakeFal:
        Completed = object
        @staticmethod
        def upload_file(p):
            return "https://fal.media/uploaded.png"
        @staticmethod
        def submit(endpoint, arguments):
            captured["endpoint"] = endpoint
            captured["arguments"] = arguments
            return _Handler()

    monkeypatch.setattr(fal_veo, "_client", lambda: _FakeFal)
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "1080p")

    rid = fal_veo.submit_image_to_video(
        image=Path("/frame.png"), prompt="she nods",
        duration_secs=8, aspect_ratio="9:16", generate_audio=True,
    )
    assert rid == "rid123"
    assert captured["endpoint"] == "fal-ai/veo3.1/fast/image-to-video"
    args = captured["arguments"]
    assert args["image_url"] == "https://fal.media/uploaded.png"
    assert args["duration"] == "8s"          # enum string with the "s" suffix
    assert args["resolution"] == "1080p"
    assert args["aspect_ratio"] == "9:16"
    assert args["generate_audio"] is True
