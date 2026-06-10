"""fal.ai swap client (clients/fal_image.py) — hermetic tests, no real network.

Covers: queue submit→poll→download flow, per-model payload shaping (kontext
aspect_ratio vs seedream image_size), data-URI inputs, dispatch routing from
pipeline._dispatch_variant, and the not-configured guard.
"""
from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from character_swap import pipeline
from character_swap.clients import ProviderNotConfigured, fal_image

PNG = base64.b64encode(b"fake-png-bytes").decode()
RESULT_URI = f"data:image/png;base64,{PNG}"


class _Resp:
    def __init__(self, status_code=200, body=None, text=""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body


class _FakeClient:
    """Stands in for httpx.Client; replays canned responses and records calls."""
    calls: list[tuple[str, str, dict | None]] = []   # (method, url, json)

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, json=None, **kw):
        _FakeClient.calls.append(("POST", url, json))
        return _Resp(body={
            "request_id": "req-1",
            "status_url": "https://queue.fal.run/x/status",
            "response_url": "https://queue.fal.run/x/response",
        })

    def get(self, url, **kw):
        _FakeClient.calls.append(("GET", url, None))
        if url.endswith("/status"):
            return _Resp(body={"status": "COMPLETED"})
        return _Resp(body={"images": [{"url": RESULT_URI}]})


@pytest.fixture()
def fake_fal(monkeypatch, tmp_path):
    _FakeClient.calls = []
    monkeypatch.setattr(fal_image.httpx, "Client", _FakeClient)
    monkeypatch.setattr(type(fal_image.settings), "fal_api_key",
                        property(lambda self: "k:s"), raising=False)
    # Hermetic upload: _hosted_url normally hits fal's CDN via fal_client —
    # stub it to a deterministic per-file URL so no network is touched.
    monkeypatch.setattr(fal_image, "_hosted_url",
                        lambda p: f"https://cdn.fake/{p.name}")
    scene = tmp_path / "scene.png"; scene.write_bytes(b"scene-bytes")
    char = tmp_path / "char.jpg"; char.write_bytes(b"char-bytes")
    return scene, char


def _submit_payload():
    return next(j for m, u, j in _FakeClient.calls if m == "POST")


def test_swap_image_full_flow(fake_fal):
    scene, char = fake_fal
    out = fal_image.swap_image(model_slug="qwen-edit-swap", scene_image=scene,
                               character_image=char, prompt="swap them",
                               aspect_ratio="9:16", app_job_id="j1")
    assert out == b"fake-png-bytes"                      # decoded from data URI
    payload = _submit_payload()
    assert payload["prompt"] == "swap them"
    assert len(payload["image_urls"]) == 2
    # scene first, character second — as hosted CDN URLs (data URIs broke TLS)
    assert payload["image_urls"][0] == "https://cdn.fake/scene.png"
    assert payload["image_urls"][1] == "https://cdn.fake/char.jpg"


def test_kontext_gets_aspect_ratio(fake_fal):
    scene, char = fake_fal
    fal_image.swap_image(model_slug="kontext-max-swap", scene_image=scene,
                         character_image=char, prompt="p", aspect_ratio="9:16")
    payload = _submit_payload()
    assert payload["aspect_ratio"] == "9:16"
    assert "image_size" not in payload


def test_seedream_gets_image_size(fake_fal):
    scene, char = fake_fal
    fal_image.swap_image(model_slug="seedream-edit-swap", scene_image=scene,
                         character_image=char, prompt="p", aspect_ratio="9:16")
    payload = _submit_payload()
    assert payload["image_size"] == {"width": 1152, "height": 2048}
    assert "aspect_ratio" not in payload


def test_unknown_slug_raises(fake_fal):
    scene, char = fake_fal
    with pytest.raises(fal_image.FalError, match="Unknown fal swap model"):
        fal_image.swap_image(model_slug="nope", scene_image=scene,
                             character_image=char, prompt="p")


def test_not_configured_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(type(fal_image.settings), "fal_api_key",
                        property(lambda self: ""), raising=False)
    scene = tmp_path / "s.png"; scene.write_bytes(b"x")
    char = tmp_path / "c.png"; char.write_bytes(b"y")
    with pytest.raises(ProviderNotConfigured):
        fal_image.swap_image(model_slug="qwen-edit-swap", scene_image=scene,
                             character_image=char, prompt="p")


def test_dispatch_routes_fal_slugs(monkeypatch, tmp_path):
    seen = {}
    def fake_swap(**kw):
        seen.update(kw)
        return b"img"
    monkeypatch.setattr(fal_image, "swap_image", fake_swap)
    dest = tmp_path / "out.png"
    got = pipeline._dispatch_variant(
        model="kontext-max-swap", scene_image=Path("/s.png"),
        character_image=Path("/c.png"), character_name="X",
        prompt="PP", dest=dest, job_id="j9",
    )
    assert got == dest and dest.read_bytes() == b"img"
    assert seen["model_slug"] == "kontext-max-swap"
    assert seen["prompt"] == "PP"
    assert seen["app_job_id"] == "j9"


def test_dispatch_default_prompt_swapped_for_edit_prompt(monkeypatch, tmp_path):
    """The stock long GENERATION_PROMPT is replaced by EDIT_SWAP_PROMPT for
    instruction-edit engines; custom prompts pass through verbatim."""
    seen = {}
    monkeypatch.setattr(fal_image, "swap_image",
                        lambda **kw: seen.update(kw) or b"img")
    common = dict(model="qwen-edit-swap", scene_image=Path("/s.png"),
                  character_image=Path("/c.png"), character_name="X",
                  dest=tmp_path / "o.png", job_id=None)
    pipeline._dispatch_variant(prompt=pipeline.GENERATION_PROMPT, **common)
    assert seen["prompt"] == pipeline.EDIT_SWAP_PROMPT
    pipeline._dispatch_variant(prompt="my custom swap", **common)
    assert seen["prompt"] == "my custom swap"


def test_dispatch_unknown_model_still_raises(tmp_path):
    with pytest.raises(ValueError, match="Unknown image model"):
        pipeline._dispatch_variant(
            model="definitely-not-a-model", scene_image=Path("/s.png"),
            character_image=Path("/c.png"), character_name="X",
            prompt="p", dest=tmp_path / "o.png", job_id=None,
        )


def test_nbp_swap_payload(fake_fal):
    """The bake-off winner: nano-banana models get aspect_ratio + resolution."""
    scene, char = fake_fal
    fal_image.swap_image(model_slug="nbp-swap", scene_image=scene,
                         character_image=char, prompt="p", aspect_ratio="9:16")
    payload = _submit_payload()
    assert payload["aspect_ratio"] == "9:16"
    assert payload["resolution"] == "1K"
    assert "image_size" not in payload


def test_seedream_uses_v45():
    assert fal_image.SWAP_MODELS["seedream-edit-swap"].endswith("/v4.5/edit")


def test_winner_slugs_registered_in_picker():
    from character_swap.runner_media import IMAGE_MODELS
    for slug in ("nbp-swap", "nb2-swap", "seedream-edit-swap"):
        assert slug in IMAGE_MODELS and IMAGE_MODELS[slug]["provider"] == "fal"
    # Eliminated by the bake-off — must NOT be offered in the picker.
    for slug in ("higgsfield-swap", "qwen-edit-swap", "kontext-max-swap"):
        assert slug not in IMAGE_MODELS
