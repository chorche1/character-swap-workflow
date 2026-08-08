"""Välj karaktärsbild per karaktär i «↻ Kör om med nya karaktärer» (Hugo 2026-08-09).

The re-run modal already forwarded `character_source_image_ids` and the backend
already honoured it (`rerun.build_state` → `runner_reengineer._create_job_and_swap`
→ `CharacterAsset.resolve_source_filename`) — but nothing in the UI could set it,
and the character chips rendered `swapCharThumb()`, i.e. whatever reference image
was staged on the always-visible Swap upload card. The modal therefore showed a
picture it was not going to use and offered no way to change it.

The behavior lives in app.js and `tests/js/rerun_source_image.mjs` exercises the
real functions; this module runs it and pins the markup. A picker no control
calls is exactly as broken as a missing one — the bug class that hid the 🎤 voice
dropdown for weeks (ffe3e08).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
_APPJS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HARNESS = Path(__file__).parent / "js" / "rerun_source_image.mjs"


def _modal() -> str:
    """Just the ↻ re-run modal — assertions must not be satisfied by the Swap
    or Reengineer upload forms, which have had these controls since 08-08."""
    body = _HTML.split('x-show="rerunModal.open"')[1]
    return body.split('x-show="repurposeModal.open"')[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_rerun_source_image_behavior():
    proc = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True, text=True, timeout=60, cwd=_ROOT,
    )
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "re-run source-image regressions:\n  - " + "\n  - ".join(
        result.get("failures", [])
    )


def test_modal_chip_shows_the_image_the_rerun_will_use():
    """`swapCharThumb` reads swapFromImages.sourceOverrides — a DIFFERENT form
    whose picks have nothing to do with this run."""
    modal = _modal()
    assert "rerunCharThumb(ch)" in modal, (
        "the re-run chips must show the modal's own picked reference image")
    assert "swapCharThumb(ch)" not in modal, (
        "the re-run modal must not render the Swap upload card's staged pick")


def test_modal_exposes_both_pickers():
    modal = _modal()
    # Per-character ↕ — the thing Hugo asked for.
    assert "pickRerunSource(ch.char_id, img.image_id)" in modal, (
        "no per-character reference-image picker in the re-run modal")
    assert "rerunPickerChar = (rerunPickerChar === ch.char_id ? null : ch.char_id)" in modal, (
        "the ↕ badge does not open the modal's own popover")
    assert "rerunModal.sourceOverrides[ch.char_id] || ch.primary_image_id" in modal, (
        "the popover must mark the CURRENT pick, falling back to the ★ primary")
    # Bulk — the modal is where a 9-11 character cast is picked, so doing it one
    # popover at a time is exactly what the bulk control exists to avoid.
    assert "onBulkSourceSelect('rerun', $event)" in modal, (
        "the re-run modal has no bulk reference-image dropdown")
    assert "bulkSourceMax('rerun') > 1" in modal, (
        "the dropdown must hide when there is nothing to choose")
    assert "bulkSourceNote.rerun" in modal, (
        "the clamp to a character's last image may never be silent")


def test_bulk_picker_resolves_against_the_modals_own_cast():
    """`bulkSourceForm` used to be a two-way branch that fell through to the
    Swap form — a 'rerun' kind reaching it would have written the wrong map
    against the wrong selection, silently."""
    body = _APPJS.split("bulkSourceForm(kind) {")[1].split("\n    },")[0]
    assert "'rerun'" in body and "rerunModal" in body, (
        "bulkSourceForm does not route 'rerun' to the modal's own state")


def test_submit_forwards_the_picks():
    body = _APPJS.split("async submitRerun() {")[1].split("\n    },")[0]
    assert "m.sourceOverrides[cid]" in body, (
        "submitRerun no longer reads the picked reference images — both "
        "pickers would be inert")
    assert "character_source_image_ids" in body, (
        "submitRerun no longer sends the picks to the server")
