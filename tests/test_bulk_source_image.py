"""«Använd bild N för alla» + library image reorder (Hugo 2026-08-08).

Picking each character's reference image one ↕ popover at a time doesn't scale
to an 11-character run, so the Swap and Reengineer forms gained a dropdown that
sets EVERY selected character's source image by position — and the library
gained ◀ ⠿ ▶ controls so the user decides what position 2 means.

The behavior lives in app.js; `tests/js/bulk_source_image.mjs` exercises the
real functions (clamping, scoping, the reorder PATCH and its rollback) and this
module runs it. The markup assertions below pin the wiring: a picker no control
calls is exactly as broken as a missing one — the class of bug that hid the 🎤
voice dropdown for weeks (ffe3e08).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
_HARNESS = Path(__file__).parent / "js" / "bulk_source_image.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_bulk_source_and_reorder_behavior():
    proc = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True, text=True, timeout=60, cwd=_ROOT,
    )
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "bulk source / reorder regressions:\n  - " + "\n  - ".join(
        result.get("failures", [])
    )


def test_both_forms_expose_the_bulk_picker():
    """Swap AND Reengineer — they share the per-character ↕ picker, so a bulk
    control on only one of them is an inconsistency the user has to remember."""
    for kind in ("swap", "reengineer"):
        assert f"onBulkSourceSelect('{kind}', $event)" in _HTML, (
            f"the {kind} form has no bulk reference-image dropdown")
        assert f"bulkSourceMax('{kind}') > 1" in _HTML, (
            f"the {kind} dropdown must hide when there is nothing to choose")
        assert f"bulkSourceNote.{kind}" in _HTML, (
            f"the {kind} form must surface what the bulk pick did — the clamp "
            f"to a character's last image may never be silent")


def test_both_submits_forward_the_picks_to_the_server():
    """Hugo 2026-08-08: bild 2 för alla, sedan enskilda överstyrningar för den
    körningen. Both write the same `sourceOverrides` map — so the only thing
    that can silently break the feature is a submit that stops sending it."""
    js = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for fn in ("submitSwapFromImages", "submitReengineer"):
        body = js.split(f"async {fn}() {{")[1].split("\n    },")[0]
        assert "g.sourceOverrides[cid]" in body, (
            f"{fn} no longer reads the picked reference images — the bulk "
            f"picker and the per-character ↕ picker would both be inert")
        assert "character_source_image_ids" in body, (
            f"{fn} no longer sends the picks to the server")


def test_library_tiles_carry_the_reorder_controls():
    grid = _HTML.split("Uploaded reference images for this character")[1]
    grid = grid.split("Job appearances")[0]
    # Position is what the bulk picker counts, so it has to be readable.
    assert "x-for=\"(img, imgIdx) in (ch.images || [])\"" in grid, (
        "the tile needs its index to show the position and disable the end arrows")
    assert "x-text=\"imgIdx + 1\"" in grid, "the position number is not rendered"
    for arrow, delta in (("◀", "-1"), ("▶", "1")):
        assert f"moveCharacterImage(ch.char_id, img.image_id, {delta})" in grid, (
            f"the {arrow} control is not wired")
    assert "startImageReorder($event, ch.char_id, img.image_id)" in grid
    assert "onImageReorderOver($event, ch.char_id, img.image_id)" in grid
    assert "dropImageReorder($event, ch.char_id, img.image_id)" in grid


def test_reorder_drag_keeps_its_own_handle():
    """The tiles are ALREADY draggable to add the character to a job. If the
    reorder rode the same <img> dragstart the two gestures would collide, so it
    must live on its own ⠿ handle with a stopped dragstart."""
    grid = _HTML.split("Uploaded reference images for this character")[1]
    grid = grid.split("Job appearances")[0]
    assert "@dragstart=\"onCharDragStart($event, ch, img)\"" in grid, (
        "the add-to-job drag must stay on the image itself")
    assert "@dragstart.stop=\"startImageReorder(" in grid, (
        "the reorder drag needs its own handle and must not bubble into the "
        "add-to-job dragstart")
