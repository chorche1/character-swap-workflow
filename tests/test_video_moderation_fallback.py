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
    """The rescue is opt-in now; these tests describe it switched ON.

    Also pins VIDEO_REFUSAL_RETRIES to 0 so every test below sees exactly ONE
    take per model. In production a refused clip is re-submitted unchanged on
    its own model FIRST (Hugo 2026-08-06) and only then handed to the rescue —
    that ordering is locked in test_video_refusal_retry.py; this file is about
    what happens once the chosen model has run out of takes.
    """
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(settings), "video_refusal_retries",
                        property(lambda self: 0), raising=False)


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
    true only when the CHOSEN model honors end frames and the fallback IT would
    actually land on does not."""
    assert not runner_media.supports_end_frame(FALLBACK)
    # kling-v3 honors end frames and falls back to grok → the pose is lost.
    assert runner_media.fallback_drops_end_frame("kling-v3")
    # A SPANISH veo-3.1-fast clip falls back to kling-v3 instead (Hugo
    # 2026-08-04), which DOES interpolate start→end — so nothing is lost and
    # nothing is flagged. Half the reason Kling beat grok for this rescue.
    assert not runner_media.fallback_drops_end_frame("veo-3.1-fast",
                                                     language="es")
    # …but a German/English Veo clip has no Veo rescue, so it resolves against
    # the generic grok fallback and the pose WOULD be lost there.
    assert runner_media.fallback_drops_end_frame("veo-3.1-fast", language="de")
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


# --- VEO + SPANISH → KLING 3.0 (Hugo 2026-08-04, ALWAYS ON) -----------------
#
# "Gör så att alla veo klipp som failar fallbackar till seedance 2.0 fast."
#
# Seedance turned out to be IMPOSSIBLE and Kling 3.0 replaced it — see
# `test_the_rescue_is_not_seedance` at the bottom for the measurement, and read
# it before ever pointing this rescue at Seedance again.
#
# Veo is where the 🗣 redirect sends every Spanish clip, and fal's checker
# refuses them in bulk even at the least strict safety_tolerance — 11 of 24
# redirected Spanish clips in j_619e0a2cf2. Those clips get their own rescue,
# independent of VIDEO_MODERATION_FALLBACK. GERMAN is deliberately excluded
# (Kling scores 0.48 on it); every OTHER model keeps the 2026-08-03 default.

VEO = "veo-3.1-fast"
VEO_FALLBACK = "kling-v3"


@pytest.fixture
def _speaks(monkeypatch):
    """Give the character a 🗣 flag, which is what forces its clips onto Veo in
    the first place. Returns a setter so each test picks es / de / None.

    The localizer is stubbed to a no-op: these tests are about WHICH model a
    refused clip lands on, and a real translate call would need a live key.
    """
    def _set(code):
        monkeypatch.setattr(runner, "_character_language", lambda cid: code)
        monkeypatch.setattr(runner.reengineer, "localize_motion_prompt",
                            lambda prompt, *a, **k: prompt)
        # The rewrite cache is keyed by job — clear this file's job so a
        # neighbouring test's cached prompt can never leak in.
        runner._clear_prompt_cache("j1")
    return _set


def test_veo_rescue_constants():
    assert runner_media.VEO_MODERATION_FALLBACK_MODEL == VEO_FALLBACK
    assert VEO in runner_media.VEO_VIDEO_MODELS
    # The rescue target must be a REAL registered model, or submit would 422.
    assert VEO_FALLBACK in runner_media.VIDEO_MODELS
    # …and end-frame capable, which is half the reason it beat grok.
    assert runner_media.supports_end_frame(VEO_FALLBACK)
    # Spanish only — German is measured 0.48 on Kling and must NOT be rescued.
    assert runner_media.VEO_FALLBACK_LANGUAGES == frozenset({"es"})


def test_veo_resolver_is_language_scoped(monkeypatch):
    """The Veo rescue is NOT gated on VIDEO_MODERATION_FALLBACK, but it IS
    gated on the language: Spanish resolves, German does not."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: False), raising=False)
    assert runner_media.video_fallback_model(VEO, language="es") == VEO_FALLBACK
    assert runner_media.video_fallback_model(VEO, language="de") is None
    assert runner_media.video_fallback_model(VEO) is None       # English
    assert runner_media.video_fallback_model("kling-v3", language="es") is None
    # Flag ON: everything else gets the old generic rescue; Spanish Veo keeps
    # Kling rather than being dragged onto grok.
    monkeypatch.setattr(type(settings), "video_moderation_fallback",
                        property(lambda self: True), raising=False)
    assert runner_media.video_fallback_model(VEO, language="es") == VEO_FALLBACK
    assert runner_media.video_fallback_model(VEO, language="de") == FALLBACK
    assert runner_media.video_fallback_model("kling-v3") == FALLBACK


def test_refused_spanish_veo_clip_renders_on_kling(
        monkeypatch, tmp_path, _fallback_off, _speaks):
    """The case from j_619e0a2cf2: fal refuses the Spanish Veo clip on content
    policy → it comes back rendered on Kling 3.0 instead of dying."""
    _speaks("es")
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

    _run(runner._animate_one_video(job, jc, video, "dice la frase"))

    # The 🗣 redirect put it on Veo; the rescue moved it to Kling.
    assert submits == [VEO, VEO_FALLBACK]
    assert video.status == VideoStatus.DONE
    assert video.fallback_model == VEO_FALLBACK
    assert video.error is None


def test_refused_german_veo_clip_fails_loudly(
        monkeypatch, tmp_path, _fallback_off, _speaks):
    """Hugo 2026-08-04: rescue Spanish only. Kling is measured 0.48 on German —
    rescuing it there would ship a clip the language net has to reject anyway,
    so a refused German clip fails with the real reason instead."""
    _speaks("de")
    job, jc, video = _job_one_clip(tmp_path, video_model="kling-v3")
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        raise RuntimeError(_MODERATION)
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)

    _run(runner._animate_one_video(job, jc, video, "sagt den Satz"))

    assert submits == [VEO], "a German clip must never be rescued onto Kling"
    assert video.status == VideoStatus.ERROR
    assert video.fallback_model is None
    assert "nsfw" in (video.error or "").lower()


def test_veo_rescue_keeps_the_end_pose(
        monkeypatch, tmp_path, _fallback_off, _speaks):
    """Unlike the grok rescue, this one costs no 🎯 end pose — the end frame is
    forwarded to Kling and nothing is flagged as dropped."""
    _speaks("es")
    job, jc, video = _job_one_clip(tmp_path, video_model="kling-v3")
    _stub(monkeypatch, tmp_path)
    end = tmp_path / "end.png"; end.write_bytes(b"end")
    seen = []

    def fake_submit(**kw):
        seen.append((kw["model"], kw.get("end_image")))
        if len(seen) == 1:
            raise RuntimeError(_MODERATION)
        return "req-2"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "dice la frase",
                                   end_image=end))

    assert seen[1] == (VEO_FALLBACK, end), "end frame must reach the rescue"
    assert video.fallback_dropped_end_frame is False
    assert video.status == VideoStatus.DONE


def test_veo_non_content_failure_still_fails_loudly(
        monkeypatch, tmp_path, _fallback_off, _speaks):
    """A timeout is not a content block — no second submit, real reason kept."""
    _speaks("es")
    job, jc, video = _job_one_clip(tmp_path, video_model="kling-v3")
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        raise RuntimeError(_TIMEOUT)
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)

    _run(runner._animate_one_video(job, jc, video, "dice la frase"))

    assert submits == [VEO], "only refusals may trigger the rescue"
    assert video.status == VideoStatus.ERROR
    assert video.fallback_model is None
    assert "timed out" in (video.error or "")


def test_veo_rescue_also_refused_fails_naming_both(
        monkeypatch, tmp_path, _fallback_off, _speaks):
    """Both providers blocked it → fail LOUDLY naming the rescue, never a
    half-rendered clip."""
    _speaks("es")
    job, jc, video = _job_one_clip(tmp_path, video_model="kling-v3")
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        raise RuntimeError(_MODERATION)
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)

    _run(runner._animate_one_video(job, jc, video, "dice la frase"))

    assert submits == [VEO, VEO_FALLBACK]
    assert video.status == VideoStatus.ERROR
    assert VEO_FALLBACK in (video.error or "")


def test_rescued_veo_clip_resumes_on_kling(tmp_path):
    """fal request_ids are endpoint-scoped: a rescued clip must re-poll Kling's
    endpoint, not Veo's — and `fallback_model` has to outrank the 🗣 redirect
    record, or resume would send it back to Veo with a Kling request_id."""
    job, jc, video = _job_one_clip(tmp_path, video_model=VEO)
    video.language_model_redirect = "kling-v3"
    assert runner._eff_video_model(job, jc, video) == VEO
    video.fallback_model = VEO_FALLBACK
    assert runner._eff_video_model(job, jc, video) == VEO_FALLBACK


# --- Veo's SECOND refusal shape: fal `no_media_generated` -------------------
#
# Veo does not always say content_policy_violation. Sometimes it accepts the
# submit, runs, and returns nothing — fal reports "The model did not generate
# the expected output for this prompt … including unsafe content". 4 of the 11
# refused Spanish clips in j_619e0a2cf2 failed this way, matched NO signal in
# content_policy, and died with no rescue. Treated as a refusal on the Veo leg
# only (see runner_media.VEO_EMPTY_OUTPUT_SIGNALS).

_NO_MEDIA = (
    "[{'loc': ['body'], 'msg': 'The model did not generate the expected output "
    "for this prompt. This may occur for several reasons, including unsafe "
    "content, a prompt that is incompatible with the selected media type…', "
    "'type': 'no_media_generated'}]"
)


def test_no_media_generated_is_not_a_global_content_rejection():
    """It must NOT enter content_policy's shared signal list — that detector
    also drives the image ladder and the generic video rescue, and this is a
    fal-wide catch-all whose other causes are real bugs."""
    assert not content_policy.is_content_rejection(RuntimeError(_NO_MEDIA))


def test_triggers_fallback_is_model_scoped():
    # Veo: both refusal shapes rescue.
    assert runner_media.triggers_fallback(VEO, RuntimeError(_MODERATION))
    assert runner_media.triggers_fallback(VEO, RuntimeError(_NO_MEDIA))
    # …but a Veo timeout is still just a timeout.
    assert not runner_media.triggers_fallback(VEO, RuntimeError(_TIMEOUT))
    # Non-Veo models: only a genuine content rejection counts, empty output
    # keeps failing loudly wherever it means something else.
    assert runner_media.triggers_fallback("kling-v3", RuntimeError(_MODERATION))
    assert not runner_media.triggers_fallback("kling-v3", RuntimeError(_NO_MEDIA))


def test_empty_output_spanish_veo_clip_is_rescued(
        monkeypatch, tmp_path, _fallback_off, _speaks):
    _speaks("es")
    job, jc, video = _job_one_clip(tmp_path, video_model="kling-v3")
    _stub(monkeypatch, tmp_path)
    submits = []

    def fake_submit(**kw):
        submits.append(kw["model"])
        if len(submits) == 1:
            raise RuntimeError(_NO_MEDIA)
        return "req-2"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))

    _run(runner._animate_one_video(job, jc, video, "dice la frase"))

    assert submits == [VEO, VEO_FALLBACK]
    assert video.status == VideoStatus.DONE
    assert video.fallback_model == VEO_FALLBACK


def test_the_rescue_is_not_seedance():
    """REGRESSION GUARD — do not point this rescue at Seedance again.

    Hugo asked for Seedance 2.0 Fast and it was built that way first. Measured
    2026-08-04, ByteDance refuses EVERY frame containing a real person: 11 of 11
    submits refused (7 in-app + 4 direct probes), on BOTH the fast and standard
    tiers, across three different faces, always

        loc: [body, image_url] · content_policy_violation
        "The images or videos provided may contain likenesses of real people…"
        ctx.extra_info.reason = partner_validation_failed

    A control frame with no person in it rendered fine on the same endpoint and
    key, so it is ByteDance's real-people policy — not our account, not the
    prompt. Every frame this app produces is a photoreal person, so Seedance
    can rescue exactly zero clips here and no prompt change can reach an image
    check. Re-run the probe before reconsidering.
    """
    assert "seedance" not in runner_media.VEO_MODERATION_FALLBACK_MODEL
    assert "seedance" not in runner_media.VIDEO_MODERATION_FALLBACK_MODEL
