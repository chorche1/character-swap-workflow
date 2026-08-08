"""🇪🇸/🇩🇪 clips spoke ENGLISH again — five unmet prompt shapes (Hugo 2026-08-06).

Measured on run re_db8e7b4e91 (job j_bedcc4d1f5) + j_2b5ddcbc3e: FOUR clips
shipped speaking the verbatim ENGLISH source line ("Drop your frozen shrimp in
warm water…") for all four language characters, one more (vd_295249) came out
Spanish with an English tail, and **20 of 76** language clips had video-QC
silently skipped. Every clip was transcribed to confirm this, not inferred.

The AI Director writes its AUDIO block in free-form English while the localizer
recognises only a narrow set of literal phrasings. Five shapes leaked:

  A. `AUDIO — … Dialogue exact: "…"` — `_LABELED_DIALOGUE_RE` demanded the
     colon IMMEDIATELY after the label, so the line was invisible. Nothing was
     translated AND `video_qc` got no expected speech, so `_transcribe`
     returned None → `qc_status="skipped"`. Both the translator and its safety
     net read the SAME extractor, so one miss disarmed both at once. This is
     how the four English clips shipped.
  B. `Dialogue in standard German: "…"` — the localizer's OWN
     `_inject_inline_attribution` output. It blinded the extractor on the very
     prompt it had just repaired, which is why 16 *correctly translated* clips
     also lost their QC.
  C. `Voice: Male, American accent` — bare, no "with an" preposition.
  D. `Language: English. Accent: American.` — labeled fields, never stripped.
  E. `speaking clearly and enthusiastically in English` — adverbs between the
     verb and "English", so `_EN_SPEECH_ORDER_RE` missed it. Left standing next
     to the appended Spanish attribution, this produced vd_295249's English
     tail.

The durable half of the fix is that a shape we have NOT met is now LOUD:
`_sanitized_language_prompt` re-scans its own output, and a prompt that plainly
carries a line the extractor cannot read is refused before a credit is spent
(Hugo's "refuse loudly over silent partial").
"""
from __future__ import annotations

import pytest

from character_swap import reengineer
from character_swap.video_edit import extract_dialogue

ES = reengineer.SPOKEN_LANGUAGES["es"]
DE = reengineer.SPOKEN_LANGUAGES["de"]

# Verbatim from j_bedcc4d1f5 scene sc_f863e7e1d1 — the prompt behind all four
# English clips. Shortened to the two sections that matter.
SHRIMP_PROMPT = (
    "SHOT — Close-up, still camera focused on the man holding a frozen "
    "shrimp with tweezers.\n\n"
    "AUDIO — Voice: Male, American accent, clear and confident tone conveying "
    'an educational message. Dialogue exact: "Drop your frozen shrimp in warm '
    'water. See that white film peeling off? That is sodium '
    'tripolyphosphate." Primary sounds include the subtle motions of water.\n\n'
    "STYLE — Realistic visual aesthetic with high-definition clarity (4K)."
)


# ---------------------------------------------------------------- shape A
def test_dialogue_exact_line_is_extractable():
    """A qualifier between the label and the colon no longer hides the line.

    With this reverted, `extract_dialogue` returns "" — which is simultaneously
    "nothing to translate" and "nothing for QC to check".
    """
    assert extract_dialogue(SHRIMP_PROMPT) == (
        "Drop your frozen shrimp in warm water. See that white film peeling "
        "off? That is sodium tripolyphosphate.")


@pytest.mark.parametrize("label", [
    'Dialogue exact: "one two three four five"',
    'Dialogue in standard German: "one two three four five"',
    'Dialogue in neutral Latin American Spanish: "one two three four five"',
    'Dialogue: "one two three four five"',
    'Voice-over: "one two three four five"',
])
def test_labeled_dialogue_shapes_all_parse(label):
    assert extract_dialogue(f"AUDIO — {label}") == "one two three four five"


@pytest.mark.parametrize("text", [
    # A quoted PROP is still not speech — no label.
    'VISUAL — a bottle labeled "Heinz White Vinegar" on the counter.',
    # The widened body must not cross a sentence boundary into an unrelated
    # labeled quote.
    'AUDIO — Dialogue and ambient sounds. Voice: "soft appliance hum"',
    # …nor cross a second colon.
    'AUDIO — Dialogue, tone: informative: "not the line"',
])
def test_widened_label_still_rejects_non_speech(text):
    assert extract_dialogue(text) == ""


# ---------------------------------------------------------------- shape B
def test_localizer_does_not_blind_the_extractor_on_its_own_output():
    """The attribution injection must leave the line readable.

    This is what silently skipped QC on 16 correctly-translated clips: the
    prompt was right, but nothing could check it.
    """
    src = 'AUDIO — Voice: male. Dialogue: "A chemical coating on cheap shrimp."'
    out = reengineer._force_language_speech(src, "de")

    assert "Dialogue in standard German:" in out
    assert extract_dialogue(out) == "A chemical coating on cheap shrimp."


# ------------------------------------------------------------- shapes C/D/E
@pytest.mark.parametrize("english_order", [
    "Voice: Male, American accent, clear and confident tone.",   # C
    "Language: English. Accent: American.",                      # D
    "Voice of a middle-aged man speaking clearly and enthusiastically "
    "in English.",                                               # E
])
def test_english_orders_are_detected_and_removed(english_order):
    assert reengineer._has_english_speech_directive(english_order)
    out = reengineer._force_language_speech(f"AUDIO — {english_order}", "es")
    assert reengineer._residual_english_order(out, ES) is None


def test_labeled_voice_fields_are_relabeled_not_deleted():
    """A voice field that names NO language reads as English to the model —
    the 2026-08-02 lesson. Replace, never delete."""
    out = reengineer._force_language_speech(
        "AUDIO — Voice: male. Language: English. Accent: American.", "de")
    assert "Language: German" in out
    assert "Accent: standard German" in out
    assert "English" not in out.replace(DE.speak_only_clause, "")


def test_attribution_is_not_doubled():
    """vd_295249: replacing the English order landed the attribution next to
    one the prompt already carried."""
    src = ("Voice of a man speaking clearly and enthusiastically in English "
           "in neutral Latin American Spanish.")
    out = reengineer._force_language_speech(src, "es")
    assert out.count(ES.attribution) == 1


def test_spanish_clause_survives_the_bare_accent_rule():
    """"neutral Latin American Spanish accent" contains "American" — the bare
    rule must not eat our own clause."""
    out = reengineer._force_language_speech(
        "AUDIO — Dialogue: \"uno dos tres cuatro cinco\"." + ES.accent_clause,
        "es")
    assert ES.accent_clause.strip() in out


# ------------------------------------------------- the loud structural nets
def test_unreadable_spoken_line_fails_loudly():
    """A prompt that plainly carries a line we cannot read must NOT render.

    Before: it rendered with the English line intact and `qc_status="skipped"`
    — no error, no ⚠, nothing to notice. This is the exact prompt from the four
    broken clips, with the label mangled so the widened regex cannot save it.
    """
    unreadable = SHRIMP_PROMPT.replace("Dialogue exact:", "Spoken words are")
    assert extract_dialogue(unreadable) == ""

    with pytest.raises(reengineer.LocalizationError) as e:
        reengineer.localize_motion_prompt(unreadable, "es")
    assert "cannot read" in str(e.value)


def test_survived_english_order_fails_loudly(monkeypatch):
    """The next unmet shape is loud, not silent.

    Simulated by neutering the strippers — whatever the sanitizer fails to
    remove, the assertion catches before submit, at zero render cost.
    """
    monkeypatch.setattr(reengineer, "_force_language_speech",
                        lambda text, language: text)
    with pytest.raises(reengineer.LocalizationError) as e:
        reengineer._sanitized_language_prompt(
            "AUDIO — Voice: Male, American accent.", "de", None)
    assert "still orders English" in str(e.value)


def test_a_silent_clip_is_still_left_alone():
    """The loud nets must not fire on a genuinely wordless shot.

    Hardened deliberately: `with_accent` appends the English accent + pronounce
    + no-music clauses to EVERY scene, silent ones included, and the prop quote
    here is 4 words. Neither may be read as "this clip speaks English"."""
    silent = ('SHOT — Close-up of hands pouring vinegar.\n'
              'VISUAL — a bottle labeled "Heinz Distilled White Vinegar".\n'
              "AUDIO — Ambient kitchen sounds only.")
    assert reengineer._unparsed_dialogue_line(
        reengineer.with_accent(silent), DE) is None
    assert reengineer.localize_motion_prompt(silent, "de") == silent


def test_translated_prompt_passes_the_residual_scan():
    """Our OWN clauses say "No English is spoken at any point" — the scan must
    not read that as an English order and refuse every good clip."""
    for lang, spec in (("es", ES), ("de", DE)):
        out = reengineer._force_language_speech(
            'AUDIO — Dialogue: "una dos tres cuatro cinco".', lang)
        assert spec.speak_only_clause.strip() in out
        assert reengineer._residual_english_order(out, spec) is None
