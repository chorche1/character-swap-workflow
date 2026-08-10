"""Veo 3.1 Fast on Google's own API — the default Veo path (Hugo 2026-08-10).

WHY THE PATH MOVED, in one paragraph, because every assertion below only makes
sense against it: fal's Veo deployment refused 42-79% of this app's clips per
day through August while Kling on the SAME fal key refused 0-7%; 89% of those
refusals arrived AFTER a full render (median 62.5 s vs 64.7 s for a success);
on 127 identical start images Veo refused 46% and Kling 8%, uncorrelated; and
five frames fal refuses 90-100% of the time — two of which fal had NEVER once
passed — rendered on Google's own API on the first attempt with nothing
changed. So the blocker was fal's Veo layer, not Google's model and not the
content.

Everything asserted here about the Google API was VERIFIED LIVE against Hugo's
key on 2026-08-10, not read off documentation:
  * `instances[0].lastFrame` renders start→end, so the 🎯 end pose survives;
  * `resolution: "1080p"` yields 1080x1920 and `durationSeconds: 6` yields
    exactly 6.000 s, but the two together are refused ("Your use case is
    currently not supported") — the same 1080p-needs-8s rule fal documents;
  * `generateAudio` is REJECTED as a field, and audio comes anyway (AAC stereo
    48 kHz in every probe);
  * the file download needs the key in the `x-goog-api-key` HEADER — with
    `?key=` the endpoint returns 200 and an EMPTY body, which would have
    written a 0-byte clip;
  * the 4th concurrent submit returns 429 quota.

The regressions this file exists to prevent are the ones that would be silent:
a quota 429 mistaken for a content refusal (it would burn the clip's whole take
budget on a limit only time fixes), a 0-byte download shipped as a clip, and a
resume that polls the wrong host.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap import pipeline, runner_media
from character_swap.clients import google_veo


# --- 1. the registry says what the app now does -----------------------------

def test_language_clips_render_on_google():
    """The 🗣 redirect is where nearly every Veo clip comes from, so this
    constant IS "the default Veo path"."""
    assert runner_media.SPOKEN_LANGUAGE_VIDEO_MODEL == "veo-3.1-fast-google"
    assert "veo-3.1-fast-google" in runner_media.VIDEO_MODELS


def test_google_veo_keeps_the_end_pose():
    """Verified live via `lastFrame`. If this ever regresses, every 🎯 end pose
    in a language run is silently dropped — the exact degradation the grok
    rescue has to flag."""
    assert runner_media.supports_end_frame("veo-3.1-fast-google")
    assert not runner_media.drops_end_frame("veo-3.1-fast", "veo-3.1-fast-google")


def test_google_veo_takes_the_same_durations_as_fal():
    """A scene's length is dictated by the source clip; if the buckets differed
    from fal's, moving the default would silently re-time every clip."""
    spec = runner_media.video_duration_spec("veo-3.1-fast-google")
    assert spec["options"] == [4, 6, 8]
    assert spec["default"] == 8


def test_the_fal_slug_still_exists():
    """Old clips carry it and must stay resumable — fal request_ids are
    endpoint-scoped, so a repointed slug (rather than a new one) would send
    every in-flight clip's poll to the wrong host."""
    assert runner_media.VIDEO_MODELS["veo-3.1-fast"]["provider"] == "fal"
    assert runner_media.VIDEO_MODELS["veo-3.1-fast-google"]["provider"] == "gemini"


def test_both_hosts_count_as_veo_for_the_refusal_predicate():
    assert "veo-3.1-fast-google" in runner_media.VEO_VIDEO_MODELS


# --- 2. the request we actually build ---------------------------------------

class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text or str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def _key(monkeypatch):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "gemini_api_key",
                        property(lambda self: "test-key"), raising=False)


@pytest.fixture
def _frames(tmp_path):
    a = tmp_path / "start.png"; a.write_bytes(b"start-bytes")
    b = tmp_path / "end.png"; b.write_bytes(b"end-bytes")
    return a, b


def _capture(monkeypatch, status=200, payload=None):
    sent = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["url"] = url; sent["headers"] = headers; sent["body"] = json
        return _Resp(status, payload if payload is not None
                     else {"name": "models/x/operations/abc"})
    monkeypatch.setattr(google_veo.httpx, "post", fake_post)
    return sent


def test_submit_sends_person_generation_and_the_start_frame(
        monkeypatch, _key, _frames):
    start, _ = _frames
    sent = _capture(monkeypatch)
    op = google_veo.submit_image_to_video(image=start, prompt="he waves",
                                          duration_secs=8)
    assert op == "models/x/operations/abc"
    body = sent["body"]
    inst = body["instances"][0]
    assert inst["prompt"] == "he waves"
    assert inst["image"]["mimeType"] == "image/png"
    assert "lastFrame" not in inst, "no end pose was given"
    # allow_adult is the ONLY value Google permits for image-to-video, and it
    # is sent explicitly so a future default change cannot quietly make person
    # generation stricter.
    assert body["parameters"]["personGeneration"] == "allow_adult"
    # The key rides in the header, never the query string.
    assert sent["headers"]["x-goog-api-key"] == "test-key"
    assert "key=" not in sent["url"]


def test_end_frame_rides_as_last_frame(monkeypatch, _key, _frames):
    start, end = _frames
    sent = _capture(monkeypatch)
    google_veo.submit_image_to_video(image=start, prompt="p", end_image=end,
                                     duration_secs=8)
    assert "lastFrame" in sent["body"]["instances"][0]


def test_1080p_degrades_to_720p_below_8s(monkeypatch, _key):
    """Google refuses 1080p together with a non-8s duration, exactly as fal
    documents. A scene's length is not ours to change, so the clip RENDERS at
    720p rather than failing."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "veo_fal_resolution",
                        property(lambda self: "1080p"), raising=False)
    assert google_veo._resolution(8) == "1080p"
    assert google_veo._resolution(6) == "720p"
    assert google_veo._resolution(4) == "720p"


def test_durations_snap_to_the_accepted_buckets(_key):
    assert google_veo.clamp_duration(6) == 6
    assert google_veo.clamp_duration(5) in (4, 6)
    assert google_veo.clamp_duration(None) == 8
    assert google_veo.clamp_duration(99) == 8


def test_audio_cannot_be_switched_off_and_says_so(monkeypatch, _key, _frames, caplog):
    """The API rejects `generateAudio` outright and produces audio regardless.
    Passing False must not send the field (that would 400 the whole clip) and
    must not pretend it worked."""
    start, _ = _frames
    sent = _capture(monkeypatch)
    with caplog.at_level("WARNING"):
        google_veo.submit_image_to_video(image=start, prompt="p",
                                         generate_audio=False, duration_secs=8)
    assert "generateAudio" not in str(sent["body"])
    assert "generate_audio=False" in caplog.text


# --- 3. quota is not a content refusal --------------------------------------

def test_quota_429_raises_its_own_error(monkeypatch, _key, _frames):
    """THE DANGEROUS CONFUSION. A 429 reaching the runner's refusal machinery
    would spend the clip's whole take budget re-submitting into a limit that
    only time fixes — and then fail it as if the content were blocked."""
    start, _ = _frames
    monkeypatch.setattr(google_veo, "_RETRY_WAITS", ())
    _capture(monkeypatch, status=429,
             payload={"error": {"code": 429,
                                "message": "You exceeded your current quota"}})
    with pytest.raises(google_veo.GoogleVeoQuotaError):
        google_veo.submit_image_to_video(image=start, prompt="p", duration_secs=8)


def test_quota_error_is_not_read_as_a_content_refusal():
    from character_swap import content_policy
    exc = google_veo.GoogleVeoQuotaError(
        "google veo quota exhausted after 3 retries: "
        '{"error": {"code": 429, "message": "You exceeded your current quota, '
        'please check your plan and billing details."}}')
    assert not content_policy.is_content_rejection(exc)
    assert not runner_media.triggers_fallback("veo-3.1-fast-google", exc)


def test_non_quota_submit_failure_fails_immediately(monkeypatch, _key, _frames):
    """A 400 is a real error — retrying it three times just delays the report."""
    start, _ = _frames
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(1)
        return _Resp(400, {"error": {"message": "bad request"}}, "bad request")
    monkeypatch.setattr(google_veo.httpx, "post", fake_post)
    with pytest.raises(RuntimeError, match="google veo submit failed"):
        google_veo.submit_image_to_video(image=start, prompt="p", duration_secs=8)
    assert len(calls) == 1


# --- 4. the answer we accept as a finished clip -----------------------------

def _poll(monkeypatch, payload):
    monkeypatch.setattr(google_veo.httpx, "get",
                        lambda url, headers=None, timeout=None: _Resp(200, payload))


def test_rai_filtered_output_reports_the_real_reason(monkeypatch, _key, tmp_path):
    """Google drops a filtered sample instead of erroring. Reporting that as
    'response carried no video uri' would read like a bug in our code; it is a
    content block, and unlike fal this path can name it."""
    _poll(monkeypatch, {"done": True, "response": {
        "generateVideoResponse": {"raiMediaFilteredCount": 1,
                                  "raiMediaFilteredReasons": ["58061214"]}}})
    with pytest.raises(RuntimeError, match="content-policy"):
        google_veo.wait_for_video(operation="models/x/operations/a",
                                  dest=tmp_path / "out.mp4")


def test_operation_error_surfaces_googles_own_message(monkeypatch, _key, tmp_path):
    _poll(monkeypatch, {"done": True,
                        "error": {"code": 3, "message": "unsupported use case"}})
    with pytest.raises(RuntimeError, match="unsupported use case"):
        google_veo.wait_for_video(operation="models/x/operations/a",
                                  dest=tmp_path / "out.mp4")


def test_empty_download_is_refused_not_shipped(monkeypatch, _key, tmp_path):
    """With the key in the query string the files endpoint answers 200 with an
    EMPTY body (measured). Writing that as a clip would fail much later, inside
    ffmpeg, with no hint of the cause."""
    class _Stream:
        status_code = 200

        def __enter__(self): return self
        def __exit__(self, *a): return False
        def raise_for_status(self): pass
        def iter_bytes(self, chunk_size=0): return iter([b""])
    monkeypatch.setattr(google_veo.httpx, "stream",
                        lambda *a, **k: _Stream())
    dest = tmp_path / "out.mp4"
    with pytest.raises(RuntimeError, match="empty|bytes"):
        google_veo._download("https://x/y", "k", dest, attempts=1)
    assert not dest.exists(), "a truncated clip must never be left behind"


# --- 5. the app dispatches to it --------------------------------------------

def test_pipeline_dispatches_the_slug_to_the_google_client(monkeypatch, tmp_path):
    """A registry entry nothing dispatches is an entry that fails at submit."""
    seen = {}

    def fake_submit(**kw):
        seen.update(kw)
        return "models/x/operations/z"
    monkeypatch.setattr(google_veo, "submit_image_to_video", fake_submit)
    img = tmp_path / "f.png"; img.write_bytes(b"x")
    out = pipeline.submit_video(image=img, movement_prompt="p",
                                character_name="c", model="veo-3.1-fast-google",
                                duration_secs=6)
    assert out == "models/x/operations/z"
    assert seen["duration_secs"] == 6


def test_pipeline_waits_on_the_google_client(monkeypatch, tmp_path):
    seen = {}

    def fake_wait(**kw):
        seen.update(kw)
        return kw["dest"]
    monkeypatch.setattr(google_veo, "wait_for_video", fake_wait)
    dest = tmp_path / "out.mp4"
    pipeline.wait_for_video(job_id="models/x/operations/z", character_name="c",
                            dest=dest, model="veo-3.1-fast-google")
    # The operation NAME is the provider job id — not a fal request_id.
    assert seen["operation"] == "models/x/operations/z"
