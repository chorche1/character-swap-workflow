"""Reengineer edit mode: '+ Lägg till scen' from an uploaded image or video.

Image → registered directly as a content-addressed scene; video → mid-frame
extracted (+ optional Whisper dialogue prefill). Either way the job's scene
list is extended and swap-image generation fans out for every character in
the background (covered by runner tests — stubbed here).
"""
from __future__ import annotations

import asyncio
import io

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from character_swap import api, runner_reengineer
from character_swap.models import CharStatus, Job, JobCharacter


def _job():
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/c.png",
                      status=CharStatus.DONE, images=[],
                      approved_variant_ids=[])
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p1.png",
               scene_ids=["s1"], scene_image_paths=["/p1.png"],
               characters={"cA": jc}, origin="reengineer:re_t",
               movement_prompt="animate", movement_prompts={"s1": "old"})


def _state(status="done"):
    return {"re_id": "re_t", "status": status, "job_id": "j1", "n_scenes": 1,
            "scenes": [{"idx": 0, "scene_id": "s1", "start": 0.0, "end": 5.0,
                        "duration": 5.0, "motion_prompt": "p", "speech": "",
                        "summary": "one"}]}


@pytest.fixture
def wired(monkeypatch, tmp_path):
    box = {"job": _job(), "states": {"re_t": _state()}, "scenes": {}}

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j1" else None

        def update_job(self, j):
            pass

        def get_scene(self, sid):
            return box["scenes"].get(sid)

        def add_scene(self, scene):
            box["scenes"][scene.scene_id] = scene
    monkeypatch.setattr(api, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)
    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state", load_state)
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state", save_state)
    monkeypatch.setattr(reengineer_mod, "reengineer_dir",
                        lambda rid: tmp_path / rid)
    # Content-addressing writes into the scene library — point it at tmp.
    monkeypatch.setattr(type(api.settings), "scenes_dir",
                        property(lambda self: tmp_path / "library"),
                        raising=False)
    return box


def _upload(name: str, data: bytes) -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def test_add_scene_from_image(wired):
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_add_scene(
        "re_t", bg, file=_upload("extra.png", b"png-bytes"),
        motion_prompt="Hold up the product", duration=6.0,
        whisper=False, position=-1, direct=False))
    saved = wired["states"]["re_t"]
    assert len(saved["scenes"]) == 2
    new = saved["scenes"][1]
    assert new["scene_id"].startswith("sc_")
    assert new["motion_prompt"] == "Hold up the product"
    assert new["duration"] == 6.0
    assert new["dirty"] is True and new["source"] == "image"
    assert saved["n_scenes"] == 2
    # Job extended in lockstep; scene registered in the library.
    assert new["scene_id"] in wired["job"].scene_ids
    assert wired["scenes"][new["scene_id"]] is not None
    assert len(bg.tasks) == 1                       # variant fan-out queued


def test_add_scene_default_prompt_when_empty(wired):
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_add_scene(
        "re_t", bg, file=_upload("extra.png", b"x"),
        motion_prompt="", duration=0.0, whisper=False, position=-1, direct=False))
    new = wired["states"]["re_t"]["scenes"][1]
    assert new["motion_prompt"] == runner_reengineer.ADDED_SCENE_PROMPT
    assert new["duration"] == 5.0                   # image default


def test_add_scene_from_video_extracts_midframe(wired, monkeypatch, tmp_path):
    calls = {}

    from character_swap import video_edit
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 9.0)

    def fake_extract(src, at_secs, dest):
        calls["at"] = at_secs
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"frame")
        return dest
    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "extract_frame", fake_extract)

    bg = BackgroundTasks()
    asyncio.run(api.reengineer_add_scene(
        "re_t", bg, file=_upload("clip.mp4", b"mp4-bytes"),
        motion_prompt="", duration=0.0, whisper=False, position=0, direct=False))
    saved = wired["states"]["re_t"]
    new = saved["scenes"][0]                        # position=0 → first
    assert calls["at"] == pytest.approx(4.5)        # mid-frame
    assert new["source"] == "video"
    assert new["duration"] == 9.0                   # from probe, ≤15
    assert "transcribing" not in new
    assert [e["idx"] for e in saved["scenes"]] == [0, 1]


def test_add_scene_video_whisper_flag(wired, monkeypatch):
    from character_swap import video_edit
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 4.0)
    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "extract_frame",
                        lambda src, at_secs, dest: (
                            dest.parent.mkdir(parents=True, exist_ok=True),
                            dest.write_bytes(b"f"), dest)[-1])
    monkeypatch.setattr(type(api.settings), "openai_api_key",
                        property(lambda self: "key"), raising=False)
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_add_scene(
        "re_t", bg, file=_upload("clip.mov", b"mov"),
        motion_prompt="", duration=0.0, whisper=True, position=-1, direct=False))
    new = wired["states"]["re_t"]["scenes"][1]
    assert new["transcribing"] is True


def test_add_scene_blocked_mid_phase(wired):
    wired["states"]["re_t"]["status"] = "animating"
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_add_scene(
            "re_t", BackgroundTasks(), file=_upload("x.png", b"x"),
            motion_prompt="", duration=0.0, whisper=False, position=-1, direct=False))
    assert e.value.status_code == 409


def test_generate_added_scene_fans_out_with_shared_sem(wired, monkeypatch):
    """The background half: one regen per character, all sharing ONE sem."""
    wired["job"].characters["cB"] = JobCharacter(
        char_id="cB", name="B", source_image_path="/c2.png",
        status=CharStatus.DONE, images=[])
    seen = []

    async def fake_regen(job_id, cid, scene_id, prompt=None, *, sem=None):
        seen.append((cid, scene_id, sem))
    monkeypatch.setattr(runner_reengineer.runner, "regen_scene_variants",
                        fake_regen)
    asyncio.run(runner_reengineer.generate_added_scene("re_t", "sc_new"))
    assert sorted(c for c, _, _ in seen) == ["cA", "cB"]
    assert all(s == "sc_new" for _, s, _ in seen)
    sems = {id(s) for _, _, s in seen}
    assert len(sems) == 1 and seen[0][2] is not None    # shared semaphore


def test_whisper_prefill_rewrites_default_prompt(wired, monkeypatch):
    state = wired["states"]["re_t"]
    state["scenes"].append({
        "idx": 1, "scene_id": "sc_new", "start": 0.0, "end": 4.0,
        "duration": 4.0,
        "motion_prompt": runner_reengineer.ADDED_SCENE_PROMPT,
        "speech": "", "summary": "clip.mp4", "dirty": True,
        "source": "video", "transcribing": True})

    class _W:
        def __init__(self, t):
            self.text = t
    monkeypatch.setattr(runner_reengineer.video_edit, "transcribe_words",
                        lambda p, job_id=None: [_W("grab"), _W("four"),
                                                _W("kiwis")])

    async def fake_regen(*a, **k):
        return None
    monkeypatch.setattr(runner_reengineer.runner, "regen_scene_variants",
                        fake_regen)
    asyncio.run(runner_reengineer.generate_added_scene(
        "re_t", "sc_new", whisper_source="/tmp/clip.mp4"))
    entry = wired["states"]["re_t"]["scenes"][1]
    assert "transcribing" not in entry
    assert ('The person says to the camera with an American accent: '
            '"grab four kiwis"') in entry["motion_prompt"]
    assert entry["speech"] == "grab four kiwis"
