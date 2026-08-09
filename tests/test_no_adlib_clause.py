"""The character says the scripted line and NOTHING else.

Hugo 2026-08-10: "ibland gör karaktären små ljud innan eller efter det den ska
säga" — hums, sighs, laughs, throat-clears padding the line. Same defect class
as the invented music bed: every video model SYNTHESIZES the audio from the
prompt, so suppression is prompt-level (no API switch exists). Hugo chose a
prompt clause only — no new QC net, no re-renders.

Two application points, one string (`reengineer.with_no_adlib`):
  * `reengineer.with_accent` — every Reengineer / Swap-from-images scene prompt,
    and what the gate UI mirrors via app.js klingSuffix().
  * `runner._animate_one_video` — the universal backstop, since plain Swap /
    🎬 Animate prompts never pass through with_accent at all.

The hazard this file mostly guards is NOT the clause itself but its collision
with the language machinery: the clause is appended to SILENT shots too, and
`_SPEECH_CONTEXT_RE` / `_unparsed_dialogue_line` would read stray speech words
in it as "this prompt orders speech".
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from character_swap import reengineer, video_qc

_APP_JS = Path(__file__).resolve().parents[1] / "web" / "app.js"


# --- the clause itself ----------------------------------------------------

def test_clause_names_the_sounds_hugo_reported():
    """Hums / sighs / laughs / throat-clearing / breaths before or after the
    line — the literal symptom, not a vague "no extra sounds"."""
    c = reengineer._NO_ADLIB_CLAUSE.lower()
    for term in ("hum", "sigh", "laugh", "throat", "breath", "before or after"):
        assert term in c, f"clause does not cover {term!r}"


def test_with_no_adlib_appends_once():
    assert reengineer.with_no_adlib("She nods.") == (
        "She nods." + reengineer._NO_ADLIB_CLAUSE)


def test_with_no_adlib_is_idempotent():
    once = reengineer.with_no_adlib("She nods.")
    assert reengineer.with_no_adlib(once) == once
    assert once.lower().count("ad-lib") == 1


def test_with_no_adlib_skips_a_prompt_that_already_says_it():
    p = "She nods. No ad-libbed noises please."
    assert reengineer.with_no_adlib(p) == p


def test_with_accent_appends_it_last(monkeypatch):
    """It must sit AFTER the accent/pronounce/music clauses — those carry the
    language guarantees the localizer and its loud nets key on."""
    out = reengineer.with_accent("She says hello.", "en")
    assert out.endswith(reengineer._NO_ADLIB_CLAUSE)


@pytest.mark.parametrize("lang", ["en", "es", "de"])
def test_every_language_gets_it(lang):
    assert reengineer._NO_ADLIB_CLAUSE in reengineer.with_accent("x", lang)


def test_clause_stays_english_for_a_language_character():
    """It is an INSTRUCTION, not speech — like the pronounce/no-music clauses
    it must not be translated into Spanish/German."""
    out = reengineer.with_accent('Ella dice: "hola"', "es")
    assert reengineer._NO_ADLIB_CLAUSE in out


# --- it must not disarm or trip the language machinery --------------------

def test_clause_carries_no_speech_verb():
    """`_SPEECH_CONTEXT_RE` matches says/speaks/spoken/dialogue/voice. The
    clause is appended to SILENT shots too, so a speech verb in it would make
    every wordless B-roll prompt look like it orders speech."""
    assert not reengineer._SPEECH_CONTEXT_RE.search(reengineer._NO_ADLIB_CLAUSE)


def test_without_own_clauses_subtracts_it():
    spec = reengineer.SPOKEN_LANGUAGES["de"]
    stripped = reengineer._without_own_clauses(
        "A jar sits on the counter." + reengineer._NO_ADLIB_CLAUSE, spec)
    assert "ad-lib" not in stripped.lower()


def test_silent_broll_with_a_quoted_label_is_still_not_refused():
    """Regression for the collision, not the clause: a wordless shot whose only
    quote is a long product LABEL must not be read as "orders speech, carries
    an unreadable line". That false refusal is what the 2026-08-08
    `_NO_SPEECH_RE` fix removed — appending a clause mentioning a "scripted
    line" to every silent scene could reintroduce it."""
    spec = reengineer.SPOKEN_LANGUAGES["de"]
    silent = ('SHOT — No dialogue, ambient room tone only. A bottle labeled '
              '"Certified Organic Extra Virgin Olive Oil" sits on the counter.')
    assert reengineer._unparsed_dialogue_line(silent, spec) is None
    with_clause = reengineer.with_no_adlib(silent)
    assert reengineer._unparsed_dialogue_line(with_clause, spec) is None


def test_clause_leaves_no_residual_english_order():
    """`_residual_english_order` raises LocalizationError on any English speech
    order still standing after sanitization — our own clause must not be one."""
    spec = reengineer.SPOKEN_LANGUAGES["es"]
    prompt = reengineer.with_accent('Ella dice: "hola amigos"', "es")
    assert reengineer._residual_english_order(prompt, spec) is None


def test_clause_is_invisible_to_the_dialogue_extractor():
    """It carries no quotes, so expected_speech / the caption script / video
    QC's expected line must be identical with and without it."""
    prompt = 'He says to the camera: "put baking soda on a raw beet today"'
    assert (video_qc.expected_speech(reengineer.with_no_adlib(prompt))
            == video_qc.expected_speech(prompt))


def test_a_silent_prompt_still_expects_no_speech():
    assert video_qc.expected_speech(
        reengineer.with_no_adlib("A jar sits on the counter.")).strip() == ""


# --- coverage: the plain Swap / Animate backstop --------------------------

def test_runner_applies_it_after_localization():
    """`runner._animate_one_video` must call with_no_adlib AFTER the localizer
    (so the clause is never translated) and BEFORE the submit loop's
    `prompt_text = movement_prompt` reset (so every model take carries it)."""
    src = (Path(__file__).resolve().parents[1] / "src" / "character_swap"
           / "runner.py").read_text(encoding="utf-8")
    call = src.index("reengineer.with_no_adlib(movement_prompt)")
    localize = src.index("movement_prompt = localized")
    reset = src.index("        prompt_text = movement_prompt\n", call)
    assert localize < call < reset, "ad-lib lock applied at the wrong point"


def test_js_mirror_matches_python_byte_for_byte():
    """The gate UI promises "the prompt you see + this suffix == the literal
    model input"."""
    js = _APP_JS.read_text(encoding="utf-8")
    m = re.search(r"klingSuffix\(text, lang\)\s*{(.*?)\n    },", js, re.S)
    assert m
    assert reengineer._NO_ADLIB_CLAUSE.strip() in m.group(0)
    assert "'ad-lib'" in m.group(0), "JS guard keyword missing"


def test_js_music_branch_feeds_its_clause_forward():
    """The music branch used to add its clause to `suffix` but not to `out`,
    so every guard after it scanned a prompt missing that text. Harmless while
    music was last; a latent trap the moment a clause follows it."""
    js = _APP_JS.read_text(encoding="utf-8")
    m = re.search(r"klingSuffix\(text, lang\)\s*{(.*?)\n    },", js, re.S)
    body = m.group(0)
    music_at = body.index("No background music")
    tail = body[music_at:music_at + 400]
    assert "out = out.replace(/\\s+$/, '') + clause;" in tail
