"""Backlog #18 (2026-06-12): no template may MANDATE daylight.

The reengineer light_rule was fixed earlier (organic everyday light), but
SWAP_DIRECTOR_SYSTEM and the static pipeline prompts still hardcoded
'mundane natural daylight' / 'mundane ambient daylight' — flat wrong for
indoor or evening scenes, fighting the scene's actual light. The wording
must anchor light to WHAT THE SCENE ACTUALLY HAS (daylight, lamps, evening
mix); 'daylight' may only appear as one example in that list.
"""
from __future__ import annotations

import re

from character_swap import pipeline, prompt_director

_MANDATES = (
    "mundane natural daylight",
    "mundane ambient daylight",
    "plain ambient daylight",
)


def _flat(s: str) -> str:
    return " ".join(s.split()).lower()


def test_swap_director_system_does_not_mandate_daylight():
    text = _flat(prompt_director.SWAP_DIRECTOR_SYSTEM)
    for m in _MANDATES:
        assert m not in text, m
    assert "as the scene actually has it" in text
    assert "whatever it actually is" in text


def test_generation_prompt_does_not_mandate_daylight():
    text = _flat(pipeline.GENERATION_PROMPT)
    for m in _MANDATES:
        assert m not in text, m
    assert "as it appears there" in text


def test_movement_director_is_organic_not_cinematic():
    """Backlog #29 (audit 2026-06-12): the movement Director mandated 'Shot
    on cinema camera, 24fps, shallow depth of field' — contradicting both
    the organic phone-photo doctrine (start frames are ordinary phone
    photos) and the Kling I2V research (environment re-description causes
    camera cuts)."""
    text = _flat(prompt_director.MOVEMENT_DIRECTOR_SYSTEM)
    assert "cinema camera" not in text.replace("never cinema cameras", "")
    assert "shallow depth of field" not in text.replace(
        "dolly moves, shallow depth of field or film jargon", "")
    assert "handheld phone footage with subtle micro-shake" in text
    assert "never describe the environment" in text
    assert "pronounced" in text


def test_gpt_id_swap_prompts_do_not_mandate_daylight():
    src = open(pipeline.__file__, encoding="utf-8").read()
    # No remaining mandate anywhere in the module's template strings —
    # 'daylight' is allowed only inside the example list '(window daylight,'
    # / '(daylight, lamp light'.
    flat = _flat(src)
    for m in _MANDATES:
        assert m not in flat, m
    for hit in re.finditer(r"daylight", flat):
        ctx = flat[max(0, hit.start() - 40):hit.start()]
        assert "(window " in ctx or "(daylight" in flat[hit.start() - 1:hit.start() + 9] or "(" in ctx[-12:], (
            f"unexpected daylight mandate near: ...{ctx}")
