"""A quota wall CANCELS the host, it is not waited out (Hugo 2026-08-11 —
"om kvoten är slut så cancela istället").

The run that forced this, re_bc2d243011 / j_edf16f0e4d: 30 clips went to Veo on
fal, 14 rendered, 16 were refused and rerouted to Google — where the daily quota
was already exhausted. Each of those 16 then slept out the client's 2-minute
shared pause and walked its OWN ~24-minute backoff ladder, five Google entries
deep, against a wall the first clip had already found. Two hours of "renderar…"
with no API call in flight and no frame produced.

Two changes, both pinned here:

  * the client FAILS FAST while the shared pause is live (it used to sleep), and
    re-checks after `_SUBMIT_GATE` hands it a slot — the gate is held for a whole
    ladder, so the pre-gate check alone missed every verdict that arrived during
    the ~25-minute queue wait;
  * the runners RETIRE a host on its first quota error and jump to the next
    DIFFERENT model. Spanish/English clips land on Kling. A GERMAN clip has no
    reroute at all ("tyskarna får bara ha veo") so it parks for
    `drain_quota_blocked` — which is a different thing from a content refusal
    and now says so.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import runner, runner_media
from character_swap.clients import google_veo


@pytest.fixture(autouse=True)
def _no_quota_pause():
    """The breaker is module-global by design (it is an account-level fact), so
    every test must start and leave it clear."""
    google_veo.clear_quota_block()
    yield
    google_veo.clear_quota_block()


# --- 1. the pure chain helper ----------------------------------------------

def test_next_attempt_model_skips_a_dead_host():
    models = ["veo-3.1-fast-google"] * 5 + ["kling-v3", "grok-imagine-1.5"]
    assert runner_media.next_attempt_model(
        models, 0, {"veo-3.1-fast-google"}) == "kling-v3"


def test_next_attempt_model_is_none_when_only_dead_hosts_remain():
    """The German case: no reroute, so once Veo is retired there is nowhere to
    go and the caller must park the clip rather than loop."""
    models = ["veo-3.1-fast-google"] * 5
    assert runner_media.next_attempt_model(
        models, 0, {"veo-3.1-fast-google"}) is None


def test_next_attempt_model_without_a_dead_host_is_just_the_next_entry():
    models = ["veo-3.1-fast", "veo-3.1-fast-google", "kling-v3"]
    assert runner_media.next_attempt_model(models, 0, set()) == \
        "veo-3.1-fast-google"
    assert runner_media.next_attempt_model(models, 2, set()) is None


# --- 2. the client refuses instead of sleeping ------------------------------

def test_a_live_pause_refuses_immediately():
    google_veo._trip_quota_block("You exceeded your current quota")
    with pytest.raises(google_veo.GoogleVeoQuotaError) as exc:
        google_veo._refuse_if_quota_blocked()
    # The real reason travels with it — the runner's message quotes it.
    assert "quota" in str(exc.value).lower()


def test_no_pause_lets_the_submit_through():
    google_veo._refuse_if_quota_blocked()   # must not raise


def test_the_pause_expires_rather_than_latching(monkeypatch):
    """A window that reopens must not stay locked out — that is the failure
    fal_kling documents for its own lock."""
    google_veo._trip_quota_block("quota")
    monkeypatch.setattr(google_veo, "_QUOTA_BLOCK_SECS", 0.0)
    google_veo._refuse_if_quota_blocked()   # must not raise


def test_submit_rechecks_the_pause_after_the_gate(monkeypatch, tmp_path):
    """THE ONE THAT WAS SILENTLY MISSING. `_SUBMIT_GATE` is held for a clip's
    whole backoff ladder, so a queued sibling waits there for ~25 minutes — and
    the verdict it needs arrives DURING that wait. Checking only before the gate
    means every queued clip walks its own ladder anyway."""
    monkeypatch.setattr(google_veo, "_key", lambda: "k")
    img = tmp_path / "f.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(google_veo, "_b64", lambda p: "b64")

    calls = []

    class _Gate:
        def __enter__(self):
            # Whatever tripped it did so while we were queued here.
            google_veo._trip_quota_block("You exceeded your current quota")
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(google_veo, "_SUBMIT_GATE", _Gate())
    monkeypatch.setattr(google_veo.httpx, "post",
                        lambda *a, **k: calls.append(1))

    with pytest.raises(google_veo.GoogleVeoQuotaError):
        google_veo.submit_image_to_video(image=img, prompt="p",
                                        duration_secs=8)
    assert calls == [], "no HTTP call may be made once the wall is known"


# --- 3. the runner retires the host ----------------------------------------

def _job_one_clip(tmp_path, *, model="veo-3.1-fast-google"):
    from character_swap.models import (
        CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
        VideoStatus, VideoVariant)
    img = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                         prompt="B", scene_id="s1", status=VariantStatus.READY)
    Path(img.path).write_bytes(b"img")
    jc = JobCharacter(char_id="cA", name="A",
                      source_image_path=str(tmp_path / "c.png"),
                      status=CharStatus.APPROVED, images=[img],
                      approved_variant_ids=["v1"], approved_variant_id="v1")
    (tmp_path / "c.png").write_bytes(b"c")
    scene = tmp_path / "s.png"
    scene.write_bytes(b"s")
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path=str(scene), scene_ids=["s1"],
              scene_image_paths=[str(scene)],
              video_model=model, characters={"cA": jc})
    v = VideoVariant(video_id="vd1", grok_job_id="",
                     status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [v]
    return job, jc, v


def _quiet_runner(monkeypatch, tmp_path, *, language=None):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "gemini_api_key",
                        property(lambda self: "k"), raising=False)
    monkeypatch.setattr(type(settings), "fal_api_key",
                        property(lambda self: "k"), raising=False)
    monkeypatch.setattr(type(settings), "veo_host_order",
                        property(lambda self: "google"), raising=False)
    monkeypatch.setattr(type(settings), "video_qc_enabled",
                        property(lambda self: False), raising=False)
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: True), raising=False)
    for name in ("_persist", "_replace_video", "_maybe_complete_char"):
        monkeypatch.setattr(runner, name, lambda *a, **k: None)
    monkeypatch.setattr(runner, "_output_dir", lambda j, c: tmp_path)
    monkeypatch.setattr(runner, "_character_language", lambda cid: language)
    monkeypatch.setattr(runner, "_character_gender", lambda cid: "male")
    monkeypatch.setattr(runner.reengineer, "localize_motion_prompt",
                        lambda prompt, *a, **k: prompt)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)


def test_spanish_clip_skips_the_dead_host_and_lands_on_kling(
        monkeypatch, tmp_path):
    """One swing at the quota wall, then Kling. Before the fix this walked all
    five Google entries — ~25 minutes each — before reaching the same place."""
    from character_swap.models import VideoStatus

    _quiet_runner(monkeypatch, tmp_path, language="es")
    job, jc, video = _job_one_clip(tmp_path)
    tried = []

    def fake_submit(**kw):
        tried.append(kw["model"])
        if kw["model"] == "veo-3.1-fast-google":
            raise google_veo.GoogleVeoQuotaError("google veo quota exhausted")
        return "kling-req-1"

    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    asyncio.run(runner._animate_one_video(job, jc, video, "he waves"))

    assert tried.count("veo-3.1-fast-google") == 1, \
        "a retired host must be asked exactly once, not once per entry"
    assert tried[-1] == "kling-v3"
    assert video.status == VideoStatus.DONE
    # A quota wall says nothing about the clip's content.
    assert video.refusal_takes == 0
    assert not video.quota_blocked


def test_german_clip_parks_instead_of_walking_five_entries(
        monkeypatch, tmp_path):
    """German gets no reroute (Hugo: "tyskarna får bara ha veo"), so there is
    nowhere to go — it must park for the drainer after ONE attempt, and say
    PARKED rather than reading like a content block."""
    from character_swap.models import VideoStatus

    _quiet_runner(monkeypatch, tmp_path, language="de")
    job, jc, video = _job_one_clip(tmp_path)
    tried = []

    def fake_submit(**kw):
        tried.append(kw["model"])
        raise google_veo.GoogleVeoQuotaError("google veo quota exhausted")

    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)

    asyncio.run(runner._animate_one_video(job, jc, video, "er winkt"))

    assert tried == ["veo-3.1-fast-google"], \
        "the remaining Veo entries answer the same way — skip them"
    assert video.status == VideoStatus.ERROR
    assert video.quota_blocked, "the drainer finds the clip by this flag alone"
    assert "parkerat" in (video.error or "")
    assert "kvoten" in (video.error or "")
    assert video.refusal_takes == 0


def test_a_content_refusal_still_walks_every_take(monkeypatch, tmp_path):
    """The skip must key on QUOTA only. A refusal is stochastic — re-submitting
    the identical request is the cheapest recovery there is, and collapsing
    those takes would undo the 2026-08-06 fix."""
    from character_swap.config import settings

    _quiet_runner(monkeypatch, tmp_path, language="es")
    monkeypatch.setattr(type(settings), "video_refusal_retries",
                        property(lambda self: 2), raising=False)
    job, jc, video = _job_one_clip(tmp_path)
    tried = []

    def fake_submit(**kw):
        tried.append(kw["model"])
        raise RuntimeError("flagged as nsfw content policy violation")

    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)

    asyncio.run(runner._animate_one_video(job, jc, video, "he waves"))

    assert tried.count("veo-3.1-fast-google") == 3, \
        "unchanged re-submits on a REFUSAL are untouched by the quota skip"
    assert not video.quota_blocked


# --- 4. the reengineer's shared direct clip follows the same rule -----------

def _direct_scene(monkeypatch, tmp_path, *, language="es"):
    from character_swap import pipeline, runner_reengineer as rr
    from character_swap.config import settings
    from character_swap.models import Job

    for key in ("gemini_api_key", "fal_api_key"):
        monkeypatch.setattr(type(settings), key,
                            property(lambda self: "k"), raising=False)
    monkeypatch.setattr(type(settings), "veo_host_order",
                        property(lambda self: "google"), raising=False)
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: True), raising=False)

    img = tmp_path / "direct.png"
    img.write_bytes(b"x")
    state = {"job_id": "jd", "language": language,
             "scenes": [{"scene_id": "sc1", "is_direct": True,
                         "direct_image_path": str(img),
                         "motion_prompt": "she nods",
                         "video_model": "veo-3.1-fast-google"}]}
    job = Job(job_id="jd", scene_id="sc1", scene_image_path=str(img),
              scene_ids=["sc1"], characters={}, video_audio=True)
    monkeypatch.setattr(rr.reengineer, "load_state", lambda re_id: state)
    monkeypatch.setattr(rr.reengineer, "reengineer_dir", lambda re_id: tmp_path)
    monkeypatch.setattr(rr, "store", lambda: type("S", (), {
        "get_job": staticmethod(lambda jid: job if jid == "jd" else None)})())

    async def _persist(re_id, scene_id, **fields):
        _persist.calls.append(fields)
    _persist.calls = []
    monkeypatch.setattr(rr, "_persist_direct", _persist)

    async def _noop_pub(*a, **k):
        return None
    monkeypatch.setattr(rr.events, "publish", _noop_pub)
    return rr, pipeline, _persist


def test_direct_clip_skips_the_dead_host_too(monkeypatch, tmp_path):
    """A 📌 direct clip runs its own attempt loop, which had NO quota branch at
    all — the first 429 failed the whole shared scene."""
    rr, pipeline, persist = _direct_scene(monkeypatch, tmp_path, language="es")
    tried = []

    def fake_submit(**kw):
        tried.append(kw["model"])
        if kw["model"] == "veo-3.1-fast-google":
            raise google_veo.GoogleVeoQuotaError("google veo quota exhausted")
        return "kling-req"

    monkeypatch.setattr(pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    asyncio.run(rr._render_direct_clip("re1", "sc1"))

    assert tried == ["veo-3.1-fast-google", "kling-v3"]
    assert persist.calls[-1].get("direct_error") is None
    assert persist.calls[-1].get("shared_clip_path")


def test_direct_clip_with_nowhere_to_go_says_quota(monkeypatch, tmp_path):
    """German: no reroute. One attempt, then a failure that names the QUOTA —
    never a content block, which would send the user hunting for a new frame."""
    rr, pipeline, persist = _direct_scene(monkeypatch, tmp_path, language="de")
    tried = []

    def fake_submit(**kw):
        tried.append(kw["model"])
        raise google_veo.GoogleVeoQuotaError("google veo quota exhausted")

    monkeypatch.setattr(pipeline, "submit_video", fake_submit)

    asyncio.run(rr._render_direct_clip("re1", "sc1"))

    assert tried == ["veo-3.1-fast-google"]
    err = persist.calls[-1].get("direct_error") or ""
    assert "kvoten" in err
    assert "content-policy" not in err
