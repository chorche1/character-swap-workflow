"""Backlog #5 + #24 (2026-06-12): duplicate-animation guard + reanimate
exception handling.

#5  — a second animate()/reanimate() for the same run while one is in flight
      used to submit a SECOND full Kling batch (double billing on
      double-click / second tab). Both entry points now share the
      _ANIMATING in-process guard (mirror of _ASSEMBLING).
#24 — reanimate() was the only phase entry-point without a try/except: any
      exception stranded the run in status `reanimating`, which blocked
      every edit endpoint. It now flips to `failed` with the error recorded
      and clears the reanimate bookkeeping keys.
"""
from __future__ import annotations

import asyncio

from character_swap import runner_reengineer


def _capture_updates(monkeypatch):
    updates: list[dict] = []
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.append({"re_id": re_id, **kw}))
    return updates


def test_animate_duplicate_trigger_is_ignored(monkeypatch):
    calls = []

    async def fake_do_animate(re_id, state):
        calls.append(re_id)
    monkeypatch.setattr(runner_reengineer, "_do_animate", fake_do_animate)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: {"re_id": re_id, "job_id": "j1"})
    _capture_updates(monkeypatch)

    runner_reengineer._ANIMATING.add("re_x")
    try:
        asyncio.run(runner_reengineer.animate("re_x"))
    finally:
        runner_reengineer._ANIMATING.discard("re_x")
    assert calls == []                      # duplicate → no second Kling batch


def test_animate_guard_releases_after_run(monkeypatch):
    seen_during: list[bool] = []

    async def fake_do_animate(re_id, state):
        seen_during.append(re_id in runner_reengineer._ANIMATING)
    monkeypatch.setattr(runner_reengineer, "_do_animate", fake_do_animate)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: {"re_id": re_id, "job_id": "j1"})
    _capture_updates(monkeypatch)

    asyncio.run(runner_reengineer.animate("re_y"))
    assert seen_during == [True]            # held while running
    assert "re_y" not in runner_reengineer._ANIMATING   # released after


def test_reanimate_duplicate_trigger_is_ignored(monkeypatch):
    calls = []

    async def fake_do_reanimate(re_id, idxs, *, char_id, clear_dirty):
        calls.append(re_id)
    monkeypatch.setattr(runner_reengineer, "_do_reanimate", fake_do_reanimate)
    _capture_updates(monkeypatch)

    runner_reengineer._ANIMATING.add("re_z")
    try:
        asyncio.run(runner_reengineer.reanimate("re_z", [0]))
    finally:
        runner_reengineer._ANIMATING.discard("re_z")
    assert calls == []


def test_reanimate_exception_fails_run_instead_of_stranding(monkeypatch):
    async def boom(re_id, idxs, *, char_id, clear_dirty):
        raise RuntimeError("kling exploded")
    monkeypatch.setattr(runner_reengineer, "_do_reanimate", boom)
    updates = _capture_updates(monkeypatch)

    asyncio.run(runner_reengineer.reanimate("re_w", [0, 1]))
    assert "re_w" not in runner_reengineer._ANIMATING   # guard released
    assert len(updates) == 1
    u = updates[0]
    assert u["status"] == "failed"          # NOT stranded in 'reanimating'
    assert "kling exploded" in u["error"]
    # Bookkeeping cleared so the edit endpoints unlock cleanly.
    assert u["resume_status"] is None
    assert u["reanimate_idxs"] is None
