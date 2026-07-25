"""Regression tests for match_clips_by_transcript — multi-clip script ordering.

Bug (2026-07-25): single-word clips like "Cinnamon" and "Lime" were flagged
`unmatched` and dumped at the END of the reel despite a perfect 100% score,
because the matcher required an absolute >=12-character common substring —
impossible for transcripts shorter than 12 chars. The floor is now adaptive:
a clip shorter than 12 chars matches when its WHOLE transcript appears in
the script.
"""

from character_swap import video_edit

# The real script from ed_33c00771be, where the bug was observed.
SCRIPT = (
    "If you buy apple cider vinegar, cinnamon, and turmeric, you get "
    "nature's version of those weight loss jabs.\n\n"
    "If you buy a banana, almond butter, and chamomile tea, you get "
    "nature's sleeping pill.\n\n"
    "If you buy coconut water, lime and sea salt, you get nature's "
    "energy drink\n\n"
    "I got tips like this for every food, save this and comment yes if "
    "for more. Follow so you dont miss it."
)


def _placement_for(placements, idx):
    return next(p for p in placements if p["idx"] == idx)


def test_short_single_word_clips_match_at_script_position():
    """"Cinnamon" (8 chars) and "Lime" (4 chars) must be MATCHED and ordered
    at their script positions — not appended at the end as unmatched."""
    transcripts = [
        "If you buy apple cider vinegar",   # 0
        "Cinnamon",                          # 1  ← was wrongly unmatched
        "and turmeric",                      # 2
        "If you buy coconut water",          # 3
        "Lime",                              # 4  ← was wrongly unmatched
        "and sea salt",                      # 5  (exactly 12 chars — passed before too)
    ]
    placements = video_edit.match_clips_by_transcript(transcripts, SCRIPT)

    for idx in (1, 4):
        p = _placement_for(placements, idx)
        assert not p["unmatched"], f"clip {idx} flagged unmatched: {p}"
        assert p["score"] == 1.0

    order = [p["idx"] for p in placements]
    assert order == [0, 1, 2, 3, 4, 5]


def test_short_clip_not_in_script_stays_unmatched():
    """The adaptive floor must not let a short garbage transcript through:
    a word absent from the script is still flagged unmatched."""
    transcripts = ["If you buy apple cider vinegar", "Zucchini"]
    placements = video_edit.match_clips_by_transcript(transcripts, SCRIPT)
    p = _placement_for(placements, 1)
    assert p["unmatched"]
    # Unmatched clips sort after matched ones.
    assert [q["idx"] for q in placements] == [0, 1]


def test_long_clip_with_weak_overlap_still_unmatched():
    """Long transcripts keep the 12-char floor — a coincidental short
    overlap ("if you") must not count as a match."""
    transcripts = ["if you ever visit the moon bring your own oxygen tanks"]
    placements = video_edit.match_clips_by_transcript(transcripts, SCRIPT)
    assert placements[0]["unmatched"]


def test_empty_transcript_unmatched():
    placements = video_edit.match_clips_by_transcript(["", "Lime"], SCRIPT)
    assert _placement_for(placements, 0)["unmatched"]
    assert not _placement_for(placements, 1)["unmatched"]
