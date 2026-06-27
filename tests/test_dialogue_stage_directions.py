"""Stage directions inside dialogue quotes must NEVER reach captions
(Hugo 2026-06-27: "these final videos had captions that included some text
that they didn't say").

The honey run (re_2266fb21b7 / j_8b5cfe2bad) had a motion prompt with the
parenthetical stage directions written INSIDE the spoken quotes:

    He says ...: "This is store-bought honey (while he points at the honey
    without the bees on it), and this is raw natural honey. (while he points
    at the honey with the bees on it)"

Whisper couldn't read that Kling clip, so the caption fallback burned the
KNOWN line verbatim — parentheticals and all. `extract_dialogue` (compile +
video_qc) and `runner_reengineer._spoken_text` (reengineer captions) must
strip the `(...)` / `[...]` directions and heal the spacing/punctuation they
leave behind.
"""
from __future__ import annotations

from character_swap import video_edit
from character_swap.runner_reengineer import _spoken_text


HONEY_PROMPT = (
    'He says enthusiastically to the camera with an american accent: '
    '"This is store-bought honey (while he points at the honey without the '
    'bees on it), and this is raw natural honey. (while he points at the '
    'honey with the bees on it)" Every word is pronounced clearly.'
)


def test_extract_dialogue_strips_inline_stage_directions():
    out = video_edit.extract_dialogue(HONEY_PROMPT)
    assert out == "This is store-bought honey, and this is raw natural honey."
    assert "(" not in out and ")" not in out
    assert "points" not in out and "bees" not in out


def test_extract_dialogue_strips_leading_and_bracket_directions():
    prompt = ('He says: "(while pointing at the left salmon) This is farmed '
              'salmon [gestures], and this is wild caught."')
    out = video_edit.extract_dialogue(prompt)
    assert out == "This is farmed salmon, and this is wild caught."


def test_extract_dialogue_keeps_plain_dialogue_untouched():
    prompt = 'He says: "Never refrigerate your apples." He says nothing else.'
    assert video_edit.extract_dialogue(prompt) == "Never refrigerate your apples."


def test_extract_dialogue_keeps_inner_quote_cta():
    # The CTA inner-quote handling (2026-06-26) must survive the strip step.
    prompt = 'He says: "Comment "detox" and i will send it to you."'
    out = video_edit.extract_dialogue(prompt)
    assert "Comment" in out and "detox" in out and "send it to you" in out


def test_reengineer_spoken_text_strips_directions():
    out = _spoken_text({"motion_prompt": HONEY_PROMPT})
    assert out == "This is store-bought honey, and this is raw natural honey."


def test_reengineer_spoken_text_falls_back_to_speech_stripped():
    out = _spoken_text({"motion_prompt": "no quotes here",
                        "speech": "Drink the tea (while pouring it)."})
    assert out == "Drink the tea."
