"""Early approval must survive sibling variants finishing (audit 2026-07-01).

POST /api/jobs/{id}/approve sets jc.status=APPROVED on the SAME shared
JobCharacter object the runner holds. The runner's per-variant completion
persist ("first READY variant flips char to AWAITING_APPROVAL so user can act
early") used to fire for ANY status != AWAITING_APPROVAL — so approving while
the character's OTHER variants were still rendering was silently reverted to
AWAITING_APPROVAL by the next variant to finish. approved_variant_ids
survived, so the UI kept showing green ✓ rings while set_movement /
run_video_synthesis (both filter on status == APPROVED) silently dropped the
character from Step 4 — and post-movement the approval lock made it
unrecoverable. The persist must never demote an APPROVED character.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from character_swap import runner
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


class _Store:
    """Granular-persist-capable fake (same surface as the SQLite store) so the
    test exercises the update_job_character fast path production uses."""

    def __init__(self, box: dict) -> None:
        self.box = box

    def update_job(self, job):
        self.box[job.job_id] = job

    def update_job_character(self, job, jc):
        self.box[job.job_id] = job

    def update_variant(self, job, jc, v):
        self.box[job.job_id] = job


def _job(tmp_path) -> tuple[Job, JobCharacter, GeneratedImage]:
    """One char, two variants: v1 already READY, v2 still rendering."""
    v1 = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                        prompt="p", scene_id="s1", status=VariantStatus.READY)
    v2 = GeneratedImage(variant_id="v2", path=str(tmp_path / "v2.png"),
                        prompt="p", scene_id="s1",
                        status=VariantStatus.GENERATING)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/char.png",
                      status=CharStatus.GENERATING, images=[v1, v2])
    # skip_qc: the status flow is under test, not the QC loop (covered in
    # test_swap_qc.py) — one generation attempt, no judging.
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path="/scene.png", scene_ids=["s1"],
              scene_image_paths=["/scene.png"], characters={"cA": jc},
              skip_qc=True)
    return job, jc, v2


def _wire(monkeypatch) -> dict:
    box: dict = {}
    monkeypatch.setattr(runner, "store", lambda: _Store(box))
    monkeypatch.setattr(runner.pipeline, "generate_variant",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"img"))

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(runner, "_scene_path_for_variant",
                        lambda j, v: Path("/scene.png"))
    return box


def test_ready_variant_never_demotes_approved_char(monkeypatch, tmp_path):
    """The audited race: user approves v1 (endpoint sets APPROVED + approvals
    on the shared object) while v2 renders → v2 completing must NOT flip the
    character back to AWAITING_APPROVAL."""
    job, jc, v2 = _job(tmp_path)
    _wire(monkeypatch)
    jc.status = CharStatus.APPROVED
    jc.approved_variant_ids = ["v1"]
    jc.approved_variant_id = "v1"

    asyncio.run(runner._generate_one_variant(job, jc, v2, asyncio.Semaphore(1)))

    assert v2.status == VariantStatus.READY
    assert jc.status == CharStatus.APPROVED          # NOT demoted
    assert jc.approved_variant_ids == ["v1"]         # approval intact
    assert jc.approved_variant_id == "v1"


def test_first_ready_variant_still_flips_generating_char(monkeypatch, tmp_path):
    """The act-early flow the persist exists for is unchanged: a GENERATING
    character flips to AWAITING_APPROVAL on its first READY variant."""
    job, jc, v2 = _job(tmp_path)
    _wire(monkeypatch)

    asyncio.run(runner._generate_one_variant(job, jc, v2, asyncio.Semaphore(1)))

    assert v2.status == VariantStatus.READY
    assert jc.status == CharStatus.AWAITING_APPROVAL


def test_regen_demoted_approved_char_is_restored_to_approved(monkeypatch, tmp_path):
    """The adjacent door (seam review 2026-07-02): regen_scene_variants
    demotes an APPROVED char to GENERATING while rebuilding one scene. The
    completion persist must land back on APPROVED (approvals survive), not
    AWAITING_APPROVAL — set_movement filters on status == APPROVED, so the
    old behavior silently dropped the char from Step 4."""
    job, jc, v2 = _job(tmp_path)
    _wire(monkeypatch)
    jc.status = CharStatus.GENERATING            # regen demotion
    jc.approved_variant_ids = ["v1"]             # approvals survived it
    jc.approved_variant_id = "v1"

    asyncio.run(runner._generate_one_variant(job, jc, v2, asyncio.Semaphore(1)))

    assert v2.status == VariantStatus.READY
    assert jc.status == CharStatus.APPROVED      # restored, not AWAITING
    assert jc.approved_variant_ids == ["v1"]
