"""Higgsfield Character Swap client — hermetic tests (no real network).

We stub the two network seams: `higgsfield._client()` (the authenticated
platform API client) and `higgsfield.httpx.Client` (the bare client used for the
presigned PUT upload + result download). The swap orchestration (upload →
reference create+cache → soul submit → poll → download) is what's under test.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap import pipeline
from character_swap.clients import higgsfield
from character_swap.config import settings


class _Resp:
    def __init__(self, status_code=200, data=None, content=b""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.content = content
        self.text = ""

    def json(self):
        return self._data


def _completed_jobset():
    return {"id": "js_1", "jobs": [
        {"status": "completed", "results": {"raw": {"url": "https://cdn/out.png"}}}]}


class _FakeAPI:
    """Stands in for the authenticated base_url client returned by _client()."""
    def __init__(self, soul_data=None):
        self.log: list[tuple] = []
        self.create_calls = 0
        self.soul_data = soul_data or _completed_jobset()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, path, json=None):
        self.log.append(("POST", path, json))
        if path == "/files/generate-upload-url":
            return _Resp(200, {"upload_url": "https://up/x",
                               "public_url": "https://cdn/x.png"})
        if path == "/v1/custom-references":
            self.create_calls += 1
            return _Resp(200, {"id": "ref_1", "status": "completed"})
        if path == "/v1/text2image/soul":
            return _Resp(200, self.soul_data)
        return _Resp(404, {})

    def get(self, path):
        self.log.append(("GET", path, None))
        if path.startswith("/v1/custom-references/"):
            return _Resp(200, {"id": "ref_1", "status": "completed"})
        if path.startswith("/v1/job-sets/"):
            return _Resp(200, _completed_jobset())
        return _Resp(404, {})


class _FakeBare:
    """Stands in for the bare httpx.Client (PUT upload + GET download)."""
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def put(self, url, content=None, headers=None):
        return _Resp(200, {})

    def get(self, url):
        return _Resp(200, {}, content=b"PNGBYTES")


@pytest.fixture
def imgs(tmp_path):
    scene = tmp_path / "scene.png"
    char = tmp_path / "char.png"
    scene.write_bytes(b"scene-bytes")
    char.write_bytes(b"char-bytes")
    return scene, char


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # Route the reference cache + call log into a temp state dir, and reset the
    # in-memory upload cache so tests don't leak into each other.
    monkeypatch.setattr(type(settings), "state_dir",
                        property(lambda self: tmp_path), raising=False)
    higgsfield._UPLOAD_CACHE.clear()
    yield


def _patch_net(monkeypatch, api: _FakeAPI):
    monkeypatch.setattr(higgsfield, "_client", lambda: api)
    monkeypatch.setattr(higgsfield.httpx, "Client", _FakeBare)


def test_swap_happy_path_builds_payload_and_returns_bytes(monkeypatch, imgs):
    scene, char = imgs
    api = _FakeAPI()
    _patch_net(monkeypatch, api)

    out = higgsfield.generate_swap(scene_image=scene, character_image=char,
                                   prompt="swap please", aspect_ratio="9:16")
    assert out == b"PNGBYTES"

    soul = next(j for (m, p, j) in api.log if p == "/v1/text2image/soul")
    params = soul["params"]
    assert params["custom_reference_id"] == "ref_1"
    assert params["prompt"] == "swap please"
    assert params["width_and_height"] == "1152x2048"          # 9:16 mapping
    assert settings.higgsfield_scene_field in params           # scene attached
    assert params[settings.higgsfield_scene_field]["image_url"] == "https://cdn/x.png"


def test_reference_created_once_and_reused(monkeypatch, imgs):
    scene, char = imgs
    api = _FakeAPI()
    _patch_net(monkeypatch, api)

    higgsfield.generate_swap(scene_image=scene, character_image=char, prompt="a")
    higgsfield.generate_swap(scene_image=scene, character_image=char, prompt="b")
    assert api.create_calls == 1   # 2nd call reuses the on-disk cached reference


def test_nsfw_status_raises(monkeypatch, imgs):
    scene, char = imgs
    api = _FakeAPI(soul_data={"status": "nsfw"})
    _patch_net(monkeypatch, api)
    with pytest.raises(higgsfield.HiggsfieldError, match="NSFW"):
        higgsfield.generate_swap(scene_image=scene, character_image=char, prompt="x")


def test_generic_completed_shape_supported(monkeypatch, imgs):
    scene, char = imgs
    # request_id + top-level status + images[] (the generic /requests shape)
    api = _FakeAPI(soul_data={"request_id": "r1", "status": "completed",
                              "images": [{"url": "https://cdn/out.png"}]})
    _patch_net(monkeypatch, api)
    out = higgsfield.generate_swap(scene_image=scene, character_image=char, prompt="x")
    assert out == b"PNGBYTES"


def test_missing_credentials_raises(monkeypatch):
    from character_swap.clients import ProviderNotConfigured
    monkeypatch.setattr(settings, "higgsfield_api_key", "", raising=False)
    monkeypatch.setattr(settings, "higgsfield_api_secret", "", raising=False)
    with pytest.raises(ProviderNotConfigured):
        higgsfield._credential()


def test_dispatch_variant_routes_to_higgsfield(monkeypatch, tmp_path, imgs):
    scene, char = imgs
    called = {}

    def fake_swap(*, scene_image, character_image, prompt, aspect_ratio=None, app_job_id=None):
        called["ok"] = (scene_image, character_image, prompt)
        return b"RESULT"
    monkeypatch.setattr(higgsfield, "generate_swap", fake_swap)

    dest = tmp_path / "variant.png"
    out = pipeline._dispatch_variant(
        model="higgsfield-swap", scene_image=scene, character_image=char,
        character_name="X", prompt="do swap", dest=dest, job_id="j1",
    )
    assert out == dest
    assert dest.read_bytes() == b"RESULT"
    assert called["ok"][2] == "do swap"


def test_has_provider_requires_both_key_and_secret(monkeypatch):
    monkeypatch.setattr(settings, "higgsfield_api_key", "k", raising=False)
    monkeypatch.setattr(settings, "higgsfield_api_secret", "", raising=False)
    assert settings.has_provider("higgsfield") is False
    monkeypatch.setattr(settings, "higgsfield_api_secret", "s", raising=False)
    assert settings.has_provider("higgsfield") is True
