"""🇪🇸/🇩🇪 reels OPENED in English — the line was stated TWICE (Hugo 2026-08-08).

Measured on run re_8e87b525b2 (job j_7c2ff95032), scene `sc_f88b5955ae`: the
burned captions of all four language finals (wang, Ching, Helene, Susanne)
begin with the verbatim English source line — "Put baking soda on a raw beet
and see what happens. Doctors don't like this because half their patients would
no longer need appointments." — and only switch to Spanish/German from clip 2
onward. Scene 1 was also the ONLY scene of the seven with
`qc_status="skipped"`, for every one of the four characters.

The AI Director's structured prompt states the spoken line in TWO places:

    ACTION AND CAMERA MOTION — The man … addresses the camera: "<line>"
    AUDIO — … Language: English; … Dialogue: "<line>"

`dialogue_matches` returned the matches of the FIRST pattern that found
anything. `_LABELED_DIALOGUE_RE` matched the AUDIO copy, so the ACTION copy was
never returned — the localizer rewrote one of the two and the English sentence
went to the video model verbatim, sitting EARLIER in the prompt than its own
translation. First-pattern-wins can never be right for a REWRITER: it must see
every occurrence or it leaves one behind.

Two further holes made it silent rather than loud:
  * `addresses` was not in the speech-verb list, so nothing could read the
    ACTION copy even in isolation;
  * with the AUDIO copy translated, the prompt no longer carried a *shape* the
    2026-08-06 residual-English scan recognises, so it passed.

The durable half of the fix is `_surviving_source_line`: after all rewriting,
the ENGLISH line itself must not still be in the prompt. That check knows
nothing about prompt shapes, which is what every previous fix depended on —
2026-06-30, 07-31, 08-02 and 08-06 were each a new shape.
"""
from __future__ import annotations

import pytest

from character_swap import reengineer
from character_swap import video_edit
from character_swap.video_edit import dialogue_matches, extract_dialogue

LINE = ("Put baking soda on a raw beet and see what happens. Doctors don't "
        "like this because half their patients would no longer need "
        "appointments.")

# Verbatim structure of j_7c2ff95032 / sc_f88b5955ae, trimmed to the sections
# that carry speech.
TWICE_STATED = (
    "SHOT — Medium close-up framing capturing the man from the waist up.\n\n"
    "ACTION AND CAMERA MOTION — The man enthusiastically addresses the "
    f'camera: "{LINE}" He then pours all the baking soda onto the beet in a '
    "steady motion.\n\n"
    "AUDIO — Voice: male, enthusiastic, American accent; Language: English; "
    f'Tone: confident, slightly conspiratorial; Dialogue: "{LINE}" Primary '
    "sounds: natural voice, sizzling and fizzing reaction sounds.\n\n"
    "STYLE — Realistic documentary style. No subtitles, captions, text "
    "overlays, or on-screen text."
)


@pytest.fixture
def stub_translate(monkeypatch):
    """Translation is stubbed — these tests pin plumbing, not GPT output.

    The stub returns text that shares NO words with the source, so "did the
    English survive?" is answerable by inspection.
    """
    calls: list[list[str]] = []

    def fake(lines, *, language="es", re_id=None):
        calls.append(list(lines))
        return [f"ES-{i} pon bicarbonato en la remolacha cruda"
                for i, _ in enumerate(lines)]

    monkeypatch.setattr(reengineer, "translate_dialogue", fake)
    monkeypatch.setattr(reengineer, "_LOCALIZE_CACHE", {})
    return calls


# ------------------------------------------------------- the extractor
def test_both_statements_of_the_line_are_found():
    """THE regression. One span = one copy silently left in English."""
    spans = dialogue_matches(TWICE_STATED)
    assert len(spans) == 2, "the ACTION copy is invisible again"
    assert [m.group(1) for m in spans] == [LINE, LINE]
    # ...and in document order, which is the order the localizer rewrites in.
    assert spans[0].span(1)[0] < spans[1].span(1)[0]


def test_addresses_the_camera_is_speech():
    """The ACTION copy's verb, in isolation."""
    assert extract_dialogue(
        'The man enthusiastically addresses the camera: "one two three four '
        'five"') == "one two three four five"


def test_a_repeated_line_is_read_once():
    """Readers must NOT see the sentence twice.

    `extract_dialogue` feeds the caption script, the transcription bias hint,
    video-QC's expected speech and the Kling duration estimate. A doubled line
    would ask for a clip twice as long as the speech and score every clip
    against a script it never says.
    """
    assert extract_dialogue(TWICE_STATED) == LINE


def test_two_different_lines_under_two_shapes_are_both_kept():
    """Deduplication must not collapse genuinely different lines."""
    prompt = ('He addresses the camera: "first line of the script here" '
              'AUDIO — Dialogue: "second and different line entirely"')
    assert extract_dialogue(prompt) == (
        "first line of the script here second and different line entirely")


@pytest.mark.parametrize("clause", [
    # Every remaining shape in Hugo's 1021-prompt history that no pattern could
    # read before 2026-08-08. Each was measured, not invented.
    'beginning to speak with an enthusiastic American accent: "one two three '
    'four five"',                                                   # bare verb
    'enthusiastically declares with a confident American accent, "one two '
    'three four five"',                                     # comma, not colon
    'delivering his line clearly: "one two three four five"',
    'The man enthusiastically speaks to the camera with an American accent, '
    '"one two three four five"',
])
def test_the_last_unreadable_shapes_are_readable(clause):
    """With any of these missed, the line is invisible to BOTH the localizer
    and video-QC at once — the pair of failures that ships a silent English
    clip."""
    assert extract_dialogue(clause) == "one two three four five"


def test_quoted_props_are_still_not_dialogue():
    """The colon+quote guard still holds — no new false positives."""
    assert extract_dialogue(
        'A bottle labeled "Heinz White Vinegar" sits on the counter next to a '
        'sign reading "Fresh Organic Produce Daily Since Nineteen Eighty"'
    ) == ""


# ------------------------------------------------------- the localizer
def test_every_copy_of_the_line_is_translated(stub_translate):
    out = reengineer.localize_motion_prompt(TWICE_STATED, "es")
    assert "Put baking soda on a raw beet" not in out
    assert "Doctors don't like this" not in out
    assert out.count("pon bicarbonato en la remolacha cruda") == 2


def test_a_repeated_line_is_translated_once(stub_translate):
    """Both copies must read IDENTICALLY.

    Sending the same sentence twice bills a longer call and lets GPT-4o return
    two slightly different translations — the prompt would then order the model
    to say two different things.
    """
    reengineer.localize_motion_prompt(TWICE_STATED, "es")
    assert stub_translate == [[LINE]], "the repeated line was translated twice"


def test_the_language_attribution_lands_on_the_first_copy(stub_translate):
    """Kling obeys the language named next to the line it reads FIRST."""
    out = reengineer.localize_motion_prompt(TWICE_STATED, "es")
    action = out.split("ACTION AND CAMERA MOTION")[1].split("AUDIO")[0]
    assert reengineer.SPOKEN_LANGUAGES["es"].attribution in action


@pytest.mark.parametrize("code", ["es", "de"])
def test_both_languages_are_covered(stub_translate, code):
    out = reengineer.localize_motion_prompt(TWICE_STATED, code)
    assert "Put baking soda on a raw beet" not in out


def test_the_second_copys_accent_cannot_suppress_the_first_copys_attribution():
    """The union reopened the 2026-08-02 failure one layer up.

    `_force_language_speech` rewrites an inline "with an American accent" into
    this language's attribution BEFORE `_inject_inline_attribution` runs. When
    that phrase sits in the SECOND copy of the line, the attribution already
    existed *somewhere* in the text, and the old whole-prompt idempotency guard
    skipped injection at the FIRST copy — leaving the sentence the model reads
    first naming no language at all, which is exactly what made 8 of 10 German
    clips speak English in re_b3170d2118.
    """
    spec = reengineer.SPOKEN_LANGUAGES["de"]
    line = "Gib Natron auf eine rohe Rote Bete und schau was passiert."
    prompt = (f'ACTION — The man addresses the camera: "{line}"\n\n'
              f'AUDIO — Deep male voice with an American accent '
              f'enthusiastically: "{line}"')
    out = reengineer._force_language_speech(prompt, "de")
    action = out.split("ACTION")[1].split("AUDIO")[0]
    assert spec.attribution in action.split('"')[0], \
        "the first copy of the line names no language"
    assert "American accent" not in out


def test_injecting_the_attribution_stays_idempotent():
    """...without which a redo would stack "in standard German" every pass."""
    spec = reengineer.SPOKEN_LANGUAGES["de"]
    once = reengineer._force_language_speech(TWICE_STATED, "de")
    twice = reengineer._force_language_speech(once, "de")
    assert once.count(spec.attribution) == twice.count(spec.attribution)


# ------------------------------- the shape-independent backstop
def test_a_surviving_english_line_fails_the_clip_loudly(stub_translate,
                                                        monkeypatch):
    """The guarantee that does not depend on knowing the prompt's shape.

    Simulates the NEXT unknown shape by blinding the extractor to the ACTION
    copy — exactly the state this run shipped in. Before this check that state
    was silent: a prompt still carrying the English sentence was handed to the
    video model and the character spoke it.
    """
    only_audio = [dialogue_matches(TWICE_STATED)[1]]
    monkeypatch.setattr(reengineer, "dialogue_matches",
                        lambda text: only_audio if text == TWICE_STATED
                        else video_edit.dialogue_matches(text))
    with pytest.raises(reengineer.LocalizationError) as e:
        reengineer.localize_motion_prompt(TWICE_STATED, "es")
    assert "Put baking soda on a raw beet" in str(e.value)


def test_a_line_already_in_the_target_language_is_not_a_leak(monkeypatch):
    """No false positive on the documented translate no-op.

    Every `translate_system` orders "a line ALREADY in this language is
    returned EXACTLY as given", and a forced re-localize of a run-level 🗣
    prompt hits that path. The source line is then still present *because it is
    the translation* — subtracting what we wrote before searching is what tells
    the two apart.
    """
    spanish = "Pon bicarbonato de sodio en una remolacha cruda ahora mismo"
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, language="es", re_id=None: list(lines))
    monkeypatch.setattr(reengineer, "_LOCALIZE_CACHE", {})
    out = reengineer.localize_motion_prompt(
        f'He addresses the camera: "{spanish}"', "es", force=True)
    assert spanish in out


# ---------------------- the mixed prompt the 👥 agent produced
# Verbatim structure of vd_2670d7 — Susanne (🇩🇪), re_8e87b525b2 scene 1, as
# actually submitted. The speaker-fix agent rewrote the scene prompt for a
# female character and emitted an ENGLISH line in the ACTION block beside the
# GERMAN translation in the AUDIO block. `localized_movement_prompt` on that
# row is EMPTY: the marker short-circuit saw "standard German" and returned the
# whole prompt untouched, so the clip spoke the English line.
MIXED = (
    "SUBJECT — A woman facing the camera.\n\n"
    f'ACTION AND CAMERA MOTION — The woman enthusiastically addresses the '
    f'camera: "{LINE}" She then pours.\n\n'
    "AUDIO — Voice: enthusiastic; Dialogue in standard German: "
    '"Streue Natron auf eine rohe Rote Bete und sieh, was passiert." '
    "Primary sounds: natural voice.\n\n"
    "STYLE — Realistic documentary style."
)


def test_the_marker_cannot_vouch_for_a_prompt_that_says_two_things():
    """THE Susanne regression.

    One marker phrase was taken as proof the WHOLE prompt was German. That
    inference only holds while a prompt says one thing — and the 👥 speaker-fix
    agent produces prompts that say two.
    """
    assert reengineer._states_more_than_one_line(MIXED)
    # ...while a prompt stating ONE line twice is still vouched for, so the
    # ordinary run-level 🗣 path is not re-billed.
    localized_twin = TWICE_STATED.replace(LINE, "Streue Natron auf eine rohe "
                                          "Rote Bete und sieh, was passiert.")
    assert not reengineer._states_more_than_one_line(localized_twin)


def test_the_mixed_prompt_is_localized_instead_of_waved_through(stub_translate):
    out = reengineer.localize_motion_prompt(MIXED, "de")
    assert out != MIXED, "the prompt was waved through untranslated again"
    assert "Put baking soda on a raw beet" not in out


def test_an_already_localized_prompt_is_still_never_re_billed(stub_translate):
    """The short-circuit must still hold for the normal case, or every clip on
    a run-level 🗣 run pays a translate call it does not need."""
    spanish = ('He says to the camera in neutral Latin American Spanish: '
               '"Pon bicarbonato de sodio en una remolacha cruda."')
    assert reengineer.localize_motion_prompt(spanish, "es") == spanish
    assert stub_translate == [], "a translate call was billed for nothing"


def test_a_legacy_half_localized_prompt_now_reads_bilingual():
    """Known, accepted consequence on OLD data — pinned so it stays a choice.

    The seven clips this bug already shipped have a stored
    `localized_movement_prompt` carrying BOTH the English source line and its
    translation. The old extractor saw only the translation; the union sees
    both, so a rebuild of those runs that does not re-render the clip feeds a
    bilingual line to the captions. That is not a new defect — those clips
    SPEAK the English line, so every caption for them was already wrong — and
    the state cannot be produced again post-fix, since both copies now
    translate. The remedy is to re-render the clip, not to re-read the prompt.
    """
    half = ('ACTION — The man addresses the camera: "Put baking soda on a raw '
            'beet." AUDIO — Dialogue in neutral Latin American Spanish: "Pon '
            'bicarbonato de sodio en una remolacha cruda."')
    got = extract_dialogue(half)
    assert "Put baking soda on a raw beet." in got
    assert "Pon bicarbonato de sodio en una remolacha cruda." in got


def test_short_lines_never_trip_the_backstop(monkeypatch):
    """A three-word line can legitimately reappear inside a translation."""
    assert reengineer._surviving_source_line(
        "… Save this now …", ["Save this now"], ["Guarda esto"]) is None
