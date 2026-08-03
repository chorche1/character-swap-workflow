"""Speaker-attribution fix for female characters (Hugo 2026-08-02, widened
2026-08-03).

Every movement prompt in the library was written off a MALE original ("the man
says…"). A Claude vision call rewrites the prompt so SHE is unmistakably the
speaker — before language localization, and failing the clip LOUDLY when it
can't be done.

The gate was originally two-sided (female AND a scene ticked 👥), which left
the single-person case shipping "He says … while he is …" for a woman. Since
2026-08-03 EVERY female character gets the fix and the 👥 tick is only a hint
about a second person. Male characters are still never touched — the source
prompts already speak about a man.

The rewrite is computed ONCE per (scene × gender) and reused for every female
character in that scene, so the agent must describe the speaker by POSITION,
never by clothing: each character wears her own outfit.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import api, reengineer, runner, speaker_fix
from character_swap.models import (
    CharacterAsset, CharStatus, GeneratedImage, Job, JobCharacter,
    VariantStatus, VideoStatus, VideoVariant,
)

BASE = ('The man in the grey hoodie says to the camera with a clear American '
        'accent: "This is the line." He gestures with one hand. No background '
        'music — natural ambient room sound only.')
FIXED = ('The woman on the left in the beige blazer says to the camera with a '
         'clear American accent: "This is the line." She gestures with one '
         'hand. The man beside her listens silently and does not move his '
         'lips. No background music — natural ambient room sound only.')


# --- fixtures ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_prompt_cache():
    """The shared (scene × gender × language) rewrite cache is process-level;
    without this, one test's result answers the next test's call and the agent
    stub is never reached."""
    runner._PROMPT_CACHE.clear()
    runner._PROMPT_LOCKS.clear()
    yield
    runner._PROMPT_CACHE.clear()
    runner._PROMPT_LOCKS.clear()


def _job(tmp_path, *, two_person=("s1",)):
    v_img = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                           prompt="BASE", scene_id="s1",
                           status=VariantStatus.READY)
    Path(v_img.path).write_bytes(b"img")
    jc = JobCharacter(char_id="cA", name="Helene",
                      source_image_path=str(tmp_path / "char.png"),
                      status=CharStatus.APPROVED, images=[v_img],
                      approved_variant_ids=["v1"], approved_variant_id="v1")
    (tmp_path / "char.png").write_bytes(b"char")
    scene = tmp_path / "scene.png"; scene.write_bytes(b"scene")
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path=str(scene), scene_ids=["s1"],
              scene_image_paths=[str(scene)], video_model="kling-v3",
              two_person_scenes=list(two_person), characters={"cA": jc})
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]
    return job, jc, video


def _stub(monkeypatch, tmp_path, *, gender="female", language=None):
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_output_dir", lambda job_id, char_id: tmp_path)
    monkeypatch.setattr(runner, "_character_gender", lambda cid: gender)
    monkeypatch.setattr(runner, "_character_language", lambda cid: language)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(type(runner.settings), "video_qc_enabled",
                        property(lambda self: False), raising=False)
    monkeypatch.setattr(runner.video_qc, "inspect_clip", lambda *a, **k: None)
    submits: list[str] = []

    def fake_submit(**kw):
        submits.append(kw["movement_prompt"])
        return "req-1"
    monkeypatch.setattr(runner.pipeline, "submit_video", fake_submit)
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))
    return submits


def _run(coro):
    return asyncio.run(coro)


# --- the gate ----------------------------------------------------------------

def test_gate_is_female_on_any_scene():
    ticked = ["s1"]
    assert speaker_fix.needs_speaker_fix(
        gender="female", scene_id="s1", two_person_scenes=ticked)
    # A MALE character is skipped even on a ticked scene — the source prompt
    # already attributes the line to a man (Hugo 2026-08-02).
    assert not speaker_fix.needs_speaker_fix(
        gender="male", scene_id="s1", two_person_scenes=ticked)
    # A woman on an UNTICKED scene now RUNS (widened 2026-08-03): without it
    # her prompt still said "He says … while he is …".
    assert speaker_fix.needs_speaker_fix(
        gender="female", scene_id="s2", two_person_scenes=ticked)
    assert speaker_fix.needs_speaker_fix(
        gender="female", scene_id="s1", two_person_scenes=[])
    # Gender never set → treated as male, agent never runs.
    assert not speaker_fix.needs_speaker_fix(
        gender=None, scene_id="s1", two_person_scenes=ticked)
    # No scene → nothing to key a shared rewrite on.
    assert not speaker_fix.needs_speaker_fix(
        gender="female", scene_id=None, two_person_scenes=ticked)


def test_two_person_tick_is_now_only_a_hint():
    assert speaker_fix.is_two_person_scene("s1", ["s1"])
    assert not speaker_fix.is_two_person_scene("s1", [])
    assert not speaker_fix.is_two_person_scene(None, ["s1"])


def test_normalize_gender_accepts_both_languages_and_refuses_typos():
    assert speaker_fix.normalize_gender("Female") == "female"
    assert speaker_fix.normalize_gender("kvinna") == "female"
    assert speaker_fix.normalize_gender("MALE") == "male"
    assert speaker_fix.normalize_gender("manlig") == "male"
    assert speaker_fix.normalize_gender("") is None
    assert speaker_fix.normalize_gender(None) is None
    with pytest.raises(ValueError):
        speaker_fix.normalize_gender("kvinnligt kön")


def test_character_asset_defaults_to_no_gender():
    ch = CharacterAsset(char_id="c1", filename="a.png", name="A")
    assert ch.gender is None
    assert not speaker_fix.is_female(ch.gender)


# --- the agent call ----------------------------------------------------------

def _fake_anthropic(monkeypatch, payload, *, boom=None):
    """Patch the Anthropic wrapper the agent uses. `payload` is the tool call."""
    import character_swap.clients.anthropic_client as ac
    monkeypatch.setattr(type(speaker_fix_settings()), "anthropic_api_key",
                        property(lambda self: "k"), raising=False)
    monkeypatch.setattr(ac, "_file_to_image_block",
                        lambda p: {"type": "image", "source": {}})

    def fake_call(**kw):
        if boom is not None:
            raise boom
        fake_call.kwargs = kw
        return {"stub": True}
    monkeypatch.setattr(ac, "messages_with_tools", fake_call)
    monkeypatch.setattr(ac, "extract_tool_call", lambda resp, name: payload)
    return fake_call


def speaker_fix_settings():
    from character_swap.config import settings
    return settings


def test_agent_returns_rewritten_prompt(monkeypatch, tmp_path):
    img = tmp_path / "v1.png"; img.write_bytes(b"img")
    call = _fake_anthropic(monkeypatch,
                           {"prompt": FIXED, "speaker": "the woman on the left",
                            "changed": True})
    out = speaker_fix.fix_speaker_attribution(BASE, img, character_name="Helene")
    assert out == FIXED
    # The frame AND the prompt must both reach the model, and it must be forced
    # to answer through the tool.
    assert call.kwargs["tool_choice"]["name"] == "submit_speaker_fix"
    text = "".join(b.get("text", "")
                   for b in call.kwargs["messages"][0]["content"])
    assert BASE in text
    # The character's NAME is deliberately NOT sent: one rewrite serves every
    # female character in the scene, so anything person-specific in it would be
    # wrong for all but one of them.
    assert "Helene" not in text
    assert "SCENE NOTE" in text


def test_agent_reporting_no_change_keeps_the_original(monkeypatch, tmp_path):
    img = tmp_path / "v1.png"; img.write_bytes(b"img")
    _fake_anthropic(monkeypatch, {"prompt": "", "speaker": "", "changed": False})
    assert speaker_fix.fix_speaker_attribution(
        BASE, img, character_name="Helene") == BASE


@pytest.mark.parametrize("payload,boom", [
    (None, None),                                     # no tool call
    ({"prompt": "  ", "changed": True}, None),        # empty rewrite
    ({"prompt": "She talks.", "changed": True}, None),  # gutted the prompt
    (None, RuntimeError("500 overloaded")),           # API blew up
])
def test_agent_failures_raise_loudly(monkeypatch, tmp_path, payload, boom):
    img = tmp_path / "v1.png"; img.write_bytes(b"img")
    _fake_anthropic(monkeypatch, payload, boom=boom)
    with pytest.raises(speaker_fix.SpeakerFixError):
        speaker_fix.fix_speaker_attribution(BASE, img, character_name="Helene")


def test_agent_without_api_key_raises(monkeypatch, tmp_path):
    img = tmp_path / "v1.png"; img.write_bytes(b"img")
    monkeypatch.setattr(type(speaker_fix_settings()), "anthropic_api_key",
                        property(lambda self: ""), raising=False)
    with pytest.raises(speaker_fix.SpeakerFixError):
        speaker_fix.fix_speaker_attribution(BASE, img, character_name="Helene")


# --- wired into the clip submit ----------------------------------------------

def test_female_on_ticked_scene_submits_the_rewritten_prompt(monkeypatch, tmp_path):
    job, jc, video = _job(tmp_path)
    submits = _stub(monkeypatch, tmp_path, gender="female")
    monkeypatch.setattr(runner.speaker_fix, "fix_speaker_attribution",
                        lambda prompt, image, **kw: FIXED)

    _run(runner._animate_one_video(job, jc, video, BASE))

    assert submits == [FIXED]
    assert video.speaker_fix_prompt == FIXED
    assert video.status == VideoStatus.DONE


def test_male_character_on_ticked_scene_never_calls_the_agent(monkeypatch, tmp_path):
    job, jc, video = _job(tmp_path)
    submits = _stub(monkeypatch, tmp_path, gender="male")
    called = []
    monkeypatch.setattr(runner.speaker_fix, "fix_speaker_attribution",
                        lambda *a, **k: called.append(1) or FIXED)

    _run(runner._animate_one_video(job, jc, video, BASE))

    assert not called                      # no credits spent on the men
    assert submits == [BASE]
    assert video.speaker_fix_prompt is None


def test_unticked_scene_still_fixes_the_pronoun(monkeypatch, tmp_path):
    """Widened 2026-08-03: a single-person scene left "He says … while he is …"
    on a female character. Measured, Veo follows the frame and renders her
    anyway — but the text was plainly wrong, and the model was doing the
    saving, not the prompt."""
    job, jc, video = _job(tmp_path, two_person=())
    submits = _stub(monkeypatch, tmp_path, gender="female")
    hints = []
    monkeypatch.setattr(
        runner.speaker_fix, "fix_speaker_attribution",
        lambda *a, **k: hints.append(k.get("two_person")) or FIXED)

    _run(runner._animate_one_video(job, jc, video, BASE))

    assert submits == [FIXED]
    # The tick still travels — as the second-person HINT, not as the gate.
    assert hints == [False]
    assert video.speaker_fix_prompt == FIXED


def test_agent_failure_fails_the_clip_loudly(monkeypatch, tmp_path):
    """Hugo's standing rule: never silently ship a clip where the wrong person
    says the line. A failed rewrite fails the CLIP with a readable reason."""
    job, jc, video = _job(tmp_path)
    submits = _stub(monkeypatch, tmp_path, gender="female")

    def boom(*a, **k):
        raise speaker_fix.SpeakerFixError("agenten svarade utan verktygsanrop")
    monkeypatch.setattr(runner.speaker_fix, "fix_speaker_attribution", boom)

    _run(runner._animate_one_video(job, jc, video, BASE))

    assert submits == []                   # nothing was ever submitted
    assert video.status == VideoStatus.ERROR
    assert "talar-agenten" in (video.error or "")


# --- persistence + API round-trip -------------------------------------------

def test_gender_survives_a_reopened_sqlite_store(tmp_path):
    from character_swap.state import SqliteStateStore
    db = tmp_path / "s.sqlite3"
    s1 = SqliteStateStore(db_path=db)
    s1.add_character(CharacterAsset(char_id="g1", name="Helene",
                                    filename="x.png", gender="female"))
    assert SqliteStateStore(db_path=db).get_character("g1").gender == "female"


def test_patch_gender_roundtrip_and_live_lookup():
    from character_swap.state import store
    store().add_character(CharacterAsset(
        char_id="gp1", filename="gp1.png", name="Susanne"))
    out = asyncio.run(api.rename_character(
        "gp1", api.RenameCharacterBody(gender="female")))
    assert out["gender"] == "female"
    assert runner._character_gender("gp1") == "female"

    # "" clears it back to unset; a typo is refused with a 400 rather than
    # leaving the UI showing ♀ while the agent never runs.
    assert asyncio.run(api.rename_character(
        "gp1", api.RenameCharacterBody(gender="")))["gender"] is None
    with pytest.raises(Exception):
        asyncio.run(api.rename_character(
            "gp1", api.RenameCharacterBody(gender="hen")))


# --- UI mirrors --------------------------------------------------------------

def test_ui_sends_gender_on_new_character_and_two_person_per_row():
    root = Path(__file__).resolve().parents[1]
    app_js = (root / "web" / "app.js").read_text()
    index = (root / "web" / "index.html").read_text()
    # New character: gender is required client-side AND sent with the upload.
    assert "uploadNewCharGender" in app_js
    assert "fd.append('gender', this.uploadNewCharGender)" in app_js
    # Per-scene 👥 flag rides with the swap form and can be toggled after.
    assert "fd.append('two_person'" in app_js
    assert "reengineerSetTwoPerson" in app_js and "two_person: !!on" in app_js
    assert "row.twoPerson" in index
    assert "setCharacterGender" in index and "setCharacterGender" in app_js


def test_speaker_fix_runs_before_localization(monkeypatch, tmp_path):
    """A character that is BOTH female and 🇪🇸-flagged: the speaker fix runs on
    the English text first, then the localizer translates the result — so the
    Spanish clip also names the right speaker."""
    job, jc, video = _job(tmp_path)
    submits = _stub(monkeypatch, tmp_path, gender="female", language="es")
    seen: list[str] = []
    monkeypatch.setattr(runner.speaker_fix, "fix_speaker_attribution",
                        lambda prompt, image, **kw: FIXED)

    def fake_localize(prompt, code, job_id=None, force=False):
        seen.append(prompt)
        return prompt + " [ES]"
    monkeypatch.setattr(reengineer, "localize_motion_prompt", fake_localize)

    _run(runner._animate_one_video(job, jc, video, BASE))

    assert seen == [FIXED]                       # localizer saw the FIXED text
    assert submits == [FIXED + " [ES]"]
    assert video.speaker_fix_prompt == FIXED
    assert video.localized_movement_prompt == FIXED + " [ES]"
