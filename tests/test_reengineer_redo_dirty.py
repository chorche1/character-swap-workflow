"""Per-scene redo (`POST /api/reengineer/{re_id}/scenes/{idx}/redo`) and the
"ändrad" (dirty) flag.

Hugo 2026-06-21: a WHOLE-scene redo (no char_id) regenerates EVERY non-imported
clip with the synced prompt, so the scene is back in sync → the endpoint must
schedule reanimate with clear_dirty=True (like "▶ Animera om ändrade"). A
SINGLE-character redo keeps the flag (siblings may still be stale).

Regression for the dead-end where a scene's clips were all refreshed (and
matched the current prompt) but a stuck dirty flag made "▶ Bygg ihop igen"
refuse the build forever — only a full, costly re-animate could clear it. The
engine half (clear_dirty True/False → clears/keeps) is locked in
test_reengineer_reanimate.py; this locks the endpoint's char_id-based decision.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks

from character_swap import api, runner_reengineer


@pytest.fixture
def wired(monkeypatch):
    box: dict = {"states": {}}
    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)

    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    monkeypatch.setattr(runner_reengineer, "_ANIMATING", set())  # nothing in flight
    return box


def _state():
    return {"re_id": "re_t", "status": "done", "job_id": "j1",
            "scenes": [{"idx": 0, "scene_id": "s1", "start": 0.0, "end": 5.0,
                        "duration": 5.0, "motion_prompt": "p", "speech": "",
                        "summary": "one", "dirty": True}]}


def _scheduled(bg):
    """(args-after-reanimate, kwargs) of the single scheduled reanimate task."""
    assert len(bg.tasks) == 1
    t = bg.tasks[0]
    # add_task(_run_async, reanimate, re_id, [idx], char_id=..., clear_dirty=...)
    assert t.args[0] is runner_reengineer.reanimate
    return t.args[1:], t.kwargs


def test_whole_scene_redo_clears_dirty(wired):
    wired["states"]["re_t"] = _state()
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_redo_scene("re_t", 0, bg, api.ReRedoBody()))
    args, kwargs = _scheduled(bg)
    assert args == ("re_t", [0])
    assert kwargs["char_id"] is None
    assert kwargs["clear_dirty"] is True          # whole-scene redo syncs → clear
    assert out["char_id"] is None


def test_single_char_redo_keeps_dirty(wired):
    wired["states"]["re_t"] = _state()
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_redo_scene(
        "re_t", 0, bg, api.ReRedoBody(char_id="cB")))
    _args, kwargs = _scheduled(bg)
    assert kwargs["char_id"] == "cB"
    assert kwargs["clear_dirty"] is False         # siblings may still be stale


def test_redo_no_body_defaults_to_whole_scene(wired):
    wired["states"]["re_t"] = _state()
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_redo_scene("re_t", 0, bg, None))
    _args, kwargs = _scheduled(bg)
    assert kwargs["char_id"] is None
    assert kwargs["clear_dirty"] is True
