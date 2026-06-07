"""Replace a Step-3 variant with an UPLOADED image (import-your-own).

When the app can't generate a variant (e.g. a content-policy block), the user can
upload their own image into that slot. The slot becomes READY + imported and the
character flips back to AWAITING_APPROVAL. Locked once movement is submitted.
Hermetic: direct endpoint call with a stub store + tmp output dir.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from starlette.datastructures import Headers, UploadFile

from character_swap import api
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


class _FakeStore:
    def __init__(self, job): self._job = job
    def get_job(self, jid): return self._job if jid == self._job.job_id else None
    def update_job(self, j): self._job = j


def _png() -> UploadFile:
    return UploadFile(filename="mine.png",
                      file=io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"my-image-bytes"),
                      headers=Headers({"content-type": "image/png"}))


def _job() -> Job:
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/a.png", status=CharStatus.FAILED,
        images=[GeneratedImage(variant_id="v1", path="/v1.png", prompt="p", scene_id="s1",
                               status=VariantStatus.FAILED, error="blocked")],
    )
    return Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
               scene_ids=["s1"], scene_image_paths=["/p.png"], characters={"cA": jc})


@pytest.fixture(autouse=True)
def _patch(monkeypatch, tmp_path):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(api.events, "publish", _noop)
    monkeypatch.setattr(settings, "output_dir", tmp_path / "out")


def _run(c):
    return asyncio.run(c)


def test_replace_variant_imports_file(monkeypatch):
    store = _FakeStore(_job())
    monkeypatch.setattr(api, "store", lambda: store)

    result = _run(api.replace_variant("j1", "cA", "v1", file=_png()))

    v = store.get_job("j1").characters["cA"].images[0]
    assert v.status == VariantStatus.READY
    assert v.imported is True
    assert v.error is None
    assert Path(v.path).exists() and "imported_v1" in v.path   # file written
    # failed character becomes approvable again
    assert store.get_job("j1").characters["cA"].status == CharStatus.AWAITING_APPROVAL
    # the imported flag is serialized for the UI badge
    assert result["characters"]["cA"]["images"][0]["imported"] is True


def test_replace_variant_locked_after_movement(monkeypatch):
    job = _job()
    job.movement_prompts = {"s1": "go"}      # movement submitted
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.replace_variant("j1", "cA", "v1", file=_png()))
    assert ei.value.status_code == 409
