"""Backlog #21 (2026-06-12): caption cards never span real pauses.

Cards were chunked purely by word count, so a card could straddle a long
pause or a scene join — the next scene's words appeared on screen seconds
early. All three grouping sites (video_edit._group_words, Remotion's
groupIntoCards, app.js captionCards) now break a card when the inter-word
gap exceeds 0.8s; this file also pins the constant across the mirrors.
"""
from __future__ import annotations

from pathlib import Path

from character_swap import video_edit
from character_swap.video_edit import Word

_ROOT = Path(__file__).resolve().parents[1]


def _w(t0: float, t1: float, text: str = "w") -> Word:
    return Word(text=text, start=t0, end=t1)


def test_group_words_breaks_at_long_gap():
    words = [_w(0.0, 0.2), _w(0.2, 0.4),          # scene 1
             _w(2.0, 2.2), _w(2.2, 2.4)]          # 1.6s gap → scene 2
    cards = video_edit._group_words(words, per_card=4)
    assert len(cards) == 2
    (s0, e0, c0), (s1, e1, c1) = cards
    assert (s0, e0, len(c0)) == (0.0, 0.4, 2)
    assert (s1, e1, len(c1)) == (2.0, 2.4, 2)


def test_group_words_plain_chunking_without_gaps():
    words = [_w(i * 0.3, i * 0.3 + 0.3) for i in range(5)]
    cards = video_edit._group_words(words, per_card=3)
    assert [len(c) for _, _, c in cards] == [3, 2]


def test_group_words_small_pause_does_not_break():
    words = [_w(0.0, 0.2), _w(0.7, 0.9)]          # 0.5s gap < 0.8s threshold
    cards = video_edit._group_words(words, per_card=3)
    assert len(cards) == 1


def test_gap_constant_in_sync_across_mirrors():
    assert video_edit.CARD_GAP_BREAK_SECS == 0.8
    ts = (_ROOT / "remotion" / "src" / "lib" / "useCurrentWord.ts").read_text(
        encoding="utf-8")
    assert "GAP_BREAK_SECS = 0.8" in ts
    assert "GAP_BREAK_SECS" in ts.split("groupIntoCards")[1]   # actually used
    js = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "GAP_BREAK_SECS = 0.8" in js
