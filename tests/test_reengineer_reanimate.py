"""Edit-mode re-animation engine (runner_reengineer.reanimate).

Targets specific scene entries: existing clips are redone in place via
retry_one_video (assembly finds the new take automatically), scenes without
a clip (added/duplicated) get their first via generate_more_videos. Never
assembles — the user rebuilds behind the explicit button.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


def _job(*, with_clip_s2: bool = True) -> Job:
    """Two chars × two scenes. cA has clips for both scenes; cB has a clip
    for s1 only (s2 approved-but-unanimated when with_clip_s2=False there)."""
    def _char(cid, clips):
        images = [
            GeneratedImage(variant_id=f"{cid}-v1", path="/1.png", prompt="p",
                           scene_id="s1", status=VariantStatus.READY),
            GeneratedImage(variant_id=f"{cid}-v2", path="/2.png", prompt="p",
                           scene_id="s2", status=VariantStatus.READY),
        ]
        videos = [VideoVariant(video_id=f"{cid}-vid{i}", grok_job_id=f"g{i}",
                               status=VideoStatus.DONE,
                               source_variant_id=f"{cid}-v{i}",
                               final_video_path=f"/{cid}-{i}.mp4")
                  for i in clips]
        return JobCharacter(char_id=cid, name=cid, source_image_path="/c.png",
                            status=CharStatus.DONE, images=images,
                            approved_variant_ids=[f"{cid}-v1", f"{cid}-v2"],
                            videos=videos)

    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
               scene_ids=["s1", "s2"], scene_image_paths=["/p1.png", "/p2.png"],
               characters={"cA": _char("cA", [1, 2]),
                           "cB": _char("cB", [1, 2] if with_clip_s2 else [1])},
               origin="reengineer:re_t",
               movement_prompt="animate",
               movement_prompts={"s1": "old s1", "s2": "old s2"},
               durations_by_scene={"s1": 5, "s2": 5})


def _state(status="done"):
    return {"re_id": "re_t", "status": status, "job_id": "j1",
            "scenes": [
                {"idx": 0, "scene_id": "s1", "start": 0.0, "end": 5.0,
                 "duration": 5.0, "motion_prompt": "EDITED s1 prompt",
                 "speech": "", "summary": "one", "dirty": True},
                {"idx": 1, "scene_id": "s2", "start": 5.0, "end": 10.0,
                 "duration": 7.0, "motion_prompt": "s2 prompt",
                 "speech": "", "summary": "two", "dirty": True},
            ]}


def _wire(monkeypatch, job, state):
    states = {state["re_id"]: dict(state)}

    def load_state(re_id):
        s = states.get(re_id)
        return dict(s) if s else None

    def save_state(s):
        states[s["re_id"]] = dict(s)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state", load_state)
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state", save_state)

    class _S:
        def get_job(self, jid):
            return job if jid == "j1" else None

        def update_job(self, j):
            pass
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    calls = {"retry": [], "more": [], "assemble": 0}

    async def fake_retry(job_id, cid, video_id, prompt_override=None, **kw):
        calls["retry"].append((cid, video_id))
    monkeypatch.setattr(runner_reengineer.runner, "retry_one_video", fake_retry)

    async def fake_more(job_id, cid, n, *, source_variant_id=None, **kw):
        calls["more"].append((cid, source_variant_id, n))
    monkeypatch.setattr(runner_reengineer.runner, "generate_more_videos", fake_more)

    async def fake_assemble(re_id):
        calls["assemble"] += 1
    monkeypatch.setattr(runner_reengineer, "assemble", fake_assemble)
    return states, calls


def test_reanimate_redoes_existing_clips_in_place(monkeypatch):
    job = _job()
    states, calls = _wire(monkeypatch, job, _state())
    asyncio.run(runner_reengineer.reanimate("re_t", [0]))
    assert sorted(calls["retry"]) == [("cA", "cA-vid1"), ("cB", "cB-vid1")]
    assert calls["more"] == []
    assert calls["assemble"] == 0                     # never assembles
    # Edited prompt synced into the job (accent clause appended).
    assert job.movement_prompts["s1"].startswith("EDITED s1 prompt")
    assert "American" in job.movement_prompts["s1"]


def test_reanimate_generates_first_clip_for_clipless_scene(monkeypatch):
    job = _job(with_clip_s2=False)                    # cB lacks the s2 clip
    states, calls = _wire(monkeypatch, job, _state())
    asyncio.run(runner_reengineer.reanimate("re_t", [1]))
    assert calls["retry"] == [("cA", "cA-vid2")]
    assert calls["more"] == [("cB", "cB-v2", 1)]


def test_reanimate_single_character_scope(monkeypatch):
    job = _job()
    states, calls = _wire(monkeypatch, job, _state())
    asyncio.run(runner_reengineer.reanimate("re_t", [0], char_id="cB",
                                            clear_dirty=False))
    assert calls["retry"] == [("cB", "cB-vid1")]


def test_reanimate_skips_in_flight_clips(monkeypatch):
    job = _job()
    job.characters["cA"].videos[0].status = VideoStatus.PROCESSING
    states, calls = _wire(monkeypatch, job, _state())
    asyncio.run(runner_reengineer.reanimate("re_t", [0]))
    assert calls["retry"] == [("cB", "cB-vid1")]      # cA's s1 skipped


def test_reanimate_status_roundtrip_and_flags(monkeypatch):
    job = _job()
    states, calls = _wire(monkeypatch, job, _state(status="partial_success"))
    asyncio.run(runner_reengineer.reanimate("re_t", [0]))
    final = states["re_t"]
    assert final["status"] == "partial_success"       # restored, not "done"
    assert final["finals_stale"] is True
    assert not final.get("resume_status")
    assert not final.get("reanimate_idxs")
    # dirty cleared ONLY on the targeted entry.
    assert "dirty" not in final["scenes"][0]
    assert final["scenes"][1].get("dirty") is True


def test_redo_keeps_dirty_flag(monkeypatch):
    """Plain redo (clear_dirty=False): the prompt wasn't the reason for the
    redo, so an edited-but-not-reanimated scene stays marked."""
    job = _job()
    states, calls = _wire(monkeypatch, job, _state())
    asyncio.run(runner_reengineer.reanimate("re_t", [0], clear_dirty=False))
    assert states["re_t"]["scenes"][0].get("dirty") is True
    assert states["re_t"]["finals_stale"] is True


def test_reanimate_noop_before_gate(monkeypatch):
    """Pre-animate (no movement on the job) → the default animate flow owns
    everything; reanimate must do nothing."""
    job = _job()
    job.movement_prompt = None
    job.movement_prompts = {}
    states, calls = _wire(monkeypatch, job, _state(status="awaiting_approval"))
    asyncio.run(runner_reengineer.reanimate("re_t", [0]))
    assert calls["retry"] == [] and calls["more"] == []
    assert states["re_t"]["status"] == "awaiting_approval"


def test_resume_all_dispatches_reanimating(monkeypatch):
    spawned: list[str] = []
    monkeypatch.setattr(runner_reengineer, "_spawn",
                        lambda coro, name: (spawned.append(name), coro.close()))
    monkeypatch.setattr(runner_reengineer.reengineer, "list_states", lambda: [
        {"re_id": "re_r", "status": "reanimating", "job_id": "j1"},
    ])
    asyncio.run(runner_reengineer.resume_all())
    assert spawned == ["reengineer-resume-re_r"]
