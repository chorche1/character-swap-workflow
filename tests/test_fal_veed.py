"""Tests for the fal.ai VEED Subtitle Styling client.

Mocks `fal_client` since we don't want tests to hit the live API. Verifies:
- ProviderNotConfigured raised when FAL_API_KEY is unset
- The submitted `arguments` dict has the right shape (every CaptionStyle field
  flows through to the right fal parameter)
- The new `veed-*` templates are registered with engine="veed"
- render_captions in video_edit dispatches to fal_veed when engine="veed"
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from character_swap import video_edit
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


# ---------------------------------------------------------------------------
# fal_client mock — captures the arguments passed to fal.submit so tests can
# assert on shape without doing real network IO.
# ---------------------------------------------------------------------------

class _FakeHandler:
    def __init__(self, video_url: str):
        self._video_url = video_url

    def get(self):
        return {
            "video": {"url": self._video_url, "content_type": "video/mp4"},
            "transcription": "hello world",
            "subtitle_count": 1,
            "words": [
                {"text": "hello", "start": 0.0, "end": 0.5},
                {"text": "world", "start": 0.5, "end": 1.0},
            ],
        }


class _FakeFalClient(types.ModuleType):
    """Replaces the `fal_client` module so we never touch the network."""
    def __init__(self):
        super().__init__("fal_client")
        self.upload_calls: list[str] = []
        self.submit_calls: list[tuple[str, dict]] = []
        self.fake_output_url = "https://fake.fal.media/output.mp4"

    def upload_file(self, path: str) -> str:
        self.upload_calls.append(path)
        return f"https://fake.fal.media/uploads/{Path(path).name}"

    def submit(self, endpoint: str, arguments: dict):  # noqa: A002
        self.submit_calls.append((endpoint, arguments))
        return _FakeHandler(self.fake_output_url)


@pytest.fixture
def fake_fal(monkeypatch):
    fake = _FakeFalClient()
    monkeypatch.setitem(sys.modules, "fal_client", fake)
    monkeypatch.setattr(settings, "fal_api_key", "fal_test_key", raising=False)
    return fake


@pytest.fixture
def fake_video(tmp_path: Path) -> Path:
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00\x01\x02")  # not a real mp4, ffprobe will fail (ok)
    return src


# ---------------------------------------------------------------------------
# Auth / configuration
# ---------------------------------------------------------------------------

def test_render_captions_raises_when_key_missing(monkeypatch, tmp_path):
    from character_swap.clients import fal_veed
    monkeypatch.setattr(settings, "fal_api_key", "", raising=False)
    src = tmp_path / "in.mp4"
    src.write_bytes(b"\x00")
    with pytest.raises(ProviderNotConfigured):
        fal_veed.render_captions(src, tmp_path / "out.mp4")


# ---------------------------------------------------------------------------
# Argument shape
# ---------------------------------------------------------------------------

def test_render_captions_submits_full_argument_dict(fake_fal, fake_video,
                                                     tmp_path, monkeypatch):
    """Every typed parameter must flow into the fal arguments dict."""
    from character_swap.clients import fal_veed

    # Skip the actual MP4 download by stubbing httpx.stream
    out = tmp_path / "out.mp4"

    class _NoopStream:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def raise_for_status(self): pass
        def iter_bytes(self, **k): return iter([b"data"])
    monkeypatch.setattr("character_swap.clients.fal_veed.httpx.stream",
                        lambda *a, **k: _NoopStream())

    result = fal_veed.render_captions(
        fake_video, out,
        font_name="Anton", font_size=120, font_weight="black",
        font_color="white", highlight_color="yellow",
        stroke_width=5, stroke_color="black",
        background_color="none", position="bottom",
        y_offset=240, words_per_subtitle=3, enable_animation=True,
        language="en",
    )
    assert len(fake_fal.submit_calls) == 1
    endpoint, args = fake_fal.submit_calls[0]
    assert endpoint == "fal-ai/workflow-utilities/auto-subtitle"
    assert args["video_url"].endswith("/in.mp4")
    assert args["font_name"] == "Anton"
    assert args["font_size"] == 120
    assert args["font_weight"] == "black"
    assert args["highlight_color"] == "yellow"
    assert args["stroke_width"] == 5
    assert args["position"] == "bottom"
    assert args["y_offset"] == 240
    assert args["words_per_subtitle"] == 3
    assert args["enable_animation"] is True
    assert result["output_url"] == fake_fal.fake_output_url
    assert result["n_words"] == 2
    assert out.exists()


def test_render_captions_passes_extra_params(fake_fal, fake_video, tmp_path,
                                              monkeypatch):
    """extra_params should merge into the arguments dict so future fal fields
    can land without a code change."""
    from character_swap.clients import fal_veed
    class _NoopStream:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def raise_for_status(self): pass
        def iter_bytes(self, **k): return iter([b"data"])
    monkeypatch.setattr("character_swap.clients.fal_veed.httpx.stream",
                        lambda *a, **k: _NoopStream())

    fal_veed.render_captions(
        fake_video, tmp_path / "out.mp4",
        extra_params={"future_field": "x", "another": 42},
    )
    _, args = fake_fal.submit_calls[0]
    assert args["future_field"] == "x"
    assert args["another"] == 42


# ---------------------------------------------------------------------------
# Template registration
# ---------------------------------------------------------------------------

def test_veed_templates_registered():
    for slug in ("veed-yellow", "veed-purple", "veed-center", "veed-mrbeast"):
        assert slug in video_edit.TEMPLATES, f"missing template {slug}"
        style = video_edit.TEMPLATES[slug]
        assert style.engine == "veed", f"{slug} should be engine=veed"
        assert style.veed_params, f"{slug} should carry veed_params"
        # Required fal fields must be present.
        for key in ("font_name", "font_size", "highlight_color", "position"):
            assert key in style.veed_params, f"{slug} missing {key}"


def test_veed_center_uses_center_position():
    style = video_edit.TEMPLATES["veed-center"]
    assert style.veed_params["position"] == "center"
    assert style.alignment == 5  # middle-center for the editor preview


# ---------------------------------------------------------------------------
# Dispatch via video_edit.render_captions
# ---------------------------------------------------------------------------

def test_render_captions_dispatches_to_veed(fake_fal, fake_video, tmp_path,
                                              monkeypatch):
    """When a template has engine='veed', video_edit.render_captions must
    forward to fal_veed.render_captions and pass veed_params through."""
    class _NoopStream:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def raise_for_status(self): pass
        def iter_bytes(self, **k): return iter([b"data"])
    monkeypatch.setattr("character_swap.clients.fal_veed.httpx.stream",
                        lambda *a, **k: _NoopStream())

    style = video_edit.TEMPLATES["veed-yellow"]
    out = tmp_path / "out.mp4"
    summary = video_edit.render_captions(
        fake_video, out,
        words=[],  # ignored for veed engine
        style=style,
    )
    assert len(fake_fal.submit_calls) == 1
    _, args = fake_fal.submit_calls[0]
    assert args["highlight_color"] == "yellow"
    assert args["font_name"] == "Montserrat"
    assert summary["template"] == "veed"
    assert out.exists()
