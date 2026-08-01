"""The Reengineer approval gate can never be hidden by a focused input.

Hugo 2026-08-02: re_b3170d2118 stood at `awaiting_approval` server-side with
all 27 images ready and QC-passed, but the run card still read "swapping" and
offered no "✓ Approve all" / "▶ Generate videos" — the run could not be taken
forward at all. Cause: `refreshReengineer` deferred the ENTIRE fresh view
(status included) for as long as a `[data-keep-focus]` field held focus, and
every phase control is x-show'd on `r.status`. The deferral has no time cap,
so the card stayed frozen until the user happened to click elsewhere.

The fix keeps the field-churn guard but exempts the status, patching it in
place. `tests/js/refresh_reengineer_focus_gate.mjs` exercises the real
function; this module runs it and surfaces the failures. The source-grep below
pins the load-bearing lines for the case where Node isn't available.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HARNESS = Path(__file__).parent / "js" / "refresh_reengineer_focus_gate.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_refresh_reengineer_focus_gate_behavior():
    proc = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True, text=True, timeout=60, cwd=_ROOT,
    )
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "refreshReengineer regressions:\n  - " + "\n  - ".join(
        result.get("failures", [])
    )


def test_status_is_exempt_from_the_focus_guard():
    body = _JS.split("async refreshReengineer(reId) {")[1].split("\n    },")[0]
    # The fetch is unconditional — gating it meant a phase change went unseen.
    head = body.split("if (prev && this._isTypingProtectedField())")[0]
    assert "fetch('/api/reengineer/' + reId + '?slim=1')" in head, (
        "the slim fetch must run BEFORE the focus guard, or a status change "
        "is never even observed while a field holds focus"
    )
    # While focused: status is patched IN PLACE (never a splice, which would
    # re-seed the x-model bindings and eat the keystroke).
    guarded = body.split("if (prev && this._isTypingProtectedField())")[1]
    guarded = guarded.split("if (i >= 0) this.reengineerHistory.splice")[0]
    assert "prev.status = fresh.status;" in guarded
    assert "splice" not in guarded, "the focused path must not replace the run object"
    # And a retry stays queued so the rest of the view lands after blur.
    assert "this._reRefreshTimers[reId] = setTimeout(" in guarded


def test_status_milestone_fires_once_per_transition():
    # Shared by both paths so the gate appearing and the chime announcing it
    # can't come apart; the in-place patch advances prev.status, so the later
    # full splice sees no change and stays silent.
    assert _JS.count("this._fireReengineerStatusMilestone(prevStatus, fresh, reId);") == 2
    body = _JS.split("_fireReengineerStatusMilestone(prevStatus, fresh, reId) {")[1]
    assert "if (!prevStatus || prevStatus === fresh.status) return;" in body[:200]
