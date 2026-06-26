"""Per-character Spanish-speaking flag (Hugo 2026-06-26).

A library character can be marked `language="es"`. Then every video that
character makes has the quoted dialogue in its motion prompt auto-translated to
neutral Latin American Spanish + the Spanish accent clause enforced — only for
THAT character's clips; unflagged characters keep the English prompt. Additive
to the per-run 🗣 picker (a full "es" run is NOT re-translated). These tests
lock the localizer, the live lookup, and the PATCH round-trip.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import api, reengineer, runner
from character_swap.models import CharacterAsset
from character_swap.state import SqliteStateStore, store


@pytest.fixture(autouse=True)
def _clear_localize_cache():
    reengineer._LOCALIZE_CACHE.clear()
    yield
    reengineer._LOCALIZE_CACHE.clear()


def _boom(*a, **k):
    raise AssertionError("translate_dialogue must not be called here")


# --- localize_motion_prompt --------------------------------------------------

def _stub_translate(monkeypatch, fn):
    monkeypatch.setattr(reengineer, "translate_dialogue", fn)


def test_localize_es_translates_quote_and_adds_spanish_accent(monkeypatch):
    _stub_translate(monkeypatch,
                    lambda lines, *, re_id=None: ["¡Pruébalo esta noche!"])
    p = 'She pours oil. The person says to the camera: "Try this tonight."'
    out = reengineer.localize_motion_prompt(p, "es", job_id="j")
    # Quoted dialogue is replaced in place; English framing is untouched.
    assert "¡Pruébalo esta noche!" in out
    assert "Try this tonight." not in out
    assert "She pours oil." in out
    # Spanish accent enforced; the English accent clause never appears.
    assert "Latin American Spanish accent" in out
    assert reengineer.ACCENT_CLAUSE["en"][0] not in out
    assert "American English" not in out
    # The instruction guarantees ride along (English directions, not speech).
    assert "pronounced clearly" in out
    assert "No background music" in out


def test_localize_en_and_none_are_noops(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("translate must not run for en/none")
    _stub_translate(monkeypatch, _boom)
    p = 'He waves. The person says: "Hello there."'
    assert reengineer.localize_motion_prompt(p, "en") == p
    assert reengineer.localize_motion_prompt(p, None) == p
    assert reengineer.localize_motion_prompt("", "es") == ""


def test_localize_skips_already_spanish_run(monkeypatch):
    """Additive guard: a full-'es' run already carries the ES accent clause, so
    per-character localization must NOT re-translate the user-approved text."""
    def _boom(*a, **k):
        raise AssertionError("translate must not run on an already-es prompt")
    _stub_translate(monkeypatch, _boom)
    run_level = reengineer.with_accent('Él sirve. Dice: "hola"', "es")
    assert reengineer.localize_motion_prompt(run_level, "es") == run_level


def test_localize_raises_when_translation_fails(monkeypatch):
    """A clip WITH dialogue whose translation fails raises LocalizationError so
    the runner fails the clip LOUDLY (Hugo 2026-06-27) — never silently ships an
    English clip for a Spanish-flagged character."""
    _stub_translate(monkeypatch, lambda lines, *, re_id=None: None)
    p = 'He nods. The person says: "Buy it now."'
    with pytest.raises(reengineer.LocalizationError):
        reengineer.localize_motion_prompt(p, "es", job_id="j")
    # A no-dialogue clip is NOT a failure (nothing to translate) → no raise.
    assert reengineer.localize_motion_prompt("She walks in.", "es") == "She walks in."


def test_localize_reverse_span_keeps_two_clauses_intact(monkeypatch):
    _stub_translate(monkeypatch,
                    lambda lines, *, re_id=None: [f"<<{x}>>" for x in lines])
    p = ('He pours. The person says: "first" and then '
         'the person says: "second".')
    out = reengineer.localize_motion_prompt(p, "es")
    assert "<<first>>" in out and "<<second>>" in out
    assert out.index("<<first>>") < out.index("<<second>>")
    assert "He pours." in out


def test_localize_caches_by_prompt(monkeypatch):
    calls = {"n": 0}

    def _count(lines, *, re_id=None):
        calls["n"] += 1
        return [x.upper() for x in lines]
    _stub_translate(monkeypatch, _count)
    p = 'Hi. The person says: "again"'
    a = reengineer.localize_motion_prompt(p, "es")
    b = reengineer.localize_motion_prompt(p, "es")
    assert a == b
    assert calls["n"] == 1          # second call served from cache


def test_localize_strips_inline_american_accent(monkeypatch):
    """The analyst (and the English fallback) write the accent INLINE inside the
    says-clause — `...says ... with an American accent: "<x>"`. The ES localizer
    must strip that, or Kling gets a contradictory accent next to Spanish."""
    _stub_translate(monkeypatch, lambda lines, *, re_id=None: ["¡Hola amigos!"])
    p = ('He waves. The person says enthusiastically to the camera with an '
         'American accent: "Hello friends."')
    out = reengineer.localize_motion_prompt(p, "es", job_id="j")
    assert "¡Hola amigos!" in out and "Hello friends." not in out
    assert "american accent" not in out.lower()      # inline EN accent gone
    assert "American English" not in out
    assert "Latin American Spanish accent" in out     # exactly one accent, ES


def test_localize_skips_inline_spanish_attribution(monkeypatch):
    """Additive guard on the REAL full-'es' run shape: the analyst writes the
    inline Spanish attribution and with_accent then SKIPS the standalone ES
    sentence (because 'spanish' is already present). The old guard (which looked
    for the standalone sentence) missed this and re-translated approved text."""
    _stub_translate(monkeypatch, _boom)
    p = reengineer.with_accent(
        'Él sirve agua. ' + reengineer.spanish_speech_clause("hola amigos"), "es")
    # Precondition: this shape genuinely lacks the standalone ES accent sentence.
    assert reengineer.ACCENT_CLAUSE["es"][0].strip() not in p
    assert reengineer.localize_motion_prompt(p, "es") == p


def test_localize_no_dialogue_leaves_prompt_untouched(monkeypatch):
    """A purely-visual clip (no says-clause) is left COMPLETELY untouched — no
    translation, and no Spanish-accent / no-music / pronunciation directive
    injected into a silent shot that never carried an accent clause."""
    _stub_translate(monkeypatch, _boom)
    p = "She walks toward the window, slow dolly-in."
    assert reengineer.localize_motion_prompt(p, "es") == p
    # Even a silent clip that DID carry the EN accent clause is left alone.
    p2 = reengineer.with_accent("He slices a kiwi.", "en")
    assert reengineer.localize_motion_prompt(p2, "es") == p2


def test_localize_strips_standalone_en_clause_with_dialogue(monkeypatch):
    """When the clip HAS dialogue, a pre-existing standalone EN accent clause is
    replaced by the Spanish one (non-vacuous: the input genuinely contains it)."""
    _stub_translate(monkeypatch, lambda lines, *, re_id=None: ["¡Cómpralo ya!"])
    p = reengineer.with_accent('He nods. The person says: "Buy now."', "en")
    assert reengineer.ACCENT_CLAUSE["en"][0] in p          # input really has it
    out = reengineer.localize_motion_prompt(p, "es")
    assert "¡Cómpralo ya!" in out and "Buy now." not in out
    assert reengineer.ACCENT_CLAUSE["en"][0] not in out    # EN clause stripped
    assert "american accent" not in out.lower()
    assert "Latin American Spanish accent" in out


def test_localize_guard_does_not_overmatch_dialogue(monkeypatch):
    """A flagged char whose ENGLISH dialogue/scene text happens to contain
    'Latin American Spanish' must STILL be translated — the guard keys off the
    full 'neutral latin american spanish' marker, not the bare phrase."""
    _stub_translate(monkeypatch,
                    lambda lines, *, re_id=None: ["Servimos comida auténtica."])
    p = ('He gestures at the menu. The person says: '
         '"We serve authentic Latin American Spanish cuisine."')
    out = reengineer.localize_motion_prompt(p, "es")
    assert "Servimos comida auténtica." in out             # translated, not skipped
    assert "We serve authentic" not in out
    assert "Latin American Spanish accent" in out


def test_localize_sanitizes_quotes_in_translation(monkeypatch):
    """A translation that contains a stray double-quote must not unbalance the
    says-clause — the localized prompt must still re-parse to a single clean
    spoken line (video_qc reads the same prompt with the same regex)."""
    from character_swap.video_edit import extract_dialogue
    _stub_translate(monkeypatch,
                    lambda lines, *, re_id=None: ['Él dijo "hola" fuerte'])
    p = 'He waves. The person says: "He said hi loudly."'
    out = reengineer.localize_motion_prompt(p, "es")
    spoken = extract_dialogue(out)
    assert spoken == "Él dijo hola fuerte"                 # inner quotes stripped
    assert '"' not in spoken


def test_localize_guarantees_accent_despite_spanish_framing(monkeypatch):
    """If English framing contains the word 'Spanish' (e.g. 'Spanish-tiled'),
    with_accent's keyword guard would skip the accent clause — the localizer
    HARD-guarantees it instead."""
    _stub_translate(monkeypatch, lambda lines, *, re_id=None: ["Hola."])
    p = 'A rustic Spanish-tiled kitchen. The person says: "Hi there."'
    out = reengineer.localize_motion_prompt(p, "es")
    assert "Latin American Spanish accent" in out          # accent guaranteed


def test_localize_strips_adjectived_inline_accent(monkeypatch):
    """The inline-accent strip tolerates an adjective ('clear/warm/General')
    before 'American accent', not only the canonical 'natural' form."""
    _stub_translate(monkeypatch, lambda lines, *, re_id=None: ["Hola."])
    p = 'He smiles. The person says with a clear American accent: "Hello."'
    out = reengineer.localize_motion_prompt(p, "es")
    assert "american accent" not in out.lower()
    assert "Latin American Spanish accent" in out


# --- Step-6 compile reads the clip's localized (Spanish) dialogue -------------

def test_compile_dialogue_uses_localized_prompt(tmp_path):
    """_ordered_scene_videos must derive each clip's known dialogue from the
    clip's localized (Spanish) prompt, not the English job prompt — else the
    flagged character's compiled captions are burned in the wrong language."""
    from character_swap import runner_compile
    from character_swap.models import (
        CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
        VideoStatus, VideoVariant)
    clip = tmp_path / "clip.mp4"; clip.write_bytes(b"v")
    img = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                         prompt="p", scene_id="s1", status=VariantStatus.READY)
    # Real localizer output keeps the English "says" attribution and translates
    # only the quoted phrase, so extract_dialogue still anchors on it.
    vid = VideoVariant(
        video_id="vd1", grok_job_id="", status=VideoStatus.DONE,
        source_variant_id="v1", final_video_path=str(clip),
        localized_movement_prompt=(
            'Camina. The person says to the camera: "hola amigos". The person '
            'speaks fluent, natural Latin American Spanish with a neutral '
            'Latin American Spanish accent.'))
    jc = JobCharacter(char_id="cc", name="C", source_image_path="c.png",
                      status=CharStatus.DONE, images=[img], videos=[vid],
                      approved_variant_ids=["v1"])
    job = Job(job_id="jcl", title="t", scene_id="s1",
              scene_image_path=str(tmp_path / "s.png"), scene_ids=["s1"],
              movement_prompts={"s1": 'Walk. The person says: "hello friends"'})
    paths, dialogues, missing = runner_compile._ordered_scene_videos(job, jc)
    assert paths == [clip] and missing == []
    assert dialogues == ["hola amigos"]          # Spanish, from the localized clip


def test_compile_dialogue_falls_back_to_english_when_not_localized(tmp_path):
    """A clip with no localized prompt (unflagged char) falls back to the English
    job-level scene dialogue — unchanged behavior."""
    from character_swap import runner_compile
    from character_swap.models import (
        CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
        VideoStatus, VideoVariant)
    clip = tmp_path / "clip.mp4"; clip.write_bytes(b"v")
    img = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                         prompt="p", scene_id="s1", status=VariantStatus.READY)
    vid = VideoVariant(video_id="vd1", grok_job_id="", status=VideoStatus.DONE,
                       source_variant_id="v1", final_video_path=str(clip))
    jc = JobCharacter(char_id="cc2", name="C", source_image_path="c.png",
                      status=CharStatus.DONE, images=[img], videos=[vid],
                      approved_variant_ids=["v1"])
    job = Job(job_id="jcl2", title="t", scene_id="s1",
              scene_image_path=str(tmp_path / "s.png"), scene_ids=["s1"],
              movement_prompts={"s1": 'Walk. The person says: "hello friends"'})
    _, dialogues, _ = runner_compile._ordered_scene_videos(job, jc)
    assert dialogues == ["hello friends"]


# --- SQLite persistence (USE_SQLITE_STATE=1 is the documented default) --------

def test_sqlite_language_round_trip(tmp_path):
    """The flag must survive a server restart under the SQLite backend — i.e.
    persist to the `characters` table and reload. Throw the store away and
    rebuild against the same DB file (a fresh in-memory store would mask a
    write/read gap)."""
    db = tmp_path / "state.sqlite3"
    s1 = SqliteStateStore(db_path=db)
    s1.add_character(CharacterAsset(char_id="c1", name="Maria", filename="x.png"))
    ch = s1.get_character("c1")
    ch.language = "es"
    s1.update_character(ch)

    s2 = SqliteStateStore(db_path=db)
    assert s2.get_character("c1").language == "es"

    # Clearing back to English persists as NULL too.
    ch2 = s2.get_character("c1")
    ch2.language = None
    s2.update_character(ch2)
    assert SqliteStateStore(db_path=db).get_character("c1").language is None


# --- runner live lookup ------------------------------------------------------

def test_character_language_live_lookup():
    store().add_character(CharacterAsset(
        char_id="cl_es", filename="cl_es.png", name="Maria", language="es"))
    store().add_character(CharacterAsset(
        char_id="cl_en", filename="cl_en.png", name="John"))
    assert runner._character_language("cl_es") == "es"
    assert runner._character_language("cl_en") is None
    assert runner._character_language("nope") is None


# --- PATCH /api/characters/{id} round-trip ----------------------------------

def test_patch_language_roundtrip():
    store().add_character(CharacterAsset(
        char_id="cp1", filename="cp1.png", name="Ana"))
    out = asyncio.run(api.rename_character(
        "cp1", api.RenameCharacterBody(language="es")))
    assert out["language"] == "es"
    assert store().get_character("cp1").language == "es"
    # Clearing: empty string and "en" both reset to default English (None).
    asyncio.run(api.rename_character("cp1", api.RenameCharacterBody(language="")))
    assert store().get_character("cp1").language is None
    asyncio.run(api.rename_character("cp1", api.RenameCharacterBody(language="es")))
    asyncio.run(api.rename_character("cp1", api.RenameCharacterBody(language="en")))
    assert store().get_character("cp1").language is None
    # A name-only PATCH leaves language untouched.
    asyncio.run(api.rename_character("cp1", api.RenameCharacterBody(language="es")))
    asyncio.run(api.rename_character("cp1", api.RenameCharacterBody(name="Ana2")))
    assert store().get_character("cp1").language == "es"
