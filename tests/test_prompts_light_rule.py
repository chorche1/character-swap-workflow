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
    assert "actual light source" in text


def test_swap_director_budget_boilerplate_appended_in_code():
    """Backlog #33: the system used to demand ~250 words of style/
    integration/negative boilerplate INSIDE every variant prompt, crowding
    out scene-specific anchors. Boilerplate now ships via code-appended
    clauses; the agent is told not to write it and gets a ~120-word budget
    for pure scene content."""
    text = _flat(prompt_director.SWAP_DIRECTOR_SYSTEM)
    assert "do not write it" in text
    assert "appended to every prompt automatically" in text
    assert "~120 words of pure scene-specific content" in text
    assert "every prompt must specify that" not in text
    flat_avoid = _flat(prompt_director.SWAP_AVOID_CLAUSE)
    assert "pasted-in / cutout" in flat_avoid
    assert "identity bleed" in flat_avoid


def test_direct_swap_appends_style_clauses(monkeypatch, tmp_path):
    from character_swap.clients import anthropic_client

    payload = {"intent": "swap", "characters": [
        {"char_id": "c1", "name": "A", "scenes": [
            {"scene_id": "s1", "variants": [
                {"variant_index": 0, "prompt": "Scene-specific anchors."}]}]}]}
    monkeypatch.setattr(anthropic_client, "messages_with_tools",
                        lambda **kw: object())
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: payload)
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "text", "text": str(p)})

    plan = prompt_director.direct_swap(
        user_prompt="x", characters=[("c1", "A", tmp_path / "c.png")],
        scenes=[("s1", tmp_path / "s.png")], images_per_character=1)
    assert plan is not None
    (p,) = plan.lookup("c1", "s1")
    assert p.startswith("Scene-specific anchors.")
    assert "ordinary, unedited iPhone photo" in p     # ORGANIC_STYLE_CLAUSE
    assert "Avoid: a pasted-in / cutout" in p         # SWAP_AVOID_CLAUSE


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
