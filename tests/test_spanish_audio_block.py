"""🇪🇸 characters silently shipped ENGLISH clips (Hugo 2026-07-31).

Two 🇪🇸-flagged characters (wang, Ching) spoke English on some scenes. The flag
worked and the translation ran — but only on the scenes whose dialogue the
extractor could SEE. `localize_motion_prompt` translates the quoted line it
finds and returns the prompt untouched when it finds none, so an unparsed
prompt shape went to Kling with its ENGLISH instructions intact. Silent: no
error, no ⚠, `localized_movement_prompt` simply stayed empty.

Two real shapes fell through (verified against j_8d010f54b3 / j_4655f5f235):

  A. A prompt with no dialogue at all but still carrying the English accent
     clause — Kling improvises speech, and it was told to improvise it in
     English.
  B. The AI Director's structured prompt, whose line lives in an AUDIO block
     introduced by a voice DESCRIPTION, not a `says` verb or a `Dialogue:`
     label: `AUDIO — Deep, clear male voice speaking English with a thick Texas
     accent enthusiastically: "…"`. Neither existing pattern matched it.

Shape B is the same silent-hole class as the 2026-06-30 `AUDIO — … Dialogue:`
caption miss — the extractor is the single real point of failure, so the fix
lives in the SHARED extractor and every reader gets it.
"""
from __future__ import annotations

import pytest

from character_swap import reengineer
from character_swap.video_edit import (
    DIALOGUE_RE,
    dialogue_matches,
    extract_dialogue,
)

AUDIO_BLOCK = (
    "SHOT — Close-up of the man facing the camera.\n\n"
    "AUDIO — Deep, clear male voice speaking English with a thick Texas accent "
    'enthusiastically: "Did you know that you can cleanse your body of '
    'parasites?" Ambient farm sounds; no music.\n\n'
    "STYLE — Hyper-realistic. Every word is pronounced clearly."
)

ACCENT_ONLY = (" The person speaks fluent American English with a natural "
               "American accent. Every word is pronounced clearly, correctly "
               "and distinctly. No background music — natural ambient room "
               "sound only.")


@pytest.fixture
def no_billing(monkeypatch):
    """Translation is stubbed — these tests pin plumbing, not GPT output."""
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, language="es", re_id=None: ["ES::" + l for l in lines])
    monkeypatch.setattr(reengineer, "_LOCALIZE_CACHE", {})


# --- the shared extractor ----------------------------------------------------

def test_audio_block_line_is_extracted():
    """THE regression: the Director's AUDIO block IS dialogue."""
    assert extract_dialogue(AUDIO_BLOCK) == (
        "Did you know that you can cleanse your body of parasites?")


def test_quoted_prop_is_still_not_speech():
    """The colon-before-quote guard: props must never read as dialogue."""
    prop = 'He holds a bottle labeled "Heinz White Vinegar" to the camera.'
    assert extract_dialogue(prop) == ""
    assert dialogue_matches(prop) == []


def test_voice_description_without_a_quote_is_not_speech():
    assert extract_dialogue("Voice: male, American accent. No music.") == ""


def test_says_clause_still_wins_over_the_audio_block():
    """Patterns are ordered most→least specific: a prompt carrying BOTH an
    explicit says-clause and an AUDIO block resolves through the says-clause,
    so the new fallback can never double-count a line."""
    both = ('He says to the camera: "the real line" '
            'AUDIO — voice speaking English: "the audio block line"')

    matches = dialogue_matches(both)

    assert matches and matches[0].re is DIALOGUE_RE


# --- localization ------------------------------------------------------------

def test_audio_block_prompt_is_localized(no_billing):
    out = reengineer.localize_motion_prompt(AUDIO_BLOCK, "es")

    assert "ES::Did you know" in out                  # line translated in place
    assert not reengineer._has_english_speech_directive(out)
    assert reengineer._ES_LOCALIZED_MARKER in out.lower()


def test_audio_block_english_order_is_flipped_inline(no_billing):
    """The order sits RIGHT NEXT to the line — appending a Spanish clause at
    the end is not enough, the inline English one has to go."""
    out = reengineer.localize_motion_prompt(AUDIO_BLOCK, "es")

    assert "speaking English" not in out
    assert "thick Texas accent" not in out
    assert "speaking in neutral Latin American Spanish" in out


def test_dialogue_free_prompt_still_gets_the_language_swapped(no_billing):
    """Shape A: nothing to translate, but it still ORDERS English."""
    out = reengineer.localize_motion_prompt(ACCENT_ONLY, "es")

    assert "American English" not in out
    assert reengineer._ES_LOCALIZED_MARKER in out.lower()


def test_genuinely_silent_prompt_is_left_alone(no_billing):
    """A visual-only clip must NOT be given a voice — unchanged behavior."""
    silent = ("The person continues the action visible in the image "
              "naturally. Slow push-in on the hands.")

    assert reengineer.localize_motion_prompt(silent, "es") == silent


def test_english_character_is_untouched(no_billing):
    for lang in (None, "en"):
        assert reengineer.localize_motion_prompt(AUDIO_BLOCK, lang) == AUDIO_BLOCK


def test_already_spanish_prompt_is_not_retranslated(no_billing):
    """A full-'es' run localized upstream keeps its user-approved text."""
    es = ('He says to the camera in neutral Latin American Spanish: '
          '"Pon pimienta negra en la piña."')

    assert reengineer.localize_motion_prompt(es, "es") == es


def test_translation_failure_still_fails_the_clip_loudly(monkeypatch):
    """Refuse loudly over silent partial — unchanged for the new shape too."""
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, language="es", re_id=None: None)
    monkeypatch.setattr(reengineer, "_LOCALIZE_CACHE", {})

    with pytest.raises(reengineer.LocalizationError):
        reengineer.localize_motion_prompt(AUDIO_BLOCK, "es")
