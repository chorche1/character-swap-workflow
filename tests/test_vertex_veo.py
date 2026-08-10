"""Veo 3.1 Fast on Vertex AI — the third host (Hugo 2026-08-10).

WHY IT EXISTS: the same model is reachable three ways, differing only in who
moderates and who rate-limits. fal refuses 46% of these clips but has no daily
ceiling; the Gemini API refuses almost none but accepted ~14 videos a day on
Tier 1, and its ceiling lifts only when a spend threshold is crossed — which is
circular, since the quota is what stops the spending. Vertex is the same model
with a per-project, per-region Google Cloud quota: visible in the console and
raised by request.

HONEST STATUS, and the reason for the shape of this file: there were no Vertex
credentials on this machine when the client was written, so unlike
`test_google_veo.py` — every field of which was verified against a live API —
NOTHING here proves the wire format. What IS locked is everything that would
silently break the rest of the app: an unconfigured host must never occupy a
leg of the chain (a clip routed there dies at token-mint time), the token must
be cached rather than re-minted per clip, and the resume path must poll Vertex
rather than another host. The first run against a real project is the wire-
format verification, and until then `veo_host_chain()` leaves this host out.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from character_swap import pipeline, runner_media
from character_swap.clients import vertex_veo


@pytest.fixture(autouse=True)
def _fresh_token():
    vertex_veo._token.update({"value": "", "expires_at": 0.0})
    yield
    vertex_veo._token.update({"value": "", "expires_at": 0.0})


@pytest.fixture
def _configured(monkeypatch, tmp_path):
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"type": "service_account"}))
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "vertex_project_id",
                        property(lambda self: "proj-1"), raising=False)
    monkeypatch.setattr(type(settings), "vertex_location",
                        property(lambda self: "europe-west4"), raising=False)
    monkeypatch.setattr(type(settings), "vertex_credentials_file",
                        property(lambda self: str(key)), raising=False)
    monkeypatch.setattr(vertex_veo, "_access_token", lambda: "tok-123")
    return key


# --- 1. an unconfigured host must be invisible ------------------------------

def test_missing_config_keeps_vertex_out_of_the_chain(monkeypatch):
    """The load-bearing guard. A half-configured Vertex that still occupied a
    leg would send clips somewhere they die at token-mint time — and it would
    do so on the LAST leg, after everything else had already refused them."""
    from character_swap.config import settings
    for key in ("fal_api_key", "gemini_api_key"):
        monkeypatch.setattr(type(settings), key,
                            property(lambda self: "k"), raising=False)
    monkeypatch.setattr(type(settings), "veo_host_order",
                        property(lambda self: "fal,google,vertex"), raising=False)
    monkeypatch.setattr(type(settings), "vertex_project_id",
                        property(lambda self: ""), raising=False)
    assert "veo-3.1-fast-vertex" not in runner_media.veo_host_chain()


def test_a_project_without_a_key_file_is_still_not_configured(monkeypatch, tmp_path):
    """Both halves are required, and the FILE must exist — a path pointing at
    nothing is exactly as unusable as no path at all."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "vertex_project_id",
                        property(lambda self: "proj-1"), raising=False)
    monkeypatch.setattr(type(settings), "vertex_credentials_file",
                        property(lambda self: str(tmp_path / "nope.json")),
                        raising=False)
    assert vertex_veo.configured() is False
    with pytest.raises(vertex_veo.VertexNotConfigured):
        vertex_veo._access_token()


def test_configured_vertex_joins_the_chain_last(monkeypatch, _configured):
    """Last on purpose: it is the unverified host, so it only ever sees clips
    the other two already refused."""
    from character_swap.config import settings
    for key in ("fal_api_key", "gemini_api_key"):
        monkeypatch.setattr(type(settings), key,
                            property(lambda self: "k"), raising=False)
    monkeypatch.setattr(type(settings), "veo_host_order",
                        property(lambda self: "fal,google,vertex"), raising=False)
    assert runner_media.veo_host_chain() == [
        "veo-3.1-fast", "veo-3.1-fast-google", "veo-3.1-fast-vertex"]


# --- 2. the request we build ------------------------------------------------

class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._p


def test_submit_targets_the_project_and_region(monkeypatch, _configured, tmp_path):
    img = tmp_path / "f.png"; img.write_bytes(b"x")
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(url=url, headers=headers, body=json)
        return _Resp(200, {"name": "projects/p/operations/o"})
    monkeypatch.setattr(vertex_veo.httpx, "post", fake_post)

    op = vertex_veo.submit_image_to_video(image=img, prompt="p", duration_secs=8)
    assert op == "projects/p/operations/o"
    assert "europe-west4-aiplatform.googleapis.com" in sent["url"]
    assert "/projects/proj-1/locations/europe-west4/" in sent["url"]
    assert sent["url"].endswith(":predictLongRunning")
    # OAuth bearer, never an API key — Vertex rejects those outright.
    assert sent["headers"]["Authorization"] == "Bearer tok-123"
    assert "key" not in sent["url"]
    assert sent["body"]["parameters"]["personGeneration"] == "allow_adult"


def test_end_frame_and_audio_are_expressible(monkeypatch, _configured, tmp_path):
    """Vertex accepts `generateAudio`, unlike the Gemini API path where the
    field is rejected outright — so a silent clip is actually possible here."""
    img = tmp_path / "f.png"; img.write_bytes(b"x")
    end = tmp_path / "e.png"; end.write_bytes(b"y")
    sent = {}
    monkeypatch.setattr(vertex_veo.httpx, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        (sent.update(body=json),
                         _Resp(200, {"name": "op"}))[1])
    vertex_veo.submit_image_to_video(image=img, prompt="p", end_image=end,
                                     generate_audio=False, duration_secs=8)
    assert "lastFrame" in sent["body"]["instances"][0]
    assert sent["body"]["parameters"]["generateAudio"] is False


def test_1080p_degrades_below_8s(monkeypatch, _configured):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "veo_fal_resolution",
                        property(lambda self: "1080p"), raising=False)
    assert vertex_veo._resolution(8) == "1080p"
    assert vertex_veo._resolution(6) == "720p"


# --- 3. the answer we accept ------------------------------------------------

def test_inline_video_is_written(monkeypatch, _configured, tmp_path):
    import base64
    payload = {"done": True, "response": {"videos": [
        {"bytesBase64Encoded": base64.b64encode(b"m" * 2000).decode()}]}}
    monkeypatch.setattr(vertex_veo.httpx, "post",
                        lambda *a, **k: _Resp(200, payload))
    dest = tmp_path / "out.mp4"
    vertex_veo.wait_for_video(operation="op", dest=dest)
    assert dest.stat().st_size == 2000


def test_rai_filtering_reports_the_real_reason(monkeypatch, _configured, tmp_path):
    """Vertex reports post-generation filtering by returning FEWER videos than
    asked for. Calling that "no video" would read like a bug in our code."""
    payload = {"done": True, "response": {"videos": [],
                                          "raiMediaFilteredCount": 1,
                                          "raiMediaFilteredReasons": ["58061214"]}}
    monkeypatch.setattr(vertex_veo.httpx, "post",
                        lambda *a, **k: _Resp(200, payload))
    with pytest.raises(RuntimeError, match="content-policy"):
        vertex_veo.wait_for_video(operation="op", dest=tmp_path / "o.mp4")


def test_an_empty_clip_is_refused_not_shipped(monkeypatch, _configured, tmp_path):
    import base64
    payload = {"done": True, "response": {"videos": [
        {"bytesBase64Encoded": base64.b64encode(b"tiny").decode()}]}}
    monkeypatch.setattr(vertex_veo.httpx, "post",
                        lambda *a, **k: _Resp(200, payload))
    dest = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError, match="empty"):
        vertex_veo.wait_for_video(operation="op", dest=dest)
    assert not dest.exists()


# --- 4. the app dispatches to it -------------------------------------------

def test_pipeline_dispatches_and_polls_vertex(monkeypatch, tmp_path):
    """A registry entry nothing dispatches fails at submit; a wait that goes to
    the wrong host polls an operation name the other API has never heard of."""
    seen = {}
    monkeypatch.setattr(vertex_veo, "submit_image_to_video",
                        lambda **kw: seen.update(kw) or "projects/p/operations/o")
    monkeypatch.setattr(vertex_veo, "wait_for_video",
                        lambda **kw: seen.update(wait=kw) or kw["dest"])
    img = tmp_path / "f.png"; img.write_bytes(b"x")
    assert pipeline.submit_video(image=img, movement_prompt="p",
                                 character_name="c",
                                 model="veo-3.1-fast-vertex",
                                 duration_secs=6) == "projects/p/operations/o"
    pipeline.wait_for_video(job_id="projects/p/operations/o", character_name="c",
                            dest=tmp_path / "o.mp4", model="veo-3.1-fast-vertex")
    assert seen["wait"]["operation"] == "projects/p/operations/o"


def test_vertex_keeps_the_end_pose_capability():
    assert runner_media.supports_end_frame("veo-3.1-fast-vertex")


# --- 5. the user's own login is a first-class identity ----------------------

def test_adc_alone_is_enough(monkeypatch, tmp_path):
    """The path Hugo actually uses: one `gcloud auth application-default
    login` and no service-account key file at all. Requiring a key file here
    would have meant five Cloud Console pages for the same result."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "vertex_project_id",
                        property(lambda self: "proj-1"), raising=False)
    monkeypatch.setattr(type(settings), "vertex_credentials_file",
                        property(lambda self: ""), raising=False)
    adc = tmp_path / "adc.json"; adc.write_text("{}")
    monkeypatch.setattr(vertex_veo, "_ADC_PATH", adc)
    assert vertex_veo.configured() is True
    monkeypatch.setattr(vertex_veo, "_ADC_PATH", tmp_path / "gone.json")
    assert vertex_veo.configured() is False


def test_requests_name_the_quota_project(monkeypatch, _configured, tmp_path):
    """A USER identity owns no project, so Google has nothing to bill or count
    the quota against and answers 403 without this header."""
    img = tmp_path / "f.png"; img.write_bytes(b"x")
    sent = {}
    monkeypatch.setattr(vertex_veo.httpx, "post",
                        lambda url, headers=None, json=None, timeout=None:
                        (sent.update(headers=headers), _Resp(200, {"name": "op"}))[1])
    vertex_veo.submit_image_to_video(image=img, prompt="p", duration_secs=8)
    assert sent["headers"]["x-goog-user-project"] == "proj-1"


def test_the_token_is_cached_not_reminted_per_clip(monkeypatch, tmp_path):
    """Minting is a signed round trip to Google. Forty clips must not mean
    forty of them."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "vertex_project_id",
                        property(lambda self: "p"), raising=False)
    monkeypatch.setattr(type(settings), "vertex_credentials_file",
                        property(lambda self: ""), raising=False)
    adc = tmp_path / "adc.json"; adc.write_text("{}")
    monkeypatch.setattr(vertex_veo, "_ADC_PATH", adc)
    mints = []

    class _Creds:
        token = "t"

        def refresh(self, _req):
            mints.append(1)

    import google.auth
    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (_Creds(), "p"))
    assert vertex_veo._access_token() == "t"
    assert vertex_veo._access_token() == "t"
    assert len(mints) == 1


def test_adc_alone_makes_the_provider_available(monkeypatch, tmp_path):
    """REGRESSION (2026-08-10, caught on the live wire-up). `has_provider`
    demanded a key file, so an ADC-authenticated Vertex was configured,
    reachable and completely invisible to the chain — the host silently did
    not exist."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "vertex_project_id",
                        property(lambda self: "proj-1"), raising=False)
    monkeypatch.setattr(type(settings), "vertex_credentials_file",
                        property(lambda self: ""), raising=False)
    assert settings.has_provider("vertex") is True
    monkeypatch.setattr(type(settings), "vertex_project_id",
                        property(lambda self: ""), raising=False)
    assert settings.has_provider("vertex") is False
