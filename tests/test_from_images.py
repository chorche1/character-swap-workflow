"""Tests for POST /api/jobs/from_images — the Animate tab's job builder.

The Animate tab lets the user skip Swap Steps 1-3 (scene upload, character
pick, AI image gen, approval) and go straight to video. This endpoint turns
N uploaded finished images into a single-character job where every image is a
pre-approved scene slot in upload order — so the existing Step 4-6 video +
compile pipeline runs unchanged.

Hermetic: the endpoint is called directly (no HTTP server) with a stub store
and a tmp output dir, so nothing touches the real shared data store.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from starlette.datastructures import Headers, UploadFile

from character_swap import api
from character_swap.config import settings
from character_swap.models import CharStatus, VariantStatus


class _FakeStore:
    """Captures add_job so we can assert persistence without a real backend."""

    def __init__(self) -> None:
        self.jobs: dict = {}

    def add_job(self, job) -> None:
        self.jobs[job.job_id] = job


def _png_upload(name: str) -> UploadFile:
    """An UploadFile whose bytes the endpoint just writes to disk (it never
    decodes the image), so a PNG header + filler is enough."""
    return UploadFile(
        filename=name,
        file=io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"fake-png-bytes"),
        headers=Headers({"content-type": "image/png"}),
    )


def _run(coro):
    """Tiny sync→async bridge (the project avoids pytest-asyncio)."""
    return asyncio.run(coro)


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    fake = _FakeStore()
    monkeypatch.setattr(api, "store", lambda: fake)
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output")
    return fake


def test_creates_single_char_job_with_scenes_in_upload_order(isolated: _FakeStore) -> None:
    files = [_png_upload(f"img_{i}.png") for i in range(3)]
    result = _run(api.create_job_from_images(
        files=files, title="My reel", video_model="kling-v2-6"))

    assert result["title"] == "My reel"
    assert result["video_model"] == "kling-v2-6"

    # Three scene slots, in upload order.
    assert [s["scene_id"] for s in result["scenes"]] == ["seq_0", "seq_1", "seq_2"]

    # Exactly one character, already APPROVED so Step 4 unlocks immediately.
    chars = result["characters"]
    assert len(chars) == 1
    char = next(iter(chars.values()))
    assert char["status"] == CharStatus.APPROVED.value

    # One READY + pre-approved variant per scene, in order.
    imgs = char["images"]
    assert [im["scene_id"] for im in imgs] == ["seq_0", "seq_1", "seq_2"]
    assert all(im["status"] == VariantStatus.READY.value for im in imgs)
    assert len(char["approved_variant_ids"]) == 3
    assert {im["variant_id"] for im in imgs} == set(char["approved_variant_ids"])
    # Legacy single-pick field stays in sync (first variant).
    assert char["approved_variant_id"] == char["approved_variant_ids"][0]

    # Persisted exactly once.
    assert len(isolated.jobs) == 1


def test_unknown_video_model_falls_back_to_default(isolated: _FakeStore) -> None:
    result = _run(api.create_job_from_images(
        files=[_png_upload("a.png")], title=None, video_model="not-a-real-model"))
    # Falls back rather than 400ing — the user re-picks the model in Step 4.
    assert result["video_model"] in ("kling-v2-6", "grok-imagine")


def test_empty_file_list_is_rejected(isolated: _FakeStore) -> None:
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.create_job_from_images(files=[], title=None, video_model="kling-v2-6"))
    assert ei.value.status_code == 400


def test_variant_files_written_under_output_dir(isolated: _FakeStore) -> None:
    result = _run(api.create_job_from_images(
        files=[_png_upload("x.png"), _png_upload("y.png")],
        title="t", video_model="kling-v2-6"))
    char = next(iter(result["characters"].values()))
    # Each variant URL (/files/output/<rel>) maps to a real file on disk.
    for im in char["images"]:
        rel = im["url"].split("/files/output/")[-1]
        assert (settings.output_dir / rel).exists()


def test_title_falls_back_when_blank(isolated: _FakeStore) -> None:
    result = _run(api.create_job_from_images(
        files=[_png_upload("a.png")], title="   ", video_model="kling-v2-6"))
    # Auto-title (date-stamped) rather than empty.
    assert result["title"]
    # The synthetic character defaults its display name to "Sequence".
    char = next(iter(result["characters"].values()))
    assert char["name"] == "Sequence"
