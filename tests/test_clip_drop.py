"""Dropping a video onto "eget klipp" (Hugo 2026-08-11).

"när man klickar på eget klipp så ska man kunna dra och släppa en video också
istället för att bara kunna välja en från finder." The file picker was the only
way in; dragging a video onto the label — the obvious gesture, and the one the
rest of this app already supports for scenes, characters and Editor clips — did
nothing at all.

All THREE import controls got it, because they are the same gesture and a user
who learns it in one place will try it in the others: Swap Step 5 (📥 importera),
the Reengineer clip strip (📥 eget klipp) and the 🎞 versions modal
(📥 färdigt klipp).

The two ways this could break quietly, both locked below:

  * preventDefault() on EVERY dragover would make the label look droppable for
    the library's OWN internal drags — image reorder and drag-into-job carry
    their own dataTransfer types and no files — and would swallow those drops.
    `onImageReorderOver` already follows exactly this rule for the same reason.
  * a video whose mime type the browser did not recognise must not be refused.
    `.mkv`, and anything dragged out of certain apps, arrives with an EMPTY
    `file.type`; a false refusal there is worse than passing an odd file to the
    server, which validates it anyway.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = Path(__file__).parent / "js" / "clip_drop.mjs"
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_clip_drop_behavior():
    proc = subprocess.run(["node", str(_HARNESS)], capture_output=True,
                          text=True, timeout=60, cwd=_ROOT)
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "clip-drop regressions:\n  - " + "\n  - ".join(
        result.get("failures", []))


def test_the_whole_clip_card_is_the_drop_target():
    """Hugo 2026-08-11: "jag lyckas inte, videon öppnas bara i en ny flik."
    A 9px text label is not something you can hit with a file, so the drop
    landed on the page and the browser navigated to the video — out of the app,
    mid-run. The card itself is the target now, and the window guard below
    catches whatever still misses."""
    assert "dropReClip(r, sc, cid, v.variant_id, $event)" in _HTML, (
        "the Reengineer clip card must take the drop")
    assert "dropSwapClip(cid, vv.video_id, vv.status, $event)" in _HTML, (
        "the Swap Step-5 clip card must take the drop")


def test_a_missed_drop_cannot_navigate_away():
    """The guard is what turns a miss from "you lost your page" into "nothing
    happened". It must be installed at startup, not lazily."""
    assert "_installFileDropGuard()" in _JS
    init = _JS.split("async init() {", 1)[1][:400]
    assert "_installFileDropGuard" in init, (
        "the guard must be installed in init(), before any drag can happen")


def test_all_three_import_controls_accept_a_drop():
    """A helper nothing calls is invisible. Every place that offers "import your
    own clip" must take the drop, or the gesture works in one strip and
    mysteriously not in the next."""
    assert _HTML.count("onClipDragOver($event)") >= 3, (
        "Swap Step 5, the Reengineer clip strip and the versions modal must "
        "each accept a dropped video")
    assert _HTML.count("clipFileFromDrop($event)") >= 3
    # …and each still opens the picker on a plain click.
    assert _HTML.count('accept="video/*"') >= 3


def test_dragover_is_not_blanket_prevented_in_the_markup():
    """`@dragover.prevent` would claim the drag before the handler could decide,
    which is what breaks the library's internal drags. The decision has to live
    in JS, where the dataTransfer types can be read."""
    for line in _HTML.splitlines():
        if "onClipDragOver" in line:
            assert "@dragover.prevent" not in line, (
                "the clip label must not blanket-prevent dragover: " + line.strip())


def test_the_picker_and_the_drop_share_one_upload_path():
    """Two copies of the upload logic would drift — one would get the progress
    flag, or the error handling, and the other would not."""
    for fn in ("importSwapClipFile", "importReClipFile"):
        assert f"async {fn}(" in _JS, f"{fn} must exist"
    # The picker handler delegates rather than duplicating.
    picker = _JS.split("async importReClip(run, sc, cid, variantId, ev) {", 1)[1]
    picker = picker.split("},", 1)[0]
    assert "importReClipFile" in picker, (
        "the file-picker path must call the shared uploader")
