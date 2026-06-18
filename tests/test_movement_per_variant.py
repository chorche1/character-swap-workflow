"""Tests for per-approved-image movement prompts + durations (Step 4).

The Higgsfield "per-slot" model: every approved image animates with its OWN
motion prompt and its own clip duration, instead of sharing one prompt per
scene. These tests drive `set_movement` directly with a hermetic fake store
and assert the per-variant dicts are stored, a per-scene dict is derived for
back-compat, and validation rejects a missing per-image prompt.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
)


class _FakeStore:
    def __init__(self, job: Job) -> None:
        self._job = job

    def get_job(self, job_id: str) -> Job | None:
        return self._job if job_id == self._job.job_id else None

    def update_job(self, job: Job) -> None:
        self._job = job


def _run(coro):
    return asyncio.run(coro)


def _job_two_approved_one_scene() -> Job:
    """One character, one scene, TWO approved variants (v_a, v_b)."""
    imgs = [
        GeneratedImage(variant_id="v_a", path="/tmp/a.png", prompt="(uploaded)",
                       scene_id="sc1", status=VariantStatus.READY),
        GeneratedImage(variant_id="v_b", path="/tmp/b.png", prompt="(uploaded)",
                       scene_id="sc1", status=VariantStatus.READY),
    ]
    jc = JobCharacter(
        char_id="c1", name="Cooper", source_image_path="/tmp/a.png",
        status=CharStatus.APPROVED, images=imgs,
        approved_variant_ids=["v_a", "v_b"], approved_variant_id="v_a",
    )
    return Job(
        job_id="j_test", title="t",
        scene_id="sc1", scene_image_path="/tmp/scene.png",
        scene_ids=["sc1"], scene_image_paths=["/tmp/scene.png"],
        characters={"c1": jc},
    )


@pytest.fixture
def isolated(monkeypatch: pytest.MonkeyPatch):
    job = _job_two_approved_one_scene()
    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)
    # These tests exercise prompt/duration storage, not provider availability.
    # set_movement now validates the chosen model's API key upfront (422 when
    # locked) — disable that check so the storage assertions run regardless of
    # which keys the test env has. Key-validation itself is covered in
    # test_per_clip_video_model.py.
    monkeypatch.setattr(api, "_require_video_model_available", lambda slug: None)
    return store


def test_per_variant_prompts_and_durations_stored(isolated: _FakeStore) -> None:
    body = api.MovementBody(
        movement_prompts_by_variant={
            "v_a": "He pours the mouthwash",
            "v_b": "He holds the bowl and smiles",
        },
        durations_by_variant={"v_a": 5, "v_b": 10},
        videos_per_character=1,
        video_model="kling-v2-6",   # provider faked-available by the fixture
    )
    _run(api.set_movement("j_test", body, BackgroundTasks()))
    job = isolated.get_job("j_test")

    # Per-image dicts stored verbatim.
    assert job.movement_prompts_by_variant == {
        "v_a": "He pours the mouthwash",
        "v_b": "He holds the bowl and smiles",
    }
    assert job.durations_by_variant == {"v_a": 5, "v_b": 10}
    # A per-scene dict is derived (first approved image's prompt) so the
    # legacy lock + Step 6 compile keep working.
    assert "sc1" in job.movement_prompts
    assert job.movement_prompts["sc1"] in (
        "He pours the mouthwash", "He holds the bowl and smiles")
    assert job.movement_prompt  # singular lock field populated


def test_missing_per_image_prompt_is_rejected(isolated: _FakeStore) -> None:
    body = api.MovementBody(
        movement_prompts_by_variant={"v_a": "only this one"},   # v_b missing
        videos_per_character=1,
        video_model="kling-v2-6",
    )
    with pytest.raises(HTTPException) as ei:
        _run(api.set_movement("j_test", body, BackgroundTasks()))
    assert ei.value.status_code == 400
    assert "approved" in str(ei.value.detail).lower()


def test_out_of_range_per_image_duration_dropped(isolated: _FakeStore) -> None:
    body = api.MovementBody(
        movement_prompts_by_variant={"v_a": "p", "v_b": "q"},
        durations_by_variant={"v_a": 5, "v_b": 99},   # 99 not a kling option
        videos_per_character=1,
        video_model="kling-v2-6",
    )
    _run(api.set_movement("j_test", body, BackgroundTasks()))
    job = isolated.get_job("j_test")
    assert job.durations_by_variant == {"v_a": 5}   # 99 dropped, 5 kept


def test_per_scene_path_still_works_without_per_variant(isolated: _FakeStore) -> None:
    # No per-variant dict → falls back to the per-scene path (one prompt).
    body = api.MovementBody(
        movement_prompts={"sc1": "everyone in this scene pours oil"},
        videos_per_character=1,
        video_model="kling-v2-6",
    )
    _run(api.set_movement("j_test", body, BackgroundTasks()))
    job = isolated.get_job("j_test")
    assert job.movement_prompts == {"sc1": "everyone in this scene pours oil"}
    assert job.movement_prompts_by_variant == {}   # none set
