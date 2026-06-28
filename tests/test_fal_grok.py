"""Tests for routing Grok Imagine 1.5 (grok-imagine-1.5) through fal.ai.

xAI's newest Grok video model is routed to fal.ai's Grok Imagine 1.5
image-to-video endpoint (clients/fal_grok.py). These tests cover the duration
clamp + INTEGER formatting (Grok takes a plain int, not Veo's "<n>s" string),
resolution resolution, the registry entry, and that pipeline's submit/wait
dispatch sends the model to fal_grok — without hitting any network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap import pipeline, runner_media
from character_swap.clients import fal_grok, fal_veo


# --- duration clamp (integer, floored to [3,15]) --------------------------

@pytest.mark.parametrize("inp,expected", [
    (3, 3), (6, 6), (15, 15),
    (5, 5),
    (2, 3),          # below floor → 3
    (1, 3),
    (20, 15),        # above ceiling → 15
    (None, 5),       # default
    ("abc", 5),      # garbage → default
])
def test_clamp_duration(inp, expected):
    assert fal_grok.clamp_duration(inp) == expected


# --- resolution resolution ------------------------------------------------

def test_resolution_defaults_and_clamps(monkeypatch):
    monkeypatch.setattr(fal_grok.settings, "grok_fal_resolution", "720p")
    assert fal_grok._resolution() == "720p"
    monkeypatch.setattr(fal_grok.settings, "grok_fal_resolution", "480p")
    assert fal_grok._resolution() == "480p"
    monkeypatch.setattr(fal_grok.settings, "grok_fal_resolution", "1080p")
    assert fal_grok._resolution() == "1080p"
    monkeypatch.setattr(fal_grok.settings, "grok_fal_resolution", "bogus")
    assert fal_grok._resolution() == "720p"   # invalid → default


# --- registry -------------------------------------------------------------

def test_registry_grok_15_routes_to_fal():
    entry = runner_media.VIDEO_MODELS["grok-imagine-1.5"]
    assert entry["provider"] == "fal"
    assert entry["duration_options"] == [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    assert entry["duration_default"] == 5
    assert "Grok Imagine 1.5" in entry["label"]
    # price_setting must name a real Settings field (cost lookups dereference it).
    assert hasattr(pipeline.settings, entry["price_setting"])


# --- routing --------------------------------------------------------------

def test_submit_video_routes_grok_15_to_fal(monkeypatch):
    captured = {}
    monkeypatch.setattr(fal_grok, "submit_image_to_video",
                        lambda **kw: (captured.update(kw), "fal_req_grok")[1])
    # Guard: must NOT hit the Veo client.
    monkeypatch.setattr(fal_veo, "submit_image_to_video",
                        lambda **kw: pytest.fail("routed to fal Veo, not Grok"))

    rid = pipeline.submit_video(
        image=Path("/frame.png"), movement_prompt="he waves",
        character_name="X", model="grok-imagine-1.5", duration_secs=6,
        aspect_ratio="9:16",
    )
    assert rid == "fal_req_grok"
    assert captured["duration_secs"] == 6
    assert captured["prompt"] == "he waves"
    # Grok client takes no aspect_ratio/generate_audio (audio always on,
    # aspect inferred) — the dispatch must not pass them.
    assert "aspect_ratio" not in captured
    assert "generate_audio" not in captured


def test_wait_for_video_routes_grok_15_to_fal(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(fal_grok, "wait_for_video",
                        lambda **kw: (captured.update(kw), kw["dest"])[1])
    monkeypatch.setattr(fal_veo, "wait_for_video",
                        lambda **kw: pytest.fail("routed to fal Veo, not Grok"))

    dest = tmp_path / "out.mp4"
    pipeline.wait_for_video(
        job_id="fal_req_grok", character_name="X", dest=dest,
        model="grok-imagine-1.5",
    )
    assert captured["request_id"] == "fal_req_grok"
    assert captured["dest"] == dest


# --- argument shaping (duration -> int, no network) -----------------------

def test_submit_builds_fal_arguments(monkeypatch):
    """The fal `arguments` dict must use image_url + an INTEGER duration + the
    configured resolution, and must NOT send aspect_ratio/generate_audio/
    negative_prompt (Grok 1.5 i2v has none). Stub fal_client so nothing hits
    the network."""
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

    monkeypatch.setattr(fal_grok, "_client", lambda: _FakeFal)
    monkeypatch.setattr(fal_grok, "_check_account_block", lambda: None)
    monkeypatch.setattr(fal_grok.settings, "grok_fal_resolution", "720p")

    rid = fal_grok.submit_image_to_video(
        image=Path("/frame.png"), prompt="she nods", duration_secs=8,
    )
    assert rid == "rid123"
    assert captured["endpoint"] == "xai/grok-imagine-video/v1.5/image-to-video"
    args = captured["arguments"]
    assert args["image_url"] == "https://fal.media/uploaded.png"
    assert args["duration"] == 8                # plain int, not "8s"
    assert isinstance(args["duration"], int)
    assert args["resolution"] == "720p"
    assert "aspect_ratio" not in args
    assert "generate_audio" not in args
    assert "negative_prompt" not in args


def test_account_block_short_circuits_submit(monkeypatch):
    """A tripped shared fal breaker must fail the submit fast (no upload)."""
    def _boom():
        raise fal_grok.FalAccountError("fal submits paused")
    monkeypatch.setattr(fal_grok, "_check_account_block", _boom)
    with pytest.raises(fal_grok.FalAccountError):
        fal_grok.submit_image_to_video(
            image=Path("/frame.png"), prompt="x", duration_secs=5)
