"""POST /api/jobs/{job_id}/approve_all — bulk approval (Step 3 "✓ Approve all").

2026-07-01 audit gap #10: this endpoint directly gates the expensive video
phase — it auto-picks "the first READY variant per (character, scene)" with
legacy scene_id=None mapping and REJECTED/ANIMATING/DONE skips, and had ZERO
tests. A regression that picks a FAILED/GENERATING variant, overwrites a
manual pick, or double-approves would kick off wrong/broken clips across ALL
characters × scenes in one click.

Locked here:
  - first READY per (char, scene) — FAILED/GENERATING slots never picked;
  - a scene that already has a manual approval is left untouched;
  - REJECTED / ANIMATING / DONE characters are skipped entirely;
  - legacy variants with scene_id=None map to the first scene (both for
    coverage detection via the singular approved_variant_id AND for picking);
  - idempotent: second call returns approved == [] and changes nothing;
  - 409 after the movement prompt is submitted.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from character_swap import api
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
)


def _run(coro):
    return asyncio.run(coro)


class _FakeStore:
    def __init__(self, job: Job) -> None:
        self.job = job
        self.updates = 0

    def get_job(self, job_id: str) -> Job | None:
        return self.job if job_id == self.job.job_id else None

    def update_job(self, job: Job) -> None:
        self.job = job
        self.updates += 1


def _img(vid: str, scene_id: str | None, status: VariantStatus) -> GeneratedImage:
    return GeneratedImage(variant_id=vid, path=f"/tmp/{vid}.png", prompt="(p)",
                          scene_id=scene_id, status=status)


def _two_scene_job() -> Job:
    """2 scenes × 2 chars.

    Char A (awaiting approval):
      - s1: FAILED, READY(v_a1), READY(v_a1b)  → approve_all must pick v_a1
      - s2: READY(v_a2) manually pre-approved  → must stay untouched
    Char B (READY variants everywhere but ANIMATING) → skipped entirely.
    """
    jc_a = JobCharacter(
        char_id="cA", name="A", source_image_path="/tmp/a.png",
        status=CharStatus.AWAITING_APPROVAL,
        images=[
            _img("v_a1_fail", "s1", VariantStatus.FAILED),
            _img("v_a1", "s1", VariantStatus.READY),
            _img("v_a1b", "s1", VariantStatus.READY),
            _img("v_a2", "s2", VariantStatus.READY),
        ],
        approved_variant_ids=["v_a2"], approved_variant_id="v_a2",
    )
    jc_b = JobCharacter(
        char_id="cB", name="B", source_image_path="/tmp/b.png",
        status=CharStatus.ANIMATING,
        images=[
            _img("v_b1", "s1", VariantStatus.READY),
            _img("v_b2", "s2", VariantStatus.READY),
        ],
    )
    return Job(job_id="j_bulk", title="t", scene_id="s1",
               scene_image_path="/tmp/s1.png",
               scene_ids=["s1", "s2"],
               scene_image_paths=["/tmp/s1.png", "/tmp/s2.png"],
               characters={"cA": jc_a, "cB": jc_b})


@pytest.fixture
def job_and_store(monkeypatch) -> tuple[Job, _FakeStore]:
    job = _two_scene_job()
    fake = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: fake)
    return job, fake


# --- picks first READY, only where missing, never a manual pick ----------------------


def test_picks_first_ready_per_uncovered_scene_only(job_and_store):
    job, fake = job_and_store
    out = _run(api.approve_all("j_bulk"))

    # Exactly ONE pick: char A's s1 slot. s2 already had a manual approval,
    # char B is animating.
    assert out["approved"] == [
        {"char_id": "cA", "variant_id": "v_a1", "scene_id": "s1"},
    ]
    jc_a = job.characters["cA"]
    # First READY picked — never the FAILED slot before it, and only ONE of
    # the two READY s1 variants.
    assert jc_a.approved_variant_ids == ["v_a1", "v_a2"]
    # Manual s2 pick untouched; legacy singular mirrors the first entry.
    assert "v_a2" in jc_a.approved_variant_ids
    assert jc_a.approved_variant_id == "v_a1"
    assert jc_a.status == CharStatus.APPROVED
    assert fake.updates == 1


def test_animating_char_left_untouched(job_and_store):
    job, _ = job_and_store
    _run(api.approve_all("j_bulk"))
    jc_b = job.characters["cB"]
    assert jc_b.approved_variant_ids == []
    assert jc_b.approved_variant_id is None
    assert jc_b.status == CharStatus.ANIMATING


@pytest.mark.parametrize("skip_status", [CharStatus.REJECTED, CharStatus.DONE])
def test_rejected_and_done_chars_skipped(monkeypatch, skip_status):
    job = _two_scene_job()
    job.characters["cB"].status = skip_status
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))

    out = _run(api.approve_all("j_bulk"))
    assert all(p["char_id"] != "cB" for p in out["approved"])
    assert job.characters["cB"].approved_variant_ids == []
    assert job.characters["cB"].status == skip_status


def test_generating_and_failed_variants_never_picked(monkeypatch):
    """A scene whose only variants are FAILED/GENERATING gets NO pick — the
    char stays awaiting (nothing money-burning is auto-approved)."""
    jc = JobCharacter(
        char_id="cC", name="C", source_image_path="/tmp/c.png",
        status=CharStatus.AWAITING_APPROVAL,
        images=[
            _img("v_c1_fail", "s1", VariantStatus.FAILED),
            _img("v_c1_gen", "s1", VariantStatus.GENERATING),
        ],
    )
    job = Job(job_id="j_bulk", title="t", scene_id="s1",
              scene_image_path="/tmp/s1.png", characters={"cC": jc})
    fake = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: fake)

    out = _run(api.approve_all("j_bulk"))
    assert out["approved"] == []
    assert jc.approved_variant_ids == []
    assert jc.status == CharStatus.AWAITING_APPROVAL
    assert fake.updates == 0                       # nothing picked → no write


# --- legacy scene_id=None mapping -----------------------------------------------------


def test_legacy_none_scene_variant_picked_as_first_scene(monkeypatch):
    """Old single-scene jobs: variants carry scene_id=None and the job may
    have no scene_ids list. approve_all must treat those variants as
    belonging to scene_ids[0] and pick exactly one."""
    jc = JobCharacter(
        char_id="cL", name="L", source_image_path="/tmp/l.png",
        status=CharStatus.AWAITING_APPROVAL,
        images=[
            _img("v_l1", None, VariantStatus.READY),
            _img("v_l2", None, VariantStatus.READY),
        ],
    )
    job = Job(job_id="j_bulk", title="t", scene_id="s_legacy",
              scene_image_path="/tmp/s.png", characters={"cL": jc})
    assert job.scene_ids == []                     # true legacy shape
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))

    out = _run(api.approve_all("j_bulk"))
    assert out["approved"] == [
        {"char_id": "cL", "variant_id": "v_l1", "scene_id": "s_legacy"},
    ]
    assert jc.approved_variant_ids == ["v_l1"]     # ONE pick, not both


def test_legacy_singular_approval_counts_as_scene_coverage(monkeypatch):
    """A pre-multi-approval job that only has the SINGULAR
    approved_variant_id set (list empty) on a scene_id=None variant: that
    scene is already covered — approve_all must not double-approve it."""
    jc = JobCharacter(
        char_id="cL", name="L", source_image_path="/tmp/l.png",
        status=CharStatus.APPROVED,
        images=[
            _img("v_l1", None, VariantStatus.READY),
            _img("v_l2", None, VariantStatus.READY),
        ],
        approved_variant_ids=[], approved_variant_id="v_l1",
    )
    job = Job(job_id="j_bulk", title="t", scene_id="s_legacy",
              scene_image_path="/tmp/s.png", characters={"cL": jc})
    fake = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: fake)

    out = _run(api.approve_all("j_bulk"))
    assert out["approved"] == []
    assert jc.approved_variant_ids == []           # untouched
    assert jc.approved_variant_id == "v_l1"
    assert fake.updates == 0


# --- idempotency ----------------------------------------------------------------------


def test_second_call_is_idempotent(job_and_store):
    job, fake = job_and_store
    first = _run(api.approve_all("j_bulk"))
    assert len(first["approved"]) == 1
    snapshot = {cid: list(jc.approved_variant_ids)
                for cid, jc in job.characters.items()}

    second = _run(api.approve_all("j_bulk"))
    assert second["approved"] == []                # nothing left to pick
    assert {cid: list(jc.approved_variant_ids)
            for cid, jc in job.characters.items()} == snapshot
    assert fake.updates == 1                       # second call wrote nothing


# --- locks ----------------------------------------------------------------------------


def test_409_after_movement_submitted(monkeypatch):
    job = _two_scene_job()
    job.movement_prompt = "walks forward"
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))

    with pytest.raises(HTTPException) as ei:
        _run(api.approve_all("j_bulk"))
    assert ei.value.status_code == 409


def test_404_unknown_job(monkeypatch):
    monkeypatch.setattr(api, "store", lambda: _FakeStore(_two_scene_job()))
    with pytest.raises(HTTPException) as ei:
        _run(api.approve_all("j_missing"))
    assert ei.value.status_code == 404
