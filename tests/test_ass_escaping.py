"""ASS caption text escaping (2026-07-02 reliability audit).

Word text used to be interpolated VERBATIM into the ASS Dialogue line. libass
parses `{...}` as an override block and renders NONE of it — one stray brace
in a word (a Whisper artifact, or a free-text edit from the visual caption
editor) silently swallowed caption text. `_ass_escape` neutralizes libass
metacharacters at the single point where word text enters the line
(`_ass_events`' `_case`), leaving our own generated override tags
(`\\pos`, karaoke `\\c` highlights) untouched.
"""
from __future__ import annotations

from character_swap.video_edit import (
    CaptionStyle,
    Word,
    _ass_escape,
    _ass_events,
    _write_ass,
)


# ------------------------------------------------------------- _ass_escape

def test_escape_braces():
    assert _ass_escape("a{b}c") == "a\\{b\\}c"
    assert _ass_escape("{\\b1}bold{\\b0}") == (
        "\\{\\\u200bb1\\}bold\\{\\\u200bb0\\}")


def test_escape_breaks_backslash_escape_pairs():
    # `\N` / `\n` / `\h` are libass line-break / hard-space escapes — a
    # zero-width space after the backslash breaks the pair but stays invisible.
    assert _ass_escape("\\N") == "\\\u200bN"
    assert _ass_escape("path\\here") == "path\\\u200bhere"


def test_escape_newlines_become_spaces():
    # A raw newline would terminate the one-line Dialogue event itself.
    assert _ass_escape("two\nlines") == "two lines"
    assert _ass_escape("cr\r\nlf") == "cr  lf"


def test_escape_plain_text_untouched():
    assert _ass_escape("Hello, world! Isn't it 100% fine?") == (
        "Hello, world! Isn't it 100% fine?")
    assert _ass_escape("") == ""


# ------------------------------------------------------------- _ass_events

def test_brace_word_is_escaped_and_card_text_survives():
    """The audit's failure mode: `so {cheap deal` swallowed everything after
    the `{`. The escaped line must carry every word."""
    style = CaptionStyle()  # no highlight, no free-position override
    words = [Word(text="so", start=0.0, end=0.4),
             Word(text="{cheap", start=0.4, end=0.8),
             Word(text="deal}", start=0.8, end=1.2)]
    out = _ass_events(words, style)
    assert "so \\{cheap deal\\}" in out
    # No unescaped brace remains — nothing for libass to parse as overrides.
    assert "{" not in out.replace("\\{", "").replace("\\}", "")


def test_highlight_branch_escapes_words_but_keeps_generated_tags():
    """Karaoke templates wrap the active word in real `{\\c...}` overrides —
    those must survive while the word text itself is escaped."""
    style = CaptionStyle(highlight_color="&H0000FFFF")
    words = [Word(text="{hej}", start=0.0, end=0.5),
             Word(text="där", start=0.5, end=1.0)]
    out = _ass_events(words, style)
    assert "\\{hej\\}" in out                      # user text escaped
    assert "{\\c&H0000FFFF}" in out                # generated highlight intact
    assert f"{{\\c{style.primary_color}}}" in out  # generated reset intact


def test_pos_override_prefix_survives_escaping():
    """The free-position `\\pos(x,y)` prefix is generated code, added AFTER
    escaping — it must remain a live override block."""
    style = CaptionStyle(margin_h=100, margin_v=200)
    words = [Word(text="{x}", start=0.0, end=0.5)]
    out = _ass_events(words, style)
    assert "{\\pos(640,1720)}" in out
    assert "\\{x\\}" in out


def test_write_ass_persists_escaped_text(tmp_path):
    style = CaptionStyle()
    words = [Word(text="{override}", start=0.0, end=0.5)]
    dest = _write_ass(words, style, tmp_path / "caps.ass")
    text = dest.read_text(encoding="utf-8")
    assert "\\{override\\}" in text
