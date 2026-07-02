"""Lost-update guard on the Reengineer scene endpoints (audit 2026-07-01).

add_scene / set_direct / import_clip / replace_scene_image load the WHOLE run
state, then await long work (upload stream, ffprobe, frame extraction — up to
minutes for a 1 GiB clip import), then save. Saving the pre-await snapshot
silently reverted any state write that landed in the window: a prior add's
Whisper dialogue prefill (generate_added_scene re-loads, saves motion_prompt
and pops 'transcribing'), a PATCH /scenes/{idx} prompt edit, the poll's
dirty-clear. The fix re-loads right before applying the endpoint's own
mutation — the same reload-before-save pattern as reengineer_char_drive_push
and runner_reengineer._persist_direct.

Each test simulates the concurrent write INSIDE the endpoint's long await
(monkeypatched _read_capped / _import_clip_common) and asserts it survives
the endpoint's final save alongside the endpoint's own mutation.
"""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, UploadFile

from character_swap import api, runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    SceneAsset,
    VariantStatus,
)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    box = {"jobs": {}, "states": {}, "scenes": {}}

    class _S:
        def get_job(self, jid):
            return box["jobs"].get(jid)

        def update_job(self, j):
            box["jobs"][j.job_id] = j

        def get_scene(self, sid):
            return box["scenes"].get(sid)

        def add_scene(self, scene):
            box["scenes"][scene.scene_id] = scene
    monkeypatch.setattr(api, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return json.loads(json.dumps(s)) if s else None   # fresh deep copy

    def save_state(s):
        box["states"][s["re_id"]] = json.loads(json.dumps(s))
    for mod in (reengineer_mod, runner_reengineer.reengineer):
        monkeypatch.setattr(mod, "load_state", load_state)
        monkeypatch.setattr(mod, "save_state", save_state)
    monkeypatch.setattr(reengineer_mod, "reengineer_dir",
                        lambda rid: tmp_path / rid)
    monkeypatch.setattr(type(api.settings), "scenes_dir",
                        property(lambda self: tmp_path / "library"),
                        raising=False)
    return box


def _upload(name="a.png", data=b"png"):
    return UploadFile(file=io.BytesIO(data), filename=name)


def _scene(idx, sid, **extra):
    return {"idx": idx, "scene_id": sid, "start": 0.0, "end": 5.0,
            "duration": 5.0, "motion_prompt": "p", "speech": "",
            "summary": sid, **extra}


def _char(cid, sids):
    images = [GeneratedImage(variant_id=f"{cid}-{sid}", path=f"/{cid}-{sid}.png",
                             prompt="p", scene_id=sid,
                             status=VariantStatus.READY)
              for sid in sids]
    return JobCharacter(char_id=cid, name=cid.upper(),
                        source_image_path="/c.png", status=CharStatus.APPROVED,
                        images=images,
                        approved_variant_ids=[v.variant_id for v in images],
                        approved_variant_id=images[0].variant_id)


def _job(sids):
    return Job(job_id="j1", title="t", scene_id=sids[0],
               scene_image_path=f"/{sids[0]}.png", scene_ids=list(sids),
               scene_image_paths=[f"/{s}.png" for s in sids],
               characters={"cA": _char("cA", sids)},
               origin="reengineer:re_t", movement_prompt="animate",
               movement_prompts={s: "m" for s in sids})


def _racing_read(box, mutate, data=b"PNGBYTES"):
    """An api._read_capped stand-in that lands a CONCURRENT state write
    mid-await, exactly like a background task saving during the upload."""
    async def read(f):
        st = json.loads(json.dumps(box["states"]["re_t"]))
        mutate(st)
        box["states"]["re_t"] = st
        return data
    return read


# --------------------------------------------------------------------------- add_scene

def test_add_scene_preserves_concurrent_whisper_prefill(wired, monkeypatch):
    """The audited failure: scene A's Whisper prefill lands while scene B's
    add is mid-upload → B's save used to erase A's dialogue and resurrect
    'transcribing: true' forever (A's clip then rendered with an empty
    prompt). The prefill must survive AND the new scene must be added."""
    box = wired
    box["jobs"]["j1"] = _job(["s1"])
    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "done", "job_id": "j1", "n_scenes": 1,
        "scenes": [_scene(0, "s1", motion_prompt="", transcribing=True)]}

    def prefill(st):     # generate_added_scene's write for the PRIOR add
        st["scenes"][0]["motion_prompt"] = "TRANSCRIBED LINE"
        st["scenes"][0].pop("transcribing", None)
    monkeypatch.setattr(api, "_read_capped", _racing_read(box, prefill))

    bg = BackgroundTasks()
    asyncio.run(api.reengineer_add_scene(
        "re_t", bg, file=_upload("extra.png", b"new-scene-bytes"),
        motion_prompt="hold it up", duration=5.0, whisper=False,
        position=-1, direct=False))

    saved = box["states"]["re_t"]
    assert len(saved["scenes"]) == 2                       # own mutation applied
    assert saved["scenes"][1]["motion_prompt"] == "hold it up"
    assert saved["scenes"][0]["motion_prompt"] == "TRANSCRIBED LINE"
    assert "transcribing" not in saved["scenes"][0]        # not resurrected


# --------------------------------------------------------------------------- set_direct

def test_set_direct_upload_preserves_concurrent_prompt_edit(wired, monkeypatch):
    box = wired
    box["jobs"]["j1"] = _job(["s1", "s2"])
    box["scenes"]["s1"] = SceneAsset(scene_id="s1", filename="s1.png",
                                     original_name="s1")
    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "awaiting_approval", "job_id": "j1",
        "n_scenes": 2, "scenes": [_scene(0, "s1"), _scene(1, "s2")]}

    def edit(st):        # a PATCH /scenes/1 prompt edit landing mid-upload
        st["scenes"][1]["motion_prompt"] = "CONCURRENT EDIT"
    monkeypatch.setattr(api, "_read_capped",
                        _racing_read(box, edit, data=b"direct-image-bytes"))

    asyncio.run(api.reengineer_set_direct(
        "re_t", 0, file=_upload("direct.png", b"direct-image-bytes")))

    saved = box["states"]["re_t"]
    assert saved["scenes"][0]["is_direct"] is True         # own mutation applied
    assert saved["scenes"][1]["motion_prompt"] == "CONCURRENT EDIT"


# --------------------------------------------------------------------------- import_clip

def test_import_clip_preserves_concurrent_prompt_edit(wired, monkeypatch):
    box = wired
    box["jobs"]["j1"] = _job(["s1", "s2"])
    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "done", "job_id": "j1", "n_scenes": 2,
        "finals": {"cA": {"status": "done", "final_path": "/f.mp4"}},
        "scenes": [_scene(0, "s1", dirty=True), _scene(1, "s2")]}

    async def fake_import(job_id, char_id, file, *, variant_id=None,
                          video_id=None):
        # The minutes-long upload window: an edit lands and is saved.
        st = json.loads(json.dumps(box["states"]["re_t"]))
        st["scenes"][1]["motion_prompt"] = "CONCURRENT EDIT"
        box["states"]["re_t"] = st
        return SimpleNamespace(video_id="vid_new")
    monkeypatch.setattr(api, "_import_clip_common", fake_import)
    monkeypatch.setattr(runner_reengineer, "_scene_all_imported",
                        lambda job, sid: True)

    out = asyncio.run(api.reengineer_import_clip(
        "re_t", 0, char_id="cA", file=_upload("clip.mp4", b"mp4")))

    saved = box["states"]["re_t"]
    assert saved["scenes"][1]["motion_prompt"] == "CONCURRENT EDIT"
    assert saved["finals_stale"] is True                   # own mutation applied
    assert "dirty" not in saved["scenes"][0]               # all-imported → cleared
    assert out["regen_variants"] == {"cA": "vid_new"}


# --------------------------------------------------------------------------- replace_scene_image

def test_replace_scene_image_preserves_concurrent_prompt_edit(wired, monkeypatch):
    from pathlib import Path
    box = wired
    box["jobs"]["j1"] = _job(["s1", "s2"])
    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "done", "job_id": "j1", "n_scenes": 2,
        "finals": {"cA": {"status": "done", "final_path": "/f.mp4"}},
        "scenes": [_scene(0, "s1"), _scene(1, "s2")]}
    monkeypatch.setattr(runner_reengineer, "_register_frame_as_scene",
                        lambda p: ("sc_new", Path("/new.png")))

    def edit(st):
        st["scenes"][1]["motion_prompt"] = "CONCURRENT EDIT"
    monkeypatch.setattr(api, "_read_capped", _racing_read(box, edit))

    bg = BackgroundTasks()
    asyncio.run(api.reengineer_replace_scene_image(
        "re_t", 0, bg, file=_upload("swap.png", b"PNGBYTES")))

    saved = box["states"]["re_t"]
    assert saved["scenes"][0]["scene_id"] == "sc_new"      # own mutation applied
    assert saved["scenes"][1]["motion_prompt"] == "CONCURRENT EDIT"
    assert saved["finals_stale"] is True


# --------------------------------------------------------------------------- run deleted mid-await

def test_add_scene_refuses_when_run_deleted_mid_upload(wired, monkeypatch):
    """Reload finding NOTHING must refuse loudly — saving the stale snapshot
    would resurrect a run the user deleted during the upload."""
    from fastapi import HTTPException
    box = wired
    box["jobs"]["j1"] = _job(["s1"])
    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "done", "job_id": "j1", "n_scenes": 1,
        "scenes": [_scene(0, "s1")]}

    async def deleting_read(f):
        box["states"].pop("re_t")
        return b"PNGBYTES"
    monkeypatch.setattr(api, "_read_capped", deleting_read)

    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_add_scene(
            "re_t", BackgroundTasks(), file=_upload("x.png", b"PNGBYTES"),
            motion_prompt="", duration=5.0, whisper=False,
            position=-1, direct=False))
    assert e.value.status_code == 409
    assert "re_t" not in box["states"]                     # NOT resurrected
