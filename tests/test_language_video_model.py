"""🗣 language-flagged characters always render on the language video model.

Hugo 2026-08-03. Measured over j_2a4d1ff40e + j_b72c2c5536: Kling 3.0 scored
0.48 mean word-similarity on German against 1.00 English / 0.93 Spanish from
the same runs, and shipped plain non-words ("Kalber starke Vervantics in
Zucker"). The same two lines re-rendered from the same swapped image on
veo-3.1-fast scored 0.95 / 0.93. So a character with a 🗣 flag is forced onto
`runner_media.SPOKEN_LANGUAGE_VIDEO_MODEL` whatever model the run, the scene or
the regen-clip modal picked — and the move is recorded, never silent.
"""
from __future__ import annotations

import pytest

from character_swap import runner, runner_media
from character_swap.models import (
    CharacterAsset,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)

LANG_MODEL = runner_media.SPOKEN_LANGUAGE_VIDEO_MODEL


class _Store:
    """Library store double: char_id -> language flag."""

    def __init__(self, langs: dict[str, str | None]):
        self.langs = langs

    def get_character(self, cid):
        if cid not in self.langs:
            return None
        return CharacterAsset(char_id=cid, filename=f"{cid}.png", name=cid,
                              language=self.langs[cid])


def _job(**kw) -> Job:
    return Job(job_id="j1", scene_id="scA", scene_image_path="/tmp/s.png",
               scene_ids=["scA"], prompt="(p)", video_model="kling-v3", **kw)


def _char(char_id="ch_de") -> JobCharacter:
    return JobCharacter(
        char_id=char_id, name=char_id, source_image_path="/tmp/c.png",
        images=[GeneratedImage(variant_id="v_a", path="/tmp/a.png", prompt="(p)",
                               scene_id="scA", status=VariantStatus.READY)],
        approved_variant_ids=["v_a"])


def _video(**kw) -> VideoVariant:
    return VideoVariant(video_id="vd_1", grok_job_id="req_1",
                        status=VideoStatus.PENDING, source_variant_id="v_a", **kw)


@pytest.fixture
def store(monkeypatch):
    def _install(langs):
        monkeypatch.setattr(runner, "store", lambda: _Store(langs))
    return _install


# ---------------------------------------------------------------- redirect ---

@pytest.mark.parametrize("lang", ["de", "es"])
def test_language_flagged_character_overrides_the_job_model(store, lang):
    store({"ch_de": lang})
    job, jc = _job(), _char()
    assert job.video_model == "kling-v3"
    assert runner._eff_video_model_for_variant(job, jc, "v_a") == LANG_MODEL


def test_english_character_keeps_the_picked_model(store):
    store({"ch_de": None})
    assert runner._eff_video_model_for_variant(_job(), _char(), "v_a") == "kling-v3"


def test_character_missing_from_library_keeps_the_picked_model(store):
    # A job snapshot can outlive its library row; that must not silently
    # reroute an English clip.
    store({})
    assert runner._eff_video_model_for_variant(_job(), _char(), "v_a") == "kling-v3"


def test_redirect_beats_per_scene_and_per_clip_overrides(store):
    # "oavsett vilken modell jag har angett" — the explicit per-clip pick from
    # the regen-clip modal is the strongest user signal, and it still loses.
    store({"ch_de": "de"})
    job = _job(video_models_by_scene={"scA": "grok-imagine-1.5"},
               video_models_by_variant={"v_a": "seedance-2.0"})
    assert runner._eff_video_model_for_variant(job, _char(), "v_a") == LANG_MODEL


def test_moderation_fallback_still_wins_over_the_redirect(store):
    # A clip that already fell back must be POLLED on the fallback endpoint —
    # fal request_ids are endpoint-scoped, so resolving it back to the language
    # model would 404 the salvage re-poll.
    store({"ch_de": "de"})
    video = _video(fallback_model=runner_media.VIDEO_MODERATION_FALLBACK_MODEL)
    assert (runner._eff_video_model(_job(), _char(), video)
            == runner_media.VIDEO_MODERATION_FALLBACK_MODEL)


def test_recorded_redirect_survives_clearing_the_language_flag(store):
    # Same endpoint-scoping hazard: un-flagging the character after submit must
    # not make resume poll the ORIGINAL model with a redirect-model job id.
    store({"ch_de": None})
    video = _video(language_model_redirect="kling-v3")
    assert runner._eff_video_model(_job(), _char(), video) == LANG_MODEL


# ---------------------------------------------------------------- duration ---

@pytest.mark.parametrize("asked,expect", [
    (3, 4), (4, 4), (5, 6), (6, 6), (7, 8), (8, 8),   # round UP, never shorter
    (9, 8), (10, 8), (15, 8),                          # above the ceiling -> cap
])
def test_clip_length_rounds_up_and_caps(asked, expect):
    assert runner_media.language_clip_secs(asked) == expect


def test_only_over_ceiling_counts_as_truncated():
    assert not runner_media.language_clip_truncated(5)   # 5 -> 6 is longer
    assert not runner_media.language_clip_truncated(8)
    assert runner_media.language_clip_truncated(9)
    assert runner_media.language_clip_truncated(10)


def test_no_length_asked_uses_the_model_default():
    spec = runner_media.VIDEO_MODELS[LANG_MODEL]
    assert runner_media.language_clip_secs(None) == spec["duration_default"]
    assert not runner_media.language_clip_truncated(None)


def test_snapped_length_is_always_offered_by_the_model():
    opts = runner_media.VIDEO_MODELS[LANG_MODEL]["duration_options"]
    for asked in range(1, 20):
        assert runner_media.language_clip_secs(asked) in opts


# ------------------------------------------------------------- consistency ---

def test_redirect_model_keeps_end_frames():
    # The redirect must not cost a 🎯 end pose the way the moderation fallback
    # does — that would trade one silent degradation for another.
    assert runner_media.supports_end_frame(LANG_MODEL)


def test_registry_and_frontend_agree_on_the_slug():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text("utf-8")
    # The frontend reads the slug from the models payload — never hardcoded.
    assert "this.models.language_video_model" in js
    assert f'"{LANG_MODEL}"' not in js and f"'{LANG_MODEL}'" not in js


# --------------------------------------------------------------- end-to-end ---
# The resolver being right is not the feature; what SUBMITS is. These drive the
# real _animate_one_video and assert on the provider call it makes.

def _e2e(monkeypatch, tmp_path, *, language, picked="kling-v3"):
    """Hermetic one-clip animate: no persistence/events/QC, no localizer call."""
    import asyncio  # noqa: F401 — used by the caller via _run

    img = GeneratedImage(variant_id="v_a", path=str(tmp_path / "v_a.png"),
                         prompt="(p)", scene_id="scA", status=VariantStatus.READY)
    tmp_path.joinpath("v_a.png").write_bytes(b"img")
    jc = JobCharacter(char_id="ch_x", name="X",
                      source_image_path=str(tmp_path / "c.png"),
                      images=[img], approved_variant_ids=["v_a"],
                      approved_variant_id="v_a")
    tmp_path.joinpath("c.png").write_bytes(b"c")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    job = Job(job_id="j1", scene_id="scA", scene_image_path=str(scene),
              scene_ids=["scA"], prompt="(p)", video_model=picked,
              characters={"ch_x": jc})
    video = _video()
    jc.videos = [video]

    monkeypatch.setattr(runner, "store", lambda: _Store({"ch_x": language}))
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_output_dir", lambda *a: tmp_path)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(type(runner.settings), "video_qc_enabled",
                        property(lambda self: False), raising=False)
    monkeypatch.setattr(runner.video_qc, "inspect_clip", lambda *a, **k: None)
    # The localizer is a billed Claude call; the redirect is orthogonal to it.
    monkeypatch.setattr(runner.reengineer, "localize_motion_prompt",
                        lambda p, *a, **k: p)

    calls = []
    monkeypatch.setattr(runner.pipeline, "submit_video",
                        lambda **kw: (calls.append(kw), "req-1")[1])
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: kw["dest"].write_bytes(b"clip"))
    return job, jc, video, calls


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_german_clip_submits_on_the_language_model(monkeypatch, tmp_path):
    job, jc, video, calls = _e2e(monkeypatch, tmp_path, language="de")
    _run(runner._animate_one_video(job, jc, video, "she says: hallo",
                                   duration_secs=5))
    assert [c["model"] for c in calls] == [LANG_MODEL]
    assert calls[0]["duration_secs"] == 6          # 5 rounded UP, never cut
    assert video.language_model_redirect == "kling-v3"
    assert video.language_secs_from == 5
    assert video.language_secs_truncated is False


def test_english_clip_is_untouched(monkeypatch, tmp_path):
    job, jc, video, calls = _e2e(monkeypatch, tmp_path, language=None)
    _run(runner._animate_one_video(job, jc, video, "he waves", duration_secs=7))
    assert [c["model"] for c in calls] == ["kling-v3"]
    assert calls[0]["duration_secs"] == 7          # length untouched
    assert video.language_model_redirect is None
    assert video.language_secs_from is None


def test_over_ceiling_clip_is_flagged_as_shortened(monkeypatch, tmp_path):
    job, jc, video, calls = _e2e(monkeypatch, tmp_path, language="de")
    _run(runner._animate_one_video(job, jc, video, "she talks", duration_secs=9))
    assert calls[0]["duration_secs"] == 8
    assert video.language_secs_from == 9
    assert video.language_secs_truncated is True   # surfaced, never silent


def test_no_redirect_chip_when_the_run_already_picked_that_model(monkeypatch, tmp_path):
    # Nothing was overridden, so there is nothing to tell the user about.
    job, jc, video, calls = _e2e(monkeypatch, tmp_path, language="es",
                                 picked=LANG_MODEL)
    _run(runner._animate_one_video(job, jc, video, "ella habla", duration_secs=4))
    assert [c["model"] for c in calls] == [LANG_MODEL]
    assert video.language_model_redirect is None
