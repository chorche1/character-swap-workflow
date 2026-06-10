"""The two CapCut-replica caption templates (decoded from Hugo's reference
videos "silas ears 11.mov" / "Silas ears 10.mov"): registry entries, engine
routing, and the prop values that define the look (sampled colors, font,
karaoke behavior baked into the compositions)."""
from __future__ import annotations

from character_swap.video_edit import TEMPLATES


def test_capcut_yellow_registry():
    t = TEMPLATES["capcut-yellow"]
    assert t.engine == "remotion"
    assert t.composition_id == "CapCutYellowKaraoke"
    assert t.font == "Poppins"
    assert t.all_caps is True
    assert t.words_per_card == 4
    p = t.to_remotion_props()
    assert p["accent"] == "#F8F800"          # yellow sampled from the reference
    assert p["fontFamily"] == "Poppins"
    assert p["fontWeight"] == 900
    # Mid-screen position (~52% down), not bottom-anchored like most styles.
    assert 0.45 <= p["positionPct"]["y"] <= 0.58


def test_capcut_bluebox_registry():
    t = TEMPLATES["capcut-bluebox"]
    assert t.engine == "remotion"
    assert t.composition_id == "CapCutBlueBox"
    assert t.font == "Poppins"
    assert t.all_caps is True
    assert t.words_per_card == 4
    p = t.to_remotion_props()
    assert p["accent"] == "#0070F8"          # blue sampled from the reference
    assert p["fontWeight"] == 900
    assert 0.45 <= p["positionPct"]["y"] <= 0.58


def test_compositions_registered_in_remotion_root():
    """Root.tsx must register a <Composition> for each composition_id the
    registry references — a missing registration only fails at render time."""
    from pathlib import Path
    root = (Path(__file__).resolve().parent.parent
            / "remotion" / "src" / "Root.tsx").read_text()
    for comp_id in ("CapCutYellowKaraoke", "CapCutBlueBox"):
        assert f'id="{comp_id}"' in root, f"{comp_id} not registered in Root.tsx"


def test_preview_bundle_contains_new_compositions():
    """The esbuild preview bundle must include the new comps, else the
    in-browser editor preview silently falls back/errors."""
    from pathlib import Path
    bundle = (Path(__file__).resolve().parent.parent
              / "web" / "static" / "remotion-preview.js")
    if not bundle.exists():
        import pytest
        pytest.skip("preview bundle not built in this checkout")
    text = bundle.read_text(errors="ignore")
    assert "CapCutYellowKaraoke" in text
    assert "CapCutBlueBox" in text
