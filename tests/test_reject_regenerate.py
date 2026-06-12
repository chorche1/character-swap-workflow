"""Reject & regenerate a READY variant (Hugo, 2026-06-11).

The per-variant retry endpoint used to refuse anything but FAILED slots —
a ready-but-wrong image (wrong character/clothes/background) could only be
fixed via ✎ edit or a full scene regen. Now READY slots are accepted as a
"reject & regenerate": the slot re-rolls in place and any approval of the
rejected image is withdrawn first (the new image must be re-approved).
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


def _job(status: VariantStatus, *, approved: bool = False,
         char_status: CharStatus = CharStatus.AWAITING_APPROVAL):
    v = GeneratedImage(variant_id="v1", path="/v1.png", prompt="P",
                       scene_id="s1", status=status)
    other = GeneratedImage(variant_id="v2", path="/v2.png", prompt="P",
                           scene_id="s2", status=VariantStatus.READY)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/a.png",
                      status=char_status, images=[v, other],
                      approved_variant_ids=(["v1"] if approved else []),
                      approved_variant_id=("v1" if approved else None))
    job = Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
              characters={"cA": jc})
    return job, jc


@pytest.fixture
def fake_store(monkeypatch):
    holder = {}

    class _S:
        def get_job(self, jid):
            return holder.get("job") if jid == "j1" else None

        def update_job(self, j):
            holder["updated"] = True

    monkeypatch.setattr(api, "store", lambda: _S())
    return holder


def _call(job, holder):
    holder["job"] = job
    bg = BackgroundTasks()
    asyncio.run(api.retry_variant("j1", "cA", "v1", bg))
    return bg


def test_ready_variant_can_be_rejected_and_regenerated(fake_store):
    job, jc = _job(VariantStatus.READY)
    bg = _call(job, fake_store)
    assert len(bg.tasks) == 1          # regen scheduled


def test_reject_withdraws_approval_and_rearms_gate(fake_store):
    job, jc = _job(VariantStatus.READY, approved=True,
                   char_status=CharStatus.APPROVED)
    bg = _call(job, fake_store)
    assert len(bg.tasks) == 1
    assert jc.approved_variant_ids == []
    assert jc.approved_variant_id is None
    assert jc.status == CharStatus.AWAITING_APPROVAL
    assert fake_store.get("updated")


def test_reject_keeps_other_approvals_and_status(fake_store):
    job, jc = _job(VariantStatus.READY, approved=True,
                   char_status=CharStatus.APPROVED)
    jc.approved_variant_ids = ["v1", "v2"]
    bg = _call(job, fake_store)
    assert len(bg.tasks) == 1
    assert jc.approved_variant_ids == ["v2"]
    assert jc.approved_variant_id == "v2"
    assert jc.status == CharStatus.APPROVED   # still has an approved image


def test_failed_variant_still_retryable(fake_store):
    job, jc = _job(VariantStatus.FAILED, char_status=CharStatus.FAILED)
    bg = _call(job, fake_store)
    assert len(bg.tasks) == 1


def test_generating_variant_is_refused(fake_store):
    job, jc = _job(VariantStatus.GENERATING)
    fake_store["job"] = job
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.retry_variant("j1", "cA", "v1", BackgroundTasks()))
    assert e.value.status_code == 409


def test_locked_job_is_refused(fake_store):
    job, jc = _job(VariantStatus.READY)
    job.movement_prompt = "already animating"
    fake_store["job"] = job
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.retry_variant("j1", "cA", "v1", BackgroundTasks()))
    assert e.value.status_code == 409


def test_retry_with_altered_prompt_forwards_override(fake_store):
    """Hugo 2026-06-12: the ✎↻ modal regenerates with an EDITED prompt —
    the override must reach runner.retry_single_variant (which persists it
    on the slot so the next ✎↻ pre-fills the last iteration)."""
    job, jc = _job(VariantStatus.READY)
    fake_store["job"] = job
    bg = BackgroundTasks()
    asyncio.run(api.retry_variant(
        "j1", "cA", "v1", bg,
        body=api.RetryVariantBody(prompt="white shirt, no scrubs")))
    assert len(bg.tasks) == 1
    # BackgroundTasks wraps (_run_async, retry_single_variant, *args) —
    # the LAST positional arg is the prompt override.
    assert bg.tasks[0].args[-1] == "white shirt, no scrubs"


def test_img_regen_modal_ui_wired():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    js = (root / "web" / "app.js").read_text(encoding="utf-8")
    assert "imgRegenModal" in js
    assert "openImgRegenModal(jobId, charId, v, reRun = null)" in js
    sub = js.split("async submitImgRegen()")[1][:900]
    assert "/retry" in sub and "prompt:" in sub
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert "openImgRegenModal(r.job_id, cid, v, r)" in html   # ✎↻ in strip
    assert "submitImgRegen()" in html                         # modal action
