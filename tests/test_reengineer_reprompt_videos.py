"""One-click per-scene video re-prompt for the Swap/Reengineer flow
(`POST /api/reengineer/{re_id}/scenes/{idx}/reprompt_videos`, Hugo 2026-06-23).

Combines the edit-scene PATCH + whole-scene redo into a single action: set the
scene's motion prompt (synced onto the backing job), mark it dirty + finals
stale, then re-animate every non-imported clip of the scene for ALL characters
via `reanimate(clear_dirty=True)` — reusing the approved images. No ✎ edit mode.

These tests monkeypatch the reengineer state store + the reanimate engine and
assert: the prompt is written + synced, the scene goes dirty, finals are
stale, reanimate is scheduled whole-scene with clear_dirty=True, and the
validation guards fire — empty prompt → 400, direct scene → 409,
already-animating → 409, missing backing job → 409 (no state mutation / no
scheduled no-op), forbidden run status → 409, out-of-range idx → 404.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, runner_reengineer
from character_swap.models import Job


@pytest.fixture
def wired(monkeypatch):
    box: dict = {"states": {}, "synced": [], "job": None}
    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)

    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    monkeypatch.setattr(runner_reengineer, "_ANIMATING", set())

    # Record sync calls instead of mutating a real job.
    def fake_sync(job, state, idxs):
        box["synced"].append((id(job), list(idxs),
                              [state["scenes"][i]["motion_prompt"] for i in idxs]))
    monkeypatch.setattr(runner_reengineer, "_sync_movement_from_state", fake_sync)

    class _S:
        def get_job(self, jid):
            return box["job"] if box["job"] and box["job"].job_id == jid else None

        def get_scene(self, sid):          # _reengineer_view builds scene URLs
            return None
    monkeypatch.setattr(api, "store", lambda: _S())
    return box


def _state(*, is_direct=False, status="done"):
    return {"re_id": "re_t", "status": status, "job_id": "j1",
            "finals": {"cA": {"path": "/tmp/final.mp4"}},
            "scenes": [{"idx": 0, "scene_id": "s1", "start": 0.0, "end": 5.0,
                        "duration": 5.0, "motion_prompt": "old prompt",
                        "speech": "", "summary": "one",
                        **({"is_direct": True} if is_direct else {})}]}


def _job_with_movement():
    # Minimal post-gate job so the sync branch runs.
    return Job(job_id="j1", title="t", scene_id="s1",
               scene_image_path="/tmp/s1.png", scene_ids=["s1"],
               scene_image_paths=["/tmp/s1.png"],
               movement_prompts={"s1": "old prompt"}, characters={})


def _scheduled(bg):
    assert len(bg.tasks) == 1
    t = bg.tasks[0]
    assert t.args[0] is runner_reengineer.reanimate
    return t.args[1:], t.kwargs


def test_reprompt_sets_prompt_and_reanimates_whole_scene(wired):
    wired["states"]["re_t"] = _state()
    wired["job"] = _job_with_movement()
    bg = BackgroundTasks()
    body = api.ReRepromptVideosBody(prompt="  NEW motion for the scene  ")
    asyncio.run(api.reengineer_reprompt_scene_videos("re_t", 0, bg, body))

    st = wired["states"]["re_t"]
    entry = st["scenes"][0]
    assert entry["motion_prompt"] == "NEW motion for the scene"   # stripped + stored
    assert entry.get("dirty")                                      # marked dirty
    # Synced onto the job with the new prompt.
    assert wired["synced"] and wired["synced"][0][1] == [0]
    assert wired["synced"][0][2] == ["NEW motion for the scene"]
    # Finals went stale.
    assert st.get("finals_stale") or all(
        f.get("stale") for f in (st.get("finals") or {}).values())
    # Whole-scene reanimate scheduled, clear_dirty=True (back in sync).
    args, kwargs = _scheduled(bg)
    assert args == ("re_t", [0])
    assert kwargs["char_id"] is None
    assert kwargs["clear_dirty"] is True


def test_empty_prompt_rejected(wired):
    wired["states"]["re_t"] = _state()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, BackgroundTasks(),
            api.ReRepromptVideosBody(prompt="   ")))
    assert ei.value.status_code == 400


def test_direct_scene_rejected(wired):
    wired["states"]["re_t"] = _state(is_direct=True)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, BackgroundTasks(),
            api.ReRepromptVideosBody(prompt="x")))
    assert ei.value.status_code == 409


def test_rejected_while_animating(wired, monkeypatch):
    wired["states"]["re_t"] = _state()
    monkeypatch.setattr(runner_reengineer, "_ANIMATING", {"re_t"})
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, BackgroundTasks(),
            api.ReRepromptVideosBody(prompt="x")))
    assert ei.value.status_code == 409


def test_missing_job_refuses_loudly(wired):
    # Backing swap job gone (store returns None). The endpoint must 409 BEFORE
    # mutating state or scheduling a no-op reanimate (else the scene silently
    # sticks dirty/stale with no error). Mirrors reengineer_regen_clip.
    wired["states"]["re_t"] = _state()
    wired["job"] = None
    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, bg, api.ReRepromptVideosBody(prompt="new")))
    assert ei.value.status_code == 409
    # State untouched, nothing scheduled.
    assert wired["states"]["re_t"]["scenes"][0]["motion_prompt"] == "old prompt"
    assert not wired["states"]["re_t"]["scenes"][0].get("dirty")
    assert bg.tasks == []


def test_forbidden_status_rejected(wired):
    # An interactive gate (awaiting_approval) is not in the editable status set.
    wired["states"]["re_t"] = _state(status="awaiting_approval")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, BackgroundTasks(),
            api.ReRepromptVideosBody(prompt="x")))
    assert ei.value.status_code == 409


def test_out_of_range_idx_404(wired):
    wired["states"]["re_t"] = _state()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 5, BackgroundTasks(),
            api.ReRepromptVideosBody(prompt="x")))
    assert ei.value.status_code == 404
