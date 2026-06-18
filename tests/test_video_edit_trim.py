"""Tests for the silence-trim helpers in video_edit.

Covers:
  - _invert_silences first-keep-no-pad behavior (so leading silence is fully
    discarded; interior keeps still get the pad_secs cushion).
  - The "no silences detected" + "all silent" edge cases of _invert_silences.

The end-to-end trim_silences + trim_leading_silence functions shell out to
ffmpeg, so they're not unit-tested here — those are exercised by the live
server and by manual smoke-tests when Hugo runs the Editor.
"""
from __future__ import annotations

import pytest

from character_swap.video_edit import (
    _invert_silences, Word, shift_word_timestamps,
    _word_gap_keep_ranges, _shift_words_to_keeps,
    caption_transcript_ratio, script_fallback_words,
)


def test_invert_silences_no_silences_returns_full_clip():
    """No silences at all → one keep range covering the whole video. No pad
    because the first keep gets no pre-pad (already starts at 0)."""
    keep = _invert_silences([], total_duration=10.0, pad_secs=0.05)
    assert keep == [(0.0, 10.0)]


def test_invert_silences_leading_silence_fully_discarded():
    """Video starts with 2s of silence → first keep starts EXACTLY at 2.0,
    not at 1.95 (the old behavior added a 50ms pad before speech)."""
    silences = [(0.0, 2.0)]  # leading silence only
    keep = _invert_silences(silences, total_duration=10.0, pad_secs=0.05)
    assert len(keep) == 1
    start, end = keep[0]
    # Critical: no pre-pad on the first keep range — leading silence is gone.
    assert start == 2.0, f"expected start=2.0 (no leading pad); got {start}"
    assert end == 10.0


def test_invert_silences_interior_keep_still_gets_pad():
    """A mid-clip silence: the keep range AFTER it (interior) still gets the
    pre-pad. This preserves natural in-breaths between sentences."""
    silences = [(3.0, 4.0)]  # mid-clip silence; no leading
    keep = _invert_silences(silences, total_duration=10.0, pad_secs=0.05)
    # Two keeps: (0..3.05) before silence, (3.95..10) after.
    assert len(keep) == 2
    # First keep (no leading silence to drop, so starts at 0):
    assert keep[0] == (0.0, 3.05)
    # Second keep: starts BEFORE silence ended (cursor=4.0 minus pad=0.05).
    assert keep[1] == (3.95, 10.0)


def test_invert_silences_leading_plus_mid_silence():
    """Combined: leading silence + mid silence. First keep starts on speech
    (no pad), second keep (interior) gets the pad."""
    silences = [(0.0, 1.5), (5.0, 6.0)]
    keep = _invert_silences(silences, total_duration=10.0, pad_secs=0.05)
    assert len(keep) == 2
    # First keep: 1.5 (no pre-pad — leading discarded) → 5.05 (post-pad).
    assert keep[0] == (1.5, 5.05)
    # Second keep: 5.95 (pre-pad on interior) → 10.0 (trailing fully cut).
    assert keep[1] == (5.95, 10.0)


def test_invert_silences_all_silent_returns_empty():
    """Silence covers the whole duration → no keep ranges (caller falls back
    to a 0.5s stub clip)."""
    silences = [(0.0, 10.0)]
    keep = _invert_silences(silences, total_duration=10.0, pad_secs=0.05)
    assert keep == []


def test_invert_silences_drops_microscopic_slivers():
    """A keep range smaller than 50ms gets pruned — these are usually
    detection artifacts and clipping them out keeps the output clean."""
    # Tiny speech burst between two silences. Without the sliver-drop the
    # output would have a 30ms "keep" of audio garbage.
    silences = [(0.0, 1.0), (1.03, 5.0)]   # 30ms gap between silences
    keep = _invert_silences(silences, total_duration=5.0, pad_secs=0.0)
    assert keep == []


def test_invert_silences_pad_does_not_overshoot_duration():
    """Pad after the last interior keep can't go past total_duration."""
    silences = [(0.0, 1.0), (8.0, 9.0)]
    keep = _invert_silences(silences, total_duration=10.0, pad_secs=0.05)
    # First keep: 1.0 (no leading pad) → 8.05.
    # Second keep: 8.95 (interior pre-pad) → 10.0 (trailing fully cut).
    assert keep[-1][1] == 10.0


def test_invert_silences_first_silence_not_at_zero_means_clip_starts_on_speech():
    """If the first silence starts at e.g. 1.5s, the clip ALREADY begins
    with speech. The first keep covers 0..1.55 with the post-pad."""
    silences = [(1.5, 3.0)]
    keep = _invert_silences(silences, total_duration=10.0, pad_secs=0.05)
    assert len(keep) == 2
    # First keep starts at 0 (no pre-pad needed anyway since cursor=0).
    assert keep[0] == (0.0, 1.55)
    # Second keep: gets the interior pre-pad.
    assert keep[1] == (2.95, 10.0)


# --- shift_word_timestamps -------------------------------------------------------------


def test_shift_word_timestamps_zero_offset_returns_unchanged():
    words = [Word("hi", 0.0, 0.3), Word("there", 0.4, 0.7)]
    out = shift_word_timestamps(words, 0.0)
    assert out == words


def test_shift_word_timestamps_negative_offset_returns_unchanged():
    """Negative or zero offset = no-op (we don't time-shift forward)."""
    words = [Word("hi", 1.0, 1.3)]
    out = shift_word_timestamps(words, -0.5)
    assert out[0].start == 1.0
    assert out[0].end == 1.3


def test_shift_word_timestamps_subtracts_offset_from_each():
    """After a 0.5s recut, every timestamp moves 0.5s earlier."""
    words = [Word("hi", 0.5, 0.8), Word("there", 1.0, 1.3)]
    out = shift_word_timestamps(words, 0.5)
    assert out[0].start == 0.0 and out[0].end == pytest.approx(0.3)
    assert out[1].start == 0.5 and out[1].end == 0.8


def test_shift_word_timestamps_clamps_negative_starts_to_zero():
    """If a word's pre-shift start is BEFORE the offset (shouldn't happen in
    practice but defensive), clamp to 0 rather than negative."""
    words = [Word("hi", 0.3, 0.6), Word("you", 0.8, 1.0)]
    out = shift_word_timestamps(words, 0.5)
    # First word's start (0.3) - offset (0.5) = -0.2 → clamped to 0
    assert out[0].start == 0.0
    # End is also clamped but kept > start to avoid zero-duration cues
    assert out[0].end > out[0].start
    # Second word lands correctly
    assert out[1].start == pytest.approx(0.3)


def test_shift_word_timestamps_returns_new_list_not_mutation():
    """Caller's original list must not be mutated in place."""
    words = [Word("hi", 1.0, 1.5)]
    original_start = words[0].start
    out = shift_word_timestamps(words, 0.5)
    assert words[0].start == original_start  # unchanged
    assert out[0].start == 0.5               # shifted


# --- _word_gap_keep_ranges (word-gap trim, Hugo 2026-06-17) ----------------


def test_word_gap_no_gaps_keeps_whole_clip():
    """Words back-to-back, no pause exceeds max_gap → one keep, whole clip."""
    words = [Word("a", 0.0, 1.0), Word("b", 1.0, 2.0)]
    keep = _word_gap_keep_ranges(words, 2.0, max_gap_secs=0.35, pad_secs=0.05)
    assert keep == [(0.0, 2.0)]


def test_word_gap_interior_pause_collapsed():
    """A 2s pause between words gets cut down, leaving pad_secs each side."""
    words = [Word("a", 0.0, 1.0), Word("b", 3.0, 4.0)]
    keep = _word_gap_keep_ranges(words, 4.0, max_gap_secs=0.35, pad_secs=0.05)
    assert keep == [(0.0, 1.05), (2.95, 4.0)]
    removed = 4.0 - sum(b - a for a, b in keep)
    assert removed == pytest.approx(1.9)


def test_word_gap_leading_silence_dropped():
    """Room tone before the first word is removed (down to the word - pad)."""
    words = [Word("a", 1.0, 2.0), Word("b", 2.0, 3.0)]
    keep = _word_gap_keep_ranges(words, 3.0, max_gap_secs=0.35, pad_secs=0.05)
    assert keep == [(0.95, 3.0)]


def test_word_gap_trailing_room_tone_dropped():
    """Silence after the last word (Kling sitting still) is removed."""
    words = [Word("a", 0.0, 1.0), Word("b", 1.0, 2.0)]
    keep = _word_gap_keep_ranges(words, 5.0, max_gap_secs=0.35, pad_secs=0.05)
    assert keep == [(0.0, 2.05)]


def test_word_gap_drops_word_past_eof_no_truncation():
    """Regression (review 2026-06-17): a hallucinated Whisper word whose start
    lands past EOF must NOT truncate the clip. The out-of-range word is dropped;
    only the real trailing room tone is removed. Before the fix the keep-cursor
    overshot total_duration and the tail keep was never appended → the clip got
    chopped to ~2s."""
    words = [Word("a", 0.0, 1.0), Word("b", 1.2, 2.0), Word("ghost", 100.0, 101.0)]
    keep = _word_gap_keep_ranges(words, 5.0, max_gap_secs=0.35, pad_secs=0.05)
    assert keep == [(0.0, 2.05)]                       # ghost dropped, tail cut
    assert sum(b - a for a, b in keep) == pytest.approx(2.05)


def test_word_gap_clamps_word_end_past_eof():
    """A word whose END exceeds duration is clamped, not dropped — its speech
    is real, only the timestamp overshoots."""
    words = [Word("a", 0.0, 1.0), Word("b", 4.8, 6.0)]
    keep = _word_gap_keep_ranges(words, 5.0, max_gap_secs=0.35, pad_secs=0.05)
    # gap a→b is cut; b kept with end clamped to 5.0 (no trailing removal).
    assert keep[0] == (0.0, 1.05)
    assert keep[-1][1] == pytest.approx(5.0)


def test_word_gap_short_pause_under_threshold_kept():
    """A pause shorter than max_gap is NOT cut (natural in-breath survives)."""
    words = [Word("a", 0.0, 1.0), Word("b", 1.3, 2.3)]   # 0.3s gap < 0.35
    keep = _word_gap_keep_ranges(words, 2.3, max_gap_secs=0.35, pad_secs=0.05)
    assert keep == [(0.0, 2.3)]


# --- _shift_words_to_keeps -------------------------------------------------


def test_shift_words_to_keeps_retimes_onto_trimmed_timeline():
    """After collapsing the gap, words re-map onto the concatenated timeline."""
    words = [Word("a", 0.0, 1.0), Word("b", 3.0, 4.0)]
    keep = [(0.0, 1.05), (2.95, 4.0)]
    out = _shift_words_to_keeps(words, keep)
    assert out[0].start == pytest.approx(0.0)
    assert out[0].end == pytest.approx(1.0)
    # 'b' starts at 3.0 → 1.05 (first keep len) + (3.0 - 2.95) = 1.10
    assert out[1].start == pytest.approx(1.10)
    assert out[1].end == pytest.approx(2.10)


def test_shift_words_to_keeps_does_not_mutate_input():
    words = [Word("a", 0.0, 1.0), Word("b", 3.0, 4.0)]
    _shift_words_to_keeps(words, [(0.0, 1.05), (2.95, 4.0)])
    assert words[1].start == 3.0 and words[1].end == 4.0


# --- hybrid captions: Whisper-vs-script reconciliation (Hugo 2026-06-17) -----


def test_caption_ratio_high_when_transcript_matches_script():
    script = "Rub coconut oil and baking soda on your neck before bed"
    whisper = "rub coconut oil and baking soda on your neck before bed"
    assert caption_transcript_ratio(whisper, script) > 0.9


def test_caption_ratio_low_on_hallucination():
    """Whisper's classic 'thanks for watching' outro vs the real script."""
    script = "Rub coconut oil and baking soda on your neck before bed"
    whisper = ("Thanks for watching, I hope you found this video helpful. "
               "See you in the next video, bye!")
    assert caption_transcript_ratio(whisper, script) < 0.55


def test_caption_ratio_zero_when_transcript_empty():
    assert caption_transcript_ratio("", "some real script here") == 0.0


def test_caption_ratio_one_when_no_script():
    # No script to compare against → trust Whisper (don't fall back).
    assert caption_transcript_ratio("anything at all", "") == 1.0


def test_script_fallback_words_even_timing_and_text():
    words = script_fallback_words("hello there friend", 9.0)
    assert [w.text for w in words] == ["hello", "there", "friend"]
    assert words[0].start == 0.0
    assert words[0].end == pytest.approx(3.0)
    assert words[1].start == pytest.approx(3.0)
    assert words[-1].end == pytest.approx(9.0)


def test_script_fallback_words_empty_inputs():
    assert script_fallback_words("", 10.0) == []
    assert script_fallback_words("words here", 0.0) == []
