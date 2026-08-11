"""The rerun modal opens with Telegram delivery ON (Hugo 2026-08-11).

"gör så att bygg ihop och skicka till telegram är på som preset."

Four of the five places that offer this checkbox already defaulted to true.
The fifth — the ↻ Nya karaktärer modal — INHERITED it from the parent run via
`!!s.auto_telegram_send`, so a parent that had it off, or an older run with no
such field at all, opened the box unticked and the rerun built finals nobody
delivered.

ON is also the fail-safe direction: a final that never reaches Telegram is a
silent loss, noticed only when someone goes looking for it, while an unwanted
delivery is visible immediately and one click to avoid. The same reasoning is
already written down for the repurpose toggle (2026-08-09), where a MISSING
stored value must resolve to true rather than being coerced with `!!`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_HARNESS = Path(__file__).parent / "js" / "rerun_telegram_preset.mjs"
_HTML = (_ROOT / "web" / "index.html").read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js not installed")
def test_rerun_telegram_preset_behavior():
    proc = subprocess.run(["node", str(_HARNESS)], capture_output=True,
                          text=True, timeout=60, cwd=_ROOT)
    assert proc.returncode == 0, f"harness crashed:\n{proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["ok"], "rerun Telegram preset regressions:\n  - " + "\n  - ".join(
        result.get("failures", []))


def test_every_start_a_run_form_still_offers_the_choice():
    """Defaulting it on must not mean removing the control — an unwanted
    delivery has to stay one click away."""
    for model in ("swapFromImages.autoTelegramSend",
                  "reengineerGen.autoTelegramSend",
                  "rerunModal.autoTelegramSend",
                  "versionModal.autoTelegramSend"):
        assert f'x-model="{model}"' in _HTML, f"{model} must still have a checkbox"
