"""Per-character GERMAN flag (Hugo 2026-08-02) — "tyska precis som spanska".

Adding a language is a data change: one `reengineer.SPOKEN_LANGUAGES` row plus
its UI option. These tests lock the German behavior itself AND the invariants
that make the registry safe to extend — every helper must read the row instead
of branching on "es".

German is per-CHARACTER only (Hugo's call): the run-level 🗣 picker stays
en | es, so a 🇩🇪 character can sit inside a full Spanish run — that mix is
tested here too.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import api, reengineer, runner
from character_swap.models import CharacterAsset
from character_swap.state import SqliteStateStore, store

DE = reengineer.SPOKEN_LANGUAGES["de"]
ES = reengineer.SPOKEN_LANGUAGES["es"]


@pytest.fixture(autouse=True)
def _clear_localize_cache():
    reengineer._LOCALIZE_CACHE.clear()
    yield
    reengineer._LOCALIZE_CACHE.clear()


def _stub_translate(monkeypatch, fn):
    monkeypatch.setattr(reengineer, "translate_dialogue", fn)


def _never_translate(*a, **k):
    raise AssertionError("translate_dialogue must not be called here")


# --- the registry itself -----------------------------------------------------

def test_registry_invariants_hold_for_every_language():
    """Each row must be self-consistent, or `_force_language_speech`'s hard
    guarantee silently degrades: it appends the accent clause only when the
    marker is absent, and treats the marker as proof the language is set."""
    for code, spec in reengineer.SPOKEN_LANGUAGES.items():
        assert spec.code == code
        low = spec.accent_clause.lower()
        assert spec.marker in low, f"{code}: marker missing from its clause"
        assert spec.accent_key in low, f"{code}: accent_key missing from clause"
        # The inline attribution must also establish the language, else a
        # prompt that only carries it would get a second, redundant clause.
        assert spec.marker in spec.speech_order.lower()
        assert spec.accent_clause.startswith(" ")
        assert spec.accent_clause.rstrip().endswith(".")
        assert spec.translate_system.strip()


def test_accent_clause_table_covers_registry_plus_english():
    assert set(reengineer.ACCENT_CLAUSE) == {"en", "es", "de"}
    assert reengineer.ACCENT_CLAUSE["de"] == (DE.accent_clause, DE.accent_key)
    # The historical Spanish alias still resolves to the registry value.
    assert reengineer._ES_LOCALIZED_MARKER == ES.marker


def test_with_accent_enforces_the_german_clause():
    out = reengineer.with_accent("She smiles.", "de")
    assert DE.accent_clause in out
    assert reengineer.ACCENT_CLAUSE["en"][0] not in out
    # Idempotent — a second pass never doubles the clause.
    assert reengineer.with_accent(out, "de") == out


# --- localize_motion_prompt --------------------------------------------------

def test_localize_de_translates_quote_and_adds_german_accent(monkeypatch):
    seen: list[str] = []

    def _tr(lines, *, language="es", re_id=None):
        seen.append(language)
        return ["Probier das heute Abend!"]

    _stub_translate(monkeypatch, _tr)
    p = 'She pours oil. The person says to the camera: "Try this tonight."'
    out = reengineer.localize_motion_prompt(p, "de", job_id="j")
    assert seen == ["de"]                    # translator got the right target
    assert "Probier das heute Abend!" in out
    assert "Try this tonight." not in out
    assert "She pours oil." in out           # English framing untouched
    assert "standard German (Hochdeutsch)" in out
    assert reengineer.ACCENT_CLAUSE["en"][0] not in out
    assert "American English" not in out
    # The instruction guarantees ride along (English directions, not speech).
    assert "pronounced clearly" in out
    assert "No background music" in out


def test_localize_de_strips_inline_american_accent(monkeypatch):
    _stub_translate(monkeypatch,
                    lambda lines, *, language="es", re_id=None: ["Kauf es jetzt!"])
    p = ('He waves. The person says to the camera with a natural American '
         'accent: "Buy it now!"')
    out = reengineer.localize_motion_prompt(p, "de")
    assert "american accent" not in out.lower()
    assert "Kauf es jetzt!" in out
    assert DE.marker in out.lower()


def test_localize_de_flips_an_explicit_english_speech_order(monkeypatch):
    """The Director's AUDIO block orders English without any says-clause. It
    must be flipped in place — an appended German clause alone loses to the
    English order sitting next to the line (the 2026-07-31 Spanish leak)."""
    _stub_translate(monkeypatch,
                    lambda lines, *, language="es", re_id=None: ["Reines Salz."])
    p = ('AUDIO — Deep, clear male voice speaking English with a thick Texas '
         'accent enthusiastically: "This is pure salt."')
    out = reengineer.localize_motion_prompt(p, "de")
    assert "Reines Salz." in out
    assert "speaking English" not in out
    assert "Texas accent" not in out
    assert not reengineer._has_english_speech_directive(out)
    assert DE.marker in out.lower()


def test_localize_de_rewrites_a_dialogueless_english_order(monkeypatch):
    """No quoted line to translate, but the prompt still ORDERS English — Kling
    improvises English speech from it. Swap the directive without translating."""
    _stub_translate(monkeypatch, _never_translate)
    p = "She stirs the pot." + reengineer.ACCENT_CLAUSE["en"][0]
    out = reengineer.localize_motion_prompt(p, "de")
    assert reengineer.ACCENT_CLAUSE["en"][0] not in out
    assert DE.marker in out.lower()


def test_localize_de_leaves_a_silent_clip_untouched(monkeypatch):
    _stub_translate(monkeypatch, _never_translate)
    silent = "She walks through the kitchen and picks up a bowl."
    assert reengineer.localize_motion_prompt(silent, "de") == silent


def test_localize_de_is_a_noop_on_already_german_prompts(monkeypatch):
    _stub_translate(monkeypatch, _never_translate)
    p = 'Er winkt. The person says to the camera in standard German: "Hallo."'
    assert reengineer.localize_motion_prompt(p, "de") == p


def test_localize_de_over_a_spanish_run_leaves_no_spanish_order(monkeypatch):
    """A 🇩🇪 character inside a full Spanish run: the line is re-translated to
    German, and BOTH Spanish orders (the inline attribution and the standalone
    clause) must go with it — two language orders in one prompt is exactly how
    a clip ends up speaking the wrong one."""
    got: list[list[str]] = []

    def _tr(lines, *, language="es", re_id=None):
        got.append(list(lines))
        assert language == "de"
        return ["Hallo Freunde!"]

    _stub_translate(monkeypatch, _tr)
    p = ('Él sirve agua. The person says to the camera in neutral Latin '
         'American Spanish: "¡Hola amigos!"' + ES.accent_clause)
    out = reengineer.localize_motion_prompt(p, "de")
    assert got == [["¡Hola amigos!"]]        # the Spanish line was the input
    assert "Hallo Freunde!" in out
    assert "Latin American Spanish" not in out
    assert ES.accent_clause not in out
    assert DE.marker in out.lower()
    # The English framing/action of the prompt is still there, once.
    assert out.count("The person says to the camera") == 1


def test_localize_de_fails_loudly_when_translation_fails(monkeypatch):
    _stub_translate(monkeypatch, lambda lines, *, language="es", re_id=None: None)
    p = 'He waves. The person says: "Hello friends."'
    with pytest.raises(reengineer.LocalizationError) as e:
        reengineer.localize_motion_prompt(p, "de", job_id="j")
    assert "German" in str(e.value)


def test_localize_caches_per_language(monkeypatch):
    """The cache is keyed by (language, prompt) — the same prompt must not
    serve a German character its Spanish translation."""
    calls: list[str] = []

    def _tr(lines, *, language="es", re_id=None):
        calls.append(language)
        return ["ES-line"] if language == "es" else ["DE-line"]

    _stub_translate(monkeypatch, _tr)
    p = 'He waves. The person says: "Hello friends."'
    es_out = reengineer.localize_motion_prompt(p, "es")
    de_out = reengineer.localize_motion_prompt(p, "de")
    assert reengineer.localize_motion_prompt(p, "de") == de_out   # cached
    assert calls == ["es", "de"]
    assert "ES-line" in es_out and "DE-line" in de_out
    assert ES.marker in es_out.lower() and DE.marker in de_out.lower()


def test_unknown_language_is_a_noop_not_a_crash(monkeypatch):
    _stub_translate(monkeypatch, _never_translate)
    p = 'He waves. The person says: "Hello friends."'
    for lang in (None, "", "en", "fr", "sv"):
        assert reengineer.localize_motion_prompt(p, lang) == p


def test_translate_dialogue_refuses_an_unknown_target(monkeypatch):
    """No silent fall-through to Spanish — an unknown code returns None, which
    the caller turns into a loud LocalizationError."""
    assert reengineer.translate_dialogue(["hello"], language="fr") is None


def test_german_translate_system_targets_hochdeutsch():
    sys_prompt = DE.translate_system.lower()
    assert "hochdeutsch" in sys_prompt or "standard german" in sys_prompt
    assert "json" in sys_prompt
    assert "dialect" in sys_prompt          # no Bavarian/Swiss/Austrian drift


# --- persistence + API -------------------------------------------------------

def test_sqlite_german_round_trip(tmp_path):
    """The flag must survive a restart under the SQLite backend (the documented
    prod default) — the `characters` table enumerates columns."""
    db = tmp_path / "state.sqlite3"
    s1 = SqliteStateStore(db_path=db)
    s1.add_character(CharacterAsset(char_id="g1", name="Klaus",
                                    filename="k.png"))
    ch = s1.get_character("g1")
    ch.language = "de"
    s1.update_character(ch)
    assert SqliteStateStore(db_path=db).get_character("g1").language == "de"


def test_character_language_live_lookup_de():
    store().add_character(CharacterAsset(
        char_id="cl_de", filename="cl_de.png", name="Klaus", language="de"))
    assert runner._character_language("cl_de") == "de"


def test_patch_language_roundtrip_de():
    store().add_character(CharacterAsset(
        char_id="cp_de", filename="cp_de.png", name="Anna"))
    out = asyncio.run(api.rename_character(
        "cp_de", api.RenameCharacterBody(language="de")))
    assert out["language"] == "de"
    assert store().get_character("cp_de").language == "de"
    # Switching straight from German to Spanish, then clearing to English.
    asyncio.run(api.rename_character("cp_de", api.RenameCharacterBody(language="es")))
    assert store().get_character("cp_de").language == "es"
    asyncio.run(api.rename_character("cp_de", api.RenameCharacterBody(language="en")))
    assert store().get_character("cp_de").language is None


def test_patch_rejects_an_unsupported_language():
    """A typo'd code must 400, not silently store/clear — a flag that looks set
    in the UI while the localizer ignores it ships English clips (Hugo: refuse
    loudly over silent partial)."""
    from fastapi import HTTPException
    store().add_character(CharacterAsset(
        char_id="cp_bad", filename="cp_bad.png", name="Bo", language="de"))
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.rename_character(
            "cp_bad", api.RenameCharacterBody(language="dee")))
    assert e.value.status_code == 400
    # The stored value is untouched by the rejected write.
    assert store().get_character("cp_bad").language == "de"


# --- UI mirror ---------------------------------------------------------------

def test_app_js_language_picker_mirrors_the_registry():
    """The library card's 🗣 picker and the read-only flag map must offer every
    registered language — a server-side language with no UI row is unreachable."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    app_js = (root / "web" / "app.js").read_text()
    html = (root / "web" / "index.html").read_text()
    for code in reengineer.SPOKEN_LANGUAGES:
        assert f"{code}: {{ flag:" in app_js, f"{code} missing from CHAR_LANGUAGES"
        assert f'<option value="{code}">' in html, f"{code} missing from the picker"
    assert '<option value="en">' in html
