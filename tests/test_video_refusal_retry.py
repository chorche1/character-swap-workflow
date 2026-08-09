"""Unchanged re-submit of a REFUSED clip on its OWN model (Hugo 2026-08-06).

"Finns det något sätt att göra så fler videor inte blir nekade utan att ändra
prompt" — yes: send the identical request again. The refusal is not
deterministic. Measured over calls.jsonl on 2026-08-06, Veo refused 50-60% of
its calls on four consecutive days while the same prompts rendered fine on
other takes, and the 2026-08-03 probe found the same line on the same start
frame passing sometimes and refused other times.

Before this, `models_to_try` was `[chosen] + [rescue?]`: ONE take per model, so
a refused clip either jumped straight to another provider or died. Now the
chosen model gets `1 + VIDEO_REFUSAL_RETRIES` takes (4 extra since 2026-08-10,
2 before that) BEFORE the rescue, which keeps a recovered clip on the model its
reel is built on. The tests below set the knob explicitly rather than leaning on
the default, so the number can move without rewriting the suite.

What this must NOT do:
  * change the prompt between takes (that is the whole point),
  * mark a same-model take as a `fallback_model` (drives the ⇄ chip and the
    salvage/resume endpoint resolution — a wrong value 404s the re-poll),
  * re-submit anything that is not a genuine refusal — a timeout, a network
    error or a locked fal account must still fail on the first try.

Billing note (why re-submitting refused takes is safe): fal prices Veo 3.1 Fast
per GENERATED SECOND, and a refused take generates none. See
config.video_refusal_retries for the full reasoning and the number to watch.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import content_policy, runner, runner_media
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus, VideoStatus,
    VideoVariant,
)

VEO = "veo-3.1-fast"
KLING = "kling-v3"

_MODERATION = "fal submit failed: 400 rejected — flagged as nsfw content"
_NO_MEDIA = (
    "[{'loc': ['body'], 'msg': 'The model did not generate the expected output "
    "for this prompt. This may occur for several reasons, including unsafe "
    "content…', 'type': 'no_media_generated'}]"
)
_TIMEOUT = "Video job req-1 timed out after 600s"
_BALANCE = ("fal fal-ai/kling-video/v3/standard/image-to-video submit failed: "
            "User is locked. Reason: Exhausted balance. Top up your balance at "
            "fal.ai/dashboard/billing.")


# --- fixtures ----------------------------------------------------------------

@pytest.fixture
def _retries(monkeypatch):
    """Set VIDEO_REFUSAL_RETRIES for one test."""
    def _set(n):
        from character_swap.config import settings
        monkeypatch.setattr(type(settings), "video_refusal_retries",
                            property(lambda self: n), raising=False)
    return _set


@pytest.fixture
def _rescue_off(monkeypatch):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: False), raising=False)


@pytest.fixture
def _speaks(monkeypatch):
    """Give the character a 🗣 flag — what forces its clips onto Veo, and what
    decides whether the Kling rescue applies at all."""
    def _set(code):
        monkeypatch.setattr(runner, "_character_language", lambda cid: code)
        monkeypatch.setattr(runner.reengineer, "localize_motion_prompt",
                            lambda prompt, *a, **k: prompt)
        runner._clear_prompt_cache("j1")
    return _set


def _job_one_clip(tmp_path, *, video_model=KLING):
    v_img = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                           prompt="BASE", scene_id="s1",
                           status=VariantStatus.READY)
    Path(v_img.path).write_bytes(b"img")
    jc = JobCharacter(char_id="cA", name="A",
                      source_image_path=str(tmp_path / "char.png"),
                      status=CharStatus.APPROVED, images=[v_img],
                      approved_variant_ids=["v1"], approved_variant_id="v1")
    (tmp_path / "char.png").write_bytes(b"char")
    scene = tmp_path / "scene.png"; scene.write_bytes(b"scene")
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path=str(scene), scene_ids=["s1"],
              scene_image_paths=[str(scene)], video_model=video_model,
              characters={"cA": jc})
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]
    return job, jc, video


def _stub(monkeypatch, tmp_path):
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_output_dir", lambda job_id, char_id: tmp_path)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(type(runner.settings), "video_qc_enabled",
                        property(lambda self: False), raising=False)
    monkeypatch.setattr(runner.video_qc, "inspect_clip", lambda *a, **k: None)


def _run(coro):
    return asyncio.run(coro)


# --- 1. the resolver ---------------------------------------------------------

def test_default_is_four_extra_takes(monkeypatch):
    """Hugo raised it from 2 to 4 on 2026-08-10: Veo's measured per-call refusal
    rate had climbed to 79% (08-08) / 70% (08-09), where 3 takes still lose ~34%
    of clips and 5 takes lose ~17%."""
    from character_swap.config import settings
    monkeypatch.delenv("VIDEO_REFUSAL_RETRIES", raising=False)
    assert type(settings).model_fields["video_refusal_retries"].default == 4


def test_takes_come_before_the_rescue(_retries, _rescue_off):
    """A Spanish Veo clip: three unchanged Veo takes, THEN Kling. Ordering is
    the point — a clip that passes on take 2 keeps Veo's 0.992 Spanish and
    still matches the rest of a 🗣 reel, where every other clip is on Veo."""
    _retries(2)
    assert runner_media.video_attempt_models(VEO, language="es") == [
        VEO, VEO, VEO, KLING]


def test_no_rescue_still_gets_the_extra_takes(_retries, _rescue_off):
    """German has no rescue (Kling scores 0.48 on it) — so the re-submit is the
    ONLY recovery those clips have, which is most of the value here."""
    _retries(2)
    assert runner_media.video_attempt_models(VEO, language="de") == [
        VEO, VEO, VEO]
    assert runner_media.video_attempt_models(KLING) == [KLING, KLING, KLING]


def test_zero_disables_and_restores_one_take_per_model(_retries, _rescue_off):
    _retries(0)
    assert runner_media.video_attempt_models(VEO, language="es") == [VEO, KLING]
    assert runner_media.video_attempt_models(KLING) == [KLING]


def test_negative_value_is_clamped(_retries, _rescue_off):
    _retries(-5)
    assert runner_media.video_attempt_models(KLING) == [KLING]


# --- 2. a refused clip really is re-submitted, unchanged ---------------------

def test_refused_clip_is_resubmitted_and_succeeds(
        monkeypatch, tmp_path, _retries, _rescue_off):
    _retries(2)
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    calls = []

    def fake_submit(**kw):
        calls.append((kw["model"], kw["movement_prompt"]))
        if len(calls) == 1:
            raise RuntimeError(_MODERATION)
        return "req-2"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert [m for m, _ in calls] == [KLING, KLING], "same model, second take"
    assert calls[0][1] == calls[1][1], "the prompt must be IDENTICAL"
    assert video.status == VideoStatus.DONE
    # A same-model take is NOT a fallback: a stray value here would paint a
    # false ⇄ chip and send salvage/resume polling the wrong endpoint.
    assert video.fallback_model is None
    assert video.fallback_dropped_end_frame is False


def test_recovered_clip_resumes_on_its_own_model(
        monkeypatch, tmp_path, _retries, _rescue_off):
    """fal request_ids are endpoint-scoped. Because a re-submit never sets
    `fallback_model`, _eff_video_model keeps resolving the original model."""
    _retries(2)
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    calls = []

    def fake_submit(**kw):
        calls.append(kw["model"])
        if len(calls) == 1:
            raise RuntimeError(_MODERATION)
        return "req-2"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert runner._eff_video_model(job, jc, video) == KLING


def test_veo_empty_output_is_also_resubmitted(
        monkeypatch, tmp_path, _retries, _rescue_off, _speaks):
    """Veo's silent refusal shape counts too — it is a refusal on the Veo leg."""
    _retries(2)
    _speaks("de")                       # German: no rescue, so takes only
    job, jc, video = _job_one_clip(tmp_path, video_model=VEO)
    _stub(monkeypatch, tmp_path)
    calls = []

    def fake_submit(**kw):
        calls.append(kw["model"])
        if len(calls) == 1:
            raise RuntimeError(_NO_MEDIA)
        return "req-2"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "sagt den Satz"))

    assert calls == [VEO, VEO]
    assert video.status == VideoStatus.DONE


# --- 3. the takes precede the rescue in the real runner ---------------------

def test_spanish_veo_exhausts_its_takes_before_kling(
        monkeypatch, tmp_path, _retries, _rescue_off, _speaks):
    _retries(2)
    _speaks("es")
    job, jc, video = _job_one_clip(tmp_path, video_model=VEO)
    _stub(monkeypatch, tmp_path)
    calls = []

    def fake_submit(**kw):
        calls.append(kw["model"])
        if kw["model"] == VEO:
            raise RuntimeError(_MODERATION)
        return "req-k"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "dice la frase"))

    assert calls == [VEO, VEO, VEO, KLING], "three Veo takes, then the rescue"
    assert video.status == VideoStatus.DONE
    assert video.fallback_model == KLING     # only the LAST leg is a fallback


# --- 4. only genuine refusals are re-submitted ------------------------------

@pytest.mark.parametrize("err,label", [(_TIMEOUT, "timeout"),
                                       (_BALANCE, "locked fal account")])
def test_non_refusals_are_never_resubmitted(
        monkeypatch, tmp_path, _retries, _rescue_off, err, label):
    """Re-submitting these would waste time and, for a locked account, hammer
    an endpoint that cannot succeed. They keep the first-try loud failure."""
    _retries(2)
    assert not runner_media.triggers_fallback(KLING, RuntimeError(err)), label
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    calls = []

    def fake_submit(**kw):
        calls.append(kw["model"])
        raise RuntimeError(err)
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert calls == [KLING], f"{label} must not be re-submitted"
    assert video.status == VideoStatus.ERROR


def test_balance_error_is_not_mistaken_for_a_refusal():
    """Guard the detector itself — a locked account says nothing about content."""
    assert not content_policy.is_content_rejection(RuntimeError(_BALANCE))


# --- 5. exhausting the takes fails LOUDLY, naming the count -----------------

def test_exhausted_takes_name_the_count(
        monkeypatch, tmp_path, _retries, _rescue_off, _speaks):
    """A clip refused on every take is a hard block on the INPUT (the start
    frame / the scene), not one unlucky call — the message has to say so, or
    the user re-clicks ↻ forever instead of changing the frame."""
    _retries(2)
    _speaks("de")                        # German: no rescue to mask the failure
    job, jc, video = _job_one_clip(tmp_path, video_model=VEO)
    _stub(monkeypatch, tmp_path)
    calls = []

    def fake_submit(**kw):
        calls.append(kw["model"])
        raise RuntimeError(_MODERATION)
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)

    _run(runner._animate_one_video(job, jc, video, "sagt den Satz"))

    assert calls == [VEO, VEO, VEO]
    assert video.status == VideoStatus.ERROR
    assert video.fallback_model is None
    err = video.error or ""
    assert "3 gånger" in err and VEO in err
    assert "nsfw" in err.lower(), "the provider's real reason must survive"


def test_single_take_keeps_the_plain_error(
        monkeypatch, tmp_path, _retries, _rescue_off):
    """With re-submits off the message must not grow a misleading '1 gånger'."""
    _retries(0)
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    monkeypatch.setattr(
        runner.pipeline, "submit_video",
        lambda **kw: (_ for _ in ()).throw(RuntimeError(_MODERATION)))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert "gånger" not in (video.error or "")
    assert "nsfw" in (video.error or "").lower()


# --- 6. the Reengineer direct / no-swap shared clip -------------------------

def test_direct_clip_is_resubmitted_unchanged(
        monkeypatch, tmp_path, _retries, _rescue_off):
    _retries(2)
    from character_swap import runner_reengineer as rr

    img = tmp_path / "direct.png"; img.write_bytes(b"x")
    state = {"job_id": "jd", "language": "en",
             "scenes": [{"scene_id": "sc1", "is_direct": True,
                         "direct_image_path": str(img),
                         "motion_prompt": "she nods", "video_model": KLING}]}
    job = Job(job_id="jd", scene_id="sc1", scene_image_path=str(img),
              scene_ids=["sc1"], characters={}, video_audio=True)

    monkeypatch.setattr(rr.reengineer, "load_state", lambda re_id: state)
    monkeypatch.setattr(rr.reengineer, "reengineer_dir", lambda re_id: tmp_path)
    monkeypatch.setattr(rr, "store", lambda: type("S", (), {
        "get_job": staticmethod(lambda jid: job if jid == "jd" else None)})())

    async def _noop_persist(re_id, scene_id, **fields):
        _noop_persist.calls.append(fields)
    _noop_persist.calls = []
    monkeypatch.setattr(rr, "_persist_direct", _noop_persist)

    async def _noop_pub(*a, **k):
        return None
    monkeypatch.setattr(rr.events, "publish", _noop_pub)

    from character_swap import pipeline
    calls = []

    def fake_submit(**kw):
        calls.append((kw["model"], kw["movement_prompt"]))
        if len(calls) == 1:
            raise RuntimeError(_MODERATION)
        return "req-2"
    monkeypatch.setattr(pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(rr._render_direct_clip("re1", "sc1"))

    assert [m for m, _ in calls] == [KLING, KLING]
    assert calls[0][1] == calls[1][1], "prompt identical across takes"
    ok = [c for c in _noop_persist.calls if c.get("shared_clip_path")]
    assert ok and ok[-1].get("direct_error") is None


def test_direct_clip_exhausted_takes_name_the_count(
        monkeypatch, tmp_path, _retries, _rescue_off):
    _retries(2)
    from character_swap import runner_reengineer as rr

    img = tmp_path / "direct.png"; img.write_bytes(b"x")
    state = {"job_id": "jd", "language": "en",
             "scenes": [{"scene_id": "sc1", "is_direct": True,
                         "direct_image_path": str(img),
                         "motion_prompt": "she nods", "video_model": KLING}]}
    job = Job(job_id="jd", scene_id="sc1", scene_image_path=str(img),
              scene_ids=["sc1"], characters={}, video_audio=True)

    monkeypatch.setattr(rr.reengineer, "load_state", lambda re_id: state)
    monkeypatch.setattr(rr.reengineer, "reengineer_dir", lambda re_id: tmp_path)
    monkeypatch.setattr(rr, "store", lambda: type("S", (), {
        "get_job": staticmethod(lambda jid: job if jid == "jd" else None)})())

    async def _noop_persist(re_id, scene_id, **fields):
        _noop_persist.calls.append(fields)
    _noop_persist.calls = []
    monkeypatch.setattr(rr, "_persist_direct", _noop_persist)

    async def _noop_pub(*a, **k):
        return None
    monkeypatch.setattr(rr.events, "publish", _noop_pub)

    from character_swap import pipeline
    calls = []

    def fake_submit(**kw):
        calls.append(kw["model"])
        raise RuntimeError(_MODERATION)
    monkeypatch.setattr(pipeline, "submit_video", fake_submit)

    _run(rr._render_direct_clip("re1", "sc1"))

    assert calls == [KLING, KLING, KLING]
    err = (_noop_persist.calls[-1] or {}).get("direct_error") or ""
    assert "3 gånger" in err and "nsfw" in err.lower()
