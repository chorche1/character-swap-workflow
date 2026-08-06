"""Two-person scenes must not be swapped by guesswork (Hugo 2026-08-06).

re_a5613a883e scene 2: an image of a man and a woman was swapped for nine
characters and came back nine different ways — the woman deleted for most of
them, kept for three, and painted over instead of the man for two. All nine
PASSED image QC.

Three independent holes produced that, and this file locks all three shut:

1. **The person-choice gate had no coverage on the scene.** Multi-person
   detection lived inside the AI Director's per-scene call, which runs ONCE at
   run creation. The scene was added afterwards with "+ egen", so nothing ever
   asked which person to replace. Detection now lives in `scene_people` and
   runs on added scenes and on Director-OFF runs too.
2. **The prompt never forbade deleting anyone.** Every stock swap prompt says
   "Replace THE PERSON" (singular) and bans ADDING people while saying nothing
   about removing them. `pipeline.CAST_LOCK` is now appended to every
   dispatched swap prompt, stock or custom.
3. **QC could not see it.** MISSING/EXTRA PEOPLE only fires on "no person at
   all" or an invented extra, so 2 people → 1 person passed. QC now gets the
   measured head count and the chosen target — for multi-person scenes ONLY, so
   the catastrophe-only policy is untouched everywhere else.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import (
    api, pipeline, prompt_director, runner, runner_reengineer, scene_people,
    swap_qc,
)
from character_swap.models import (
    CharStatus, CharacterAsset, GeneratedImage, Job, JobCharacter,
    VariantStatus,
)
from character_swap.swap_qc import QCVerdict


# --------------------------------------------------------------- detection

def _stub_detect(monkeypatch, payload, *, raises: Exception | None = None):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "anthropic_api_key",
                        property(lambda self: "k"), raising=False)
    from character_swap.clients import anthropic_client

    def fake(**kw):
        if raises is not None:
            raise raises
        return "RESP"
    monkeypatch.setattr(anthropic_client, "messages_with_tools", fake)
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: payload)
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p, **k: {"type": "text", "text": str(p)})


def test_detect_people_reports_two(monkeypatch, tmp_path):
    _stub_detect(monkeypatch, {"multi_person": True, "people": [
        {"position": "left", "description": "older man grey beard"},
        {"position": "right", "description": "blonde woman"}]})
    meta = scene_people.detect_people(tmp_path / "s.png")
    assert meta == {"multi_person": True, "people": [
        {"position": "left", "description": "older man grey beard"},
        {"position": "right", "description": "blonde woman"}]}
    assert scene_people.people_count(meta) == 2


def test_lone_person_is_not_an_ambiguity(monkeypatch, tmp_path):
    """A "multi_person" verdict with one person described is not a choice the
    user can make — stopping the run to ask would be a dead end."""
    _stub_detect(monkeypatch, {"multi_person": True, "people": [
        {"position": "center", "description": "a woman"}]})
    assert scene_people.detect_people(tmp_path / "s.png") == {
        "multi_person": False, "people": []}


def test_detection_failure_is_advisory(monkeypatch, tmp_path):
    """The judge being down must never block a run — None, and the caller
    generates anyway behind the cast lock + QC."""
    _stub_detect(monkeypatch, None, raises=RuntimeError("boom"))
    assert scene_people.detect_people(tmp_path / "s.png") is None
    assert scene_people.people_count(None) == 1


def test_detection_without_key_is_skipped(monkeypatch, tmp_path):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "anthropic_api_key",
                        property(lambda self: ""), raising=False)
    assert scene_people.detect_people(tmp_path / "s.png") is None


# --------------------------------------------------------------- cast lock

def test_cast_lock_forbids_deleting_the_other_person():
    low = pipeline.CAST_LOCK.lower()
    assert "only one of them" in low
    assert "never delete" in low
    assert "same number of people" in low


def test_cast_lock_is_idempotent():
    once = pipeline.with_cast_lock("P")
    assert pipeline.with_cast_lock(once) == once
    assert once.count(pipeline.CAST_LOCK) == 1


def test_stock_prompt_reaches_the_engine_with_the_cast_lock(monkeypatch,
                                                            tmp_path):
    """The lock rides on the ENGINE-effective prompt, so gpt2-id-swap still
    gets its compact identity-first rebuild rather than the long template."""
    seen: dict = {}
    monkeypatch.setattr(pipeline.openai_image, "generate",
                        lambda **kw: seen.update(kw) or b"png")
    scene = tmp_path / "s.png"; scene.write_bytes(b"s")
    char = tmp_path / "c.png"; char.write_bytes(b"c")
    pipeline._dispatch_variant(
        model="gpt2-id-swap", scene_image=scene, character_image=char,
        character_name="X", prompt=pipeline.GENERATION_PROMPT,
        dest=tmp_path / "o.png", job_id=None)
    assert seen["prompt"].startswith("Image 1 is only the identity reference")
    assert seen["prompt"].endswith(pipeline.CAST_LOCK)


def test_stock_prompts_unchanged_so_old_jobs_stay_stock():
    """The lock must NOT be baked into the builders: `stock_swap_prompts()` has
    to keep matching the exact strings already stored on existing jobs, or a
    retry would treat them as custom text and skip the engine rebuild."""
    for p in pipeline.stock_swap_prompts("scene"):
        assert pipeline.CAST_LOCK not in p


# ------------------------------------------------------- added-scene gate

@pytest.fixture
def added(monkeypatch, tmp_path):
    """A run sitting at `awaiting_assembly` (finished finals) that just had a
    scene added — the exact shape of the re_a5613a883e incident."""
    scenes_dir = tmp_path / "scenes"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    (scenes_dir / "s_new.png").write_bytes(b"img")

    job = Job(job_id="j_t", scene_id="s1", scene_image_path="/p",
              scene_ids=["s1", "s_new"], scene_image_paths=["/p", "/n"],
              characters={
                  "ch_a": JobCharacter(char_id="ch_a", name="A",
                                       source_image_path="/a.png",
                                       status=CharStatus.APPROVED),
                  "ch_f": JobCharacter(char_id="ch_f", name="F",
                                       source_image_path="/f.png",
                                       status=CharStatus.APPROVED)},
              origin="reengineer:re_t")
    box = {"job": job, "states": {}, "regen": [], "events": []}

    class _Scene:
        filename = "s_new.png"

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j_t" else None

        def update_job(self, j):
            box["job"] = j

        def get_scene(self, sid):
            return _Scene() if sid == "s_new" else None
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(api, "store", lambda: _S())

    genders = {"ch_a": "male", "ch_f": "female"}

    class _CS:
        def get_character(self, cid):
            return CharacterAsset(char_id=cid, name=cid, filename=f"{cid}.png",
                                  gender=genders.get(cid))
    monkeypatch.setattr(runner, "store", lambda: _CS())

    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "awaiting_assembly", "job_id": "j_t",
        "scenes": [{"idx": 0, "scene_id": "s1"},
                   {"idx": 1, "scene_id": "s_new", "source": "image"}]}

    from character_swap import reengineer as reengineer_mod
    for mod in (runner_reengineer.reengineer, reengineer_mod):
        monkeypatch.setattr(mod, "load_state", lambda rid: (
            json.loads(json.dumps(box["states"].get(rid)))
            if box["states"].get(rid) else None))
        monkeypatch.setattr(mod, "save_state", lambda s: box["states"].__setitem__(
            s["re_id"], json.loads(json.dumps(s))))
    monkeypatch.setattr(api, "_save_reengineer_state",
                        lambda s: box["states"].__setitem__(
                            s["re_id"], json.loads(json.dumps(s))))

    async def fake_regen(job_id, cid, sid, *a, **kw):
        box["regen"].append((cid, sid))
    monkeypatch.setattr(runner_reengineer.runner, "regen_scene_variants",
                        fake_regen)

    async def fake_publish(jid, ev):
        box["events"].append(ev)
    monkeypatch.setattr(runner_reengineer.events, "publish", fake_publish)

    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "scenes_dir",
                        property(lambda self: scenes_dir), raising=False)
    return box


def _two_people(monkeypatch, target=runner_reengineer):
    async def fake(scenes, *, job_id=None):
        return {sid: {"multi_person": True, "people": [
            {"position": "left", "description": "older man grey beard"},
            {"position": "right", "description": "blonde woman"}]}
            for sid, _ in scenes}
    monkeypatch.setattr(target, "detect_people_for_scenes", fake)


def test_added_two_person_scene_holds_before_spending_credits(added,
                                                              monkeypatch):
    box = added
    _two_people(monkeypatch)
    asyncio.run(runner_reengineer.generate_added_scene("re_t", "s_new"))
    assert box["regen"] == []                       # nothing generated yet
    sc = box["states"]["re_t"]["scenes"][1]
    assert sc["awaiting_person"] is True
    assert sc["multi_person"] is True
    assert [p["description"] for p in sc["people"]] == [
        "older man grey beard", "blonde woman"]
    # The head count is recorded for QC even before the choice is made.
    assert box["job"].scene_people_counts["s_new"] == 2
    assert any(e.get("kind") == "scene.awaiting_person" for e in box["events"])
    # The REST of the run is untouched — no global gate, finals stay put.
    assert box["states"]["re_t"]["status"] == "awaiting_assembly"


def test_added_single_person_scene_generates_as_before(added, monkeypatch):
    async def none_found(scenes, *, job_id=None):
        return {}
    monkeypatch.setattr(runner_reengineer, "detect_people_for_scenes",
                        none_found)
    asyncio.run(runner_reengineer.generate_added_scene("re_t", "s_new"))
    assert sorted(added["regen"]) == [("ch_a", "s_new"), ("ch_f", "s_new")]
    assert "awaiting_person" not in added["states"]["re_t"]["scenes"][1]


def test_detection_outage_never_blocks_an_added_scene(added, monkeypatch):
    """No Anthropic key / a failed judge must not strand the scene: it
    generates, protected by the cast lock instead."""
    async def down(scenes, *, job_id=None):
        return {}
    monkeypatch.setattr(runner_reengineer, "detect_people_for_scenes", down)
    asyncio.run(runner_reengineer.generate_added_scene("re_t", "s_new"))
    assert len(added["regen"]) == 2


# -------------------------------------------------- per-scene resolve endpoint

def test_scene_resolve_bakes_per_character_directive(added, monkeypatch):
    box = added
    _two_people(monkeypatch)
    asyncio.run(runner_reengineer.generate_added_scene("re_t", "s_new"))

    bg = BackgroundTasks()
    asyncio.run(api.reengineer_resolve_scene_person(
        "re_t", 1, bg, api.ResolveScenePersonBody(swap_person_idx=0)))

    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        box["job"].director_prompts_json)
    male = plan.lookup("ch_a", "s_new")[0]
    female = plan.lookup("ch_f", "s_new")[0]
    for p in (male, female):
        assert "Replace SPECIFICALLY the older man grey beard on the left" in p
        assert "The blonde woman on the right is NOT the new character" in p
    # The gender clause is the whole reason the directive is per character:
    # without it a female character lands on the woman standing next to him.
    assert "The new character is a woman" in female
    assert "is not the same gender as the new character" in female
    assert "The new character is a man" in male

    # QC now knows both halves of the contract for this scene.
    assert box["job"].scene_people_counts["s_new"] == 2
    assert (box["job"].scene_swap_targets["s_new"]
            == "the older man grey beard on the left")

    sc = box["states"]["re_t"]["scenes"][1]
    assert sc["swap_person_idx"] == 0
    assert "awaiting_person" not in sc and "multi_person" not in sc
    assert sc["people"], "descriptions are kept — a retake re-derives from them"
    assert len(bg.tasks) == 1              # generation released


def test_baking_the_same_scene_twice_does_not_stack_directives(added,
                                                               monkeypatch):
    """Answering the gate twice for one scene must be idempotent.

    `base` is read back OUT of the plan, where the first bake already left the
    FIRST character's directive — gender clause and all. Without stripping it,
    every character ends up carrying both "The new character is a man …" and
    "The new character is a woman …" for the same target; GPT Image 2 resolves
    the target by gender first, so the female character lands on the woman
    again — the original incident, on the scene the gate exists to protect.

    Re-entry is reachable without any code change: "📌 ingen swap" → "↩ ta bort
    direkt" re-arms this gate, and two scene entries can share one scene_id."""
    box = added
    _two_people(monkeypatch)

    def answer():
        asyncio.run(runner_reengineer.generate_added_scene("re_t", "s_new"))
        asyncio.run(api.reengineer_resolve_scene_person(
            "re_t", 1, BackgroundTasks(),
            api.ResolveScenePersonBody(swap_person_idx=0)))

    answer()
    answer()

    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        box["job"].director_prompts_json)
    for cid, own, other in (("ch_f", "woman", "man"), ("ch_a", "man", "woman")):
        p = plan.lookup(cid, "s_new")[0]
        assert p.count("Replace SPECIFICALLY") == 1, p
        assert p.count(f"The new character is a {own}") == 1
        assert f"The new character is a {other}" not in p


def test_scene_resolve_refuses_a_scene_that_is_not_waiting(added):
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_resolve_scene_person(
            "re_t", 0, BackgroundTasks(),
            api.ResolveScenePersonBody(swap_person_idx=0)))
    assert e.value.status_code == 409


def test_scene_resolve_clamps_an_out_of_range_choice(added, monkeypatch):
    box = added
    _two_people(monkeypatch)
    asyncio.run(runner_reengineer.generate_added_scene("re_t", "s_new"))
    asyncio.run(api.reengineer_resolve_scene_person(
        "re_t", 1, BackgroundTasks(),
        api.ResolveScenePersonBody(swap_person_idx=99)))
    assert box["states"]["re_t"]["scenes"][1]["swap_person_idx"] == 1


def test_released_scene_generates_for_every_character(added, monkeypatch):
    box = added
    _two_people(monkeypatch)
    asyncio.run(runner_reengineer.generate_added_scene("re_t", "s_new"))
    asyncio.run(api.reengineer_resolve_scene_person(
        "re_t", 1, BackgroundTasks(),
        api.ResolveScenePersonBody(swap_person_idx=0)))
    asyncio.run(runner_reengineer.swap_added_scene("re_t", "s_new"))
    assert sorted(box["regen"]) == [("ch_a", "s_new"), ("ch_f", "s_new")]


# ------------------------------- a held scene must never vanish from a final

def _state_with_held_scene():
    return {"re_id": "re_t", "status": "awaiting_assembly", "job_id": "j_t",
            "scenes": [
                {"idx": 0, "scene_id": "s1", "motion_prompt": "m"},
                {"idx": 1, "scene_id": "s_new", "motion_prompt": "m",
                 "awaiting_person": True, "multi_person": True,
                 "people": [{"position": "left", "description": "man"},
                            {"position": "right", "description": "woman"}]}]}


def _job_with_one_approved_scene(tmp_path):
    """Scene s1 is fully done (approved image + a DONE clip on disk), so the
    ONLY thing that can show up as a gap is the held scene s_new."""
    from character_swap.models import VideoStatus, VideoVariant
    clip = tmp_path / "s1.mp4"
    clip.write_bytes(b"mp4")
    v = GeneratedImage(variant_id="v1", path="/v1.png", prompt="p",
                       scene_id="s1", status=VariantStatus.READY)
    vv = VideoVariant(video_id="vd1", source_variant_id="v1", grok_job_id="g1",
                      status=VideoStatus.DONE, final_video_path=str(clip))
    jc = JobCharacter(char_id="ch_a", name="A", source_image_path="/a.png",
                      status=CharStatus.APPROVED, images=[v], videos=[vv],
                      approved_variant_ids=["v1"])
    return Job(job_id="j_t", scene_id="s1", scene_image_path="/p",
               scene_ids=["s1", "s_new"], scene_image_paths=["/p", "/n"],
               characters={"ch_a": jc}, origin="reengineer:re_t"), jc


def test_held_scene_is_a_loud_gap_not_a_silent_drop(tmp_path):
    """A scene waiting on the person choice has NO variant slots — and the
    "no slots = the scene was never this character's" rule would have dropped
    it from the final without a word. That is the exact silent-partial class
    Hugo's standing rule forbids (and that shipped an incomplete reel to
    Telegram once already)."""
    job, jc = _job_with_one_approved_scene(tmp_path)
    state = _state_with_held_scene()

    clips, _dialogues, missing, waitable = runner_reengineer._collect_clips(
        state, jc)
    assert any("scen 2" in m for m in missing), missing
    assert any("välj vem" in m for m in missing), missing
    assert len(clips) == 1, "the finished scene still contributes its clip"
    # Not waitable: no amount of polling produces the answer, so the build must
    # refuse now rather than sit in the coverage wait until it times out.
    assert waitable is False

    gaps = runner_reengineer._assembly_gaps(state, job)
    assert [g["idx"] for g in gaps["hard"]] == [1]
    assert gaps["hard"][0]["reason"] == "välj vem som ska bytas ut"
    assert gaps["pending"] == []


def test_answered_scene_stops_blocking_the_build(tmp_path):
    """Once answered the flag is gone, and the scene falls back to the normal
    rules — here: no slots yet, so it is simply not built INTO the final until
    its images exist, exactly as any other freshly added scene."""
    job, jc = _job_with_one_approved_scene(tmp_path)
    state = _state_with_held_scene()
    del state["scenes"][1]["awaiting_person"]
    state["scenes"][1]["swap_person_idx"] = 0
    gaps = runner_reengineer._assembly_gaps(state, job)
    assert gaps["hard"] == []


# ------------------------------------------------- Director-OFF coverage

@pytest.fixture
def offdirector(monkeypatch, tmp_path):
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

    states: dict = {}
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: json.loads(json.dumps(states.get(rid)))
                        if states.get(rid) else None)
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state",
                        lambda s: states.__setitem__(
                            s["re_id"], json.loads(json.dumps(s))))

    async def fake_run(job_id, char_ids=None):
        box["calls"].append("run_image_generation")
    monkeypatch.setattr(runner_reengineer.runner, "run_image_generation",
                        fake_run)

    async def fake_watch(re_id, job_id, tasks=None):
        return None
    monkeypatch.setattr(runner_reengineer, "_watch_swap_phase", fake_watch)
    return box, states


def _off_state():
    return {"re_id": "re_t", "status": "analyzing", "use_director": False,
            "image_model": "gpt2-id-swap", "character_ids": ["ch_a"],
            "outfit_mode": "scene", "video_model": "kling-v3"}


def _off_entries():
    return [{"idx": 0, "scene_id": "s1", "motion_prompt": "m", "duration": 2.0},
            {"idx": 1, "scene_id": "s2", "motion_prompt": "m", "duration": 2.0}]


def test_gate_fires_with_the_director_switched_off(offdirector, monkeypatch):
    """Detection used to live INSIDE the Director call, so turning the Director
    off turned the whole multi-person gate off with it — silently."""
    box, states = offdirector

    async def fake(scenes, *, job_id=None):
        assert [s for s, _ in scenes] == ["s1", "s2"]
        return {"s2": {"multi_person": True, "people": [
            {"position": "left", "description": "woman red"},
            {"position": "right", "description": "man blue"}]}}
    monkeypatch.setattr(runner_reengineer, "detect_people_for_scenes", fake)

    states["re_t"] = _off_state()
    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", _off_state(), _off_entries(), "j_t"))

    assert states["re_t"]["status"] == "awaiting_person_choice"
    assert "run_image_generation" not in box["calls"]
    assert states["re_t"]["scenes"][1]["multi_person"] is True
    assert box["job"].scene_people_counts == {"s2": 2}


def test_single_person_run_proceeds_untouched(offdirector, monkeypatch):
    box, states = offdirector

    async def none_found(scenes, *, job_id=None):
        return {}
    monkeypatch.setattr(runner_reengineer, "detect_people_for_scenes",
                        none_found)
    states["re_t"] = _off_state()
    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", _off_state(), _off_entries(), "j_t"))
    assert states["re_t"]["status"] == "swapping"
    assert "run_image_generation" in box["calls"]
    assert box["job"].scene_people_counts == {}


def test_run_gate_bakes_the_choice_without_a_director_plan(monkeypatch,
                                                           tmp_path):
    """The run-level gate used to bail out when no Director plan was cached —
    the normal state with the Director off. The user picked a person, the UI
    recorded it, and the image model was never told."""
    job = Job(job_id="j_t", scene_id="s2", scene_image_path="/p",
              scene_ids=["s2"], scene_image_paths=["/p"],
              prompt=pipeline.GENERATION_PROMPT,
              image_model="gpt2-id-swap",
              characters={"ch_a": JobCharacter(
                  char_id="ch_a", name="A", source_image_path="/c.png",
                  status=CharStatus.QUEUED)},
              origin="reengineer:re_t")
    assert job.director_prompts_json is None
    box = {"job": job, "states": {}}

    class _S:
        def get_job(self, jid):
            return box["job"] if jid == "j_t" else None

        def update_job(self, j):
            box["job"] = j

        def get_scene(self, sid):
            return None
    monkeypatch.setattr(api, "store", lambda: _S())

    class _CS:
        def get_character(self, cid):
            return CharacterAsset(char_id=cid, name=cid, filename="x.png",
                                  gender="male")
    monkeypatch.setattr(runner, "store", lambda: _CS())

    box["states"]["re_t"] = {
        "re_id": "re_t", "status": "awaiting_person_choice", "job_id": "j_t",
        "scenes": [{"idx": 0, "scene_id": "s2", "multi_person": True,
                    "people": [{"position": "left", "description": "woman red"},
                               {"position": "right",
                                "description": "man blue"}]}]}
    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda rid: json.loads(json.dumps(box["states"][rid])))
    monkeypatch.setattr(api, "_save_reengineer_state",
                        lambda s: box["states"].__setitem__(
                            s["re_id"], json.loads(json.dumps(s))))

    asyncio.run(api.reengineer_resolve_people(
        "re_t", BackgroundTasks(),
        api.ResolvePeopleBody(scenes=[api.ResolvePeopleSceneBody(
            idx=0, swap_person_idx=1)])))

    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        box["job"].director_prompts_json)
    p = plan.lookup("ch_a", "s2")[0]
    assert "Replace SPECIFICALLY the man blue on the right" in p
    # Built on the ENGINE-effective prompt: gpt2-id-swap's COMPACT text, not
    # the long stored template that engine never runs. Stored scene-first
    # canonical (dispatch re-flips), so the round trip lands identity-first.
    assert p.startswith("Image 2 is only the identity reference")
    assert pipeline._flip_image_roles(p).startswith(
        "Image 1 is only the identity reference")
    assert "Constraints — do not violate any of these" not in p
    assert box["job"].scene_swap_targets["s2"] == "the man blue on the right"


# ------------------------------------------------------------------- QC net

def _wire_inspect(monkeypatch):
    calls: list = []
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "anthropic_api_key",
                        property(lambda self: "k"), raising=False)
    monkeypatch.setattr(type(settings), "swap_qc_enabled",
                        property(lambda self: True), raising=False)
    from character_swap.clients import anthropic_client
    monkeypatch.setattr(anthropic_client, "messages_with_tools",
                        lambda **kw: calls.append(kw) or object())
    monkeypatch.setattr(anthropic_client, "extract_tool_call",
                        lambda resp, name: {"passed": True, "reason": "",
                                            "corrective_hint": ""})
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p, **k: {"type": "text", "text": str(p)})
    return calls


def _flags(calls) -> str:
    return calls[0]["messages"][0]["content"][0]["text"]


def test_qc_told_the_head_count_and_the_target(monkeypatch, tmp_path):
    calls = _wire_inspect(monkeypatch)
    swap_qc.inspect_variant(
        scene_image=tmp_path / "s.png", character_image=tmp_path / "c.png",
        result_image=tmp_path / "r.png", scene_people_count=2,
        swap_target="the older man grey beard on the left")
    text = _flags(calls)
    assert "scene_people=2" in text
    assert 'swap_target="the older man grey beard on the left"' in text


def test_qc_stays_catastrophe_only_on_single_subject_scenes(monkeypatch,
                                                            tmp_path):
    """Hugo's 2026-06-30 loosening stands: a scene with one person (or one that
    was never measured) must reach the judge with no counting instruction at
    all, or every ordinary swap gets a new way to false-fail."""
    for count in (None, 1):
        calls = _wire_inspect(monkeypatch)
        swap_qc.inspect_variant(
            scene_image=tmp_path / "s.png", character_image=tmp_path / "c.png",
            result_image=tmp_path / "r.png", scene_people_count=count,
            swap_target="the man on the left")
        assert "scene_people" not in _flags(calls)
        assert "swap_target" not in _flags(calls)


def test_qc_prompt_defines_the_new_classes():
    sys = swap_qc.QC_SYSTEM
    assert "PERSON COUNT" in sys and "WRONG PERSON SWAPPED" in sys
    # Both are explicitly conditional on the context flag being present.
    assert "ONLY when the context flags state a `scene_people` count" in sys
    assert "ONLY when the context flags name a `swap_target`" in sys
    # The chosen person may legitimately differ in gender from the character —
    # the judge must not read that as a mistake and re-roll a correct image.
    flat = " ".join(sys.split())
    assert "they may differ from CHARACTER in gender or age" in flat
    assert "changing them anyway is CORRECT" in flat


def test_new_classes_reroll_but_plain_wrong_person_still_repairs():
    assert swap_qc.needs_reroll("PERSON COUNT — the woman on the right is gone")
    assert swap_qc.needs_reroll("WRONG PERSON SWAPPED — the man is unchanged")
    # Unchanged: an identity miss is still fixed by a minimal-change face edit.
    assert not swap_qc.needs_reroll("WRONG PERSON: the face is a blend")


def test_runner_forwards_the_scene_head_count_to_the_judge(monkeypatch,
                                                           tmp_path):
    dest = tmp_path / "variant_v1.png"
    v = GeneratedImage(variant_id="v1", path=str(dest), prompt="BASE",
                       scene_id="s1", status=VariantStatus.GENERATING)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/char.png",
                      status=CharStatus.GENERATING, images=[v])
    job = Job(job_id="j1", scene_id="s1", scene_image_path="/scene.png",
              scene_ids=["s1"], scene_image_paths=["/scene.png"],
              characters={"cA": jc},
              scene_people_counts={"s1": 2},
              scene_swap_targets={"s1": "the man on the left"})

    seen: dict = {}
    monkeypatch.setattr(runner.pipeline, "generate_variant",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"img"))
    monkeypatch.setattr(runner.swap_qc, "inspect_variant",
                        lambda **kw: seen.update(kw) or QCVerdict(True, "", ""))
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_variant", lambda *a, **k: None)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(runner, "_scene_path_for_variant",
                        lambda j, v: Path("/scene.png"))
    asyncio.run(runner._generate_one_variant(job, jc, v, asyncio.Semaphore(1)))
    assert seen["scene_people_count"] == 2
    assert seen["swap_target"] == "the man on the left"
