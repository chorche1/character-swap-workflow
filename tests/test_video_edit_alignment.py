"""Tests for the alignment-aware Remotion-props mapping and the
`instagram-center` caption template that depends on it."""
from __future__ import annotations

from character_swap import video_edit
from character_swap.video_edit import CaptionStyle, TEMPLATES


def test_instagram_center_template_registered():
    style = TEMPLATES["instagram-center"]
    assert style.font == "Instagram Sans Bold"
    assert style.alignment == 5
    assert style.words_per_card == 3


def test_to_remotion_props_middle_alignment_forces_center():
    # Middle alignments (4/5/6) should ignore margin_v and pin y to 50%
    # — matches libass behavior for the ASS Style line.
    for align in (4, 5, 6):
        style = CaptionStyle(alignment=align, margin_v=0)
        props = style.to_remotion_props()
        assert props["positionPct"]["y"] == 0.5, f"alignment={align}"

    # Even with a non-zero margin_v, middle alignment still pins to center.
    style = CaptionStyle(alignment=5, margin_v=400)
    assert style.to_remotion_props()["positionPct"]["y"] == 0.5


def test_to_remotion_props_top_alignment_uses_top_distance():
    style = CaptionStyle(alignment=8, margin_v=200)
    y = style.to_remotion_props()["positionPct"]["y"]
    # 200/1920 ≈ 0.104 → top region
    assert 0.05 <= y < 0.2


def test_to_remotion_props_bottom_alignment_unchanged():
    # Default alignment (2 = bottom-center) keeps the existing math.
    style = CaptionStyle(alignment=2, margin_v=400)
    y = style.to_remotion_props()["positionPct"]["y"]
    # 1 - 400/1920 ≈ 0.79 → lower-third
    assert 0.7 < y < 0.85
