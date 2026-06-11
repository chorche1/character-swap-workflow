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
    assert out == ("ugc kiwi video",
                   {"s1": "Tailored one", "s2": "Tailored two"})
    # System prompt carries the verbatim identity + outfit + framing rules.
    assert "recognizable likeness" in seen["system"]
    assert "outfit from Image 1" in seen["system"]       # scene mode
    assert "do not zoom out" in seen["system"]
    assert "There is no Image 3" in seen["system"]       # no background


def test_direct_reengineer_swap_background_and_custom(monkeypatch, tmp_path):
    payload = {"intent": "x", "scenes": [{"scene_id": "s1", "prompt": "p"}]}
    seen = _stub_call(monkeypatch, payload)
    out = prompt_director.direct_reengineer_swap(
        scenes=[("s1", tmp_path / "f1.png")],
        outfit_mode="custom", outfit_text="a red hoodie", background=True)
    assert out is not None
    assert "NEW ENVIRONMENT" in seen["system"]
    assert "a red hoodie" in seen["system"]


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
        director_result=("intent!", {"s1": "TAILORED s1", "s2": "TAILORED s2"}))

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
