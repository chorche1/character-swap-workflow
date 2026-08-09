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


def test_resolution_downgrades_above_720p_for_sub_8s(monkeypatch):
    """Regression: fal's Veo 3.1 Fast rejects 1080p/4k at 4s/6s with
    "value_error, 1080p resolution is only supported with a duration of 8s",
    so EVERY sub-8s clip failed. _resolution(dur) must downgrade 1080p/4k to
    720p for non-8s clips while keeping the configured res at 8s."""
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "1080p")
    assert fal_veo._resolution(8) == "1080p"   # 8s keeps configured res
    assert fal_veo._resolution(6) == "720p"    # sub-8s → forced down
    assert fal_veo._resolution(4) == "720p"
    assert fal_veo._resolution(None) == "1080p"  # legacy/no-duration unchanged
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "4k")
    assert fal_veo._resolution(8) == "4k"
    assert fal_veo._resolution(6) == "720p"
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "720p")
    assert fal_veo._resolution(4) == "720p"    # already 720p → stays


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
    # Veo 3.1 Fast honors a per-scene end pose via the first-last-frame endpoint.
    assert entry.get("end_frame") is True
    assert runner_media.supports_end_frame("veo-3.1-fast") is True


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
    assert captured["end_image"] is None         # no end frame on this call


def test_submit_video_forwards_end_image_to_fal_veo(monkeypatch):
    """A veo-3.1-fast scene with a 🎯 end pose must hand the end frame to the
    fal Veo client (which routes it to the first-last-frame endpoint)."""
    captured = {}
    monkeypatch.setattr(fal_veo, "submit_image_to_video",
                        lambda **kw: (captured.update(kw), "fal_req_veo")[1])
    monkeypatch.setattr(google_genai, "submit_veo",
                        lambda **kw: pytest.fail("routed to Gemini Veo, not fal"))

    rid = pipeline.submit_video(
        image=Path("/frame.png"), movement_prompt="he turns",
        character_name="X", model="veo-3.1-fast", duration_secs=8,
        aspect_ratio="9:16", end_image=Path("/end.png"),
    )
    assert rid == "fal_req_veo"
    assert captured["end_image"] == Path("/end.png")   # end frame forwarded


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
    # No end frame → no first/last frame fields (those belong to the FLF endpoint).
    assert "first_frame_url" not in args
    assert "last_frame_url" not in args


def test_submit_builds_fal_arguments_with_end_frame(monkeypatch):
    """When an end frame is set, submit must route to the SEPARATE
    first-last-frame endpoint with first_frame_url + last_frame_url (and NO
    image_url), uploading both frames."""
    captured = {}
    uploads = []

    class _Handler:
        request_id = "ridFLF"

    class _FakeFal:
        Completed = object
        @staticmethod
        def upload_file(p):
            uploads.append(p)
            # Distinct URLs so we can assert which frame went where.
            return ("https://fal.media/end.png" if "end" in str(p)
                    else "https://fal.media/start.png")
        @staticmethod
        def submit(endpoint, arguments):
            captured["endpoint"] = endpoint
            captured["arguments"] = arguments
            return _Handler()

    monkeypatch.setattr(fal_veo, "_client", lambda: _FakeFal)
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "1080p")

    rid = fal_veo.submit_image_to_video(
        image=Path("/start.png"), prompt="he turns to face the camera",
        duration_secs=8, aspect_ratio="9:16", generate_audio=True,
        end_image=Path("/end.png"),
    )
    assert rid == "ridFLF"
    assert captured["endpoint"] == "fal-ai/veo3.1/fast/first-last-frame-to-video"
    args = captured["arguments"]
    assert args["first_frame_url"] == "https://fal.media/start.png"
    assert args["last_frame_url"] == "https://fal.media/end.png"
    assert "image_url" not in args            # FLF endpoint has no image_url
    assert args["duration"] == "8s"
    assert args["resolution"] == "1080p"
    assert args["aspect_ratio"] == "9:16"
    assert args["generate_audio"] is True
    assert len(uploads) == 2                  # both start + end uploaded


def test_submit_downgrades_resolution_for_short_clip(monkeypatch):
    """At the submit boundary: a sub-8s clip with VEO_FAL_RESOLUTION=1080p must
    send resolution '720p' so fal accepts it (regression for the 10/20 failed
    Veo clips in re_d2c6425f15)."""
    captured = {}

    class _Handler:
        request_id = "rid6"

    class _FakeFal:
        Completed = object
        @staticmethod
        def upload_file(p):
            return "https://fal.media/u.png"
        @staticmethod
        def submit(endpoint, arguments):
            captured["arguments"] = arguments
            return _Handler()

    monkeypatch.setattr(fal_veo, "_client", lambda: _FakeFal)
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "1080p")

    fal_veo.submit_image_to_video(
        image=Path("/frame.png"), prompt="x", duration_secs=5,  # clamps to 4s
        aspect_ratio="9:16", generate_audio=True,
    )
    args = captured["arguments"]
    assert args["duration"] == "4s"
    assert args["resolution"] == "720p"      # downgraded from 1080p for sub-8s


def test_submit_downgrades_resolution_for_short_clip_on_end_frame(monkeypatch):
    """The sub-8s 1080p→720p downgrade must ALSO apply on the END-FRAME (FLF)
    path — the resolution arg is shared across both endpoints. Without this
    lock, a refactor that moved `_resolution(dur)` into only the i2v branch
    would send raw 1080p to the first-last-frame endpoint and reintroduce the
    `re_d2c6425f15` "1080p only at duration=8s" failure for every short
    end-frame clip (the common Reengineer case is scene-length-dictated)."""
    captured = {}

    class _Handler:
        request_id = "ridFLF6"

    class _FakeFal:
        Completed = object
        @staticmethod
        def upload_file(p):
            return ("https://fal.media/end.png" if "end" in str(p)
                    else "https://fal.media/start.png")
        @staticmethod
        def submit(endpoint, arguments):
            captured["endpoint"] = endpoint
            captured["arguments"] = arguments
            return _Handler()

    monkeypatch.setattr(fal_veo, "_client", lambda: _FakeFal)
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    monkeypatch.setattr(fal_veo.settings, "veo_fal_resolution", "1080p")

    fal_veo.submit_image_to_video(
        image=Path("/start.png"), prompt="x", duration_secs=6,  # sub-8s
        aspect_ratio="9:16", generate_audio=True, end_image=Path("/end.png"),
    )
    args = captured["arguments"]
    assert captured["endpoint"] == "fal-ai/veo3.1/fast/first-last-frame-to-video"
    assert args["duration"] == "6s"
    assert args["resolution"] == "720p"      # downgraded on the FLF path too
    assert args["first_frame_url"] == "https://fal.media/start.png"
    assert args["last_frame_url"] == "https://fal.media/end.png"


# --- fal's own moderation dial (safety_tolerance) --------------------------
#
# Hugo 2026-08-04, after the 2026-08-03 failure wave (33 of 43 failed clips
# were content-policy rejections, almost all Spanish dialogue that passed
# verbatim in English): send fal's LEAST strict setting instead of letting fal
# apply its default "4". Google's own Veo filter still applies underneath.

def _fake_fal(captured):
    class _Handler:
        request_id = "rid-st"

    class _FakeFal:
        Completed = object
        @staticmethod
        def upload_file(p):
            return f"https://fal.media/{Path(p).name}"
        @staticmethod
        def submit(endpoint, arguments):
            captured["endpoint"] = endpoint
            captured["arguments"] = arguments
            return _Handler()
    return _FakeFal


def test_safety_tolerance_defaults_to_least_strict():
    assert fal_veo.settings.veo_safety_tolerance == "6"
    assert fal_veo._safety_tolerance() == "6"


@pytest.mark.parametrize("configured,expected", [
    ("6", "6"), ("1", "1"), ("4", "4"),
    (" 5 ", "5"),      # whitespace tolerated
    ("7", "6"),        # out of range → least strict, never fal's stricter default
    ("bogus", "6"),
    ("", "6"),
    (None, "6"),
])
def test_safety_tolerance_clamps(monkeypatch, configured, expected):
    monkeypatch.setattr(fal_veo.settings, "veo_safety_tolerance", configured)
    assert fal_veo._safety_tolerance() == expected


def test_submit_sends_safety_tolerance(monkeypatch):
    captured = {}
    monkeypatch.setattr(fal_veo, "_client", lambda: _fake_fal(captured))
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    fal_veo.submit_image_to_video(
        image=Path("/frame.png"), prompt="she nods", duration_secs=8,
        aspect_ratio="9:16", generate_audio=True,
    )
    assert captured["arguments"]["safety_tolerance"] == "6"
    # auto_fix stays OFF: it rewrites the PROMPT, which carries the exact line
    # the character must speak.
    assert "auto_fix" not in captured["arguments"]


def test_submit_sends_safety_tolerance_on_end_frame_endpoint(monkeypatch):
    captured = {}
    monkeypatch.setattr(fal_veo, "_client", lambda: _fake_fal(captured))
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    fal_veo.submit_image_to_video(
        image=Path("/start.png"), prompt="she turns", duration_secs=8,
        aspect_ratio="9:16", generate_audio=True, end_image=Path("/end.png"),
    )
    assert captured["endpoint"] == "fal-ai/veo3.1/fast/first-last-frame-to-video"
    assert captured["arguments"]["safety_tolerance"] == "6"


def test_env_can_still_tighten_it(monkeypatch):
    captured = {}
    monkeypatch.setattr(fal_veo, "_client", lambda: _fake_fal(captured))
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    monkeypatch.setattr(fal_veo.settings, "veo_safety_tolerance", "2")
    fal_veo.submit_image_to_video(
        image=Path("/frame.png"), prompt="x", duration_secs=8,
        aspect_ratio="9:16", generate_audio=True,
    )
    assert captured["arguments"]["safety_tolerance"] == "2"


# --- always-on negative clause -------------------------------------------
# Hugo 2026-08-10: "automatiskt lägg till 'No cuts, no music, no animal
# sounds.' i varje veo prompt" — resolved to the NEGATIVE prompt (his choice
# after we discussed that a positive-prompt directive carries continuity
# instructions better; he wanted it in the negative field only). Worded as
# "camera cuts, …" because a negative prompt lists what to avoid (a leading
# "no" is a double negative) and bare "cuts" reads as skin wounds.

def test_always_negative_leads_the_configured_value(monkeypatch):
    monkeypatch.setattr(fal_veo.settings, "veo_negative_prompt",
                        "subtitles, watermark")
    assert fal_veo._negative_prompt() == (
        "camera cuts, background music, animal sounds, subtitles, watermark")


def test_always_negative_survives_an_empty_env_value(monkeypatch):
    """Regression: VEO_NEGATIVE_PROMPT= used to omit the field entirely and let
    fal apply its own default. The three always-on terms must still be sent."""
    for empty in ("", "   ", None, ","):
        monkeypatch.setattr(fal_veo.settings, "veo_negative_prompt", empty)
        assert fal_veo._negative_prompt() == fal_veo._ALWAYS_NEGATIVE


def test_always_negative_is_idempotent(monkeypatch):
    """A configured value that already carries the clause must not double it."""
    monkeypatch.setattr(
        fal_veo.settings, "veo_negative_prompt",
        "camera cuts, background music, animal sounds, subtitles")
    out = fal_veo._negative_prompt()
    assert out.lower().count("animal sounds") == 1
    assert out == "camera cuts, background music, animal sounds, subtitles"


def test_default_config_still_carries_veos_own_terms(monkeypatch):
    """The always-on clause must PREPEND to Veo's own set, never replace it —
    the burned-in-subtitle terms are why VEO_NEGATIVE_PROMPT exists."""
    out = fal_veo._negative_prompt()
    assert out.startswith("camera cuts, background music, animal sounds, ")
    assert "subtitles" in out


def test_submit_sends_the_always_negative_clause(monkeypatch):
    captured = {}
    monkeypatch.setattr(fal_veo, "_client", lambda: _fake_fal(captured))
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    fal_veo.submit_image_to_video(
        image=Path("/frame.png"), prompt="she nods", duration_secs=8,
        aspect_ratio="9:16", generate_audio=True,
    )
    neg = captured["arguments"]["negative_prompt"]
    assert neg.startswith("camera cuts, background music, animal sounds")
    # The POSITIVE prompt is untouched — nothing the dialogue extractor, the
    # language nets or video QC's expected speech reads may change here.
    assert captured["arguments"]["prompt"] == "she nods"


def test_submit_sends_the_always_negative_clause_on_end_frame_endpoint(monkeypatch):
    """The 🎯 end-pose path submits to a DIFFERENT fal endpoint — it must carry
    the clause too."""
    captured = {}
    monkeypatch.setattr(fal_veo, "_client", lambda: _fake_fal(captured))
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    fal_veo.submit_image_to_video(
        image=Path("/start.png"), prompt="she turns", duration_secs=8,
        aspect_ratio="9:16", generate_audio=True, end_image=Path("/end.png"),
    )
    assert captured["endpoint"] == "fal-ai/veo3.1/fast/first-last-frame-to-video"
    assert captured["arguments"]["negative_prompt"].startswith(
        "camera cuts, background music, animal sounds")


def test_submit_sends_it_even_with_env_cleared(monkeypatch):
    captured = {}
    monkeypatch.setattr(fal_veo, "_client", lambda: _fake_fal(captured))
    monkeypatch.setattr(fal_veo, "_check_account_block", lambda: None)
    monkeypatch.setattr(fal_veo.settings, "veo_negative_prompt", "")
    fal_veo.submit_image_to_video(
        image=Path("/frame.png"), prompt="x", duration_secs=8,
        aspect_ratio="9:16", generate_audio=True,
    )
    assert captured["arguments"]["negative_prompt"] == fal_veo._ALWAYS_NEGATIVE


def test_only_veo_is_affected(monkeypatch):
    """Kling's negative set is tuned for Kling's own failure modes — Hugo asked
    for Veo, so kling_negative_prompt must be untouched."""
    from character_swap.config import settings as cfg
    assert "animal sounds" not in (cfg.kling_negative_prompt or "").lower()
