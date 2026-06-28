"""Tests for routing ByteDance Seedance 2.0 (seedance-2.0) through fal.ai.

Seedance 2.0 is routed to fal's bytedance/seedance-2.0 image-to-video endpoint
(clients/fal_seedance.py). Unlike Grok/Veo, it supports a per-scene END FRAME
(start→end interpolation), so it joins kling-v3 in END_FRAME_VIDEO_MODELS.
These tests cover the duration clamp + INTEGER arg, tier/resolution resolution
(incl. the fast-tier downgrade), aspect passthrough, the registry entry +
end-frame flag, the end-frame-capability set, and that pipeline's submit/wait
dispatch sends the model to fal_seedance with the end frame — no network.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap import api, pipeline, runner_media
from character_swap.clients import fal_seedance, fal_kling


# --- duration clamp (integer, floored to [4,15]) --------------------------

@pytest.mark.parametrize("inp,expected", [
    (4, 4), (6, 6), (15, 15),
    (5, 5),
    (3, 4),          # below floor → 4 (Seedance min is 4, not Kling's 3)
    (2, 4),
    (20, 15),        # above ceiling → 15
    (None, 5),       # default
    ("abc", 5),      # garbage → default
])
def test_clamp_duration(inp, expected):
    assert fal_seedance.clamp_duration(inp) == expected


# --- tier + endpoint resolution -------------------------------------------

def test_tier_and_endpoint(monkeypatch):
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_tier", "standard")
    assert fal_seedance._tier() == "standard"
    assert fal_seedance._endpoint() == "bytedance/seedance-2.0/image-to-video"
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_tier", "fast")
    assert fal_seedance._tier() == "fast"
    assert fal_seedance._endpoint() == "bytedance/seedance-2.0/fast/image-to-video"
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_tier", "bogus")
    assert fal_seedance._tier() == "standard"   # invalid → standard


# --- resolution resolution (+ fast-tier downgrade) ------------------------

def test_resolution_defaults_and_fast_downgrade(monkeypatch):
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_tier", "standard")
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_resolution", "720p")
    assert fal_seedance._resolution() == "720p"
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_resolution", "1080p")
    assert fal_seedance._resolution() == "1080p"   # standard keeps 1080p
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_resolution", "bogus")
    assert fal_seedance._resolution() == "720p"     # invalid → default
    # Fast tier rejects >720p → downgrade so the clip renders.
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_tier", "fast")
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_resolution", "1080p")
    assert fal_seedance._resolution() == "720p"
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_resolution", "4k")
    assert fal_seedance._resolution() == "720p"
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_resolution", "480p")
    assert fal_seedance._resolution() == "480p"     # already ≤720p → stays


def test_aspect_ratio_passthrough_else_auto():
    assert fal_seedance._aspect_ratio("9:16") == "9:16"
    assert fal_seedance._aspect_ratio("16:9") == "16:9"
    assert fal_seedance._aspect_ratio("5:7") == "auto"   # unsupported → auto
    assert fal_seedance._aspect_ratio(None) == "auto"


# --- registry + end-frame capability --------------------------------------

def test_registry_seedance_routes_to_fal():
    entry = runner_media.VIDEO_MODELS["seedance-2.0"]
    assert entry["provider"] == "fal"
    assert entry["duration_options"] == [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    assert entry["duration_default"] == 5
    assert entry["end_frame"] is True
    assert "Seedance 2.0" in entry["label"]
    assert hasattr(pipeline.settings, entry["price_setting"])


def test_end_frame_capability_set():
    # Single source of truth: only Kling 3.0 + Seedance 2.0 interpolate end frames.
    assert runner_media.END_FRAME_VIDEO_MODELS == frozenset({"kling-v3", "seedance-2.0"})
    assert runner_media.supports_end_frame("seedance-2.0") is True
    assert runner_media.supports_end_frame("kling-v3") is True
    assert runner_media.supports_end_frame("grok-imagine-1.5") is False
    assert runner_media.supports_end_frame("veo-3.1-fast") is False


def test_models_payload_surfaces_end_frame_flag():
    p = api._models_payload()
    by_slug = {m["slug"]: m for m in p["video"]}
    assert by_slug["seedance-2.0"].get("end_frame") is True
    assert by_slug["kling-v3"].get("end_frame") is True
    # Models without end-frame support must NOT carry a truthy flag.
    assert not by_slug["grok-imagine-1.5"].get("end_frame")
    assert not by_slug["veo-3.1-fast"].get("end_frame")


# --- routing (incl. end frame) --------------------------------------------

def test_submit_video_routes_seedance_to_fal_with_end_frame(monkeypatch):
    captured = {}
    monkeypatch.setattr(fal_seedance, "submit_image_to_video",
                        lambda **kw: (captured.update(kw), "fal_req_sd")[1])

    rid = pipeline.submit_video(
        image=Path("/frame.png"), movement_prompt="he turns",
        character_name="X", model="seedance-2.0", duration_secs=8,
        aspect_ratio="9:16", end_image=Path("/end.png"), generate_audio=True,
    )
    assert rid == "fal_req_sd"
    assert captured["duration_secs"] == 8
    assert captured["prompt"] == "he turns"
    assert captured["aspect_ratio"] == "9:16"
    assert captured["end_image"] == Path("/end.png")   # end frame forwarded
    assert captured["generate_audio"] is True


def test_wait_for_video_routes_seedance_to_fal(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(fal_seedance, "wait_for_video",
                        lambda **kw: (captured.update(kw), kw["dest"])[1])
    dest = tmp_path / "out.mp4"
    pipeline.wait_for_video(
        job_id="fal_req_sd", character_name="X", dest=dest, model="seedance-2.0",
    )
    assert captured["request_id"] == "fal_req_sd"
    assert captured["dest"] == dest


# --- argument shaping (integer duration + end_image_url, no network) ------

def test_submit_builds_fal_arguments_with_end_frame(monkeypatch):
    """Seedance args: image_url (start) + INTEGER duration + resolution +
    aspect_ratio + generate_audio, and end_image_url when an end frame is
    given. No negative_prompt. Stub fal_client so nothing hits the network."""
    captured = {}
    uploads = []

    class _Handler:
        request_id = "rid_sd"

    class _FakeFal:
        Completed = object
        @staticmethod
        def upload_file(p):
            uploads.append(p)
            return f"https://fal.media/{Path(p).name}"
        @staticmethod
        def submit(endpoint, arguments):
            captured["endpoint"] = endpoint
            captured["arguments"] = arguments
            return _Handler()

    monkeypatch.setattr(fal_seedance, "_client", lambda: _FakeFal)
    monkeypatch.setattr(fal_seedance, "_check_account_block", lambda: None)
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_tier", "standard")
    monkeypatch.setattr(fal_seedance.settings, "seedance_fal_resolution", "720p")

    rid = fal_seedance.submit_image_to_video(
        image=Path("/frame.png"), prompt="she nods", duration_secs=8,
        aspect_ratio="9:16", generate_audio=True, end_image=Path("/end.png"),
    )
    assert rid == "rid_sd"
    assert captured["endpoint"] == "bytedance/seedance-2.0/image-to-video"
    args = captured["arguments"]
    assert args["image_url"] == "https://fal.media/frame.png"
    assert args["duration"] == 8 and isinstance(args["duration"], int)
    assert args["resolution"] == "720p"
    assert args["aspect_ratio"] == "9:16"
    assert args["generate_audio"] is True
    assert args["end_image_url"] == "https://fal.media/end.png"   # end frame
    assert "negative_prompt" not in args


def test_submit_omits_end_frame_when_absent(monkeypatch):
    captured = {}

    class _Handler:
        request_id = "rid_sd2"

    class _FakeFal:
        Completed = object
        @staticmethod
        def upload_file(p):
            return "https://fal.media/u.png"
        @staticmethod
        def submit(endpoint, arguments):
            captured["arguments"] = arguments
            return _Handler()

    monkeypatch.setattr(fal_seedance, "_client", lambda: _FakeFal)
    monkeypatch.setattr(fal_seedance, "_check_account_block", lambda: None)

    fal_seedance.submit_image_to_video(
        image=Path("/frame.png"), prompt="x", duration_secs=6)
    assert "end_image_url" not in captured["arguments"]


def test_account_block_short_circuits_submit(monkeypatch):
    def _boom():
        raise fal_seedance.FalAccountError("fal submits paused")
    monkeypatch.setattr(fal_seedance, "_check_account_block", _boom)
    with pytest.raises(fal_seedance.FalAccountError):
        fal_seedance.submit_image_to_video(
            image=Path("/frame.png"), prompt="x", duration_secs=5)


def test_shared_account_breaker_is_the_kling_one():
    # fal_seedance must reuse fal_kling's account breaker so a balance/locked
    # error pauses ALL fal video submits, not just one model.
    assert fal_seedance.FalAccountError is fal_kling.FalAccountError
