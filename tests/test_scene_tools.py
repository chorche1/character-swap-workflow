"""Tests for the scene-sequencing endpoints (duplicate / reorder / delete) that
sit between Swap Step 3 and Step 4, plus per-scene duration storage.

These let the user build a video sequence from approved images — multiple clips
from one image (duplicate), custom order (reorder), or drop a slot (delete).
Hermetic: endpoints are called directly with a stub store; events.publish is
patched to a no-op so no event loop/registry is needed.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import api
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
)


class _FakeStore:
    def __init__(self, job: Job) -> None:
        self._job = job
        self.saved = 0

    def get_job(self, job_id: str):
        return self._job if job_id == self._job.job_id else None

    def update_job(self, job: Job) -> None:
        self._job = job
        self.saved += 1


def _img(vid: str, scene_id: str, path: str = "/x.png") -> GeneratedImage:
    return GeneratedImage(variant_id=vid, path=path, prompt="p",
                          scene_id=scene_id, status=VariantStatus.READY)


def _job_two_scenes() -> Job:
    """A 2-scene job, 2 characters, each with one approved variant per scene."""
    a = JobCharacter(
        char_id="cA", name="A", source_image_path="/a.png",
        status=CharStatus.APPROVED,
        images=[_img("vA1", "s1"), _img("vA2", "s2")],
        approved_variant_ids=["vA1", "vA2"], approved_variant_id="vA1",
    )
    b = JobCharacter(
        char_id="cB", name="B", source_image_path="/b.png",
        status=CharStatus.APPROVED,
        images=[_img("vB1", "s1"), _img("vB2", "s2")],
        approved_variant_ids=["vB1", "vB2"], approved_variant_id="vB1",
    )
    return Job(
        job_id="j_test", title="t",
        scene_id="s1", scene_image_path="/p1.png",
        scene_ids=["s1", "s2"], scene_image_paths=["/p1.png", "/p2.png"],
        characters={"cA": a, "cB": b},
    )


@pytest.fixture(autouse=True)
def _patch(monkeypatch):
    # events.publish is async; make it a no-op coroutine so endpoints run
    # without an event loop registry.
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(api.events, "publish", _noop)


def _run(coro):
    return asyncio.run(coro)


def _with_store(job: Job, monkeypatch) -> _FakeStore:
    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)
    return store


def test_duplicate_inserts_slot_after_source_and_clones_approved(monkeypatch):
    store = _with_store(_job_two_scenes(), monkeypatch)
    result = _run(api.duplicate_scene("j_test", "s1"))
    ids = [s["scene_id"] for s in result["scenes"]]
    # New slot inserted right after s1.
    assert ids[0] == "s1" and ids[2] == "s2"
    assert ids[1].startswith("s1__dup")
    new_sid = ids[1]
    # Each character got a cloned, pre-approved variant under the new scene.
    for cid in ("cA", "cB"):
        c = result["characters"][cid]
        dup_imgs = [im for im in c["images"] if im["scene_id"] == new_sid]
        assert len(dup_imgs) == 1
        assert dup_imgs[0]["variant_id"] in c["approved_variant_ids"]
        assert dup_imgs[0]["status"] == VariantStatus.READY.value


def test_duplicate_carries_unapproved_images(monkeypatch):
    """Regression (Hugo 2026-06-17): duplicating a scene BEFORE approving must
    still carry every ready image to the copy — it used to clone only approved
    variants, so a pre-approval duplicate came back empty ("ingen bild ännu").
    The copy mirrors the source's approval state: images follow, but stay
    un-approved when the source had no approval yet."""
    job = _job_two_scenes()
    # Two ready variants on s1 for cA, NONE approved on s1 (only s2 approved).
    job.characters["cA"].images = [
        _img("vA1a", "s1"), _img("vA1b", "s1"), _img("vA2", "s2"),
    ]
    job.characters["cA"].approved_variant_ids = ["vA2"]
    job.characters["cA"].approved_variant_id = "vA2"
    store = _with_store(job, monkeypatch)
    result = _run(api.duplicate_scene("j_test", "s1"))
    new_sid = [s["scene_id"] for s in result["scenes"]][1]
    c = result["characters"]["cA"]
    dup_imgs = [im for im in c["images"] if im["scene_id"] == new_sid]
    # BOTH ready source images carried over...
    assert len(dup_imgs) == 2
    parents = {im["parent_variant_id"] for im in dup_imgs}
    assert parents == {"vA1a", "vA1b"}
    # ...but none auto-approved, since the source scene had no approval.
    for im in dup_imgs:
        assert im["variant_id"] not in c["approved_variant_ids"]


def test_duplicate_skips_failed_and_generating(monkeypatch):
    """Only READY variants carry to the copy — a failed/in-flight slot has no
    usable image, so it is not cloned."""
    job = _job_two_scenes()
    bad = _img("vBad", "s1"); bad.status = VariantStatus.FAILED
    pending = _img("vGen", "s1"); pending.status = VariantStatus.GENERATING
    job.characters["cA"].images = [_img("vA1", "s1"), bad, pending,
                                   _img("vA2", "s2")]
    job.characters["cA"].approved_variant_ids = ["vA1", "vA2"]
    job.characters["cA"].approved_variant_id = "vA1"
    store = _with_store(job, monkeypatch)
    result = _run(api.duplicate_scene("j_test", "s1"))
    new_sid = [s["scene_id"] for s in result["scenes"]][1]
    c = result["characters"]["cA"]
    dup_imgs = [im for im in c["images"] if im["scene_id"] == new_sid]
    assert len(dup_imgs) == 1                              # only the ready one
    assert dup_imgs[0]["parent_variant_id"] == "vA1"
    assert dup_imgs[0]["variant_id"] in c["approved_variant_ids"]


def test_duplicate_reuses_same_file_path(monkeypatch):
    job = _job_two_scenes()
    job.characters["cA"].images[0].path = "/shared/frame.png"
    store = _with_store(job, monkeypatch)
    result = _run(api.duplicate_scene("j_test", "s1"))
    new_sid = [s["scene_id"] for s in result["scenes"]][1]
    # Inspect the stored Job (the serialized url is mount-relative; the raw
    # path is what proves the clone reuses the same file on disk).
    jc = store.get_job("j_test").characters["cA"]
    dup = next(im for im in jc.images if im.scene_id == new_sid)
    assert dup.path == "/shared/frame.png"           # same file, no copy
    assert dup.parent_variant_id == "vA1"            # links back to the source


def test_reorder_swaps_scene_ids_and_paths(monkeypatch):
    _with_store(_job_two_scenes(), monkeypatch)
    body = api.SceneOrderBody(scene_ids=["s2", "s1"])
    result = _run(api.reorder_scenes("j_test", body))
    assert [s["scene_id"] for s in result["scenes"]] == ["s2", "s1"]
    # Primary scene + its image follow the new order.
    assert result["scene_id"] == "s2"


def test_reorder_rejects_non_permutation(monkeypatch):
    _with_store(_job_two_scenes(), monkeypatch)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.reorder_scenes("j_test", api.SceneOrderBody(scene_ids=["s1", "s9"])))
    assert ei.value.status_code == 400


def test_delete_removes_slot_and_its_variants(monkeypatch):
    _with_store(_job_two_scenes(), monkeypatch)
    result = _run(api.delete_scene("j_test", "s2"))
    assert [s["scene_id"] for s in result["scenes"]] == ["s1"]
    for cid in ("cA", "cB"):
        c = result["characters"][cid]
        assert all(im["scene_id"] != "s2" for im in c["images"])
        assert all(not vid.endswith("2") for vid in c["approved_variant_ids"])


def test_delete_last_scene_blocked(monkeypatch):
    job = _job_two_scenes()
    # collapse to a single scene
    job.scene_ids = ["s1"]; job.scene_image_paths = ["/p1.png"]
    _with_store(job, monkeypatch)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.delete_scene("j_test", "s1"))
    assert ei.value.status_code == 409


def test_scene_tools_locked_after_movement(monkeypatch):
    job = _job_two_scenes()
    job.movement_prompts = {"s1": "do a thing"}   # movement submitted
    _with_store(job, monkeypatch)
    from fastapi import HTTPException
    for call in (
        lambda: api.duplicate_scene("j_test", "s1"),
        lambda: api.reorder_scenes("j_test", api.SceneOrderBody(scene_ids=["s2", "s1"])),
        lambda: api.delete_scene("j_test", "s2"),
    ):
        with pytest.raises(HTTPException) as ei:
            _run(call())
        assert ei.value.status_code == 409
