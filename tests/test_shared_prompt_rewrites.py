"""One billed rewrite per (scene × gender × language), shared by every clip.

Hugo 2026-08-03: "3 och två personer agents-funktionen kan använda en agent
körning per scen per språk per kön för att få samma prompt för alla som pratar
samma språk med samma kön."

Both rewrites the runner pays for — the 👥 speaker-attribution agent and the 🗣
language localizer — depend only on the input text, the scene, the character's
GENDER and (for the localizer) the LANGUAGE. They never depend on WHICH
character is being animated. A 9-character run was buying the same rewrite 9
times, and the nine answers could differ from each other, so the same scene
could ship with different wording per character.

Locked here: the sharing itself, the boundaries it must NOT cross (different
gender, different language, a per-clip prompt override), and that a failure is
not cached.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import runner


@pytest.fixture(autouse=True)
def _isolate():
    runner._PROMPT_CACHE.clear()
    runner._PROMPT_LOCKS.clear()
    yield
    runner._PROMPT_CACHE.clear()
    runner._PROMPT_LOCKS.clear()


def _key(**kw):
    base = dict(job_id="j1", kind="localize", scene_id="s1", gender="female",
                language="de", prompt="THE PROMPT")
    base.update(kw)
    return runner._prompt_cache_key(base["job_id"], base["kind"],
                                    base["scene_id"], base["gender"],
                                    base["language"], base["prompt"])


def _counting():
    calls = []

    async def compute():
        calls.append(1)
        return f"rewrite-{len(calls)}"
    return calls, compute


def test_same_scene_gender_language_computes_once():
    calls, compute = _counting()
    out = asyncio.run(_gather_same(_key(), compute, n=9))
    assert calls == [1], "nine characters must buy ONE rewrite"
    assert out == ["rewrite-1"] * 9, "and all nine must get the SAME text"


async def _gather_same(key, compute, *, n):
    return list(await asyncio.gather(
        *[runner._cached_prompt(key, compute) for _ in range(n)]))


def test_concurrent_callers_do_not_race_into_duplicate_calls():
    """The clips animate in parallel, so a plain check-then-set would let all
    nine through before the first result lands."""
    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(0.02)
        return "one"

    out = asyncio.run(_gather_same(_key(), slow, n=9))
    assert calls == [1]
    assert out == ["one"] * 9


@pytest.mark.parametrize("differs", [
    {"gender": "male"},
    {"language": "es"},
    {"scene_id": "s2"},
    {"prompt": "A DIFFERENT PROMPT"},   # per-clip override must not share
    {"kind": "speaker_fix"},
    {"job_id": "j2"},
])
def test_each_dimension_gets_its_own_rewrite(differs):
    calls, compute = _counting()
    asyncio.run(runner._cached_prompt(_key(), compute))
    asyncio.run(runner._cached_prompt(_key(**differs), compute))
    assert len(calls) == 2, f"{differs} must not reuse another slot's rewrite"


def test_failure_is_not_cached():
    """A localizer that failed once must be retryable on the next clip rather
    than poisoning every sibling with the same error."""
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("translation failed")
        return "ok"

    async def go():
        with pytest.raises(RuntimeError):
            await runner._cached_prompt(_key(), flaky)
        return await runner._cached_prompt(_key(), flaky)

    assert asyncio.run(go()) == "ok"
    assert len(attempts) == 2


def test_clear_drops_only_the_finished_job():
    calls, compute = _counting()
    asyncio.run(runner._cached_prompt(_key(job_id="j1"), compute))
    asyncio.run(runner._cached_prompt(_key(job_id="j2"), compute))
    runner._clear_prompt_cache("j1")
    assert all(k[0] != "j1" for k in runner._PROMPT_CACHE)
    assert any(k[0] == "j2" for k in runner._PROMPT_CACHE)
    assert all(k[0] != "j1" for k in runner._PROMPT_LOCKS)


def test_cache_is_bounded():
    """The retry / "+N more" / reanimate paths call _animate_one_video directly
    and never reach the batch-end clear, so the dicts are hard-capped."""
    async def go():
        for i in range(runner._MAX_PROMPT_CACHE + 40):
            calls, compute = _counting()
            await runner._cached_prompt(_key(prompt=f"p{i}"), compute)
    asyncio.run(go())
    assert len(runner._PROMPT_CACHE) <= runner._MAX_PROMPT_CACHE
    assert len(runner._PROMPT_LOCKS) <= runner._MAX_PROMPT_CACHE + 1
