"""Every GPT Image call must request OpenAI moderation="low".

Hugo's directive (2026-06-16): the API defaults to the stricter "auto"
moderation, which was rejecting ~49% of swap calls on safety grounds — far more
than the consumer chatgpt.com product. `openai_image._generate_once` hardcodes
moderation="low" (permissive but still filtered) on BOTH the create and edit
endpoints, for every GPT path (Swap gpt-image, Swap/Reengineer gpt2-id-swap,
free-form Image tab). It is NOT switchable.

Robustness guard: if some model/endpoint ever rejects `moderation` as an
unknown parameter (a 400 that is NOT a content block), we drop the param and
retry once rather than fail the slot — but a genuine content rejection must
still propagate so content_policy's softening ladder handles it.

Hermetic: the OpenAI client + call_log.record are stubbed, no network/key.
"""
from __future__ import annotations

import base64
import contextlib

import openai
import pytest

from character_swap.clients import openai_image


class _Item:
    b64_json = base64.b64encode(b"PNGDATA").decode()


class _Resp:
    data = [_Item()]
    _request_id = "req_x"


class _BadReq(openai.BadRequestError):
    """A BadRequestError with a controllable message — the real __init__ needs
    a live httpx response/body, so we bypass it (isinstance still holds)."""
    def __init__(self, message: str):
        self._message = message

    def __str__(self) -> str:
        return self._message


class _FakeImages:
    def __init__(self, raises=None):
        self.calls: list[dict] = []
        # `raises` is a list of exceptions/None applied per call, in order.
        self._raises = list(raises or [])

    def _maybe_raise(self):
        if self._raises:
            exc = self._raises.pop(0)
            if exc is not None:
                raise exc

    def generate(self, **kw):
        self.calls.append(kw)
        self._maybe_raise()
        return _Resp()

    def edit(self, **kw):
        self.calls.append(kw)
        self._maybe_raise()
        return _Resp()


class _FakeClient:
    def __init__(self, raises=None):
        self.images = _FakeImages(raises)


@contextlib.contextmanager
def _rec(**kw):
    yield {}


def _install(monkeypatch, raises=None) -> _FakeClient:
    client = _FakeClient(raises)
    monkeypatch.setattr(openai_image, "_client", lambda: client)
    monkeypatch.setattr(openai_image, "record", _rec)
    return client


def _moderation_of(call: dict):
    # moderation now rides in extra_body — the typed images.edit() signature has
    # no `moderation` kwarg in openai 2.36 (a top-level kwarg TypeErrors before
    # the request). Still tolerate a top-level kwarg for forward-compat.
    if "moderation" in call:
        return call["moderation"]
    return call.get("extra_body", {}).get("moderation", "__absent__")


def _last_moderation(client):
    return _moderation_of(client.images.calls[-1])


def test_moderation_low_on_create(monkeypatch):
    client = _install(monkeypatch)
    openai_image._generate_once(prompt="x", phase="generate", character="c")
    assert len(client.images.calls) == 1
    assert _last_moderation(client) == "low"


def test_moderation_low_on_edit(monkeypatch, tmp_path):
    client = _install(monkeypatch)
    ref = tmp_path / "scene.png"
    ref.write_bytes(b"PNGDATA")
    openai_image._generate_once(
        prompt="x", phase="generate", character="c", reference_images=[ref]
    )
    assert _last_moderation(client) == "low"


def test_unknown_moderation_param_falls_back(monkeypatch):
    # First call rejects `moderation` as an unknown arg → drop it + retry once.
    client = _install(
        monkeypatch,
        raises=[_BadReq("Unknown parameter: 'moderation'."), None],
    )
    out = openai_image._generate_once(prompt="x", phase="generate", character="c")
    assert out == b"PNGDATA"
    assert len(client.images.calls) == 2
    # First attempt carried moderation; the fallback retry omitted it.
    assert _moderation_of(client.images.calls[0]) == "low"
    assert _moderation_of(client.images.calls[1]) == "__absent__"


def test_typeerror_moderation_falls_back(monkeypatch):
    # REGRESSION (2026-06-17): the installed openai SDK's typed images.edit()
    # has no `moderation` kwarg, so a top-level kwarg raised a CLIENT-SIDE
    # TypeError (not a 400) that the old BadRequestError-only guard missed →
    # every swap failed. The guard now also catches TypeError and retries
    # without the param. (Production sends moderation via extra_body, which
    # avoids the TypeError entirely; this locks the safety net regardless.)
    client = _install(
        monkeypatch,
        raises=[TypeError(
            "Images.edit() got an unexpected keyword argument 'moderation'"),
            None],
    )
    out = openai_image._generate_once(prompt="x", phase="generate", character="c")
    assert out == b"PNGDATA"
    assert len(client.images.calls) == 2
    assert _moderation_of(client.images.calls[0]) == "low"
    assert _moderation_of(client.images.calls[1]) == "__absent__"


def test_real_content_block_propagates(monkeypatch):
    # A genuine safety rejection is also a 400 but must NOT be mistaken for the
    # unknown-param case: propagate it (no param-dropping retry) so the
    # content_policy softening ladder upstream handles it.
    client = _install(
        monkeypatch,
        raises=[_BadReq("Your request was rejected by our safety system.")],
    )
    with pytest.raises(openai.BadRequestError):
        openai_image._generate_once(prompt="x", phase="generate", character="c")
    assert len(client.images.calls) == 1  # no fallback retry
