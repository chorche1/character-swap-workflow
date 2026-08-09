"""Retrying a 🇪🇸/🇩🇪 clip must come back in that language (Hugo 2026-08-03).

The "Regenerate this clip" modal used to prefill the ENGLISH source prompt for
a language-flagged character, which read like the retry would come back
speaking English. It now prefills the LOCALIZED prompt — the text actually
submitted — and that opened a real hole:

    `localize_motion_prompt` returns a prompt UNCHANGED as soon as it carries
    the language marker ("in standard German"). With the German prompt in the
    box, a NEW ENGLISH line typed into it sails straight through that
    short-circuit and is spoken in English, silently.

So a prompt the USER typed (`VideoVariant.movement_prompt_override`) is always
re-localized (`force=True`). Two things must hold for that to be safe:

1. re-localizing an ALREADY-translated line must not reword it (the translator
   is told to return such a line verbatim), and
2. it must not arm the wrong-language QC check with a German "original" —
   that would compare German audio against a German source line and could fail
   a perfectly correct clip as "fel språk".
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import api, reengineer, runner, video_qc
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus, VideoStatus,
    VideoVariant,
)

DE = reengineer.SPOKEN_LANGUAGES["de"]

# What the regen modal now prefills: the localized prompt of the last take —
# the exact shape the runner submits (verbatim from j_17ae3faa13/Susanne).
GERMAN_PROMPT = (
    f'He says enthusiastically to the camera {DE.attribution}: '
    '"Nimm jeden Morgen einen Löffel auf nüchternen Magen." while he pours.'
    ' Every word is pronounced clearly, correctly and distinctly.'
    ' No background music — natural ambient room sound only.'
    f'{DE.accent_clause}{DE.speak_only_clause}')

# The same prompt after Hugo typed a NEW line — in English, as he writes them.
EDITED_EN_LINE = "Then take one spoonful every morning on an empty stomach."
EDITED_PROMPT = GERMAN_PROMPT.replace(
    "Nimm jeden Morgen einen Löffel auf nüchternen Magen.", EDITED_EN_LINE)


def _translator(monkeypatch, out: str):
    """Stub the billed GPT-4o translate call; record what it was asked for."""
    seen: list[list[str]] = []

    def fake(lines, *, language="es", re_id=None):
        seen.append(list(lines))
        return [out] * len(lines)
    monkeypatch.setattr(reengineer, "translate_dialogue", fake)
    return seen


@pytest.fixture(autouse=True)
def _clear_cache():
    reengineer._LOCALIZE_CACHE.clear()
    yield
    reengineer._LOCALIZE_CACHE.clear()


# --- 1. the localizer -------------------------------------------------------

def test_english_line_typed_into_a_german_prompt_is_translated(monkeypatch):
    """THE regression: the marker says "German", the LINE is English."""
    seen = _translator(monkeypatch, "ÜBERSETZT")

    out = reengineer.localize_motion_prompt(EDITED_PROMPT, "de", force=True)

    assert seen == [[EDITED_EN_LINE]]          # the new line WAS translated
    assert "ÜBERSETZT" in out
    assert EDITED_EN_LINE not in out
    # …and the three directive layers survive the re-localization intact.
    assert f"to the camera {DE.attribution}:" in out
    assert DE.accent_clause.strip() in out
    assert DE.speak_only_key in out.lower()


def test_without_force_the_marker_still_short_circuits(monkeypatch):
    """The default (initial-run) path is untouched: a prompt already carrying
    the marker is returned verbatim, with no billed translate call. This is
    what keeps a run-level 🗣 run's user-approved text from being re-written."""
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda *a, **k: pytest.fail("must not translate"))
    assert reengineer.localize_motion_prompt(EDITED_PROMPT, "de") == EDITED_PROMPT


def test_forcing_an_untouched_localized_prompt_is_a_no_op(monkeypatch):
    """"Same prompt = new random take" must not drift. The translator returns
    an already-German line unchanged (its system prompt says so), and the
    directive layers are idempotent — so the text comes back identical."""
    _translator(monkeypatch, "Nimm jeden Morgen einen Löffel auf nüchternen Magen.")
    # One-time addition: the directive layer also appends the ad-lib lock
    # (Hugo 2026-08-10) to a prompt that predates it.
    once = reengineer.localize_motion_prompt(GERMAN_PROMPT, "de", force=True)
    assert once == GERMAN_PROMPT + reengineer._NO_ADLIB_CLAUSE
    # The invariant under test — no DRIFT on repeat — holds from there on.
    assert reengineer.localize_motion_prompt(once, "de", force=True) == once


def test_translator_is_told_to_return_an_already_translated_line_verbatim():
    """The no-op above is only safe because the translator is instructed to
    leave a line that is already in the target language alone."""
    for spec in reengineer.SPOKEN_LANGUAGES.values():
        assert "already in" in spec.translate_system.lower()
        assert "exactly as given" in spec.translate_system.lower()


def test_force_still_leaves_a_silent_clip_alone(monkeypatch):
    """force only overrides the MARKER check — never the "no dialogue, no
    speech directive" rule."""
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda *a, **k: pytest.fail("must not translate"))
    silent = "She walks across the room and picks up the box. Still camera."
    assert reengineer.localize_motion_prompt(silent, "de", force=True) == silent


def test_force_still_fails_loudly_when_translation_fails(monkeypatch):
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda *a, **k: None)
    with pytest.raises(reengineer.LocalizationError):
        reengineer.localize_motion_prompt(EDITED_PROMPT, "de", force=True)


# --- 2. the runner: a typed prompt is always re-localized -------------------

def _job(tmp_path):
    img = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                         prompt="p", scene_id="s1", status=VariantStatus.READY)
    Path(img.path).write_bytes(b"img")
    jc = JobCharacter(char_id="cA", name="Susanne",
                      source_image_path=str(tmp_path / "c.png"),
                      status=CharStatus.APPROVED, images=[img],
                      approved_variant_ids=["v1"], approved_variant_id="v1")
    (tmp_path / "c.png").write_bytes(b"c")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path=str(scene), scene_ids=["s1"],
              scene_image_paths=[str(scene)], video_model="kling-v3",
              characters={"cA": jc})
    return job, jc


def _stub_runner(monkeypatch, tmp_path):
    """Hermetic runner around the REAL localizer. Returns (takes, qc_kwargs)."""
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_output_dir", lambda j, c: tmp_path)
    monkeypatch.setattr(runner, "_character_gender", lambda cid: None)
    monkeypatch.setattr(runner, "_character_language", lambda cid: "de")

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    cfg = runner.settings
    monkeypatch.setattr(type(cfg), "video_qc_enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(cfg), "video_qc_visual_enabled",
                        property(lambda self: False), raising=False)
    monkeypatch.setattr(type(cfg), "video_qc_max_retries",
                        property(lambda self: 0), raising=False)

    takes: list[str] = []

    def fake_submit(**kw):
        takes.append(kw["movement_prompt"])
        return f"req-{len(takes)}"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))
    qc_kwargs: list[dict] = []

    def fake_inspect(*a, **k):
        qc_kwargs.append(k)
        return video_qc.ClipVerdict(True, "", "")
    monkeypatch.setattr(video_qc, "inspect_clip", fake_inspect)
    return takes, qc_kwargs


def test_regen_with_an_edited_prompt_submits_the_translated_line(
        monkeypatch, tmp_path):
    """The whole point: ↻ with an English line typed into the German box
    submits GERMAN."""
    job, jc = _job(tmp_path)
    _translator(monkeypatch, "ÜBERSETZT")
    takes, _ = _stub_runner(monkeypatch, tmp_path)
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1",
                         movement_prompt_override=EDITED_PROMPT)
    jc.videos = [video]

    asyncio.run(runner._animate_one_video(job, jc, video, EDITED_PROMPT))

    assert len(takes) == 1
    assert "ÜBERSETZT" in takes[0]
    assert EDITED_EN_LINE not in takes[0]
    assert video.localized_movement_prompt == takes[0]


def test_untyped_prompt_keeps_the_marker_short_circuit(monkeypatch, tmp_path):
    """No override = the prompt came from the pipeline, not from Hugo — the
    cheap marker check still applies and no translate call is billed."""
    job, jc = _job(tmp_path)
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda *a, **k: pytest.fail("must not translate"))
    takes, _ = _stub_runner(monkeypatch, tmp_path)
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]

    asyncio.run(runner._animate_one_video(job, jc, video, GERMAN_PROMPT))

    assert takes == [GERMAN_PROMPT + reengineer._NO_ADLIB_CLAUSE]


def test_unchanged_dialogue_never_arms_the_wrong_language_check(
        monkeypatch, tmp_path):
    """False-positive guard. Re-localizing an already-German prompt must not
    hand video_qc a GERMAN `original_speech` — the check would then score
    German audio against a German "English source line" and could fail a
    correct clip as "fel språk"."""
    job, jc = _job(tmp_path)
    # Translator returns the line unchanged; only the clause layout may move.
    _translator(monkeypatch, "Nimm jeden Morgen einen Löffel auf nüchternen Magen.")
    _takes, qc_kwargs = _stub_runner(monkeypatch, tmp_path)
    stripped = GERMAN_PROMPT.replace(DE.speak_only_clause, "")
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1",
                         movement_prompt_override=stripped)
    jc.videos = [video]

    asyncio.run(runner._animate_one_video(job, jc, video, stripped))

    assert qc_kwargs and qc_kwargs[0]["expected_language"] == "de"
    assert qc_kwargs[0]["original_speech"] is None


def test_translated_dialogue_still_arms_the_wrong_language_check(
        monkeypatch, tmp_path):
    """…while a line that WAS translated keeps the net armed with the English
    source line (Hugo 2026-08-02)."""
    job, jc = _job(tmp_path)
    _translator(monkeypatch, "ÜBERSETZT")
    _takes, qc_kwargs = _stub_runner(monkeypatch, tmp_path)
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1",
                         movement_prompt_override=EDITED_PROMPT)
    jc.videos = [video]

    asyncio.run(runner._animate_one_video(job, jc, video, EDITED_PROMPT))

    assert qc_kwargs[0]["original_speech"] == EDITED_EN_LINE


# --- 3. the prompt the modal shows -----------------------------------------

def test_api_exposes_the_submitted_prompt_on_every_clip(tmp_path):
    """The modal can only prefill the localized text if the API ships it."""
    job, jc = _job(tmp_path)
    jc.videos = [VideoVariant(video_id="vd1", grok_job_id="",
                              status=VideoStatus.DONE, source_variant_id="v1",
                              localized_movement_prompt=GERMAN_PROMPT)]
    out = api._job_to_dict(job)
    assert (out["characters"]["cA"]["videos"][0]["localized_movement_prompt"]
            == GERMAN_PROMPT)


def test_app_js_prefills_the_localized_prompt():
    """Mirror check: the regen modal must prefer the submitted (localized)
    prompt over the English source — the ask that started this."""
    src = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text()
    prefill = src.split("regenPromptPrefill(vv, lang) {", 1)[1].split("},", 1)[0]
    assert "lang && vv.localized_movement_prompt" in prefill
    # …and it must be FIRST — an override is the English text Hugo typed last.
    assert (prefill.index("localized_movement_prompt")
            < prefill.index("movement_prompt_override"))
