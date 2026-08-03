"""Content-policy / NSFW video fallback → Grok Imagine 1.5 (Hugo 2026-07-14;
fallback model switched from seedance-2.0 to grok-imagine-1.5 on 2026-07-26).

OPT-IN since 2026-08-03 (Hugo: "ta bort fallbacken till en annan modell om ett
klipp failar") — every test below therefore turns `VIDEO_MODERATION_FALLBACK`
ON via the autouse fixture. The DEFAULT behaviour, a refused clip failing
loudly on its own model, is locked at the bottom of this file.

When a clip's chosen video model refuses it on moderation grounds, the runner
retries the SAME clip ONCE on `grok-imagine-1.5` (a different, markedly more
permissive provider stack). Only
genuine content-policy rejections trigger it (detected by
content_policy.is_content_rejection — NOT mocked here, so the real substring
detector is exercised); timeouts / network / billing keep the normal fail path.
If the fallback ALSO rejects, the clip fails LOUDLY. Covers Swap + Reengineer
per-character clips (runner._animate_one_video) and the Reengineer "direct /
no-swap" shared clip (runner_reengineer._render_direct_clip).
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

FALLBACK = "grok-imagine-1.5"
_MODERATION = "fal submit failed: 400 rejected — flagged as nsfw content"
_TIMEOUT = "Video job req-1 timed out after 600s"


# --- fixtures ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fallback_on(monkeypatch):
    """The rescue is opt-in now; these tests describe it switched ON."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: True), raising=False)


def _job_one_clip(tmp_path, *, video_model="kling-v3"):
    """One character, one approved image, one PENDING video clip."""
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
    """Hermetic runner: no persistence/events/QC; QC OFF so 1 take per model."""
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


# --- sanity: the constant + detector line up --------------------------------

def test_fallback_model_constant_is_grok15():
    assert runner_media.VIDEO_MODERATION_FALLBACK_MODEL == FALLBACK
    # The fake exceptions really do / don't look like moderation to the detector.
    assert content_policy.is_content_rejection(RuntimeError(_MODERATION))
    assert not content_policy.is_content_rejection(RuntimeError(_TIMEOUT))


# --- 0b. resume/salvage of a fallen-back clip polls the fallback, not the original

def test_eff_video_model_prefers_fallback_for_resume(tmp_path):
    """salvage re-poll and post-restart resume both resolve the poll provider
    via _eff_video_model — a clip that fell back to the fallback must resolve to
    the fallback (fal request_ids are endpoint-scoped, so polling kling with a
    fallback job id 404s). A fresh clip still resolves to the scene model."""
    job, jc, video = _job_one_clip(tmp_path, video_model="kling-v3")
    assert runner._eff_video_model(job, jc, video) == "kling-v3"   # fresh clip
    video.fallback_model = FALLBACK
    assert runner._eff_video_model(job, jc, video) == FALLBACK      # after fallback


# --- 1. moderation rejection → retry on the fallback, succeed --------------------

def test_moderation_failure_retries_on_fallback(monkeypatch, tmp_path):
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        if len(submits) == 1:               # first (kling) submit is refused
            raise RuntimeError(_MODERATION)
        return f"req-{len(submits)}"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert submits == ["kling-v3", FALLBACK]     # fell back
    assert video.status == VideoStatus.DONE
    assert video.fallback_model == FALLBACK
    assert video.error is None


# --- 2. non-moderation failure → NO fallback, fails as before ----------------

def test_timeout_failure_does_not_fall_back(monkeypatch, tmp_path):
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        raise RuntimeError(_TIMEOUT)         # not a content rejection
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert submits == ["kling-v3"]               # never retried on the fallback
    assert video.status == VideoStatus.ERROR
    assert video.fallback_model is None
    assert "submit:" in (video.error or "")


# --- 3. the fallback ALSO rejects → loud failure, no infinite loop ---------------

def test_fallback_also_rejected_fails_loudly(monkeypatch, tmp_path):
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        raise RuntimeError(_MODERATION)      # both models refuse
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert submits == ["kling-v3", FALLBACK]     # tried each exactly once
    assert video.status == VideoStatus.ERROR
    assert video.fallback_model == FALLBACK      # recorded that we tried it
    assert "reservmodellen" in (video.error or "") and FALLBACK in (video.error or "")


# --- 3b. the fallback fails for a NON-content reason → real reason, not a false
#         content block (review 2026-07-14) -----------------------------------

def test_fallback_leg_non_content_failure_reports_real_reason(monkeypatch, tmp_path):
    """Kling content-rejects → fall back to the fallback → the fallback TIMES OUT (not a
    content block). The error must NOT claim 'content-policy … nekades också'
    (that would push Hugo to reword a fine prompt); it must name the real cause."""
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        if len(submits) == 1:
            raise RuntimeError(_MODERATION)      # kling content-rejects
        return f"req-{len(submits)}"             # fallback submit OK…
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)

    def fake_wait(**kw):
        raise RuntimeError(_TIMEOUT)             # …but the fallback wait times out
    monkeypatch.setattr(runner.pipeline, "wait_for_video", fake_wait)

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert submits == ["kling-v3", FALLBACK]
    assert video.status == VideoStatus.ERROR
    assert video.fallback_model == FALLBACK           # we did fall back
    err = (video.error or "").lower()
    assert "content-policy" not in err and "nekades" not in err   # no false block
    assert "timed out" in err                          # the REAL reason survives


# --- 4. already-the fallback → no self-fallback ---------------------------------

def test_already_on_fallback_no_self_fallback(monkeypatch, tmp_path):
    job, jc, video = _job_one_clip(tmp_path, video_model=FALLBACK)
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        raise RuntimeError(_MODERATION)
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert submits == [FALLBACK]                 # exactly one attempt, no re-try
    assert video.status == VideoStatus.ERROR
    assert video.fallback_model is None
    # No fallback was recorded → plain error message, not the "reservmodellen" one.
    assert "reservmodellen" not in (video.error or "")


# --- 5. fallback take drops the QC-retry hint (clean prompt) -----------------

def test_fallback_take_uses_clean_prompt(monkeypatch, tmp_path):
    """A moderation rejection on the FIRST attempt hands the fallback the original
    prompt, never a QC-hint-appended one (guards the prompt_text reset)."""
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    seen = []

    def fake_submit(**kw):
        seen.append((kw["model"], kw["movement_prompt"]))
        if len(seen) == 1:
            raise RuntimeError(_MODERATION)
        return "req-2"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert seen[1] == (FALLBACK, "he waves")      # clean prompt on the retry


# --- 6. Reengineer direct (no-swap) clip gets the same fallback --------------

def test_direct_clip_moderation_fallback(monkeypatch, tmp_path):
    from character_swap import runner_reengineer as rr

    img = tmp_path / "direct.png"; img.write_bytes(b"x")
    state = {"job_id": "jd", "language": "en",
             "scenes": [{"scene_id": "sc1", "is_direct": True,
                         "direct_image_path": str(img),
                         "motion_prompt": "she nods", "video_model": "kling-v3"}]}
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
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        if len(submits) == 1:
            raise RuntimeError(_MODERATION)
        return "req-2"
    monkeypatch.setattr(pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(rr._render_direct_clip("re1", "sc1"))

    assert submits == ["kling-v3", FALLBACK]     # direct clip fell back too
    # Final persist recorded a successful shared clip (no direct_error).
    ok = [c for c in _noop_persist.calls if c.get("shared_clip_path")]
    assert ok and ok[-1].get("direct_error") is None


# --- 8. END FRAMES: the fallback has no end-frame input ----------------------
#
# grok-imagine-1.5 cannot interpolate start→end, so a clip that falls back LOSES
# its 🎯 end pose. Hugo 2026-07-26: fall back anyway (a clip without the end pose
# beats no clip) but record the loss so the UI can say it out loud — never
# silently ship a differently-composed clip.

def test_fallback_drops_end_frame_helper():
    """The capability helper is the single source of truth for the degradation:
    true only when the CHOSEN model honors end frames and the fallback doesn't."""
    assert not runner_media.supports_end_frame(FALLBACK)
    # kling-v3 / seedance-2.0 / veo-3.1-fast honor end frames → falling back loses it.
    assert runner_media.fallback_drops_end_frame("kling-v3")
    assert runner_media.fallback_drops_end_frame("veo-3.1-fast")
    # A model that never honored the pose loses nothing.
    assert not runner_media.fallback_drops_end_frame("grok-imagine")
    assert not runner_media.fallback_drops_end_frame(FALLBACK)


def test_fallback_with_end_frame_flags_dropped_pose(monkeypatch, tmp_path):
    """Kling clip WITH a resolved end frame → content-refused → renders on the
    fallback, and the lost end pose is flagged (not silent)."""
    job, jc, video = _job_one_clip(tmp_path, video_model="kling-v3")
    _stub(monkeypatch, tmp_path)
    end_img = tmp_path / "end.png"; end_img.write_bytes(b"end")
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        if len(submits) == 1:
            raise RuntimeError(_MODERATION)
        return f"req-{len(submits)}"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves",
                                   end_image=end_img))

    assert submits == ["kling-v3", FALLBACK]
    assert video.status == VideoStatus.DONE          # clip still delivered
    assert video.fallback_model == FALLBACK
    assert video.fallback_dropped_end_frame is True  # …and the loss is VISIBLE


def test_fallback_without_end_frame_does_not_flag(monkeypatch, tmp_path):
    """No end frame resolved → nothing was lost → no misleading flag."""
    job, jc, video = _job_one_clip(tmp_path, video_model="kling-v3")
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        if len(submits) == 1:
            raise RuntimeError(_MODERATION)
        return f"req-{len(submits)}"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert video.fallback_model == FALLBACK
    assert video.fallback_dropped_end_frame is False


def test_end_frame_model_without_end_frame_support_not_flagged(monkeypatch, tmp_path):
    """A scene on a model that ALREADY ignored the end pose (grok-imagine) loses
    nothing by falling back — flagging it would be a false alarm."""
    job, jc, video = _job_one_clip(tmp_path, video_model="grok-imagine")
    _stub(monkeypatch, tmp_path)
    end_img = tmp_path / "end.png"; end_img.write_bytes(b"end")
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        if len(submits) == 1:
            raise RuntimeError(_MODERATION)
        return f"req-{len(submits)}"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves",
                                   end_image=end_img))

    assert submits == ["grok-imagine", FALLBACK]
    assert video.fallback_dropped_end_frame is False


def test_dropped_end_frame_flag_is_serialized(tmp_path):
    """The flag must reach the browser — the ⇄ chip's "slutposen tappad" note
    reads it straight off the job payload."""
    from character_swap import api
    job, jc, video = _job_one_clip(tmp_path, video_model="kling-v3")
    video.status = VideoStatus.DONE
    video.final_video_path = str(tmp_path / "clip.mp4")
    Path(video.final_video_path).write_bytes(b"clip")
    video.fallback_model = FALLBACK
    video.fallback_dropped_end_frame = True

    payload = api._job_to_dict(job)
    vv = payload["characters"]["cA"]["videos"][0]
    assert vv["fallback_model"] == FALLBACK
    assert vv["fallback_dropped_end_frame"] is True


# --- DEFAULT (rescue disabled): a refused clip fails on its own model --------

@pytest.fixture
def _fallback_off(monkeypatch):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: False), raising=False)


def test_default_is_off(monkeypatch):
    """Ships disabled: the resolver returns nothing to fall back to."""
    from character_swap.config import settings
    monkeypatch.delenv("VIDEO_MODERATION_FALLBACK", raising=False)
    assert type(settings).model_fields["video_moderation_fallback"].default is False
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: False), raising=False)
    assert runner_media.video_fallback_model() is None
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: True), raising=False)
    assert runner_media.video_fallback_model() == FALLBACK


def test_refused_clip_fails_loudly_instead_of_switching_model(
        monkeypatch, tmp_path, _fallback_off):
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        raise RuntimeError(_MODERATION)
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "he waves"))

    assert submits == ["kling-v3"], "no second provider may be tried"
    assert video.status == VideoStatus.ERROR
    assert video.fallback_model is None
    # The REAL reason survives, so the user can reword rather than guess.
    assert "nsfw" in (video.error or "").lower()


def test_end_pose_is_never_dropped_when_the_rescue_is_off(
        monkeypatch, tmp_path, _fallback_off):
    """The dropped-end-frame flag existed only to make the fallback's loss
    visible; with no fallback there is no loss to report."""
    job, jc, video = _job_one_clip(tmp_path)
    _stub(monkeypatch, tmp_path)
    monkeypatch.setattr(runner.pipeline, "submit_video",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError(_MODERATION)))
    end = tmp_path / "end.png"; end.write_bytes(b"end")

    _run(runner._animate_one_video(job, jc, video, "he waves", end_image=end))

    assert video.fallback_dropped_end_frame is False
