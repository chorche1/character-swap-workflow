"""Reengineer "kör samma recept för fler karaktärer" (Hugo 2026-06-21).

Once a run is finished, the user picks more characters and the SAME recipe
(scenes + current swap/motion prompts + durations + per-clip models + end
frames + background + language) runs for them as new columns in the SAME run —
fully automatic or step-by-step. char 1 is never touched:

  * run_image_generation's movement lock is relaxed for from_reengineer jobs
    (gap #1) so the new chars can generate after movement exists; a plain Swap
    job stays locked.
  * the Director plan is extended from char 1's CURRENT approved slot prompts
    (carries 🪄 edits) so the new chars use identical per-scene prompts.
  * assembly is SCOPED via state["add_scope_char_ids"]: only the new chars are
    (re)built; char 1's existing final is preserved; the scope is then cleared.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, prompt_director, runner, runner_reengineer
from character_swap.models import (
    CharStatus,
    CharacterAsset,
    GeneratedImage,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
    VariantStatus,
)


# ----------------------------------------------------------------- gap #1: lock

def _gen_job(origin):
    done_v = GeneratedImage(variant_id="v1", path="/v1.png", prompt="SCENE-1",
                            scene_id="s1", status=VariantStatus.READY)
    char1 = JobCharacter(char_id="c1", name="One", source_image_path="/c1.png",
                         status=CharStatus.DONE, images=[done_v],
                         approved_variant_ids=["v1"])
    new = JobCharacter(char_id="c2", name="Two", source_image_path="/c2.png",
                       status=CharStatus.QUEUED, images=[])
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
               scene_ids=["s1"], scene_image_paths=["/p.png"],
               characters={"c1": char1, "c2": new}, origin=origin,
               images_per_character=1,
               movement_prompt="animate", movement_prompts={"s1": "animate"})


def _stub_gen(monkeypatch, job):
    class _S:
        def get_job(self, jid):
            return job if jid == "j1" else None

        def update_job(self, j):
            pass
    monkeypatch.setattr(runner, "store", lambda: _S())

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_maybe_run_director_swap", _noop)
    monkeypatch.setattr(runner, "_swap_image_model", lambda j: "gpt2-id-swap")
    monkeypatch.setattr(runner, "_image_concurrency_for_model", lambda m: 2)
    kicked: list[str] = []

    async def fake_kick(job_, jc_, n, sem):
        kicked.append(jc_.char_id)
    monkeypatch.setattr(runner, "_kick_char", fake_kick)
    return kicked


def test_run_image_generation_relaxed_for_reengineer(monkeypatch):
    job = _gen_job(origin="reengineer:re_x")
    kicked = _stub_gen(monkeypatch, job)
    asyncio.run(runner.run_image_generation("j1", char_ids=["c2"]))
    assert kicked == ["c2"]            # new char generated; DONE char1 skipped


def test_run_image_generation_still_locked_for_plain_swap(monkeypatch):
    job = _gen_job(origin=None)
    kicked = _stub_gen(monkeypatch, job)
    asyncio.run(runner.run_image_generation("j1", char_ids=["c2"]))
    assert kicked == []               # movement lock holds for non-reengineer


# ----------------------------------------------------------- add_characters core

@pytest.fixture
def wired(monkeypatch, tmp_path):
    char1 = JobCharacter(
        char_id="c1", name="One", source_image_path="/c1.png",
        status=CharStatus.DONE,
        images=[GeneratedImage(variant_id="v1", path="/v1.png",
                               prompt="SCENE-PROMPT-1", scene_id="s1",
                               status=VariantStatus.READY)],
        approved_variant_ids=["v1"])
    job = Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
              scene_ids=["s1"], scene_image_paths=["/p.png"],
              characters={"c1": char1}, origin="reengineer:re_t",
              images_per_character=1, prompt=None,
              movement_prompt="animate", movement_prompts={"s1": "animate"})
    chars = {"c2": CharacterAsset(char_id="c2", filename="c2.png", name="Two"),
             "c3": CharacterAsset(char_id="c3", filename="c3.png", name="Three")}
    (tmp_path / "c2.png").write_bytes(b"x")
    (tmp_path / "c3.png").write_bytes(b"x")
    state = {"re_id": "re_t", "status": "done", "job_id": "j1",
             "character_ids": ["c1"], "character_source_image_ids": {},
             "scenes": [{"idx": 0, "scene_id": "s1", "duration": 5.0,
                         "motion_prompt": "p", "summary": "one"}],
             "finals": {"c1": {"status": "done", "final_path": "/old.mp4",
                               "edit_id": "ed_old", "n_clips": 1}}}
    box = {"job": job, "states": {"re_t": state}, "chars": chars}

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j1" else None

        def update_job(self, j):
            box["job"] = j

        def get_character(self, cid):
            return box["chars"].get(cid)
    monkeypatch.setattr(api, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    from character_swap import reengineer as reengineer_mod

    def load_state(rid):
        s = box["states"].get(rid)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)
    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state", load_state)
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state", save_state)
    monkeypatch.setattr(type(runner_reengineer.settings), "characters_dir",
                        property(lambda self: tmp_path), raising=False)
    return box


def test_add_characters_replicates_recipe(wired, monkeypatch):
    recorded = {}

    async def fake_gen(job_id, char_ids=None):
        recorded["char_ids"] = char_ids
    monkeypatch.setattr(runner_reengineer.runner, "run_image_generation", fake_gen)

    async def fake_watch(re_id, job_id, tasks=None):
        recorded["watched"] = True
    monkeypatch.setattr(runner_reengineer, "_watch_swap_phase", fake_watch)

    asyncio.run(runner_reengineer.add_characters(
        "re_t", ["c2"], {}, auto=False))

    job = wired["job"]
    # New char added as a QUEUED column; char 1 untouched (still DONE, 1 image).
    assert set(job.characters) == {"c1", "c2"}
    assert job.characters["c2"].status == CharStatus.QUEUED
    assert job.characters["c2"].images == []
    assert job.characters["c1"].status == CharStatus.DONE
    assert len(job.characters["c1"].images) == 1

    # Director plan covers BOTH chars with char 1's CURRENT scene prompt, fresh
    # prompt_version (so _parse_director_plan accepts it).
    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        job.director_prompts_json)
    assert plan.prompt_version == prompt_director.prompt_fingerprint()
    by_id = {c.char_id: c for c in plan.characters}
    assert set(by_id) == {"c1", "c2"}
    assert by_id["c2"].scenes[0].scene_id == "s1"
    assert by_id["c2"].scenes[0].variants[0].prompt == "SCENE-PROMPT-1"

    # Generation kicked for the new char only; state carries the scope.
    assert recorded["char_ids"] == ["c2"]
    st = wired["states"]["re_t"]
    assert st["status"] == "swapping"
    assert st["auto_mode"] is False
    assert st["character_ids"] == ["c1", "c2"]
    assert st["add_scope_char_ids"] == ["c2"]


def test_add_characters_with_source_override(wired, monkeypatch):
    wired["chars"]["c2"].images = []  # primary still resolves to filename "c2.png"

    async def fake_gen(job_id, char_ids=None):
        pass
    monkeypatch.setattr(runner_reengineer.runner, "run_image_generation", fake_gen)

    async def fake_watch(*a, **k):
        pass
    monkeypatch.setattr(runner_reengineer, "_watch_swap_phase", fake_watch)

    asyncio.run(runner_reengineer.add_characters(
        "re_t", ["c2"], {}, auto=True))
    st = wired["states"]["re_t"]
    assert st["auto_mode"] is True          # fully-automatic flows through


def test_add_characters_skips_already_present(wired, monkeypatch):
    called = {"gen": False}

    async def fake_gen(job_id, char_ids=None):
        called["gen"] = True
    monkeypatch.setattr(runner_reengineer.runner, "run_image_generation", fake_gen)
    # c1 already in the run → nothing to do, no generation kicked.
    asyncio.run(runner_reengineer.add_characters("re_t", ["c1"], {}, auto=False))
    assert called["gen"] is False


# ------------------------------------------------------------- assemble scoping

def _assemble_job():
    char1 = JobCharacter(char_id="c1", name="One", source_image_path="/c1.png",
                         status=CharStatus.DONE, images=[], approved_variant_ids=[])
    vid = VideoVariant(video_id="vid2", source_variant_id="va2", grok_job_id="g2",
                       status=VideoStatus.DONE, final_video_path="/c2.mp4")
    char2 = JobCharacter(
        char_id="c2", name="Two", source_image_path="/c2.png",
        status=CharStatus.APPROVED,
        images=[GeneratedImage(variant_id="va2", path="/va2.png", prompt="P",
                               scene_id="s1", status=VariantStatus.READY)],
        approved_variant_ids=["va2"], videos=[vid])
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
               scene_ids=["s1"], scene_image_paths=["/p.png"],
               characters={"c1": char1, "c2": char2}, origin="reengineer:re_t",
               movement_prompt="animate", movement_prompts={"s1": "a"})


@pytest.fixture
def assemble_wired(monkeypatch, tmp_path):
    job = _assemble_job()
    state = {"re_id": "re_t", "status": "assembling", "job_id": "j1",
             "scenes": [{"idx": 0, "scene_id": "s1", "duration": 5.0,
                         "motion_prompt": "p", "summary": "one"}],
             "finals": {"c1": {"status": "done", "final_path": "/old.mp4",
                               "edit_id": "ed_old", "n_clips": 1}},
             "add_scope_char_ids": ["c2"]}
    box = {"job": job, "states": {"re_t": state}, "built": []}

    class _S:
        def get_job(self, jid):
            return box["job"]

        def update_job(self, j):
            pass

        def get_character(self, cid):
            return None
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda rid: dict(box["states"].get(rid) or {}))
    monkeypatch.setattr(reengineer_mod, "save_state",
                        lambda s: box["states"].__setitem__(s["re_id"], dict(s)))
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: dict(box["states"].get(rid) or {}))
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state",
                        lambda s: box["states"].__setitem__(s["re_id"], dict(s)))
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: tmp_path)
    monkeypatch.setattr(runner_reengineer.runner_compile,
                        "_resolve_compile_voice", lambda *a, **k: None)
    monkeypatch.setattr(runner_reengineer, "_collect_clips",
                        lambda state, jc: ([tmp_path / "clip.mp4"], [], False))
    (tmp_path / "clip.mp4").write_bytes(b"v")
    monkeypatch.setattr(runner_reengineer.shutil, "copyfile",
                        lambda *a, **k: None)

    class _Res:
        final = tmp_path / "out.mp4"

    async def fake_pipeline(clips, *, edit_id, edit_dir, **k):
        box["built"].append(edit_id)
        # record which char by source clip is not available; track via call count
        return _Res()
    monkeypatch.setattr(runner_reengineer.runner_compile, "run_editor_pipeline",
                        fake_pipeline)
    return box


def test_do_assemble_scoped_preserves_char1(assemble_wired):
    asyncio.run(runner_reengineer._do_assemble(
        "re_t", dict(assemble_wired["states"]["re_t"])))
    st = assemble_wired["states"]["re_t"]
    # Only ONE character (c2) was built; char 1 NOT rebuilt.
    assert len(assemble_wired["built"]) == 1
    # char 1's final preserved verbatim (same edit_id), c2 freshly built.
    assert st["finals"]["c1"]["edit_id"] == "ed_old"
    assert st["finals"]["c2"]["status"] == "done"
    assert st["finals"]["c2"]["edit_id"] != "ed_old"
    assert st["status"] == "done"               # union: both done
    # Scope cleared so a later normal rebuild rebuilds everyone.
    assert st.get("add_scope_char_ids") is None


def test_do_assemble_unscoped_rebuilds_all(assemble_wired):
    state = dict(assemble_wired["states"]["re_t"])
    state.pop("add_scope_char_ids", None)
    asyncio.run(runner_reengineer._do_assemble("re_t", state))
    # No scope → both chars (re)built, scope absent/None.
    assert len(assemble_wired["built"]) == 2
    assert assemble_wired["states"]["re_t"].get("add_scope_char_ids") is None


# ------------------------------------------------------------------- endpoint

@pytest.fixture
def ep_box(monkeypatch):
    char1 = JobCharacter(char_id="c1", name="One", source_image_path="/c1.png",
                         status=CharStatus.DONE,
                         images=[GeneratedImage(variant_id="v1", path="/v1.png",
                                                prompt="P", scene_id="s1",
                                                status=VariantStatus.READY)],
                         approved_variant_ids=["v1"])
    job = Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
              scene_ids=["s1"], scene_image_paths=["/p.png"],
              characters={"c1": char1}, origin="reengineer:re_t",
              movement_prompt="animate", movement_prompts={"s1": "a"})
    chars = {"c1": CharacterAsset(char_id="c1", filename="c1.png", name="One"),
             "c2": CharacterAsset(char_id="c2", filename="c2.png", name="Two")}
    box = {"job": job, "state": {"re_id": "re_t", "status": "done",
                                 "job_id": "j1"}, "chars": chars}

    class _S:
        def get_job(self, jid):
            return box["job"]

        def get_character(self, cid):
            return box["chars"].get(cid)
    monkeypatch.setattr(api, "store", lambda: _S())
    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda rid: dict(box["state"]) if box["state"] else None)
    runner_reengineer._ANIMATING.discard("re_t")
    runner_reengineer._ASSEMBLING.discard("re_t")
    return box


def test_endpoint_ok_schedules_task(ep_box):
    bg = BackgroundTasks()
    body = api.ReAddCharactersBody(character_ids=["c2"], auto=True)
    out = asyncio.run(api.reengineer_add_characters("re_t", bg, body))
    assert out["character_ids"] == ["c2"]
    assert len(bg.tasks) == 1


def test_endpoint_409_when_run_unfinished(ep_box):
    ep_box["state"]["status"] = "animating"
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_add_characters(
            "re_t", BackgroundTasks(),
            api.ReAddCharactersBody(character_ids=["c2"])))
    assert e.value.status_code == 409


def test_endpoint_409_duplicate_char(ep_box):
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_add_characters(
            "re_t", BackgroundTasks(),
            api.ReAddCharactersBody(character_ids=["c1"])))
    assert e.value.status_code == 409


def test_endpoint_409_no_reference_char(ep_box):
    # Strip char 1's approval → no completed recipe to clone.
    ep_box["job"].characters["c1"].approved_variant_ids = []
    ep_box["job"].characters["c1"].approved_variant_id = None
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_add_characters(
            "re_t", BackgroundTasks(),
            api.ReAddCharactersBody(character_ids=["c2"])))
    assert e.value.status_code == 409


def test_endpoint_404_unknown_char(ep_box):
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_add_characters(
            "re_t", BackgroundTasks(),
            api.ReAddCharactersBody(character_ids=["nope"])))
    assert e.value.status_code == 404
