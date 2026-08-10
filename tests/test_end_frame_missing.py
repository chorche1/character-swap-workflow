"""A missing end frame must say so, not pretend to be loading (Hugo 2026-08-11).

"varför är det fortfarande vitt här" — five blank white boxes under
"Gemensam slutpose". They were a SKELETON placeholder whose tooltip read
"genererar slutbild…", shown whenever a scene had a shared end pose and a
character had no swapped frame, WITHOUT asking whether anything was actually
generating.

On the run he was looking at (j_edf16f0e4d / re_bc2d243011) the swap phase made
30 variants and zero end frames — verified in `calls.jsonl`, which recorded 30
`generate` calls and not one more. So the placeholder claimed progress
indefinitely and offered nothing to click, and the run would have animated
without the end poses it was configured for.

End poses are produced ONLY by the swap phase (`_kick_char`) and by the ↻
button, so in any other run state a missing frame is stuck rather than pending.
That is the distinction the UI now makes.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = Path(__file__).parent / "js" / "end_frame_missing.mjs"
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_end_frame_missing_behavior():
    proc = subprocess.run(["node", str(_HARNESS)], capture_output=True,
                          text=True, timeout=60, cwd=_ROOT)
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "end-frame state regressions:\n  - " + "\n  - ".join(
        result.get("failures", []))


def test_the_skeleton_is_gated_on_actually_generating():
    """The whole defect in one line: the placeholder must not appear merely
    because the image is absent."""
    idx = _HTML.index("genererar slutbild")
    # The x-show attribute sits a couple of lines above the title, so read the
    # whole element rather than one line.
    element = _HTML[max(0, idx - 900):idx]
    assert "skeleton" in element, "the generating skeleton must still exist"
    assert "reEndFrameGenerating(r)" in element, (
        "the skeleton must be gated on something actually generating, not "
        "merely on the image being absent")


def test_the_missing_state_offers_a_way_out():
    """A dead end that says 'missing' is only half a fix — the tile has to be
    able to create the frame, and the endpoint for that already exists."""
    assert "reEndFrameMissing(r, sc, jc)" in _HTML
    missing = [l for l in _HTML.splitlines() if "reEndFrameMissing" in l]
    assert any("↻ saknas" in l for l in _HTML.splitlines()), (
        "the missing tile must be labelled, not blank")
    idx = _HTML.index("reEndFrameMissing(r, sc, jc)")
    assert "reengineerRegenEndFrame(r, sc)" in _HTML[idx:idx + 900], (
        "the missing tile must be clickable and regenerate the pose")
