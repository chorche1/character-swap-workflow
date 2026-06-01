"""Tests for the Step-1 end-pose → Step-3 generated-end-frame path.

The user uploads an optional end-POSE reference per scene in Step 1; at job
creation it lands on Job.end_frames_by_scene; during Step 3 the runner swaps
the character into that pose and stores the result on
JobCharacter.end_frame_paths[scene_id]. duplicate_scene carries both over.

Hermetic: create_job's CreateJobBody resolution is tested via a stub store;
the runner's end-frame generation is tested by stubbing
pipeline.generate_variant so no real image API is hit.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import api, runner
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
)


def _run(coro):
    return asyncio.run(coro)


# --- create_job: end_poses → Job.end_frames_by_scene ----------------------

class _CreateStore:
    """Minimal store for create_job: scenes + characters exist, captures job."""
    def __init__(self, scenes_dir, chars_dir):
        from character_swap.models import SceneAsset, CharacterAsset
        # two scenes + one end-pose scene, all with real files on disk
        self._scenes = {}
        for sid, name in [("s1", "s1.png"), ("s2", "s2.png"), ("pose1", "pose1.png")]:
            (scenes_dir / name).write_bytes(b"img")
            self._scenes[sid] = SceneAsset(scene_id=sid, filename=name, original_name=name)
        (chars_dir / "ch.png").write_bytes(b"c")
        self._chars = {"cA": CharacterAsset(char_id="cA", filename="ch.png", name="A")}
        self.saved = None

    def get_scene(self, sid):
        return self._scenes.get(sid)

    def get_character(self, cid):
        return self._chars.get(cid)

    def get_project(self, pid):
        return None

    def add_job(self, job):
        self.saved = job


@pytest.fixture
def _create_env(tmp_path, monkeypatch):
    scenes_dir = tmp_path / "input" / "scenes"
    chars_dir = tmp_path / "characters"
    scenes_dir.mkdir(parents=True); chars_dir.mkdir(parents=True)
    store = _CreateStore(scenes_dir, chars_dir)
    monkeypatch.setattr(api, "store", lambda: store)
    # scenes_dir is a read-only property = input_dir/scenes; set input_dir.
    monkeypatch.setattr(api.settings, "input_dir", tmp_path / "input")
    monkeypatch.setattr(api.settings, "characters_dir", chars_dir)
    # require_keys / has_provider are methods → patch on the class.
    monkeypatch.setattr(type(api.settings), "require_keys", lambda self, *a: None)
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    # Don't actually launch the background runner.
    class _BG:
        def add_task(self, *a, **k): pass
    return store, _BG()


def test_create_job_resolves_end_poses(_create_env, monkeypatch):
    store, bg = _create_env
    body = api.CreateJobBody(
        scene_ids=["s1", "s2"], character_ids=["cA"],
        end_poses={"s1": "pose1"},   # scene s1 gets pose1 as its end pose
    )
    _run(api.create_job(body, bg))
    job = store.saved
    assert "s1" in job.end_frames_by_scene
    assert job.end_frames_by_scene["s1"].endswith("pose1.png")
    assert "s2" not in job.end_frames_by_scene   # no pose given for s2


def test_create_job_ignores_unknown_pose(_create_env, monkeypatch):
    store, bg = _create_env
    body = api.CreateJobBody(
        scene_ids=["s1"], character_ids=["cA"],
        end_poses={"s1": "nonexistent_pose"},
    )
    _run(api.create_job(body, bg))
    assert store.saved.end_frames_by_scene == {}


# --- runner: _kick_char generates the end frame ---------------------------

def test_kick_char_generates_end_frame(tmp_path, monkeypatch):
    # Stub the actual image generation: variants + end frame both just touch
    # their dest file so we can assert the end frame got produced + recorded.
    def fake_generate_variant(*, dest, **kw):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"img")
        return Path(dest)
    monkeypatch.setattr(runner.pipeline, "generate_variant", fake_generate_variant)
    monkeypatch.setattr(runner.settings, "output_dir", tmp_path / "out")
    pose = tmp_path / "pose.png"; pose.write_bytes(b"pose")

    jc = JobCharacter(char_id="cA", name="A", source_image_path=str(tmp_path / "src.png"),
                      status=CharStatus.QUEUED)
    (tmp_path / "src.png").write_bytes(b"s")
    job = Job(
        job_id="j_ef", title="t", scene_id="s1", scene_image_path=str(tmp_path / "s1.png"),
        scene_ids=["s1"], scene_image_paths=[str(tmp_path / "s1.png")],
        characters={"cA": jc},
        end_frames_by_scene={"s1": str(pose)},
    )
    (tmp_path / "s1.png").write_bytes(b"sc")

    # Stub persistence/emit so _kick_char runs without a real store/event loop reg.
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    async def _noop_emit(*a, **k): return None
    monkeypatch.setattr(runner, "_emit", _noop_emit)

    import asyncio as _aio
    _run(runner._kick_char(job, jc, 1, _aio.Semaphore(2)))

    # The end frame for scene s1 got generated + recorded on the character.
    assert "s1" in jc.end_frame_paths
    assert Path(jc.end_frame_paths["s1"]).exists()


# --- duplicate_scene carries the end pose + end frame ---------------------

class _DupStore:
    def __init__(self, job): self._job = job
    def get_job(self, jid): return self._job if jid == self._job.job_id else None
    def update_job(self, job): self._job = job


def test_duplicate_scene_carries_end_pose_and_frame(monkeypatch):
    async def _noop(*a, **k): return None
    monkeypatch.setattr(api.events, "publish", _noop)
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/a.png", status=CharStatus.APPROVED,
        images=[GeneratedImage(variant_id="vA1", path="/a.png", prompt="p",
                               scene_id="s1", status=VariantStatus.READY)],
        approved_variant_ids=["vA1"], approved_variant_id="vA1",
        end_frame_paths={"s1": "/end_s1.png"},
    )
    job = Job(
        job_id="j_d", title="t", scene_id="s1", scene_image_path="/p1.png",
        scene_ids=["s1"], scene_image_paths=["/p1.png"],
        characters={"cA": jc}, end_frames_by_scene={"s1": "/pose1.png"},
    )
    monkeypatch.setattr(api, "store", lambda: _DupStore(job))
    result = _run(api.duplicate_scene("j_d", "s1"))
    new_sid = [s["scene_id"] for s in result["scenes"] if s["scene_id"] != "s1"][0]
    saved = api.store().get_job("j_d")
    # Both the end-pose ref and the generated end frame carried to the dup.
    assert saved.end_frames_by_scene.get(new_sid) == "/pose1.png"
    assert saved.characters["cA"].end_frame_paths.get(new_sid) == "/end_s1.png"
