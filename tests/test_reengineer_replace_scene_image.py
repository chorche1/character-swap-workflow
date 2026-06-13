"""Replace a scene's REFERENCE image with an uploaded image, then re-swap every
character against it (Hugo 2026-06-13).

`POST /api/reengineer/{re_id}/scenes/{idx}/replace_scene_image` registers the
upload as a NEW content-addressed scene_id, re-points THIS run's scene to it
(`_repoint_scene`), withdraws approvals, marks the scene dirty + finals stale,
and queues `regen_scene_images_with_prompt(job_id, None, targets, None)` so each
slot re-swaps with its EXISTING prompt against the new background.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, prompt_director
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)
from character_swap.prompt_director import SwapDirectorPlan


def _job(*, movement=True, n_chars=2):
    def _char(cid):
        images = [
            GeneratedImage(variant_id=f"{cid}-v1", path=f"/{cid}-1.png", prompt="p1",
                           scene_id="s1", status=VariantStatus.READY),
            GeneratedImage(variant_id=f"{cid}-v2", path=f"/{cid}-2.png", prompt="p2",
                           scene_id="s2", status=VariantStatus.READY),
        ]
        return JobCharacter(char_id=cid, name=cid, source_image_path="/c.png",
                            status=CharStatus.APPROVED, images=images,
                            approved_variant_ids=[f"{cid}-v1", f"{cid}-v2"])
    chars = {f"c{i}": _char(f"c{i}") for i in range(n_chars)}
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p1.png",
               scene_ids=["s1", "s2"], scene_image_paths=["/p1.png", "/p2.png"],
               characters=chars, origin="reengineer:re_t",
               movement_prompt=("animate" if movement else None),
               movement_prompts=({"s1": "m1", "s2": "m2"} if movement else {}),
               durations_by_scene=({"s1": 5, "s2": 6} if movement else {}))


def _scenes(pairs):
    out = []
    for i, sid in enumerate(pairs):
        out.append({"idx": i, "scene_id": sid, "start": float(i * 5),
                    "end": float(i * 5 + 5), "duration": 5.0,
                    "motion_prompt": "m", "speech": "", "summary": sid})
    return out


def _state(status="done", finals=True, scenes=None):
    st = {"re_id": "re_t", "status": status, "job_id": "j1",
          "scenes": scenes or _scenes(["s1", "s2"])}
    st["n_scenes"] = len(st["scenes"])
    if finals:
        st["finals"] = {"c0": {"status": "done", "final_path": "/f.mp4"}}
    return st


# ------------------------------------------------------ _repoint_scene (unit)

def test_repoint_happy_path():
    job = _job()
    state = _state()
    api._repoint_scene(job, state, 0, "s1", "sc_new", "/new.png")
    assert job.scene_ids == ["sc_new", "s2"]
    assert job.scene_image_paths == ["/new.png", "/p2.png"]
    assert job.scene_id == "sc_new" and job.scene_image_path == "/new.png"
    assert state["scenes"][0]["scene_id"] == "sc_new"
    for jc in job.characters.values():
        v1 = next(v for v in jc.images if v.variant_id.endswith("-v1"))
        v2 = next(v for v in jc.images if v.variant_id.endswith("-v2"))
        assert v1.scene_id == "sc_new"   # this scene re-pointed
        assert v2.scene_id == "s2"       # other scene untouched
    assert job.movement_prompts == {"sc_new": "m1", "s2": "m2"}
    assert job.durations_by_scene == {"sc_new": 5, "s2": 6}


def test_repoint_legacy_single_scene_catches_null_variant():
    jc = JobCharacter(char_id="c0", name="c0", source_image_path="/c.png",
                      status=CharStatus.APPROVED,
                      images=[GeneratedImage(variant_id="c0-leg", path="/l.png",
                                             prompt="p", scene_id=None,
                                             status=VariantStatus.READY)],
                      approved_variant_ids=["c0-leg"])
    job = Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p1.png",
              scene_ids=[], scene_image_paths=[], characters={"c0": jc},
              origin="reengineer:re_t", movement_prompt="animate")
    state = _state(scenes=_scenes(["s1"]))
    api._repoint_scene(job, state, 0, "s1", "sc_new", "/new.png")
    assert job.scene_ids == ["sc_new"]
    assert job.scene_image_paths == ["/new.png"]
    assert job.scene_id == "sc_new"
    assert jc.images[0].scene_id == "sc_new"   # null-scene variant (primary=s1)


def test_repoint_sibling_collision_refused():
    job = _job()
    state = _state(scenes=_scenes(["s1", "s1"]))   # two entries share s1
    with pytest.raises(HTTPException) as ei:
        api._repoint_scene(job, state, 0, "s1", "sc_new", "/new.png")
    assert ei.value.status_code == 409


def test_repoint_duplicate_sibling_untouched():
    job = _job(n_chars=1)
    jc = job.characters["c0"]
    jc.images.append(GeneratedImage(variant_id="c0-dup", path="/d.png", prompt="p",
                                    scene_id="s1__dupabc", status=VariantStatus.READY))
    job.scene_ids = ["s1", "s1__dupabc", "s2"]
    job.scene_image_paths = ["/p1.png", "/p1.png", "/p2.png"]
    state = _state(scenes=_scenes(["s1", "s1__dupabc", "s2"]))
    api._repoint_scene(job, state, 0, "s1", "sc_new", "/new.png")
    assert next(v for v in jc.images if v.variant_id == "c0-v1").scene_id == "sc_new"
    assert next(v for v in jc.images if v.variant_id == "c0-dup").scene_id == "s1__dupabc"


def test_repoint_end_frame_policy():
    job = _job(n_chars=1)
    jc = job.characters["c0"]
    jc.end_frame_paths = {"s1": "/swapped_end.png"}
    job.end_frames_by_scene = {"s1": "/pose_ref.png"}
    api._repoint_scene(job, _state(), 0, "s1", "sc_new", "/new.png")
    # swapped OUTPUT dropped (re-swap), end-pose REFERENCE migrated
    assert "s1" not in jc.end_frame_paths and "sc_new" not in jc.end_frame_paths
    assert job.end_frames_by_scene == {"sc_new": "/pose_ref.png"}


def test_repoint_director_plan_rekey():
    job = _job(n_chars=2)
    job.director_prompts_json = prompt_director.plan_from_scene_prompts(
        "intent", {"s1": "P1", "s2": "P2"},
        [("c0", "c0"), ("c1", "c1")]).model_dump_json()
    api._repoint_scene(job, _state(), 0, "s1", "sc_new", "/new.png")
    p = SwapDirectorPlan.model_validate_json(job.director_prompts_json)
    assert p.lookup("c0", "sc_new") and all(x == "P1" for x in p.lookup("c0", "sc_new"))
    assert p.lookup("c0", "s1") == []
    assert p.lookup("c0", "s2") and all(x == "P2" for x in p.lookup("c0", "s2"))


# ------------------------------------------------------ endpoint (api-level)

@pytest.fixture
def wired(monkeypatch):
    box = {"job": None, "states": {}}

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j1" else None

        def update_job(self, j):
            box["job_updated"] = True

        def get_scene(self, sid):
            return None

    monkeypatch.setattr(api, "store", lambda: _S())
    from character_swap import reengineer as reengineer_mod, runner_reengineer

    def load_state(re_id):
        s = box["states"].get(re_id)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)

    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    # Fake the frame registration (no disk hashing) + capped read (no UploadFile).
    monkeypatch.setattr(runner_reengineer, "_register_frame_as_scene",
                        lambda p: ("sc_new", Path("/new.png")))

    async def fake_read(f):
        return b"PNGBYTES"
    monkeypatch.setattr(api, "_read_capped", fake_read)
    return box


class _F:
    filename = "x.png"


def test_replace_endpoint_repoints_unapproves_and_queues(wired):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done", finals=True)
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_replace_scene_image("re_t", 0, bg, _F()))
    assert wired["job"].scene_ids[0] == "sc_new"
    assert wired["job"].scene_image_paths[0] == "/new.png"
    assert wired["states"]["re_t"]["scenes"][0]["scene_id"] == "sc_new"
    assert out["regen_variants"] == {"c0": "c0-v1", "c1": "c1-v1"}
    assert len(bg.tasks) == 1
    # prompt=None (keep each slot's existing prompt) + the scene-1 targets.
    assert bg.tasks[0].args[1:] == ("j1", None, {"c0": "c0-v1", "c1": "c1-v1"}, None)
    assert wired["states"]["re_t"]["scenes"][0].get("dirty") is True
    assert wired["states"]["re_t"]["finals_stale"] is True


def test_replace_endpoint_at_gate_no_dirty(wired):
    wired["job"] = _job(movement=False)
    wired["states"]["re_t"] = _state("awaiting_approval", finals=False)
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_replace_scene_image("re_t", 0, bg, _F()))
    assert "dirty" not in wired["states"]["re_t"]["scenes"][0]
    assert len(bg.tasks) == 1


def test_replace_endpoint_noop_identical_hash(wired, monkeypatch):
    from character_swap import runner_reengineer
    monkeypatch.setattr(runner_reengineer, "_register_frame_as_scene",
                        lambda p: ("s1", Path("/p1.png")))   # same as old_sid
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("done")
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_replace_scene_image("re_t", 0, bg, _F()))
    assert out.get("noop") is True
    assert len(bg.tasks) == 0
    assert wired["job"].scene_ids[0] == "s1"   # unchanged


def test_replace_endpoint_409_sibling_collision(wired):
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("done", scenes=_scenes(["s1", "s1"]))
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_replace_scene_image("re_t", 0, BackgroundTasks(), _F()))
    assert ei.value.status_code == 409


def test_replace_endpoint_409_when_animating(wired, monkeypatch):
    from character_swap import runner_reengineer
    monkeypatch.setattr(runner_reengineer, "_ANIMATING", {"re_t"})
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_replace_scene_image("re_t", 0, BackgroundTasks(), _F()))
    assert ei.value.status_code == 409


def test_replace_endpoint_409_all_slots_generating(wired):
    job = _job()
    for jc in job.characters.values():
        for v in jc.images:
            if v.scene_id == "s1":
                v.status = VariantStatus.GENERATING
        jc.approved_variant_ids = [vid for vid in jc.approved_variant_ids
                                   if not vid.endswith("-v1")]
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_replace_scene_image("re_t", 0, BackgroundTasks(), _F()))
    assert ei.value.status_code == 409


def test_replace_endpoint_400_empty_upload(wired, monkeypatch):
    async def empty(f):
        return b""
    monkeypatch.setattr(api, "_read_capped", empty)
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_replace_scene_image("re_t", 0, BackgroundTasks(), _F()))
    assert ei.value.status_code == 400


def test_replace_endpoint_404_bad_idx(wired):
    wired["job"] = _job()
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_replace_scene_image("re_t", 9, BackgroundTasks(), _F()))
    assert ei.value.status_code == 404
