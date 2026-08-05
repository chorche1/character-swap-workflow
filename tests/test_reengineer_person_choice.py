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
    # ch_a = male, ch_f = FEMALE. The person directive is written per
    # character precisely because those two must not read the same.
    plan = prompt_director.plan_from_scene_prompts(
        "intent", {"s2": "BASE PROMPT s2"}, [("ch_a", "A"), ("ch_f", "F")])
    job = Job(job_id="j_t", scene_id="s2", scene_image_path="/p",
              scene_ids=["s2"], scene_image_paths=["/p"], use_director=True,
              director_prompts_json=plan.model_dump_json(),
              characters={"ch_a": JobCharacter(char_id="ch_a", name="A",
                          source_image_path="/c.png", status=CharStatus.QUEUED),
                          "ch_f": JobCharacter(char_id="ch_f", name="F",
                          source_image_path="/f.png", status=CharStatus.QUEUED)},
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

    genders = {"ch_a": "male", "ch_f": "female"}

    class _CS:
        def get_character(self, cid):
            return CharacterAsset(char_id=cid, name=cid, filename=f"{cid}.png",
                                  gender=genders.get(cid))
    monkeypatch.setattr(runner, "store", lambda: _CS())

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
    # The non-chosen person is named and locked, never removed.
    assert "The woman red on the left is NOT the new character" in p
    assert "Remove the other people" not in p
    # Scene flag cleared + choice recorded; swap kicked.
    sc = box["states"]["re_t"]["scenes"][0]
    assert "multi_person" not in sc
    assert sc["swap_person_idx"] == 1
    assert "other_action" not in sc
    assert len(bg.tasks) == 1


def test_resolve_people_keeps_people_descriptions(gate):
    """Only the GATE flag is cleared. `people` stays on the entry: the
    descriptions are the sole record of who was in frame, and rebuilding the
    directive for a retake needs them."""
    box = gate
    asyncio.run(api.reengineer_resolve_people("re_t", BackgroundTasks(), _body()))
    sc = box["states"]["re_t"]["scenes"][0]
    assert "multi_person" not in sc
    assert [p["description"] for p in sc["people"]] == ["woman red", "man blue"]


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


# ------------------------------------------------------- gender (Hugo 2026-08-06)
#
# re_a5613a883e: on a man+woman scene the user chose "the older man grey beard
# on the left". All 6 MALE characters swapped onto the man correctly; both
# FEMALE characters were painted onto the BLONDE WOMAN instead, leaving the
# chosen man untouched — 4 of 4 images, both scenes. GPT Image 2 resolves the
# new character by GENDER first and position second, so a position-only
# directive loses whenever character and target are different genders.

_MAN = {"position": "left", "description": "older man grey beard"}
_WOMAN = {"position": "right", "description": "blonde woman denim jacket"}


def test_described_gender_matches_whole_words_only():
    # "woman" contains "man" — the substring must not win.
    assert api._described_gender("blonde woman denim jacket") == "female"
    assert api._described_gender("older man grey beard") == "male"
    assert api._described_gender("person in a red top") is None


def test_directive_states_the_characters_gender():
    d = api._person_directive(_MAN, others=[_WOMAN], gender="female")
    assert "The new character is a woman" in d
    assert "the older man grey beard on the left becomes a woman" in d


def test_directive_forbids_the_gender_substitution_that_broke_the_run():
    d = api._person_directive(_MAN, others=[_WOMAN], gender="female")
    assert ("NEVER put the new character on a different person in the frame "
            "because that person's gender matches the new character better") in d
    # …and the woman she was wrongly painted onto is named + locked.
    assert ("The blonde woman denim jacket on the right is NOT the new character "
            "and must stay exactly as in the original photo") in d


def test_directive_replaces_the_whole_person_including_the_hands():
    """Hugo, on the retakes that DID land on the right person: "mannen är ju i
    bild" — the original man's big weathered farm hands were still holding the
    cotton pad and the shot glass, on a woman whose own arms were gone. The
    base prompt's "keep hand positions EXACTLY" framing anchor is what the
    model satisfies by keeping his actual hands, so the split between POSITION
    (keep) and WHOSE HANDS (replace) has to be said out loud."""
    d = api._person_directive(_MAN, others=[_WOMAN], gender="female")
    assert "Replace the ENTIRE person, not only the face" in d
    assert "arms and HANDS all become the new character's" in d
    assert ("The hands holding the props are the new character's own hands — "
            "same position, same objects") in d
    assert ("No part of the older man grey beard on the left may remain visible "
            "anywhere in the frame.") in d
    # …and the co-star's hands are locked the other way.
    assert "same face, same hair, same hands, same clothes" in d


def test_whole_person_clause_does_not_need_a_known_gender():
    # It is the fix for a wrong-hands take, not a gender question.
    d = api._person_directive(_MAN, gender=None)
    assert "Replace the ENTIRE person, not only the face" in d
    assert "The new character is a" not in d


def test_directive_allows_exactly_one_changed_face():
    """Helene's first retake landed on the right person but ALSO replaced the
    co-star — both women in frame came back as Helene. Her reference and the
    scene's co-star are both blonde women of a similar age, so naming the
    co-star was not enough; the count is."""
    d = api._person_directive(_MAN, others=[_WOMAN], gender="female")
    assert ("Exactly ONE face in the image changes; every other face stays "
            "identical to the original photo.") in d


def test_directive_calls_out_a_real_gender_mismatch():
    d = api._person_directive(_MAN, others=[_WOMAN], gender="female")
    assert ("The older man grey beard on the left is not the same gender as the "
            "new character — replace them anyway.") in d


def test_directive_claims_no_mismatch_when_genders_agree():
    d = api._person_directive(_MAN, others=[_WOMAN], gender="male")
    assert "The new character is a man" in d
    assert "not the same gender" not in d       # would be a lie


def test_directive_forbids_mirroring_the_frame():
    # Susanne's take also flipped the composition (man moved left→right).
    d = api._person_directive(_MAN, others=[_WOMAN], gender="female")
    assert "do not mirror, flip or rearrange the people" in d


def test_directive_without_a_known_gender_stays_position_only():
    d = api._person_directive(_MAN, gender=None)
    assert "The new character is a" not in d
    assert "Replace SPECIFICALLY the older man grey beard on the left" in d
    assert "Keep the other people in the scene exactly as they are." in d


def test_resolve_people_writes_a_DIFFERENT_directive_per_character(gate):
    """The regression test for the run itself: one shared scene prompt cannot
    express 'she replaces the man', so the directive is written per character."""
    box = gate
    box["states"]["re_t"]["scenes"][0]["people"] = [_MAN, _WOMAN]
    asyncio.run(api.reengineer_resolve_people(
        "re_t", BackgroundTasks(), _body(swap_person_idx=0)))
    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        box["job"].director_prompts_json)
    male = plan.lookup("ch_a", "s2")[0]
    female = plan.lookup("ch_f", "s2")[0]
    assert male != female
    # Both target the SAME person — the man the user picked.
    for p in (male, female):
        assert p.startswith("BASE PROMPT s2")
        assert "Replace SPECIFICALLY the older man grey beard on the left" in p
    assert "The new character is a woman" in female
    assert "The new character is a man" in male
    assert "not the same gender" in female and "not the same gender" not in male


def test_replace_scene_prompt_in_plan_can_scope_to_one_character():
    plan = prompt_director.plan_from_scene_prompts(
        "i", {"s1": "BASE"}, [("ch_a", "A"), ("ch_f", "F")])
    assert prompt_director.replace_scene_prompt_in_plan(
        plan, "s1", "ONLY-F", char_id="ch_f") is True
    assert plan.lookup("ch_f", "s1") == ["ONLY-F"]
    assert plan.lookup("ch_a", "s1") == ["BASE"]
    # Unknown character changes nothing.
    assert prompt_director.replace_scene_prompt_in_plan(
        plan, "s1", "X", char_id="ch_nope") is False


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


def test_person_choice_gate_is_always_hydrated():
    """Audit 2026-07-01: GET /api/reengineer returns LIGHT rows (no scenes/
    job); loadReengineerHistory hydrates full details for the 8 newest runs
    PLUS every run parked at an interactive gate. `awaiting_person_choice`
    was missing from that gate allowlist (the exact bug class fixed
    2026-06-19 for awaiting_approval/awaiting_assembly), so a person-choice
    run older than the 8 newest rendered the violet status with ZERO radio
    choices and '▶ Fortsätt' sent {scenes: []} → 400 forever."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "web" / "app.js").read_text(encoding="utf-8")
    assert "const gate = x =>" in app_js
    gate_src = app_js.split("const gate = x =>", 1)[1][:250]
    assert "'awaiting_person_choice'" in gate_src
    assert "'awaiting_approval'" in gate_src
    assert "'awaiting_assembly'" in gate_src
