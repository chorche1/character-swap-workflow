"""Per-scene "direct image — no swap" mode.

A scene flagged `is_direct` uses its uploaded image AS-IS (no per-character
swap) and is animated into ONE shared Kling clip reused by every character.
Covers: the from_images `direct` flag, _create_job_and_swap setting
`Job.direct_scene_ids` (+ the all-direct skip), _collect_clips returning the
shared clip for all characters, the swap/video readiness gates, and the gate
set/clear endpoints.
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from character_swap import api, runner, runner_reengineer
from character_swap.models import (
    CharacterAsset, CharStatus, GeneratedImage, Job, JobCharacter,
    SceneAsset, VariantStatus, VideoStatus, VideoVariant,
)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    box = {"states": {}, "scenes": {}, "jobs": {}, "chars": {}}
    (tmp_path / "chars").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scenes").mkdir(parents=True, exist_ok=True)
    for cid in ("cA", "cB"):
        (tmp_path / "chars" / f"{cid}.png").write_bytes(b"charpng")
        box["chars"][cid] = CharacterAsset(char_id=cid, filename=f"{cid}.png",
                                           name=cid.upper())

    class _S:
        def get_character(self, cid):
            return box["chars"].get(cid)

        def get_scene(self, sid):
            return box["scenes"].get(sid)

        def add_scene(self, scene):
            box["scenes"][scene.scene_id] = scene

        def add_job(self, job):
            box["jobs"][job.job_id] = job

        def get_job(self, jid):
            return box["jobs"].get(jid)

        def update_job(self, j):
            box["jobs"][j.job_id] = j
    for mod in (api, runner_reengineer, runner):
        monkeypatch.setattr(mod, "store", lambda: _S())

    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return json.loads(json.dumps(s)) if s else None   # deep copy

    def save_state(s):
        box["states"][s["re_id"]] = json.loads(json.dumps(s))
    for mod in (reengineer_mod, runner_reengineer.reengineer):
        monkeypatch.setattr(mod, "load_state", load_state)
        monkeypatch.setattr(mod, "save_state", save_state)
    monkeypatch.setattr(reengineer_mod, "reengineer_dir", lambda rid: tmp_path / rid)
    monkeypatch.setattr(type(api.settings), "scenes_dir",
                        property(lambda self: tmp_path / "scenes"), raising=False)
    monkeypatch.setattr(type(api.settings), "characters_dir",
                        property(lambda self: tmp_path / "chars"), raising=False)
    monkeypatch.setattr(type(api.settings), "has_provider",
                        lambda self, p: True)
    return box, tmp_path


def _upload(name="a.png", data=b"png"):
    return UploadFile(file=io.BytesIO(data), filename=name)


# --------------------------------------------------------------------------- from_images

def test_from_images_direct_flag_in_state(wired):
    box, _ = wired
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_from_images(
        bg, files=[_upload("a.png"), _upload("b.png", b"png2")],
        motion_prompts=json.dumps(["wave", "pour"]),
        lengths=json.dumps([5, 6]),
        direct=json.dumps([False, True]),
        character_ids=json.dumps(["cA"]),
        image_model="gpt2-id-swap", outfit_mode="scene", outfit_text="",
        auto_mode=False, use_director=False, background_file=None,
        background_source="character",
        character_source_image_ids=""))
    scenes = box["states"][out["re_id"]]["scenes"]
    assert "is_direct" not in scenes[0]
    assert scenes[1]["is_direct"] is True
    assert scenes[1]["direct_image_path"].endswith(".png")


# --------------------------------------------------------------------------- job build

def _seed_state(box, *, scenes):
    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "queued", "job_id": None,
        "character_ids": ["cA", "cB"], "image_model": "gpt2-id-swap",
        "outfit_mode": "scene", "video_model": "kling-v3", "scenes": scenes,
    }


def test_create_job_sets_direct_scene_ids(wired, monkeypatch):
    box, _ = wired
    calls = {"gen": 0}

    async def fake_gen(job_id):
        calls["gen"] += 1

    async def fake_watch(re_id, job_id, *, tasks=None):
        pass
    monkeypatch.setattr(runner, "run_image_generation", fake_gen)
    monkeypatch.setattr(runner_reengineer, "_watch_swap_phase", fake_watch)

    entries = [
        {"idx": 0, "scene_id": "sc_swap", "motion_prompt": "p", "duration": 5.0},
        {"idx": 1, "scene_id": "sc_direct", "motion_prompt": "p", "duration": 5.0,
         "is_direct": True, "direct_image_path": "/x.png"},
    ]
    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", {"re_id": "re_t", "character_ids": ["cA", "cB"],
                 "image_model": "gpt2-id-swap", "video_model": "kling-v3"},
        entries, "j_t"))
    job = box["jobs"]["j_t"]
    assert job.scene_ids == ["sc_swap", "sc_direct"]      # both kept (order)
    assert job.direct_scene_ids == ["sc_direct"]
    assert calls["gen"] == 1                              # swap phase still runs


def test_all_direct_skips_swap(wired, monkeypatch):
    box, _ = wired
    calls = {"gen": 0}

    async def fake_gen(job_id):
        calls["gen"] += 1
    monkeypatch.setattr(runner, "run_image_generation", fake_gen)

    entries = [{"idx": 0, "scene_id": "sc_d", "motion_prompt": "p",
                "duration": 5.0, "is_direct": True, "direct_image_path": "/x.png"}]
    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", {"re_id": "re_t", "character_ids": ["cA"], "auto_mode": False,
                 "image_model": "gpt2-id-swap", "video_model": "kling-v3"},
        entries, "j_t"))
    assert calls["gen"] == 0                              # no swap phase
    assert box["jobs"]["j_t"].direct_scene_ids == ["sc_d"]
    assert box["states"]["re_t"]["status"] == "awaiting_approval"


# --------------------------------------------------------------------------- assemble

def _jc(cid="cA"):
    return JobCharacter(char_id=cid, name=cid.upper(), source_image_path="/c.png",
                        status=CharStatus.APPROVED, images=[], videos=[])


def test_collect_clips_returns_shared_for_all_chars(wired):
    _, tmp = wired
    clip = tmp / "direct_clip_sc_d.mp4"
    clip.write_bytes(b"mp4")
    state = {"scenes": [{"idx": 0, "scene_id": "sc_d", "is_direct": True,
                         "shared_clip_path": str(clip)}]}
    for cid in ("cA", "cB"):
        clips, missing, waitable = runner_reengineer._collect_clips(state, _jc(cid))
        assert clips == [clip] and not missing


def test_collect_clips_direct_waitable_until_ready(wired):
    state = {"scenes": [{"idx": 0, "scene_id": "sc_d", "is_direct": True}]}
    clips, missing, waitable = runner_reengineer._collect_clips(state, _jc())
    assert not clips and missing and waitable is True


def test_swap_videos_done_gate():
    all_direct = Job(job_id="j", scene_id="s1", scene_image_path="/p",
                     scene_ids=["s1"], direct_scene_ids=["s1"],
                     characters={"cA": _jc()})
    assert runner_reengineer._swap_videos_done(all_direct) is True   # no clips expected
    mixed = Job(job_id="j", scene_id="s1", scene_image_path="/p",
                scene_ids=["s1", "s2"], direct_scene_ids=["s2"],
                characters={"cA": _jc()})
    assert runner_reengineer._swap_videos_done(mixed) is False       # waits for s1's clip


# --------------------------------------------------------------------------- gate endpoints

def _seed_job_with_scene(box, tmp):
    """A run at the gate: one scene with an approved swap variant per char."""
    box["scenes"]["sc_x"] = SceneAsset(scene_id="sc_x", filename="sc_x.png",
                                       original_name="x")
    (tmp / "scenes" / "sc_x.png").write_bytes(b"scenepng")
    chars = {}
    for cid in ("cA", "cB"):
        v = GeneratedImage(variant_id=f"v_{cid}", path="/v.png",
                           prompt="p", scene_id="sc_x", status=VariantStatus.READY)
        chars[cid] = JobCharacter(char_id=cid, name=cid.upper(),
                                  source_image_path="/c.png",
                                  status=CharStatus.APPROVED, images=[v],
                                  approved_variant_ids=[f"v_{cid}"],
                                  approved_variant_id=f"v_{cid}")
    box["jobs"]["j_t"] = Job(job_id="j_t", scene_id="sc_x", scene_image_path="/p",
                             scene_ids=["sc_x"], scene_image_paths=["/p"],
                             characters=chars, origin="reengineer:re_t")
    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "awaiting_approval", "job_id": "j_t",
        "n_scenes": 1, "finals": {"cA": {"status": "done"}},
        "scenes": [{"idx": 0, "scene_id": "sc_x", "motion_prompt": "p",
                    "duration": 5.0}]}


def test_set_direct_reuses_scene_image(wired):
    box, tmp = wired
    _seed_job_with_scene(box, tmp)
    out = asyncio.run(api.reengineer_set_direct("re_t", 0, file=None))
    entry = box["states"]["re_t"]["scenes"][0]
    assert entry["is_direct"] is True
    assert entry["direct_image_path"].endswith("sc_x.png")
    job = box["jobs"]["j_t"]
    assert "sc_x" in job.direct_scene_ids
    # per-character swap variants + approvals dropped
    for jc in job.characters.values():
        assert not jc.images and not jc.approved_variant_ids
    assert box["states"]["re_t"]["finals_stale"] is True


def test_set_direct_blocked_while_animating(wired, monkeypatch):
    box, tmp = wired
    _seed_job_with_scene(box, tmp)
    monkeypatch.setattr(runner_reengineer, "_ANIMATING", {"re_t"})
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_set_direct("re_t", 0, file=None))
    assert e.value.status_code == 409


def test_clear_direct_requeues_swap(wired):
    box, tmp = wired
    _seed_job_with_scene(box, tmp)
    asyncio.run(api.reengineer_set_direct("re_t", 0, file=None))
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_clear_direct("re_t", 0, bg))
    entry = box["states"]["re_t"]["scenes"][0]
    assert "is_direct" not in entry and "direct_image_path" not in entry
    assert "sc_x" not in box["jobs"]["j_t"].direct_scene_ids
    assert len(bg.tasks) == 1                            # re-swap queued


def test_clear_direct_rejects_non_direct(wired):
    box, tmp = wired
    _seed_job_with_scene(box, tmp)
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_clear_direct("re_t", 0, BackgroundTasks()))
    assert e.value.status_code == 400


# --------------------------------------------------------------------------- resume

def test_resume_respawns_missing_direct_clips(wired, monkeypatch):
    box, tmp = wired
    box["jobs"]["j_t"] = Job(job_id="j_t", scene_id="sc_d", scene_image_path="/p",
                             scene_ids=["sc_d"], direct_scene_ids=["sc_d"],
                             characters={"cA": _jc()})
    rendered = []

    async def fake_render(re_id, sid):
        rendered.append(sid)

    async def fake_watch(re_id, job_id, *, direct_tasks=()):
        await asyncio.gather(*direct_tasks, return_exceptions=True)
    monkeypatch.setattr(runner_reengineer, "_render_direct_clip", fake_render)
    monkeypatch.setattr(runner_reengineer, "_watch_video_phase", fake_watch)
    state = {"re_id": "re_t", "job_id": "j_t",
             "scenes": [{"idx": 0, "scene_id": "sc_d", "is_direct": True}]}
    asyncio.run(runner_reengineer._resume_animating("re_t", state))
    assert rendered == ["sc_d"]


# --------------------------------------------------------------------------- frontend

def test_frontend_wiring_present():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "web" / "app.js").read_text(encoding="utf-8")
    index = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert "reengineerSetDirect" in app_js
    assert "reengineerClearDirect" in app_js
    assert "sc.is_direct" in index
    assert "row.direct" in index
