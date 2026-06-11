"""Reengineer edit-mode endpoints: PATCH scene, duplicate, delete, reorder.

All endpoints key scenes by LIST INDEX (scene_id is not unique in
state.scenes). The default pipeline is untouched — these only run on user
action, in statuses {awaiting_approval, done, partial_success, failed}.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    SceneAsset,
    VariantStatus,
)


def _job(*, movement: bool):
    def _char(cid):
        images = [
            GeneratedImage(variant_id=f"{cid}-v1", path="/1.png", prompt="p",
                           scene_id="s1", status=VariantStatus.READY),
            GeneratedImage(variant_id=f"{cid}-v2", path="/2.png", prompt="p",
                           scene_id="s2", status=VariantStatus.READY),
        ]
        return JobCharacter(char_id=cid, name=cid, source_image_path="/c.png",
                            status=CharStatus.APPROVED, images=images,
                            approved_variant_ids=[f"{cid}-v1", f"{cid}-v2"])
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p1.png",
               scene_ids=["s1", "s2"], scene_image_paths=["/p1.png", "/p2.png"],
               characters={"cA": _char("cA")}, origin="reengineer:re_t",
               movement_prompt=("animate" if movement else None),
               movement_prompts=({"s1": "old", "s2": "old"} if movement else {}),
               durations_by_scene=({"s1": 5, "s2": 5} if movement else {}))


def _state(status="awaiting_approval", finals=False):
    st = {"re_id": "re_t", "status": status, "job_id": "j1", "n_scenes": 2,
          "scenes": [
              {"idx": 0, "scene_id": "s1", "start": 0.0, "end": 5.0,
               "duration": 5.0, "motion_prompt": "p one", "speech": "",
               "summary": "one"},
              {"idx": 1, "scene_id": "s2", "start": 5.0, "end": 10.0,
               "duration": 5.0, "motion_prompt": "p two", "speech": "",
               "summary": "two"},
          ]}
    if finals:
        st["finals"] = {"cA": {"status": "done", "final_path": "/f.mp4"}}
    return st


@pytest.fixture
def wired(monkeypatch):
    """Fake store + state IO for api-level endpoint tests."""
    box = {"job": None, "states": {}, "scenes": {}}

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j1" else None

        def update_job(self, j):
            box["job_updated"] = True

        def get_scene(self, sid):
            return box["scenes"].get(sid)

        def add_scene(self, scene):
            box["scenes"][scene.scene_id] = scene
    monkeypatch.setattr(api, "store", lambda: _S())

    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)
    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    # runner_reengineer reads via its own import of `reengineer`.
    from character_swap import runner_reengineer
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state", load_state)
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state", save_state)

    class _RS:
        get_job = _S.get_job
        update_job = _S.update_job
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    return box


# ---------------------------------------------------------------- PATCH scene

def test_patch_at_gate_updates_without_dirty(wired):
    wired["job"] = _job(movement=False)
    wired["states"]["re_t"] = _state("awaiting_approval")
    out = asyncio.run(api.reengineer_edit_scene(
        "re_t", 0, api.ReSceneEditBody(motion_prompt="NEW prompt")))
    saved = wired["states"]["re_t"]
    assert saved["scenes"][0]["motion_prompt"] == "NEW prompt"
    assert "dirty" not in saved["scenes"][0]            # gate = free edit
    assert wired["job"].movement_prompts == {}          # job untouched


def test_patch_post_gate_marks_dirty_and_syncs_job(wired):
    job = _job(movement=True)
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    asyncio.run(api.reengineer_edit_scene(
        "re_t", 1, api.ReSceneEditBody(motion_prompt="Say something else",
                                       duration=8.0)))
    saved = wired["states"]["re_t"]
    assert saved["scenes"][1]["dirty"] is True
    assert saved["scenes"][1]["duration"] == 8.0
    assert job.movement_prompts["s2"].startswith("Say something else")
    assert "American" in job.movement_prompts["s2"]     # accent enforced
    assert job.durations_by_scene["s2"] == 8            # clamped int


def test_patch_duration_clamped(wired):
    wired["job"] = _job(movement=False)
    wired["states"]["re_t"] = _state()
    asyncio.run(api.reengineer_edit_scene(
        "re_t", 0, api.ReSceneEditBody(duration=99.0)))
    assert wired["states"]["re_t"]["scenes"][0]["duration"] == 15.0


def test_patch_bad_idx_404(wired):
    wired["job"] = _job(movement=False)
    wired["states"]["re_t"] = _state()
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_edit_scene(
            "re_t", 9, api.ReSceneEditBody(motion_prompt="x")))
    assert e.value.status_code == 404


def test_patch_blocked_mid_phase(wired):
    wired["job"] = _job(movement=False)
    wired["states"]["re_t"] = _state("swapping")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_edit_scene(
            "re_t", 0, api.ReSceneEditBody(motion_prompt="x")))
    assert e.value.status_code == 409


# ---------------------------------------------------------------- duplicate

def test_duplicate_clones_approved_zero_generations(wired, monkeypatch):
    job = _job(movement=True)
    wired["job"] = job
    wired["states"]["re_t"] = _state("done", finals=True)
    wired["scenes"]["s1"] = SceneAsset(scene_id="s1", filename="s1.png",
                                       original_name="s1.png")
    gen_calls = []
    from character_swap import pipeline
    monkeypatch.setattr(pipeline, "generate_variant",
                        lambda **kw: gen_calls.append(kw))

    out = asyncio.run(api.reengineer_duplicate_scene("re_t", 0))
    saved = wired["states"]["re_t"]
    assert len(saved["scenes"]) == 3
    copy = saved["scenes"][1]                        # inserted after source
    assert copy["scene_id"] != "s1"
    assert copy["scene_id"].startswith("s1__dup")
    assert copy["dirty"] is True and copy["source"] == "duplicate"
    assert "(kopia)" in copy["summary"]
    assert [e["idx"] for e in saved["scenes"]] == [0, 1, 2]
    assert saved["n_scenes"] == 3
    assert saved["finals_stale"] is True
    # SceneAsset registered → strip thumbnail resolves, SAME file.
    assert wired["scenes"][copy["scene_id"]].filename == "s1.png"
    # Approved variant cloned + auto-approved on the job; NO generation.
    jc = job.characters["cA"]
    clones = [v for v in jc.images if v.scene_id == copy["scene_id"]]
    assert len(clones) == 1 and clones[0].path == "/1.png"
    assert clones[0].variant_id in jc.approved_variant_ids
    assert gen_calls == []


# ---------------------------------------------------------------- delete

def test_delete_scene_removes_entry_and_job_variants(wired):
    job = _job(movement=True)
    wired["job"] = job
    wired["states"]["re_t"] = _state("done", finals=True)
    asyncio.run(api.reengineer_delete_scene("re_t", 1))
    saved = wired["states"]["re_t"]
    assert [e["scene_id"] for e in saved["scenes"]] == ["s1"]
    assert saved["n_scenes"] == 1
    assert saved["finals_stale"] is True
    assert job.scene_ids == ["s1"]
    assert all(v.scene_id != "s2" for v in job.characters["cA"].images)


def test_delete_keeps_job_scene_when_another_entry_shares_it(wired):
    job = _job(movement=True)
    wired["job"] = job
    st = _state("done")
    st["scenes"].append({"idx": 2, "scene_id": "s2", "start": 0.0, "end": 5.0,
                         "duration": 5.0, "motion_prompt": "p two again",
                         "speech": "", "summary": "two again"})
    wired["states"]["re_t"] = st
    asyncio.run(api.reengineer_delete_scene("re_t", 1))
    saved = wired["states"]["re_t"]
    assert [e["scene_id"] for e in saved["scenes"]] == ["s1", "s2"]
    assert job.scene_ids == ["s1", "s2"]            # job slot survives
    assert any(v.scene_id == "s2" for v in job.characters["cA"].images)


def test_delete_last_scene_refused(wired):
    job = _job(movement=True)
    wired["job"] = job
    st = _state("done")
    st["scenes"] = st["scenes"][:1]
    wired["states"]["re_t"] = st
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_delete_scene("re_t", 0))
    assert e.value.status_code == 409


def test_delete_refused_while_images_generating(wired):
    job = _job(movement=True)
    job.characters["cA"].images[1].status = VariantStatus.GENERATING
    wired["job"] = job
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_delete_scene("re_t", 1))
    assert e.value.status_code == 409


# ---------------------------------------------------------------- reorder

def test_reorder_permutes_state_and_job(wired):
    job = _job(movement=True)
    wired["job"] = job
    wired["states"]["re_t"] = _state("done", finals=True)
    asyncio.run(api.reengineer_scene_order(
        "re_t", api.ReSceneOrderBody(order=[1, 0])))
    saved = wired["states"]["re_t"]
    assert [e["scene_id"] for e in saved["scenes"]] == ["s2", "s1"]
    assert [e["idx"] for e in saved["scenes"]] == [0, 1]
    assert saved["finals_stale"] is True
    assert job.scene_ids == ["s2", "s1"]


def test_reorder_rejects_bad_permutation(wired):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_scene_order(
            "re_t", api.ReSceneOrderBody(order=[0, 0])))
    assert e.value.status_code == 400


# ---------------------------------------------------------------- redo / animate_scenes guards

def test_redo_blocked_at_gate(wired):
    wired["job"] = _job(movement=False)
    wired["states"]["re_t"] = _state("awaiting_approval")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_redo_scene("re_t", 0, BackgroundTasks()))
    assert e.value.status_code == 409


def test_redo_schedules_background_task(wired):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_redo_scene(
        "re_t", 0, bg, api.ReRedoBody(char_id="cA")))
    assert len(bg.tasks) == 1
    assert out["char_id"] == "cA"


def test_animate_scenes_defaults_to_dirty_and_reports_skips(wired):
    job = _job(movement=True)
    # cA has NO approval for s2 → that pair must be reported as skipped.
    job.characters["cA"].approved_variant_ids = ["cA-v1"]
    wired["job"] = job
    st = _state("done")
    st["scenes"][1]["dirty"] = True
    wired["states"]["re_t"] = st
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_animate_scenes("re_t", bg))
    assert out["idxs"] == [1]
    assert out["skipped"] == [{"idx": 1, "char_id": "cA",
                               "reason": "no approved variant"}]
    assert len(bg.tasks) == 1


def test_animate_scenes_400_when_nothing_dirty(wired):
    wired["job"] = _job(movement=True)
    wired["states"]["re_t"] = _state("done")
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_animate_scenes("re_t", BackgroundTasks()))
    assert e.value.status_code == 400
