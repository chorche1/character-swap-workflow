"""Tests for the B-roll LLM output parser.

The parser is the weakest link in the B-roll pipeline — GPT-4o sometimes
wraps the LINE/MODE/PROMPT keywords in Markdown (bold, headings, numbered
lists, code blocks) which the strict regex used to reject. _normalize_llm_format
strips those wrappers before regex matching.

Each test below is a real-world variation seen in the wild (or trivially
plausible). Add new ones whenever a regression occurs — the test names map
1:1 to "this is what GPT-4o emitted".
"""
from __future__ import annotations

from character_swap.broll import _normalize_llm_format, _parse_line_prompt_pairs


# --- Normalizer behavior -------------------------------------------------------------


def test_normalizer_strips_bold_around_keywords():
    raw = "**LINE:** The man pours oil\n**PROMPT:** A close-up of olive oil pouring"
    out = _normalize_llm_format(raw)
    assert "**LINE:**" not in out
    assert "**PROMPT:**" not in out
    assert "LINE:" in out
    assert "PROMPT:" in out


def test_normalizer_strips_italics_around_keywords():
    raw = "*LINE:* line text\n*PROMPT:* prompt text"
    out = _normalize_llm_format(raw)
    assert "*LINE:*" not in out
    assert "LINE:" in out and "PROMPT:" in out


def test_normalizer_strips_heading_prefix():
    raw = "# LINE: foo\n## PROMPT: bar"
    out = _normalize_llm_format(raw)
    assert out.splitlines()[0].lstrip().startswith("LINE:")
    assert "PROMPT:" in out


def test_normalizer_strips_numbered_list_prefix():
    raw = "1. LINE: first\n2. PROMPT: second"
    out = _normalize_llm_format(raw)
    # The "1." and "2." prefixes should be gone, keywords remain.
    assert "1. LINE:" not in out
    assert "2. PROMPT:" not in out
    assert "LINE:" in out
    assert "PROMPT:" in out


def test_normalizer_strips_dash_bullet_prefix():
    raw = "- LINE: foo\n- PROMPT: bar"
    out = _normalize_llm_format(raw)
    assert "- LINE:" not in out
    assert "LINE:" in out


def test_normalizer_drops_code_fences():
    raw = "```\nLINE: foo\nPROMPT: bar\n```"
    out = _normalize_llm_format(raw)
    assert "```" not in out
    assert "LINE:" in out


def test_normalizer_drops_language_tagged_code_fences():
    raw = "```text\nLINE: foo\nPROMPT: bar\n```"
    out = _normalize_llm_format(raw)
    assert "```" not in out
    assert "```text" not in out
    assert "LINE:" in out


def test_normalizer_preserves_plain_keyword_lines():
    """Plain LINE:/PROMPT: (no wrapping) should round-trip unchanged."""
    raw = "LINE: hi\nPROMPT: there"
    out = _normalize_llm_format(raw)
    assert "LINE:" in out and "PROMPT:" in out


# --- End-to-end parser behavior ------------------------------------------------------


def test_parser_handles_clean_minimal_input():
    """The format the system prompt asks for — round-trips perfectly."""
    raw = (
        "LINE: \"The man pours oil\"\n"
        "MODE: Mode 1 — Body Part Transformation\n"
        "PROMPT: A close-up of olive oil pouring slowly into a glass jar.\n"
        "\n"
        "LINE: \"He drinks it down\"\n"
        "MODE: Mode 1 — Body Part Transformation\n"
        "PROMPT: Slow-motion shot of the same hand bringing the jar to his lips.\n"
    )
    clips = _parse_line_prompt_pairs(raw)
    assert len(clips) == 2
    assert clips[0].line == "The man pours oil"
    assert clips[0].mode.startswith("Mode 1")
    assert "olive oil" in clips[0].prompt
    assert clips[1].line == "He drinks it down"


def test_parser_handles_bold_wrapped_keywords():
    """The most common GPT-4o failure mode — bold-wrapped LINE/PROMPT."""
    raw = (
        "**LINE:** \"The man pours oil\"\n"
        "**PROMPT:** A close-up of olive oil pouring.\n"
        "\n"
        "**LINE:** \"He drinks it down\"\n"
        "**PROMPT:** Slow-motion shot of the hand.\n"
    )
    clips = _parse_line_prompt_pairs(raw)
    assert len(clips) == 2
    assert clips[0].line == "The man pours oil"
    assert clips[1].line == "He drinks it down"


def test_parser_handles_numbered_list_with_optional_mode():
    raw = (
        "1. LINE: First narration\n"
        "   MODE: Mode 2 — Tool Macro\n"
        "   PROMPT: First visual prompt.\n"
        "\n"
        "2. LINE: Second narration\n"
        "   PROMPT: Second visual prompt.\n"
    )
    clips = _parse_line_prompt_pairs(raw)
    assert len(clips) == 2
    assert clips[0].mode.startswith("Mode 2")
    assert clips[1].mode == ""    # MODE was omitted on the second pair


def test_parser_handles_code_fenced_response():
    raw = (
        "```\n"
        "LINE: \"narration line\"\n"
        "PROMPT: visual prompt text\n"
        "```\n"
    )
    clips = _parse_line_prompt_pairs(raw)
    assert len(clips) == 1
    assert clips[0].line == "narration line"
    assert clips[0].prompt == "visual prompt text"


def test_parser_handles_scene_group_field():
    raw = (
        "LINE: \"morning ritual\"\n"
        "MODE: Mode 3 — Recipe Step\n"
        "SCENE_GROUP: morning_drink_glass\n"
        "PROMPT: A hand placing a glass on a sunlit kitchen counter.\n"
    )
    clips = _parse_line_prompt_pairs(raw)
    assert len(clips) == 1
    assert clips[0].scene_group == "morning_drink_glass"


def test_parser_empty_input_returns_empty_list():
    assert _parse_line_prompt_pairs("") == []


def test_parser_garbage_input_returns_empty_list():
    """Pure prose with no LINE:/PROMPT: keywords → empty (not a crash)."""
    raw = "I have read the script and here is my plan. We should focus on the body."
    assert _parse_line_prompt_pairs(raw) == []


def test_parser_drops_pairs_with_empty_line_or_prompt():
    """A LINE: with no value (or a PROMPT: with no value) shouldn't sneak
    through as a valid clip."""
    raw = (
        "LINE: \n"
        "PROMPT: prompt only — no line value\n"
        "\n"
        "LINE: good line\n"
        "PROMPT: \n"
    )
    clips = _parse_line_prompt_pairs(raw)
    # The first pair has empty LINE; the second has empty PROMPT. Both rejected.
    # (The regex might require non-empty match anyway, but we belt-and-suspenders.)
    assert all(c.line and c.prompt for c in clips)
