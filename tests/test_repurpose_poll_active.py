"""Regression: a repurpose-in-flight reengineer run must keep the poller alive.

2026-06-27 bug — the Reengineer 🔁 Repurpose build runs AFTER the run is already
`done`, with no images/videos in flight. `_reengineerIsActive()` decided a
finished run is "active" only via `_reengineerHasInFlight()` (which checks
generating images / pending videos). So the moment a repurpose was submitted the
5s poller dropped the run and stopped; the client-set `repurposing` flag was
never replaced by the server's cleared value, and the "spegelvänder…" spinner
hung forever even though all mirrored videos had finished. The fix makes
`_reengineerIsActive` return true while `r.repurposing` is set, so polling
continues and naturally ends when the server clears the flag.
"""
from __future__ import annotations

import re

from character_swap.config import settings

_JS = (settings.web_dir / "app.js").read_text(encoding="utf-8")


def _fn_body(defn: str) -> str:
    # Target the DEFINITION (e.g. "_reengineerIsActive(r) {"), not a call site.
    i = _JS.index(defn)
    return _JS[i:i + 800]


def test_reengineer_is_active_tracks_repurposing():
    body = _fn_body("_reengineerIsActive(r) {")
    assert "repurposing" in body, (
        "_reengineerIsActive must treat a repurpose-in-flight run as active so "
        "the poll keeps refreshing and clears the spinner on completion")
