"""Hugo 2026-06-12: replace a generated image with one created elsewhere.

POST /api/jobs/{id}/characters/{cid}/variants/upload drops an external image
in as a READY variant (qc_status='skipped' — the user chose it) and, by
default, auto-approves it for its scene, replacing any previous approval on
THAT scene only. Locks mirror retry_variant: plain Swap jobs freeze after
movement is submitted; reengineer-origin jobs stay editable.
"""
from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

from character_swap import api
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


def _job(*, origin: str | None = None, movement: str | None = None) -> Job:
    v1 = GeneratedImage(variant_id="v_old1", path="/old1.png", prompt="p",
                        scene_id="s1", status=VariantStatus.READY)
    v2 = GeneratedImage(variant_id="v_old2", path="/old2.png", prompt="p",
                        scene_id="s2", status=VariantStatus.READY)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/c.png",
                      status=CharStatus.AWAITING_APPROVAL, images=[v1, v2],
                      approved_variant_ids=["v_old1", "v_old2"],
                      approved_variant_id="v_old1")
    return Job(job_id="j1", title="t", scene_id="s1",
               scene_image_path="/s.png", scene_ids=["s1", "s2"],
               scene_image_paths=["/s.png", "/s2.png"],
               characters={"cA": jc}, origin=origin,
               movement_prompt=movement)


def _wire(monkeypatch, tmp_path, job):
    monkeypatch.setattr(api, "store", lambda: SimpleNamespace(
        get_job=lambda jid: job if jid == "j1" else None,
        update_job=lambda j: None,
        get_scene=lambda sid: None,
        get_character=lambda cid: None,
    ))
    monkeypatch.setattr(settings, "output_dir", tmp_path, raising=False)


def _upload(name="own.png", content=b"png-bytes", scene_id="s1",
            approve=True):
    return api.upload_own_variant(
        "j1", "cA",
        file=UploadFile(file=io.BytesIO(content), filename=name),
        scene_id=scene_id, approve=approve)


def test_upload_lands_ready_and_replaces_scene_approval(monkeypatch, tmp_path):
    job = _job()
    _wire(monkeypatch, tmp_path, job)

    out = asyncio.run(_upload(scene_id="s1"))
    assert out["ok"] is True
    jc = job.characters["cA"]
    new = next(v for v in jc.images if v.variant_id == out["variant_id"])
    assert new.status == VariantStatus.READY
    assert new.qc_status == "skipped"
    assert new.scene_id == "s1"
    assert (tmp_path / "j1" / "cA" / f"variant_{new.variant_id}.png"
            ).read_bytes() == b"png-bytes"
    # Approval on s1 replaced; s2's approval untouched.
    assert out["variant_id"] in jc.approved_variant_ids
    assert "v_old1" not in jc.approved_variant_ids
    assert "v_old2" in jc.approved_variant_ids
    assert jc.status == CharStatus.APPROVED


def test_upload_without_approve_keeps_existing_approvals(monkeypatch, tmp_path):
    job = _job()
    _wire(monkeypatch, tmp_path, job)
    out = asyncio.run(_upload(scene_id="s1", approve=False))
    jc = job.characters["cA"]
    assert out["variant_id"] not in (jc.approved_variant_ids or [])
    assert "v_old1" in jc.approved_variant_ids


def test_upload_locked_after_movement_on_plain_swap(monkeypatch, tmp_path):
    job = _job(movement="walk")
    _wire(monkeypatch, tmp_path, job)
    with pytest.raises(HTTPException) as e:
        asyncio.run(_upload())
    assert e.value.status_code == 409


def test_upload_allowed_for_reengineer_jobs_after_movement(monkeypatch, tmp_path):
    job = _job(origin="reengineer:re_t", movement="walk")
    _wire(monkeypatch, tmp_path, job)
    out = asyncio.run(_upload(scene_id="s2"))
    assert out["ok"] is True


def test_upload_rejects_non_image_and_unknown_scene(monkeypatch, tmp_path):
    job = _job()
    _wire(monkeypatch, tmp_path, job)
    with pytest.raises(HTTPException) as e:
        asyncio.run(_upload(name="movie.mp4"))
    assert e.value.status_code == 400
    with pytest.raises(HTTPException) as e:
        asyncio.run(_upload(scene_id="s_nope"))
    assert e.value.status_code == 400
