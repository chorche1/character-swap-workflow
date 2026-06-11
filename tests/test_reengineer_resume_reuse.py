"""Analysis-phase fast paths + crash-resume reuse + dup-job guard.

Covers: (1) _probe_duration / _has_audio_stream answer from metadata instead
of full-file decodes (both the ffprobe fast path and the header-only ffmpeg
fallback); (2) a crashed-and-resumed run reuses plan.json instead of
re-billing Whisper + the Claude analyst; (3) the job_id is persisted BEFORE
add_job so a crash in that window resumes into the SAME job instead of
creating a duplicate.
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from character_swap import reengineer, runner_reengineer, video_edit
from character_swap.models import Job, SceneAsset


# ----------------------------------------------------------- probe fast paths

def _color_clip(dest: Path, secs: float = 3.0, with_audio: bool = False) -> Path:
    args = ["ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", f"color=c=red:s=160x284:d={secs}:r=12"]
    if with_audio:
        args += ["-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={secs}",
                 "-shortest"]
    args += ["-pix_fmt", "yuv420p", str(dest)]
    subprocess.run(args, check=True, capture_output=True)
    return dest


@pytest.mark.parametrize("ffprobe_available", [True, False])
def test_probe_duration_both_paths(monkeypatch, tmp_path, ffprobe_available):
    clip = _color_clip(tmp_path / "v.mp4", secs=3.0)
    if not ffprobe_available:
        monkeypatch.setattr(video_edit, "_ffprobe", lambda: None)
    assert video_edit._probe_duration(clip) == pytest.approx(3.0, abs=0.25)


@pytest.mark.parametrize("ffprobe_available", [True, False])
@pytest.mark.parametrize("with_audio", [True, False])
def test_has_audio_stream_both_paths(monkeypatch, tmp_path,
                                     ffprobe_available, with_audio):
    clip = _color_clip(tmp_path / "v.mp4", secs=1.0, with_audio=with_audio)
    if not ffprobe_available:
        monkeypatch.setattr(video_edit, "_ffprobe", lambda: None)
    assert video_edit._has_audio_stream(clip) is with_audio


# ----------------------------------------------------------- resume reuse

class _Store:
    def __init__(self):
        self.jobs: dict[str, Job] = {}
        self.scenes: dict[str, SceneAsset] = {}
        self.added_jobs: list[str] = []

    def get_job(self, jid):
        return self.jobs.get(jid)

    def add_job(self, job):
        self.added_jobs.append(job.job_id)
        self.jobs[job.job_id] = job

    def get_scene(self, sid):
        return self.scenes.get(sid)

    def get_character(self, cid):
        return None


def _wire_state(monkeypatch, state: dict, store: _Store):
    states = {state["re_id"]: dict(state)}

    def load_state(re_id):
        return dict(states.get(re_id) or {})

    def save_state(s):
        states[s["re_id"]] = dict(s)

    monkeypatch.setattr(runner_reengineer.reengineer, "load_state", load_state)
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state", save_state)
    monkeypatch.setattr(runner_reengineer, "store", lambda: store)
    return states


def _boom(name):
    def f(*a, **k):
        raise AssertionError(f"{name} must not be called on this path")
    return f


def test_analyze_reuses_plan_json(monkeypatch, tmp_path):
    """With a valid plan.json + registered scenes on disk, resume must skip
    scene detection, Whisper, AND the Claude analyst entirely."""
    re_id = "re_reuse"
    run_dir = tmp_path / re_id
    (run_dir / "scenes").mkdir(parents=True)

    store = _Store()
    scenes_dir = tmp_path / "library"
    scenes_dir.mkdir()
    entries = []
    for i, sid in enumerate(["sc_a", "sc_b"]):
        (scenes_dir / f"{sid}.png").write_bytes(b"png")
        store.scenes[sid] = SceneAsset(scene_id=sid, filename=f"{sid}.png",
                                       original_name=f"{sid}.png")
        entries.append({"idx": i, "scene_id": sid, "start": float(i),
                        "end": float(i + 1), "duration": 1.0,
                        "motion_prompt": "m", "speech": "", "summary": "s"})
    (run_dir / "plan.json").write_text(json.dumps(entries), encoding="utf-8")

    state = {"re_id": re_id, "status": "analyzing",
             "source_path": str(tmp_path / "missing.mp4"),
             "character_ids": []}
    _wire_state(monkeypatch, state, store)
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    monkeypatch.setattr(runner_reengineer.settings.__class__, "scenes_dir",
                        property(lambda self: scenes_dir), raising=False)
    monkeypatch.setattr(runner_reengineer.reengineer, "detect_scenes",
                        _boom("detect_scenes"))
    monkeypatch.setattr(runner_reengineer.video_edit, "transcribe_words",
                        _boom("transcribe_words"))
    monkeypatch.setattr(runner_reengineer.reengineer, "analyze_scenes",
                        _boom("analyze_scenes"))

    captured = {}

    async def fake_create(re_id_, state_, scene_entries, job_id):
        captured["entries"] = scene_entries
        captured["job_id"] = job_id
        captured["state_job_id"] = state_.get("job_id")
    monkeypatch.setattr(runner_reengineer, "_create_job_and_swap", fake_create)

    asyncio.run(runner_reengineer._do_analyze_and_swap(re_id, dict(state)))
    assert [e["scene_id"] for e in captured["entries"]] == ["sc_a", "sc_b"]
    # job_id minted + persisted into state BEFORE job creation.
    assert captured["job_id"].startswith("j_")
    assert captured["state_job_id"] == captured["job_id"]


def test_stale_plan_json_is_not_trusted(monkeypatch, tmp_path):
    """plan.json referencing scenes that no longer exist on disk → full
    recompute (here: detect_scenes raising proves the cache was bypassed)."""
    re_id = "re_stale"
    run_dir = tmp_path / re_id
    run_dir.mkdir(parents=True)
    (run_dir / "plan.json").write_text(json.dumps(
        [{"idx": 0, "scene_id": "sc_gone"}]), encoding="utf-8")

    store = _Store()
    state = {"re_id": re_id, "status": "analyzing",
             "source_path": str(tmp_path / "v.mp4"), "character_ids": []}
    _wire_state(monkeypatch, state, store)
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)

    class _Detects(Exception):
        pass

    def detect(*a, **k):
        raise _Detects()
    monkeypatch.setattr(runner_reengineer.reengineer, "detect_scenes", detect)

    with pytest.raises(_Detects):
        asyncio.run(runner_reengineer._do_analyze_and_swap(re_id, dict(state)))


def test_no_duplicate_job_when_job_already_exists(monkeypatch, tmp_path):
    """Crash window: job_id in state AND the job in the store → re-attach
    (delegate to _resume_swapping), never re-analyze or add a second job."""
    re_id = "re_dup"
    store = _Store()
    job = Job(job_id="j_exists", title="t", scene_id="s1",
              scene_image_path="/p.png", characters={})
    store.jobs["j_exists"] = job

    state = {"re_id": re_id, "status": "analyzing", "job_id": "j_exists",
             "source_path": str(tmp_path / "v.mp4"), "character_ids": []}
    _wire_state(monkeypatch, state, store)
    monkeypatch.setattr(runner_reengineer.reengineer, "detect_scenes",
                        _boom("detect_scenes"))

    resumed = {}

    async def fake_resume(re_id_, state_):
        resumed["re_id"] = re_id_
    monkeypatch.setattr(runner_reengineer, "_resume_swapping", fake_resume)

    asyncio.run(runner_reengineer._do_analyze_and_swap(re_id, dict(state)))
    assert resumed["re_id"] == re_id
    assert store.added_jobs == []           # no duplicate created


def test_job_id_reused_when_persisted_but_job_missing(monkeypatch, tmp_path):
    """Crash AFTER the job_id was persisted but BEFORE add_job: the resumed
    run must mint the job under the SAME recorded id."""
    re_id = "re_idreuse"
    run_dir = tmp_path / re_id
    (run_dir / "scenes").mkdir(parents=True)

    store = _Store()
    scenes_dir = tmp_path / "library"
    scenes_dir.mkdir()
    (scenes_dir / "sc_a.png").write_bytes(b"png")
    store.scenes["sc_a"] = SceneAsset(scene_id="sc_a", filename="sc_a.png",
                                      original_name="sc_a.png")
    (run_dir / "plan.json").write_text(json.dumps(
        [{"idx": 0, "scene_id": "sc_a", "start": 0.0, "end": 1.0,
          "duration": 1.0, "motion_prompt": "m", "speech": "",
          "summary": "s"}]), encoding="utf-8")

    state = {"re_id": re_id, "status": "analyzing", "job_id": "j_recorded",
             "source_path": str(tmp_path / "v.mp4"), "character_ids": []}
    _wire_state(monkeypatch, state, store)
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    monkeypatch.setattr(runner_reengineer.settings.__class__, "scenes_dir",
                        property(lambda self: scenes_dir), raising=False)

    captured = {}

    async def fake_create(re_id_, state_, scene_entries, job_id):
        captured["job_id"] = job_id
    monkeypatch.setattr(runner_reengineer, "_create_job_and_swap", fake_create)

    asyncio.run(runner_reengineer._do_analyze_and_swap(re_id, dict(state)))
    assert captured["job_id"] == "j_recorded"
