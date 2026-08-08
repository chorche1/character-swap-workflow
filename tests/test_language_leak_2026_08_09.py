"""A right double quote OPENING a line made both the translator and its net blind.

re_8183e63223 (j_40ca799535) rendered all nine of Frank's 🇩🇪 clips carrying the
verbatim ENGLISH source line, inside a prompt that ended "The person speaks ONLY
German … No English is spoken at any point." Nothing warned: not the localizer,
not video-QC, not the two loud nets added on 2026-08-06 and 2026-08-08.

The whole cause is one character. Hugo's prompts open the line with `”` (U+201D,
a RIGHT double quote) and close it with a straight `"`:

    He says enthusiastically to the camera with an american accent: ”Put baking
    soda on oranges and just watch what happens." while he pours …

Every extractor pattern demanded `"` or `“` to OPEN. So `dialogue_matches`
returned nothing, `localize_motion_prompt` had no phrase to translate, and it
rewrote only the accent clause — producing a German ORDER wrapped around an
English SENTENCE. Measured over the 1772 distinct movement prompts on disk, 52
carry a ≥5-word spoken line no pattern could read and every one of them opens
with `”`; widening the opener makes 56 lines readable and changes ZERO of the
lines that already parsed.

The structural half is the one worth keeping. `reengineer._QUOTED_LINE_RE` — the
net whose entire job is "catch a line the extractor could not read" — was
written `["“]([^"”]{8,})["”]`, the SAME opening class as the extractor. A shape
the extractor cannot see was therefore equally invisible to its guard, so one
character class disarmed both layers at once, silently. That is the 2026-08-06
lesson ("the translator and its safety net read the same extractor") recurring
one level down. The net now asks a shape-independent question instead: a long
run between ANY two quote-like characters, deliberately wider than every
extractor pattern. `test_the_net_is_not_written_in_the_extractors_alphabet` is
the durable one — it fails if anyone ever re-couples them.
"""
from __future__ import annotations

import pytest

from character_swap import reengineer as R
from character_swap import video_edit

# The exact scene-0 prompt of re_8183e63223 that shipped nine English clips.
REAL_PROMPT = (
    'He says enthusiastically to the camera with an american accent: ”Put baking '
    'soda on oranges and just watch what happens. Pharmacies hate this because '
    'half their customers would disappear overnight." while he pours a ton of '
    'baking soda on the oranges. The orange and baking soda starts boiling with '
    'bubbles when the baking soda hits the oranges.'
)
REAL_LINE = ("Put baking soda on oranges and just watch what happens. Pharmacies "
             "hate this because half their customers would disappear overnight.")

DE = R.SPOKEN_LANGUAGES["de"]


# --------------------------------------------------------------------------
# The extractor now reads a line opened with `”`
# --------------------------------------------------------------------------

def test_the_prompt_that_shipped_english_is_now_readable():
    assert video_edit.extract_dialogue(REAL_PROMPT) == REAL_LINE


def test_says_clause_accepts_a_right_double_quote_opener():
    prompt = 'He says to the camera: ”one two three four five"'
    assert video_edit.extract_dialogue(prompt) == "one two three four five"


def test_labeled_line_accepts_a_right_double_quote_opener():
    prompt = 'AUDIO — Voice: male. Dialogue: ”one two three four five"'
    assert video_edit.extract_dialogue(prompt) == "one two three four five"


def test_speech_verb_line_accepts_a_right_double_quote_opener():
    prompt = 'ACTION — The man addresses the camera: ”one two three four five"'
    assert video_edit.extract_dialogue(prompt) == "one two three four five"


@pytest.mark.parametrize("opener", ['"', "“", "”"])
def test_every_opener_yields_the_same_line(opener):
    """The three openers are interchangeable — a prompt must not mean something
    different because of which quote glyph the keyboard produced."""
    prompt = f'He says to the camera: {opener}one two three four five"'
    assert video_edit.extract_dialogue(prompt) == "one two three four five"


def test_a_readable_line_is_not_rewritten_by_the_widening():
    """Guard against the fix changing what already worked: the straight-quote
    form must still yield exactly the same phrase, inner quote pair and all."""
    prompt = ('The man says to the camera: "Comment "Skin" below and I will '
              'send it"')
    assert video_edit.extract_dialogue(prompt) == (
        'Comment "Skin" below and I will send it')


# --------------------------------------------------------------------------
# The localizer no longer produces a German order around an English sentence
# --------------------------------------------------------------------------

def test_localizer_finds_the_line_instead_of_only_swapping_the_accent():
    """The pre-fix failure in one assertion: `dialogue_matches` returning []
    is what sent `localize_motion_prompt` down the no-dialogue branch, where
    it swapped the accent clause and left the English sentence standing."""
    matches = video_edit.dialogue_matches(REAL_PROMPT)
    assert [m.group(1).strip() for m in matches] == [REAL_LINE]


def test_the_shipped_prompt_would_not_have_been_caught_by_the_old_net():
    """Why nothing warned. With the line unreadable, the ONLY thing standing
    between the run and nine English clips was `_unparsed_dialogue_line` — and
    the pre-fix `["“]` class could not see this quote either."""
    old = R.re.compile(r'["“]([^"”]{8,})["”]')
    assert old.search(REAL_PROMPT) is None
    assert R._QUOTED_LINE_RE.search(REAL_PROMPT) is not None


# --------------------------------------------------------------------------
# THE DURABLE ONE: the net must not share the extractor's alphabet
# --------------------------------------------------------------------------

# Guillemets: a quote shape NO extractor pattern knows. It stands in for the
# NEXT unmet form, whatever it turns out to be.
UNKNOWN_SHAPE = (
    'He says enthusiastically to the camera with an american accent: «Put '
    'baking soda on oranges and just watch what happens» while he pours.'
)


def test_the_net_is_not_written_in_the_extractors_alphabet():
    """The property the whole incident is about: a quoted LINE the extractor
    cannot read must still be VISIBLE to the net. If someone ever narrows
    `_QUOTED_LINE_RE` back to the extractor's own opening class, this fails."""
    assert not video_edit.dialogue_matches(UNKNOWN_SHAPE)
    assert R._unparsed_dialogue_line(UNKNOWN_SHAPE, DE) == (
        "Put baking soda on oranges and just watch what happens")


def test_an_unreadable_line_fails_the_clip_loudly():
    """…and being visible must mean REFUSED, not rendered blind. No API call:
    the refusal happens before anything is translated."""
    with pytest.raises(R.LocalizationError) as err:
        R.localize_motion_prompt(UNKNOWN_SHAPE, "de")
    assert "cannot read" in str(err.value)


def test_the_net_admits_every_opener_the_extractor_does():
    """Structural: the net's quote class must be a SUPERSET of the extractor's
    openers, in both directions of the pair. A cheap invariant that catches
    re-coupling even if the patterns are rewritten wholesale."""
    for glyph in ('"', "“", "”"):
        assert glyph in R._QUOTE_CHARS, (
            f"the extractor opens lines with {glyph!r} but the net cannot see it")


# --------------------------------------------------------------------------
# The widened net must not refuse prompts that are genuinely silent
# --------------------------------------------------------------------------

def test_a_quoted_prop_in_a_silent_shot_is_not_refused():
    silent = ('SHOT — a still close-up of a bottle labeled "Heinz White Vinegar" '
              'on a wooden counter. No dialogue, ambient room tone only.')
    assert R._unparsed_dialogue_line(silent, DE) is None


def test_apostrophes_are_not_treated_as_quote_pairs():
    """`’` is the ordinary apostrophe in "isn't". Admitting it to the net's
    class would pair up two unrelated contractions and refuse a prompt that
    contains no quoted line at all."""
    apos = ("SHOT — the man’s hands work the dough; it isn’t rushed and he "
            "doesn’t look up. No dialogue, ambient room tone only.")
    assert R._unparsed_dialogue_line(apos, DE) is None
    assert "’" not in R._QUOTE_CHARS


def test_a_readable_line_never_reaches_the_net():
    """The net only ever fires when the extractor came up empty — a line it CAN
    read is translated, not refused."""
    readable = 'He says to the camera: ”one two three four five six"'
    assert video_edit.dialogue_matches(readable)
    assert R._unparsed_dialogue_line(readable, DE) is not None  # visible…
    # …but localize_motion_prompt never consults it, because phrases exist.
    assert [m.group(1).strip()
            for m in video_edit.dialogue_matches(readable)] == [
        "one two three four five six"]


# --------------------------------------------------------------------------
# A PAIR is also a shape. Two more forms are on disk right now and neither the
# extractor nor the pair-net above can see them: a line in SINGLE quotes, and
# a quote that opens and never closes. Both prompts below are verbatim from
# Hugo's own history, and both would ship a 🇪🇸/🇩🇪 clip speaking English.
# --------------------------------------------------------------------------

# re_6f4fd… — the quote opens after the colon and the sentence just ends.
UNCLOSED_PROMPT = (
    'He says enthusiastically to the camera with an american accent: "Number 5, '
    'tap water. Especially in older pipes. Every word is pronounced clearly, '
    'correctly and distinctly.'
)

# The same shape a Swede types — nothing about it is closed either.
UNCLOSED_SWEDISH = UNCLOSED_PROMPT.replace(': "Number', ': ”Number')

SINGLE_QUOTED_PROMPT = (
    "He says enthusiastically to the camera with an american accent: 'Pour "
    "mouthwash on your feet and thank me later.' while he pours the bottle "
    "over his foot."
)


@pytest.mark.parametrize("prompt, expected", [
    (UNCLOSED_PROMPT, "Number 5, tap water."),
    (UNCLOSED_SWEDISH, "Number 5, tap water."),
    (SINGLE_QUOTED_PROMPT, "Pour mouthwash on your feet"),
])
def test_a_line_without_a_closing_pair_is_refused_not_shipped(prompt, expected):
    """The pair-net cannot see either shape — there is no second quote to pair
    with in one, and `'` is deliberately absent from `_QUOTE_CHARS` in the
    other. Both must still refuse LOUDLY: this is the exact position the ”
    opener was in before 4d2f12b, one shape further along."""
    assert not video_edit.dialogue_matches(prompt)
    hit = R._unparsed_dialogue_line(prompt, DE)
    assert hit and expected in hit
    with pytest.raises(R.LocalizationError) as err:
        R.localize_motion_prompt(prompt, "de")
    assert "cannot read" in str(err.value)


def test_an_apostrophe_never_opens_an_unterminated_line():
    """The single-quote probes must carry the apostrophe guard the pair-net
    got for free by excluding `’`. Written without the `(?<!\\w)` lookbehind,
    the `'` in "doesn't look away" is read as an opening quote and everything
    after it becomes an unterminated line — measured, that produced two false
    refusals on real prompts, one of them a pure camera description.

    Both fixtures below are chosen to actually EXERCISE the guard: each has a
    speech context AND ≥5 words after the apostrophe, so the ≥5-word rule and
    the speech gate cannot be what saves them."""
    for apos in (
        "He says enthusiastically to the camera with an american accent while "
        "he pours. He doesn't look away from the bright orange bowl on the "
        "counter.",
        "Medium shot. She speaks with an american accent while she works; the "
        "dough isn't rushed and she keeps folding it over and over again.",
    ):
        # The guard is load-bearing here, not the word count or the gate.
        unguarded = R.re.compile(
            "[" + R._QUOTE_CHARS + "'‘]([^" + R._QUOTE_CHARS + "'‘’]{8,})$")
        assert unguarded.search(apos), "fixture no longer probes the guard"
        assert R._unparsed_dialogue_line(apos, DE) is None


def test_a_prompt_that_orders_speech_but_supplies_no_line_still_renders():
    """The refusals above must not swallow the IMPROVISATION pattern. Three
    real Director prompts order an accent and deliberately give no words; they
    work today (the accent clause swaps to the target language and the model
    improvises in it), and refusing them would be a 75% false-refusal rate on
    the population this branch fires on."""
    improvised = (
        "Medium close-up, static camera. He says enthusiastically to the "
        "camera with an american accent while he continuously pours out all "
        "the mouthwash onto his foot. Animated expression."
    )
    assert R._unparsed_dialogue_line(improvised, DE) is None
    out = R.localize_motion_prompt(improvised, "de")
    assert DE.marker in out.lower()          # the order was still swapped…
    assert "american accent" not in out.lower()   # …and English removed


def test_strip_quotes_removes_every_quote_the_net_knows():
    """A translated line is spliced BETWEEN the prompt's own quotes, so any
    quote character it brings unbalances the clause for the next reader. The
    translator is asked for German and Spanish, whose native quotes are `„…“`
    and `«…»` — exactly the characters the old three-way strip missed."""
    for ch in R._QUOTE_CHARS:
        assert ch not in R._strip_quotes(f"sag {ch}etwas{ch} bitte")
    spliced = R._strip_quotes('Er sagt „Hallo“ und «tschüss»')
    assert spliced == "Er sagt Hallo und tschüss"
