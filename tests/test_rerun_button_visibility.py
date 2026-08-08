"""The ↻ Nya karaktärer button must appear on the runs worth re-running.

Hugo 2026-08-08, reported from the live app: a finished run showing
"5 bilder · done · 9 scenes" and a full row of final videos had no re-run
button at all — only ✎ Redigera and ✕.

Cause: `reCanRerun` gated on `run.scenes`, but the light history row
(GET /api/reengineer) omits `scenes` entirely and `loadReengineerHistory`
hydrates only the newest 8 runs plus the ones parked at a gate. So every
older FINISHED run — precisely the ones a re-run is for — had an empty array
and the button was hidden. This is the third instance of that bug class in
this file's history (the approval gate and the multi-person chooser both hit
it), which is why the fix falls back to `n_scenes`, the same value the card's
own "N scenes" label already reads.

`tests/js/re_can_rerun.mjs` exercises the real function; this module runs it.
The source-grep pins the load-bearing fallback for the case where Node isn't
available.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_JS = (_ROOT / "web" / "app.js").read_text(encoding="utf-8")
_HARNESS = Path(__file__).parent / "js" / "re_can_rerun.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_re_can_rerun_behavior():
    proc = subprocess.run(
        ["node", str(_HARNESS)],
        capture_output=True, text=True, timeout=60, cwd=_ROOT,
    )
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "reCanRerun regressions:\n  - " + "\n  - ".join(
        result.get("failures", [])
    )


def test_re_can_rerun_falls_back_to_n_scenes_in_source():
    """Node-free backstop: the predicate must consult n_scenes, not just the
    scenes array the light history row never carries."""
    body = _JS.split("reCanRerun(run) {", 1)[1].split("},", 1)[0]
    assert "n_scenes" in body, (
        "reCanRerun must fall back to n_scenes — GET /api/reengineer omits "
        "`scenes`, so gating on it alone hides the button on every "
        "unhydrated run"
    )


def test_rerun_button_is_wired_to_the_predicate():
    """The markup must gate the button on reCanRerun (and nothing stricter)."""
    html = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'x-show="reCanRerun(r)"' in html
    assert 'openRerunModal(r)' in html
