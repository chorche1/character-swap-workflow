"""Regression: the Reengineer 🔁 Repurpose button must not be born disabled.

2026-06-27 bug — "Inget händer när jag klickar på Repurpose". The button lived
inside the run `x-for` with `:disabled="r.repurposing"`. `repurposing` is a
CLIENT-ONLY field the server never sends, so on every run object the key is
MISSING (not `false`). Alpine's boolean `:disabled` binding inside an x-for
treats a missing property as truthy → the button rendered permanently disabled
and every click was a dead no-op. Verified in-browser: a native click on the
disabled button did nothing; coercing to `!!r.repurposing` (a real boolean)
makes a missing key evaluate to `false` and the button clickable.

This locks the coercion so the bare-property form can't creep back in.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap.config import settings

_HTML = (settings.web_dir / "index.html").read_text(encoding="utf-8")


def test_reengineer_repurpose_disabled_is_boolean_coerced():
    # The fixed, coerced form must be present...
    assert ':disabled="!!r.repurposing"' in _HTML
    # ...and the bare (buggy) form must be gone.
    assert ':disabled="r.repurposing"' not in _HTML
