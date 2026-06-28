"""Per-run QC opt-out (Hugo 2026-06-28: "gör så man kan stänga av qc för
körningar om man vill").

A `Job.skip_qc` flag turns off BOTH the image (swap) vision-QC and the video
clip-QC — and their auto-retries — for every slot in that run. Default False =
QC on (the global SWAP_QC / VIDEO_QC env flags still apply). A "Hoppa över QC"
checkbox on the Swap (from_images) + Reengineer (video) forms sets it; the
endpoints persist it into the run state, and `_create_job_and_swap` lifts it
onto the Job. SQLite persistence of the new field is covered by
test_state_persistence.test_sqlite_full_fidelity_job_round_trip (bools synth to
True), so it's not re-asserted here.
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi import BackgroundTasks, UploadFile

from character_swap import api, runner
from character_swap.models import Job


def _job(skip_qc: bool) -> Job:
    return Job(job_id="j", scene_id="s", scene_image_path="p.png",
               skip_qc=skip_qc)


# --- the gating helpers honor both the env flag AND the per-job opt-out -------

def test_swap_qc_on_respects_skip_qc(monkeypatch):
    monkeypatch.setattr(runner.settings, "swap_qc_enabled", True)
    assert runner._swap_qc_on(_job(skip_qc=False)) is True
    assert runner._swap_qc_on(_job(skip_qc=True)) is False


def test_video_qc_on_respects_skip_qc(monkeypatch):
    monkeypatch.setattr(runner.settings, "video_qc_enabled", True)
    assert runner._video_qc_on(_job(skip_qc=False)) is True
    assert runner._video_qc_on(_job(skip_qc=True)) is False


def test_skip_qc_cannot_force_qc_on_when_env_off(monkeypatch):
    # The global env flag is the hard ceiling — skip_qc only ever turns QC OFF.
    monkeypatch.setattr(runner.settings, "swap_qc_enabled", False)
    monkeypatch.setattr(runner.settings, "video_qc_enabled", False)
    assert runner._swap_qc_on(_job(skip_qc=False)) is False
    assert runner._video_qc_on(_job(skip_qc=False)) is False


def test_job_skip_qc_defaults_false():
    assert Job(job_id="j", scene_id="s",
               scene_image_path="p.png").skip_qc is False


# --- both create endpoints persist skip_qc into the run state ----------------

@pytest.fixture
def wired(monkeypatch, tmp_path):
    box = {"states": {}, "scenes": {}, "jobs": {}}

    class _S:
        def get_character(self, cid):
            return object() if cid == "cA" else None

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
    monkeypatch.setattr(api, "store", lambda: _S())
    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "save_state",
                        lambda s: box["states"].update({s["re_id"]: dict(s)}))

    def _redir(rid):
        d = tmp_path / rid
        d.mkdir(parents=True, exist_ok=True)
        return d
    monkeypatch.setattr(reengineer_mod, "reengineer_dir", _redir)
    monkeypatch.setattr(type(api.settings), "scenes_dir",
                        property(lambda self: tmp_path / "library"), raising=False)
    monkeypatch.setattr(type(api.settings), "has_provider", lambda self, p: True)
    monkeypatch.setattr(type(api.settings), "openai_api_key",
                        property(lambda self: "k"), raising=False)
    return box


def _create_video(skip_qc):
    bg = BackgroundTasks()
    f = UploadFile(file=io.BytesIO(b"vid-bytes"), filename="clip.mp4")
    return asyncio.run(api.reengineer_create(
        bg, file=f, character_ids=json.dumps(["cA"]),
        image_model="gpt2-id-swap", video_model="kling-v3",
        auto_mode=False, outfit_mode="scene", outfit_text="",
        scene_sensitivity="high", language="en",
        use_director=False, skip_qc=skip_qc, background_file=None,
        background_source="character", character_source_image_ids=""))


def _create_from_images(skip_qc):
    bg = BackgroundTasks()
    files = [UploadFile(file=io.BytesIO(b"png"), filename="a.png")]
    return asyncio.run(api.reengineer_from_images(
        bg, files=files,
        motion_prompts=json.dumps(['She says: "Try this."']),
        lengths=json.dumps([5]), direct="[]",
        end_frame_files=[], end_frame_idx="[]",
        character_ids=json.dumps(["cA"]), image_model="gpt2-id-swap",
        outfit_mode="scene", outfit_text="", auto_mode=False,
        use_director=False, skip_qc=skip_qc, background_file=None,
        background_source="character", character_source_image_ids=""))


def test_video_endpoint_persists_skip_qc(wired):
    out = _create_video(skip_qc=True)
    assert wired["states"][out["re_id"]]["skip_qc"] is True


def test_video_endpoint_defaults_qc_on(wired):
    out = _create_video(skip_qc=False)
    assert wired["states"][out["re_id"]]["skip_qc"] is False


def test_from_images_endpoint_persists_skip_qc(wired):
    out = _create_from_images(skip_qc=True)
    assert wired["states"][out["re_id"]]["skip_qc"] is True


def test_from_images_endpoint_defaults_qc_on(wired):
    out = _create_from_images(skip_qc=False)
    assert wired["states"][out["re_id"]]["skip_qc"] is False
