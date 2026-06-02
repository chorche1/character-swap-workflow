"""The Swap image path must request OpenAI quality="high" by default.

`OPENAI_IMAGE_QUALITY` (settings.openai_image_quality, default "high") is applied
in `openai_image._generate_once` whenever the caller doesn't pass an explicit
quality — so every Swap variant renders at full detail instead of OpenAI's
auto/default. Regression guard: nothing should silently drop back to the
default quality. Hermetic: the OpenAI client + call_log.record are stubbed, so
no network/API key is needed.
"""
from __future__ import annotations

import base64
import contextlib

import pytest

from character_swap.clients import openai_image


class _Item:
    b64_json = base64.b64encode(b"PNGDATA").decode()


class _Resp:
    data = [_Item()]
    _request_id = "req_x"


class _FakeImages:
    def __init__(self):
        self.calls: list[dict] = []

    def generate(self, **kw):
        self.calls.append(kw)
        return _Resp()

    def edit(self, **kw):
        self.calls.append(kw)
        return _Resp()


class _FakeClient:
    def __init__(self):
        self.images = _FakeImages()


@pytest.fixture
def _fake(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(openai_image, "_client", lambda: client)

    @contextlib.contextmanager
    def _rec(**kw):
        yield {}
    monkeypatch.setattr(openai_image, "record", _rec)
    return client


def _last_quality(client):
    return client.images.calls[-1].get("quality", "__absent__")


def test_quality_defaults_to_high(_fake, monkeypatch):
    monkeypatch.setattr(openai_image.settings, "openai_image_quality", "high")
    openai_image._generate_once(prompt="x", phase="generate", character="c")
    assert _last_quality(_fake) == "high"


def test_config_overrides_quality(_fake, monkeypatch):
    monkeypatch.setattr(openai_image.settings, "openai_image_quality", "medium")
    openai_image._generate_once(prompt="x", phase="generate", character="c")
    assert _last_quality(_fake) == "medium"


def test_explicit_caller_quality_wins(_fake, monkeypatch):
    monkeypatch.setattr(openai_image.settings, "openai_image_quality", "high")
    openai_image._generate_once(prompt="x", phase="generate", character="c",
                                quality="low")
    assert _last_quality(_fake) == "low"


def test_empty_config_omits_quality(_fake, monkeypatch):
    # Empty env → omit the param so OpenAI applies its own default.
    monkeypatch.setattr(openai_image.settings, "openai_image_quality", "")
    openai_image._generate_once(prompt="x", phase="generate", character="c")
    assert _last_quality(_fake) == "__absent__"
