"""Multi-person swap gate (Hugo 2026-06-14).

When the AI Director is on, it also reports per scene whether multiple people
are visible. Such scenes PAUSE the run at `awaiting_person_choice`; the user
picks which person to swap + what to do with the other(s), and the choice is
baked into the Director plan before image generation runs.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, prompt_director, runner, runner_reengineer
from character_swap.models import CharacterAsset, Job, JobCharacter, CharStatus


# --------------------------------------------------------------------------- director parse

def _stub_director(monkeypatch, payload):
    monkeypatch.setattr(prompt_director.anthropic_client, "messages_with_tools",
                        lambda **kw: "RESP")
    monkeypatch.setattr(prompt_director.anthropic_client, "extract_tool_call",
                        lambda resp, name: payload)
    monkeypatch.setattr(prompt_director.anthropic_client, "_file_to_image_block",
                        lambda p, **k: {"type": "text", "text": str(p)})


def test_director_reports_multi_person(monkeypatch, tmp_path):
    _stub_director(monkeypatch, {"intent": "x", "scenes": [
        {"scene_id": "s1", "prompt": "p1", "multi_person": False},
        {"scene_id": "s2", "prompt": "p2", "multi_person": True,
         "people": [{"position": "left", "description": "woman red top"},
                    {"position": "right", "description": "man blue shirt"}]},
    ]})
    out = prompt_director.direct_reengineer_swap(
        scenes=[("s1", tmp_path / "a.png"), ("s2", tmp_path / "b.png")])
    assert out is not None
    _intent, prompts, meta = out
    assert "s1" not in meta                      # single subject → no flag
    assert meta["s2"]["multi_person"] is True
    assert len(meta["s2"]["people"]) == 2


def test_director_ignores_lone_person_flag(monkeypatch, tmp_path):
    # multi_person true but <2 people described → not a real ambiguity.
    _stub_director(monkeypatch, {"intent": "x", "scenes": [
        {"scene_id": "s1", "prompt": "p1", "multi_person": True,
         "people": [{"position": "center", "description": "one person"}]},
    ]})
    _intent, _prompts, meta = prompt_director.direct_reengineer_swap(
        scenes=[("s1", tmp_path / "a.png")])
    assert meta == {}


# --------------------------------------------------------------------------- the gate

@pytest.fixture
def wire(monkeypatch, tmp_path):
    (tmp_path / "chars").mkdir()
    (tmp_path / "chars" / "ch_a.png").write_bytes(b"c")
    ch = CharacterAsset(char_id="ch_a", name="A", filename="ch_a.png")
    box = {"job": None, "calls": []}

    class _S:
        def get_character(self, cid):
            return ch if cid == "ch_a" else None

        def add_job(self, job):
            box["job"] = job

        def get_job(self, jid):
            return box["job"]

        def update_job(self, job):
            box["job"] = job
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "characters_dir",
                        property(lambda self: tmp_path / "chars"), raising=False)
    monkeypatch.setattr(type(settings), "scenes_dir",
                        property(lambda self: tmp_path / "scenes"), raising=False)
    monkeypatch.setattr(type(settings), "has_provider", lambda self, p: True)

    states = {}
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: json.loads(json.dumps(states.get(rid))) if states.get(rid) else None)
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state",
                        lambda s: states.__setitem__(s["re_id"], json.loads(json.dumps(s))))

    async def fake_run(job_id, char_ids=None):
        box["calls"].append("run_image_generation")
    monkeypatch.setattr(runner_reengineer.runner, "run_image_generation", fake_run)

    async def fake_watch(re_id, job_id, tasks=None):
        return None
    monkeypatch.setattr(runner_reengineer, "_watch_swap_phase", fake_watch)
    return box, states


def _entries():
    return [{"idx": 0, "scene_id": "s1", "motion_prompt": "m", "duration": 2.0},
            {"idx": 1, "scene_id": "s2", "motion_prompt": "m", "duration": 2.0}]


def _state():
    return {"re_id": "re_t", "status": "analyzing", "use_director": True,
            "image_model": "gpt2-id-swap", "character_ids": ["ch_a"],
            "outfit_mode": "scene", "video_model": "kling-v3"}


def test_create_job_pauses_on_multi_person(wire, monkeypatch):
    box, states = wire
    monkeypatch.setattr(prompt_director, "direct_reengineer_swap", lambda **kw: (
        "intent", {"s1": "P1", "s2": "P2"},
        {"s2": {"multi_person": True,
                "people": [{"position": "left", "description": "woman red"},
                           {"position": "right", "description": "man blue"}]}}))
    states["re_t"] = _state()
    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", _state(), _entries(), "j_t"))
    assert states["re_t"]["status"] == "awaiting_person_choice"
    assert "run_image_generation" not in box["calls"]      # paused before swap
    s2 = states["re_t"]["scenes"][1]
    assert s2["multi_person"] is True and len(s2["people"]) == 2
    assert box["job"] is not None                          # job persisted


def test_create_job_no_ambiguity_proceeds(wire, monkeypatch):
    box, states = wire
    monkeypatch.setattr(prompt_director, "direct_reengineer_swap",
                        lambda **kw: ("intent", {"s1": "P1", "s2": "P2"}, {}))
    states["re_t"] = _state()
    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", _state(), _entries(), "j_t"))
    assert states["re_t"]["status"] == "swapping"
    assert "run_image_generation" in box["calls"]


# --------------------------------------------------------------------------- resolve endpoint

@pytest.fixture
def gate(monkeypatch, tmp_path):
    box = {"job": None, "states": {}, "kicked": []}
    plan = prompt_director.plan_from_scene_prompts(
        "intent", {"s2": "BASE PROMPT s2"}, [("ch_a", "A")])
    job = Job(job_id="j_t", scene_id="s2", scene_image_path="/p",
              scene_ids=["s2"], scene_image_paths=["/p"], use_director=True,
              director_prompts_json=plan.model_dump_json(),
              characters={"ch_a": JobCharacter(char_id="ch_a", name="A",
                          source_image_path="/c.png", status=CharStatus.QUEUED)},
              origin="reengineer:re_t")
    box["job"] = job

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j_t" else None

        def update_job(self, j):
            box["job"] = j

        def get_scene(self, sid):
            return None
    monkeypatch.setattr(api, "store", lambda: _S())

    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "awaiting_person_choice", "job_id": "j_t",
        "scenes": [{"idx": 0, "scene_id": "s2", "multi_person": True,
                    "people": [{"position": "left", "description": "woman red"},
                               {"position": "right", "description": "man blue"}]}]}

    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda rid: json.loads(json.dumps(box["states"].get(rid))) if box["states"].get(rid) else None)
    monkeypatch.setattr(reengineer_mod, "save_state",
                        lambda s: box["states"].__setitem__(s["re_id"], json.loads(json.dumps(s))))
    monkeypatch.setattr(reengineer_mod, "reengineer_dir", lambda rid: tmp_path / rid)
    monkeypatch.setattr(type(api.settings), "scenes_dir",
                        property(lambda self: tmp_path / "scenes"), raising=False)
    (tmp_path / "scenes").mkdir(parents=True, exist_ok=True)
    return box


def _body(idx=0, swap_person_idx=1):
    return api.ResolvePeopleBody(scenes=[api.ResolvePeopleSceneBody(
        idx=idx, swap_person_idx=swap_person_idx)])


def test_resolve_people_bakes_choice_and_kicks(gate):
    box = gate
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_resolve_people("re_t", bg, _body(swap_person_idx=1)))
    # Director plan prompt rewritten with the chosen-person directive. The other
    # people are always kept as they are (the "remove" option was dropped).
    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        box["job"].director_prompts_json)
    p = plan.lookup("ch_a", "s2")[0]
    assert p.startswith("BASE PROMPT s2")
    assert "Replace SPECIFICALLY the man blue on the right" in p
    assert "keep the other people in the scene exactly as they are" in p
    assert "Remove the other people" not in p
    # Scene flag cleared + choice recorded; swap kicked.
    sc = box["states"]["re_t"]["scenes"][0]
    assert "multi_person" not in sc
    assert sc["swap_person_idx"] == 1
    assert "other_action" not in sc
    assert len(bg.tasks) == 1


def test_resolve_people_first_person_directive(gate):
    box = gate
    asyncio.run(api.reengineer_resolve_people(
        "re_t", BackgroundTasks(), _body(swap_person_idx=0)))
    p = prompt_director.SwapDirectorPlan.model_validate_json(
        box["job"].director_prompts_json).lookup("ch_a", "s2")[0]
    assert "woman red on the left" in p


def test_resolve_people_requires_all_answered(gate):
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_resolve_people(
            "re_t", BackgroundTasks(), api.ResolvePeopleBody(scenes=[])))
    assert e.value.status_code == 400          # the ambiguous scene wasn't answered


def test_resolve_people_wrong_status_409(gate):
    box = gate
    box["states"]["re_t"]["status"] = "swapping"
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_resolve_people("re_t", BackgroundTasks(), _body()))
    assert e.value.status_code == 409


# --------------------------------------------------------------------------- resume + frontend

def test_resume_all_skips_person_choice_gate(monkeypatch):
    spawned = []
    monkeypatch.setattr(runner_reengineer, "_spawn",
                        lambda coro, name: (spawned.append(name), coro.close()))
    monkeypatch.setattr(runner_reengineer.reengineer, "list_states",
                        lambda: [{"re_id": "re_z", "status": "awaiting_person_choice"}])
    asyncio.run(runner_reengineer.resume_all())
    assert spawned == []                        # user gate — nothing re-attached


def test_frontend_wiring_present():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "web" / "app.js").read_text(encoding="utf-8")
    index = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert "submitReengineerPersonChoices" in app_js
    assert "awaiting_person_choice" in index
    assert "sc.people" in index
