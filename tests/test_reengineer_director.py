"""Opt-in AI Director for Reengineer swap prompts (Hugo, 2026-06-11).

One Claude call looks at every scene frame and writes ONE compact tailored
swap prompt per SCENE (props named with position/size in frame, camera
distance anchored — the static template's observed drift modes). The
per-scene prompts are replicated into the standard SwapDirectorPlan so the
existing `_kick_char` prompt-precedence plumbing consumes them unchanged.
Any failure → None → normal template chain; generation never blocks.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from character_swap import prompt_director, runner_reengineer
from character_swap.models import CharacterAsset, Job


# ----------------------------------------------------- plan replication

def test_plan_from_scene_prompts_replicates_per_char():
    plan = prompt_director.plan_from_scene_prompts(
        "kiwi recipe ugc",
        {"s1": "PROMPT ONE", "s2": "PROMPT TWO"},
        [("cA", "wang"), ("cB", "Cooper")],
    )
    for cid in ("cA", "cB"):
        assert plan.lookup(cid, "s1") == ["PROMPT ONE"]
        assert plan.lookup(cid, "s2") == ["PROMPT TWO"]
    assert plan.lookup("cA", "missing") == []
    # Round-trips through the Job cache format _kick_char parses.
    reparsed = prompt_director.SwapDirectorPlan.model_validate_json(
        plan.model_dump_json())
    assert reparsed.lookup("cB", "s2") == ["PROMPT TWO"]


# ----------------------------------------------------- the Director call

def _stub_call(monkeypatch, tool_payload, *, raise_exc=None):
    seen = {}

    def fake_messages(**kw):
        seen.update(kw)
        if raise_exc:
            raise raise_exc
        return "RESP"
    monkeypatch.setattr(prompt_director.anthropic_client,
                        "messages_with_tools", fake_messages)
    monkeypatch.setattr(prompt_director.anthropic_client,
                        "extract_tool_call", lambda resp, name: tool_payload)
    monkeypatch.setattr(prompt_director.anthropic_client,
                        "_file_to_image_block",
                        lambda p, **k: {"type": "text", "text": str(p)})
    return seen


def test_direct_reengineer_swap_happy_path(monkeypatch, tmp_path):
    payload = {"intent": "ugc kiwi video",
               "scenes": [{"scene_id": "s1", "prompt": "Tailored one"},
                          {"scene_id": "s2", "prompt": "Tailored two"}]}
    seen = _stub_call(monkeypatch, payload)
    out = prompt_director.direct_reengineer_swap(
        scenes=[("s1", tmp_path / "f1.png"), ("s2", tmp_path / "f2.png")],
        outfit_mode="scene", job_id="re_t")
    assert out is not None
    intent, prompts, meta = out
    assert intent == "ugc kiwi video"
    assert prompts["s1"].startswith("Tailored one")
    assert prompts["s2"].startswith("Tailored two")
    # Hugo's organic anti-"produced" style is appended VERBATIM in code to
    # every Director prompt — the agent is told NOT to write style language.
    for p in prompts.values():
        assert "unedited iPhone photo" in p
        assert "no studio lighting" in p
        # Lighting realism (2026-06-12): the appended style clause carries
        # the everyday-light + grounding cues so even a slip in the agent's
        # light line is pushed back. The Director kept writing "soft diffused
        # daylight" — flattering/produced — across every scene.
        assert "no soft flattering key light" in p
        assert "contact shadow" in p
        assert "not pasted on" in p
    assert "appended to your prompt automatically" in seen["system"]
    # System prompt carries the verbatim identity + outfit + framing rules.
    assert "recognizable likeness" in seen["system"]
    assert "outfit from Image 1" in seen["system"]       # scene mode
    assert "do not zoom out" in seen["system"]
    assert "There is no Image 3" in seen["system"]       # no background
    # The light rule forbids flattering photographic light (no-bg branch).
    low = " ".join(seen["system"].lower().split())
    assert "no 'soft', 'diffused'" in low
    assert "ordinary phone snapshot" in low


def test_direct_reengineer_swap_background_and_custom(monkeypatch, tmp_path):
    """With a replacement background, the Director SEES the background image,
    is told to anchor environment+light to IT, and is FORBIDDEN from naming
    the original scene's background (2026-06-12: the Director wrote 'red
    barn visible upper background' from the scene frame, contradicting the
    replace-surroundings directive → wrong background in the output)."""
    payload = {"intent": "x", "scenes": [{"scene_id": "s1", "prompt": "p"}]}
    seen = _stub_call(monkeypatch, payload)
    bg = tmp_path / "bg.png"
    out = prompt_director.direct_reengineer_swap(
        scenes=[("s1", tmp_path / "f1.png")],
        outfit_mode="custom", outfit_text="a red hoodie",
        background_path=bg)
    assert out is not None
    assert "NEW ENVIRONMENT" in seen["system"]
    assert "STRICTLY FORBIDDEN" in seen["system"]        # no old-bg anchors
    assert "a red hoodie" in seen["system"]
    # Backlog #3 (2026-06-12): distinctive symbols in the replacement
    # background must be anchored by their key parts — the US flag lost its
    # blue star canton in 3/6 post-fix scenes (re_c5fb4bfcd2) and flickered
    # correct/wrong across cuts.
    assert "distinctive SYMBOL" in seen["system"]
    assert "blue star canton" in seen["system"]
    # Backlog #14+#15: gaze/hand-state + prop count/state/container +
    # foreground furniture are anchored per scene.
    flat = " ".join(seen["system"].split())
    assert "PERFORMANCE ANCHORS" in flat
    assert "PROP PRECISION" in flat
    assert "gaze direction" in flat
    assert "foreground furniture" in flat
    # The bg light rule also forbids flattering light and demands everyday
    # ordinary-phone light relit from Image 3 (2026-06-12 lighting fix).
    low = " ".join(seen["system"].lower().split())
    assert "no 'soft', 'diffused'" in low
    assert "relit by exactly that ordinary light" in low
    # The background image itself is in the vision content, before scenes.
    blocks = seen["messages"][0]["content"]
    texts = [b.get("text", "") for b in blocks]
    assert any("REPLACEMENT BACKGROUND" in t for t in texts)
    assert str(bg) in texts                              # the encoded image
    assert texts.index(str(bg)) < texts.index("SCENE s1:")
    # Framing comes from Image 1, not the new background — the bg is cropped
    # to fit the scene, never importing its headroom/sky above the head
    # (2026-06-12: the doctor scene got the backyard's trees/house dumped
    # above the subject's head). Stated as an instruction the Director folds
    # in — NOT a mandated verbatim literal, to stay under the ~120w cap.
    low = " ".join(seen["system"].lower().split())
    assert "cropped behind the subject to fit image 1's framing" in low
    assert "headroom / horizon / open sky must not appear above the head" in low


def test_director_system_locks_vertical_headroom():
    """The Director system prompt always carries the explicit headroom /
    vertical-placement anchor — the recurring 'subject pushed down, dead
    space above the head' drift (Hugo 2026-06-12)."""
    sys_bg = prompt_director.REENGINEER_SWAP_DIRECTOR_SYSTEM.format(
        bg_role="X", outfit_directive="Y", light_rule="Z")
    assert "VERTICAL FRAMING / HEADROOM" in sys_bg
    low = " ".join(sys_bg.lower().split())          # flatten wrapped lines
    assert "headroom" in low
    assert "keep the head at this same height in the frame" in low
    assert "add no empty space, sky or scenery above it" in low


@pytest.mark.parametrize("case", ["no_scenes", "bad_payload", "exception",
                                  "custom_without_text"])
def test_direct_reengineer_swap_falls_back_to_none(monkeypatch, tmp_path, case):
    if case == "no_scenes":
        assert prompt_director.direct_reengineer_swap(scenes=[]) is None
        return
    if case == "custom_without_text":
        _stub_call(monkeypatch, {"intent": "x", "scenes": []})
        assert prompt_director.direct_reengineer_swap(
            scenes=[("s1", tmp_path / "f.png")], outfit_mode="custom") is None
        return
    if case == "bad_payload":
        _stub_call(monkeypatch, {"intent": "x", "scenes": []})
    else:
        _stub_call(monkeypatch, None, raise_exc=RuntimeError("api down"))
    assert prompt_director.direct_reengineer_swap(
        scenes=[("s1", tmp_path / "f.png")], outfit_mode="scene") is None


# ----------------------------------------------------- end-to-end wiring

def _wire_create(monkeypatch, tmp_path, *, director_result):
    """Minimal store/settings so _create_job_and_swap runs to completion."""
    chars_dir = tmp_path / "characters"
    chars_dir.mkdir()
    (chars_dir / "ch_a.png").write_bytes(b"c")
    ch = CharacterAsset(char_id="ch_a", name="wang", filename="ch_a.png")

    box = {"job": None}

    class _S:

        # runner_reengineer.scene_path resolves job scene paths through the
        # SceneAsset (a .webp upload / a ⧉-duplicated scene has no
        # <scene_id>.png). None keeps the historical derivation.
        def get_scene(self, sid):
            return None
        def get_character(self, cid):
            return ch if cid == "ch_a" else None

        def add_job(self, job):
            box["job"] = job

        def get_job(self, jid):
            return box["job"]

        def update_job(self, job):
            pass
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "characters_dir",
                        property(lambda self: chars_dir), raising=False)
    monkeypatch.setattr(type(settings), "has_provider",
                        lambda self, p: True)

    states = {"re_t": {"re_id": "re_t", "status": "analyzing",
                       "use_director": True, "image_model": "gpt2-id-swap",
                       "character_ids": ["ch_a"], "outfit_mode": "scene"}}
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: dict(states.get(rid) or {}))
    monkeypatch.setattr(runner_reengineer.reengineer, "save_state",
                        lambda s: states.__setitem__(s["re_id"], dict(s)))

    monkeypatch.setattr(prompt_director, "direct_reengineer_swap",
                        lambda **kw: director_result)

    async def fake_run(job_id, char_ids=None):
        return None
    monkeypatch.setattr(runner_reengineer.runner, "run_image_generation",
                        fake_run)

    async def fake_watch(re_id, job_id, tasks=None):
        return None
    monkeypatch.setattr(runner_reengineer, "_watch_swap_phase", fake_watch)
    return box, states


def _entries(tmp_path):
    (tmp_path / "s1.png").write_bytes(b"a")
    (tmp_path / "s2.png").write_bytes(b"b")
    return [
        {"idx": 0, "scene_id": "s1", "start": 0.0, "end": 2.0, "duration": 2.0,
         "motion_prompt": "m1", "speech": "", "summary": "one"},
        {"idx": 1, "scene_id": "s2", "start": 2.0, "end": 4.0, "duration": 2.0,
         "motion_prompt": "m2", "speech": "", "summary": "two"},
    ]


def test_create_job_caches_director_plan(monkeypatch, tmp_path):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "scenes_dir",
                        property(lambda self: tmp_path), raising=False)
    box, states = _wire_create(
        monkeypatch, tmp_path,
        director_result=("intent!", {"s1": "TAILORED s1", "s2": "TAILORED s2"}, {}))

    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", dict(states["re_t"]), _entries(tmp_path), "j_dir"))

    job: Job = box["job"]
    assert job.use_director is True
    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        job.director_prompts_json)
    assert plan.lookup("ch_a", "s1") == ["TAILORED s1"]
    assert plan.lookup("ch_a", "s2") == ["TAILORED s2"]


def test_create_job_falls_back_when_director_fails(monkeypatch, tmp_path):
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "scenes_dir",
                        property(lambda self: tmp_path), raising=False)
    box, states = _wire_create(monkeypatch, tmp_path, director_result=None)

    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", dict(states["re_t"]), _entries(tmp_path), "j_dir"))

    job: Job = box["job"]
    assert job.use_director is False
    assert job.director_prompts_json is None


# ----------------------------------------------------- duplicate-scene dedup


def test_duplicate_scenes_get_one_director_call_and_an_identical_prompt(
        monkeypatch, tmp_path):
    """Hugo 2026-08-09, from the app: a run with a scene duplicated 3x asked
    whether a rerun would still generate one image per (character × unique
    image). It did not, reliably.

    A ⧉-duplicated scene shares its image byte-for-byte with the scene it was
    duplicated from. Sending the Director all copies of that image and
    trusting it to describe each one with byte-identical text is not safe:
    measured on a real duplicated-scene run, the Director's own tool call
    simply OMITTED 3 of 4 duplicate entries from its response — they only
    ended up sharing a prompt because both fell back to the same job-wide
    stock string, an accident of that run, not a guarantee. The fix dedupes
    by image CONTENT before the call and copies the one answer onto every
    scene_id sharing that image, so `_kick_char`'s (image, prompt) dedup
    always has byte-identical text to match on for a duplicate group."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "scenes_dir",
                        property(lambda self: tmp_path), raising=False)
    box, states = _wire_create(monkeypatch, tmp_path, director_result=None)

    # s1 and s1dup are BYTE-IDENTICAL (the duplicate); s2 is a different shot.
    (tmp_path / "s1.png").write_bytes(b"identical-frame-bytes")
    (tmp_path / "s1dup.png").write_bytes(b"identical-frame-bytes")
    (tmp_path / "s2.png").write_bytes(b"a completely different frame")
    entries = [
        {"idx": 0, "scene_id": "s1", "start": 0.0, "end": 2.0, "duration": 2.0,
         "motion_prompt": "m1", "speech": "", "summary": "one"},
        {"idx": 1, "scene_id": "s1dup", "start": 2.0, "end": 4.0, "duration": 2.0,
         "motion_prompt": "m1 but a different line", "speech": "",
         "summary": "one (kopia)"},
        {"idx": 2, "scene_id": "s2", "start": 4.0, "end": 6.0, "duration": 2.0,
         "motion_prompt": "m2", "speech": "", "summary": "two"},
    ]

    seen_scenes = {}

    def fake_director(*, scenes, **kw):
        seen_scenes["scenes"] = list(scenes)
        # Simulate the REAL failure mode: the Director writes its OWN
        # (non-identical, but semantically equivalent) text for whichever
        # scene ids it was actually asked about. If the dedup below did not
        # already collapse s1/s1dup to one representative, this stub would
        # happily hand back two DIFFERENT prompt strings for the same image —
        # exactly what broke the naive per-scene approach.
        return ("intent!", {sid: f"director text for {sid}" for sid, _ in scenes}, {})
    monkeypatch.setattr(prompt_director, "direct_reengineer_swap", fake_director)

    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", dict(states["re_t"]), entries, "j_dir"))

    # The Director was asked about the duplicate group ONCE, not twice.
    asked_ids = [sid for sid, _ in seen_scenes["scenes"]]
    assert asked_ids.count("s1") + asked_ids.count("s1dup") == 1
    assert "s2" in asked_ids

    job: Job = box["job"]
    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        job.director_prompts_json)
    p1 = plan.lookup("ch_a", "s1")
    p1dup = plan.lookup("ch_a", "s1dup")
    p2 = plan.lookup("ch_a", "s2")
    assert p1 and p1 == p1dup, (
        "duplicate scenes must get the IDENTICAL prompt — copied from one "
        "Director answer, not two independently-generated ones")
    assert p2 != p1                 # the genuinely different scene is untouched


def test_three_way_duplicate_all_collapse_to_the_first(monkeypatch, tmp_path):
    """The reported case exactly: one original plus THREE ⧉ copies."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "scenes_dir",
                        property(lambda self: tmp_path), raising=False)
    box, states = _wire_create(monkeypatch, tmp_path, director_result=None)

    ids = ["base", "dup1", "dup2", "dup3"]
    for sid in ids:
        (tmp_path / f"{sid}.png").write_bytes(b"same-bytes-every-time")
    entries = [
        {"idx": i, "scene_id": sid, "start": float(i), "end": float(i + 1),
         "duration": 1.0, "motion_prompt": f"line {i}", "speech": "",
         "summary": sid}
        for i, sid in enumerate(ids)
    ]

    seen_scenes = {}

    def fake_director(*, scenes, **kw):
        seen_scenes["scenes"] = list(scenes)
        return ("intent!", {sid: f"unique text {sid}" for sid, _ in scenes}, {})
    monkeypatch.setattr(prompt_director, "direct_reengineer_swap", fake_director)

    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", dict(states["re_t"]), entries, "j_dir"))

    assert len(seen_scenes["scenes"]) == 1     # ONE representative for all 4

    job: Job = box["job"]
    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        job.director_prompts_json)
    prompts = {sid: plan.lookup("ch_a", sid) for sid in ids}
    assert len(set(tuple(v) for v in prompts.values())) == 1, prompts


def test_director_omitting_a_duplicate_from_its_own_answer_still_copies_it(
        monkeypatch, tmp_path):
    """The EXACT failure measured on the live run: the Director's tool call
    left 3 of 4 duplicate entries out of its response entirely. Since the
    dedup now only ever asks about ONE representative, there is nothing left
    for the Director to omit — but pin the degenerate case (a stub that
    returns an empty dict) to prove the duplicate still gets nothing rather
    than crashing, and does not silently inherit some OTHER scene's text."""
    from character_swap.config import settings
    monkeypatch.setattr(type(settings), "scenes_dir",
                        property(lambda self: tmp_path), raising=False)
    box, states = _wire_create(monkeypatch, tmp_path, director_result=None)

    (tmp_path / "s1.png").write_bytes(b"xx")
    (tmp_path / "s1dup.png").write_bytes(b"xx")
    entries = [
        {"idx": 0, "scene_id": "s1", "start": 0.0, "end": 1.0, "duration": 1.0,
         "motion_prompt": "m1", "speech": "", "summary": "one"},
        {"idx": 1, "scene_id": "s1dup", "start": 1.0, "end": 2.0,
         "duration": 1.0, "motion_prompt": "m1", "speech": "",
         "summary": "one (kopia)"},
    ]
    monkeypatch.setattr(prompt_director, "direct_reengineer_swap",
                        lambda **kw: ("intent!", {}, {}))

    asyncio.run(runner_reengineer._create_job_and_swap(
        "re_t", dict(states["re_t"]), entries, "j_dir"))

    job: Job = box["job"]
    plan = prompt_director.SwapDirectorPlan.model_validate_json(
        job.director_prompts_json)
    assert plan.lookup("ch_a", "s1") == []
    assert plan.lookup("ch_a", "s1dup") == []
