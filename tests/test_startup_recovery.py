"""Crash-resume recovery (2026-07-02, from the 2026-07-01 reliability audit).

A server restart used to strand: Step-6 compiles ('compiling' forever, no
reset), repurposes (job + editor-reel), the Resolve pipeline, ANIMATING chars
whose video placeholders were never persisted, legacy free-form generations
(now runner-less AND delete-blocked), reengineer resume watchers that crash,
and reanimates whose dirty flag was cleared over interrupted clips. All flips
are LOUD (failed + message) — never silently resumed-and-hoped.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import api, runner, runner_reengineer
from character_swap.models import (
    CharStatus,
    GenKind,
    GenStatus,
    Job,
    JobCharacter,
    MediaGeneration,
    VideoStatus,
    VideoVariant,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeStore:
    def __init__(self, jobs=None, gens=None):
        self.jobs = {j.job_id: j for j in (jobs or [])}
        self.gens = {g.gen_id: g for g in (gens or [])}
        self.updated_jobs: list[str] = []
        self.updated_gens: list[str] = []

    def list_jobs(self):
        return list(self.jobs.values())

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def update_job(self, job):
        self.updated_jobs.append(job.job_id)

    def list_generations(self):
        return list(self.gens.values())

    def get_generation(self, gen_id):
        return self.gens.get(gen_id)

    def update_generation(self, gen):
        self.updated_gens.append(gen.gen_id)


def _job(**jc_fields) -> Job:
    jc = JobCharacter(char_id="c1", name="Chang",
                      source_image_path="x.png", **jc_fields)
    return Job(job_id="j1", scene_id="s1", scene_ids=["s1"],
               scene_image_path="scene.png", characters={"c1": jc})


# ------------------------------------------------- _recover_startup_orphans

def test_recovery_fails_stuck_compile_repurpose_pipeline(monkeypatch):
    job = _job(compile_status="compiling", repurpose_status="compiling",
               pipeline_status="rendering")
    store = _FakeStore(jobs=[job])
    monkeypatch.setattr(api, "store", lambda: store)

    counts = api._recover_startup_orphans()

    jc = job.characters["c1"]
    assert jc.compile_status == "failed" and "omstart" in jc.compile_error
    assert jc.repurpose_status == "failed" and "omstart" in jc.repurpose_error
    assert jc.pipeline_status == "failed" and "omstart" in jc.pipeline_error
    assert counts == {"compile": 1, "repurpose": 1, "pipeline": 1,
                      "reel_repurpose": 0, "generation": 0}
    assert store.updated_jobs == ["j1"]


def test_recovery_leaves_terminal_and_untouched_jobs_alone(monkeypatch):
    job = _job(compile_status="done", repurpose_status=None,
               pipeline_status="failed")
    store = _FakeStore(jobs=[job])
    monkeypatch.setattr(api, "store", lambda: store)
    counts = api._recover_startup_orphans()
    assert not any(counts.values())
    assert store.updated_jobs == []          # no gratuitous re-persist


def test_recovery_fails_stuck_legacy_generation_so_delete_works(monkeypatch):
    gen = MediaGeneration(gen_id="g1", kind=GenKind.VIDEO, model="grok-imagine",
                          prompt="x", status=GenStatus.RUNNING)
    store = _FakeStore(gens=[gen])
    monkeypatch.setattr(api, "store", lambda: store)
    counts = api._recover_startup_orphans()
    assert gen.status is GenStatus.FAILED
    assert "raderas" in gen.error
    assert counts["generation"] == 1


def test_recovery_fails_stuck_editor_reel_repurpose(monkeypatch):
    gen = MediaGeneration(gen_id="g2", kind=GenKind.EDITOR, model="editor",
                          prompt="reel", status=GenStatus.DONE,
                          editor_meta={"edit_id": "e1",
                                       "repurpose": {"status": "compiling"}})
    store = _FakeStore(gens=[gen])
    monkeypatch.setattr(api, "store", lambda: store)
    counts = api._recover_startup_orphans()
    rep = gen.editor_meta["repurpose"]
    assert rep["status"] == "failed" and "omstart" in rep["error"]
    assert counts["reel_repurpose"] == 1
    # A DONE editor reel itself is never flipped.
    assert gen.status is GenStatus.DONE


# ------------------------------- resume_pending: ANIMATING with zero videos

def test_resume_fails_animating_char_with_no_video_rows(monkeypatch):
    job = _job()
    job.characters["c1"].status = CharStatus.ANIMATING
    store = _FakeStore(jobs=[job])
    monkeypatch.setattr(runner, "store", lambda: store)

    _run(runner.resume_pending("j1"))

    jc = job.characters["c1"]
    assert jc.status is CharStatus.FAILED
    assert "movement" in (jc.error or "")


def test_resume_still_completes_animating_char_with_terminal_videos(monkeypatch):
    job = _job()
    jc = job.characters["c1"]
    jc.status = CharStatus.ANIMATING
    jc.videos = [VideoVariant(video_id="v1", source_variant_id="iv1", grok_job_id="req1",
                              status=VideoStatus.DONE,
                              final_video_path="v.mp4")]
    store = _FakeStore(jobs=[job])
    monkeypatch.setattr(runner, "store", lambda: store)

    _run(runner.resume_pending("j1"))

    assert jc.status is CharStatus.DONE      # _maybe_complete_char path intact


# ------------------------------------------- guarded reengineer resume

def test_guarded_resume_flips_run_to_failed_on_crash(monkeypatch):
    updates = {}

    def fake_update(re_id, **changes):
        updates[re_id] = changes
        return changes

    monkeypatch.setattr(runner_reengineer, "_update", fake_update)

    async def boom():
        raise RuntimeError("kaboom")

    _run(runner_reengineer._guarded_resume("re9", boom()))
    assert updates["re9"]["status"] == "failed"
    assert "kaboom" in updates["re9"]["error"]


def test_guarded_resume_passes_through_success(monkeypatch):
    updates = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **c: updates.setdefault(re_id, c))

    async def fine():
        return None

    _run(runner_reengineer._guarded_resume("re9", fine()))
    assert updates == {}                     # no failure write


# ------------------------- _resume_reanimating keeps dirty on interrupted

def test_resume_reanimating_keeps_dirty_when_clips_interrupted(monkeypatch):
    job = _job()
    jc = job.characters["c1"]
    jc.videos = [VideoVariant(video_id="v1", source_variant_id="iv1", grok_job_id="req1",
                              status=VideoStatus.FAILED,
                              error="interrupted (server restart)")]
    store = _FakeStore(jobs=[job])
    monkeypatch.setattr(runner_reengineer, "store", lambda: store)
    monkeypatch.setattr(runner_reengineer, "_POLL_SECS", 0.01)
    finalized = {}
    monkeypatch.setattr(runner_reengineer, "_finalize_reanimate",
                        lambda re_id, clear_dirty: finalized.update(
                            {"re_id": re_id, "clear_dirty": clear_dirty}))
    updates = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **c: updates.update(c))

    state = {"re_id": "re1", "job_id": "j1", "scenes": []}
    _run(runner_reengineer._resume_reanimating("re1", state))

    assert finalized["clear_dirty"] is False
    assert "ändrade" in updates["error"]


def test_resume_reanimating_clears_dirty_on_clean_finish(monkeypatch):
    job = _job()
    jc = job.characters["c1"]
    jc.videos = [VideoVariant(video_id="v1", source_variant_id="iv1", grok_job_id="req1",
                              status=VideoStatus.DONE,
                              final_video_path="v.mp4")]
    store = _FakeStore(jobs=[job])
    monkeypatch.setattr(runner_reengineer, "store", lambda: store)
    monkeypatch.setattr(runner_reengineer, "_POLL_SECS", 0.01)
    finalized = {}
    monkeypatch.setattr(runner_reengineer, "_finalize_reanimate",
                        lambda re_id, clear_dirty: finalized.update(
                            {"clear_dirty": clear_dirty}))
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: {"re_id": re_id})

    state = {"re_id": "re1", "job_id": "j1", "scenes": []}
    _run(runner_reengineer._resume_reanimating("re1", state))

    assert finalized["clear_dirty"] is True


def test_recovery_clears_stuck_reengineer_repurposing_flag(monkeypatch):
    from character_swap import reengineer as reengineer_mod
    st = {"re_id": "re7", "status": "done", "repurposing": True}
    saved = []
    monkeypatch.setattr(reengineer_mod, "list_states", lambda: [st])
    monkeypatch.setattr(reengineer_mod, "save_state", lambda x: saved.append(x))
    monkeypatch.setattr(api, "store", lambda: _FakeStore())

    counts = api._recover_startup_orphans()

    assert st["repurposing"] is False
    assert "omstart" in st["error"]
    assert counts.get("re_repurpose") == 1
    assert saved == [st]


def test_reanimate_interrupted_check_scoped_to_redo_scenes(monkeypatch):
    """A stale interrupted extra-take on a DIFFERENT scene must not keep
    flagging every future reanimate (would re-bill clips that succeeded)."""
    from character_swap.models import GeneratedImage, VariantStatus
    job = _job()
    jc = job.characters["c1"]
    # Stale interrupted clip belongs to scene A; the reanimate touched scene B.
    jc.images = [GeneratedImage(variant_id="ivA", scene_id="scA", path="a.png", prompt="p",
                                status=VariantStatus.READY),
                 GeneratedImage(variant_id="ivB", scene_id="scB", path="b.png", prompt="p",
                                status=VariantStatus.READY)]
    jc.videos = [
        VideoVariant(video_id="vA", source_variant_id="ivA", grok_job_id="r1",
                     status=VideoStatus.FAILED,
                     error="interrupted (server restart)"),
        VideoVariant(video_id="vB", source_variant_id="ivB", grok_job_id="r2",
                     status=VideoStatus.DONE, final_video_path="b.mp4"),
    ]
    store = _FakeStore(jobs=[job])
    monkeypatch.setattr(runner_reengineer, "store", lambda: store)
    monkeypatch.setattr(runner_reengineer, "_POLL_SECS", 0.01)
    finalized = {}
    monkeypatch.setattr(runner_reengineer, "_finalize_reanimate",
                        lambda re_id, clear_dirty: finalized.update(
                            {"clear_dirty": clear_dirty}))
    run_state = {"re_id": "re1",
                 "scenes": [{"scene_id": "scA"}, {"scene_id": "scB"}],
                 "reanimate_idxs": [1]}          # only scene B reanimated
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: dict(run_state))

    state = {"re_id": "re1", "job_id": "j1", "scenes": []}
    _run(runner_reengineer._resume_reanimating("re1", state))

    assert finalized["clear_dirty"] is True      # stale scA row ignored


def test_reanimate_resume_terminates_for_all_direct_runs(monkeypatch):
    """Pure-direct runs have zero VideoVariants; the wait loop must exit when
    the revived direct redos finish, not spin until the 1h timeout."""
    job = _job()                                 # no videos at all
    store = _FakeStore(jobs=[job])
    monkeypatch.setattr(runner_reengineer, "store", lambda: store)
    monkeypatch.setattr(runner_reengineer, "_POLL_SECS", 0.01)
    rendered = []

    async def fake_render(re_id, sid):
        rendered.append(sid)

    monkeypatch.setattr(runner_reengineer, "_render_direct_clip", fake_render)
    finalized = {}
    monkeypatch.setattr(runner_reengineer, "_finalize_reanimate",
                        lambda re_id, clear_dirty: finalized.update(
                            {"clear_dirty": clear_dirty}))
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: {"re_id": re_id})

    state = {"re_id": "re1", "job_id": "j1",
             "scenes": [{"scene_id": "scD", "is_direct": True,
                         "shared_clip_path": None}]}
    _run(asyncio.wait_for(
        runner_reengineer._resume_reanimating("re1", state), timeout=5))

    assert rendered == ["scD"]                   # redo revived
    assert finalized["clear_dirty"] is True      # and the run finalized
