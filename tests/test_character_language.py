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


def test_localize_failsoft_keeps_english(monkeypatch):
    """Translation failure ⇒ coherent ENGLISH (no Spanish accent on English
    words), never half-translated."""
    _stub_translate(monkeypatch, lambda lines, *, re_id=None: None)
    p = 'He nods. The person says: "Buy it now."'
    out = reengineer.localize_motion_prompt(p, "es", job_id="j")
    assert out == p
    assert "Latin American Spanish accent" not in out


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


def test_localize_no_dialogue_strips_en_clause_and_adds_es(monkeypatch):
    """Silent scene (no says-clause): translate is never called, but a real EN-run
    prompt still carries the standalone EN accent clause — it must be REPLACED by
    the Spanish one (non-vacuous: the input genuinely contains the EN clause)."""
    _stub_translate(monkeypatch, _boom)
    p = reengineer.with_accent("He slices a kiwi into the bowl.", "en")
    assert reengineer.ACCENT_CLAUSE["en"][0] in p          # input really has it
    out = reengineer.localize_motion_prompt(p, "es")
    assert "He slices a kiwi into the bowl." in out
    assert reengineer.ACCENT_CLAUSE["en"][0] not in out    # EN clause stripped
    assert "American accent" not in out
    assert "Latin American Spanish accent" in out


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
