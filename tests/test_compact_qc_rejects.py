"""compact_job must reclaim orphaned QC-reject sidecars (Hugo 2026-06-20).

When the user compacts a job, compact_job drops every non-approved variant row
and every non-DONE video row, deleting their files. The QC-reject preservation
feature snapshots each rejected take to a `<stem>.qcrejectN.png|.mp4` sidecar
(plus a legacy repair-mode `<stem>.qcfail.png`) recorded on
GeneratedImage.qc_rejects / VideoVariant.qc_rejects. Once the parent row is
purged those sidecars are never referenced again — so compact_job must delete
them too and count their bytes. Approved variants' / DONE videos' rejects stay
(they're still shown in the UI).

Hermetic: direct endpoint call with a stub store + real files in a tmp dir.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import api
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    QCReject,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


class _FakeStore:
    def __init__(self, job):
        self._job = job

    def get_job(self, jid):
        return self._job if jid == self._job.job_id else None

    def update_job(self, j):
        self._job = j


def _write(p: Path, payload: bytes) -> int:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    return len(payload)


def _run(c):
    return asyncio.run(c)


def test_compact_purges_orphan_qc_rejects_and_counts_them(monkeypatch, tmp_path):
    d = tmp_path / "j1" / "cA"

    # --- KEPT (approved) variant: file + reject survive, NOT counted ---
    keep_img = d / "variant_keep.png"
    keep_img_rej = d / "variant_keep.qcreject1.png"
    _write(keep_img, b"K" * 10)
    _write(keep_img_rej, b"k" * 5)

    # --- PURGED (non-approved) variant: file + ALL sidecars removed + counted.
    # The .qcfail.png is NOT in qc_rejects (legacy repair copy) — only the glob
    # catches it, proving the glob sweep works.
    drop_img = d / "variant_drop.png"
    drop_img_r1 = d / "variant_drop.qcreject1.png"
    drop_img_r2 = d / "variant_drop.qcreject2.png"
    drop_img_fail = d / "variant_drop.qcfail.png"
    freed = 0
    freed += _write(drop_img, b"D" * 100)
    freed += _write(drop_img_r1, b"d" * 20)
    freed += _write(drop_img_r2, b"d" * 21)
    freed += _write(drop_img_fail, b"f" * 22)

    # --- KEPT (DONE) video: file + reject survive, NOT counted ---
    keep_vid = d / "video_keep.mp4"
    keep_vid_rej = d / "video_keep.qcreject1.mp4"
    _write(keep_vid, b"V" * 200)
    _write(keep_vid_rej, b"v" * 30)

    # --- PURGED (failed) video: file + reject removed + counted ---
    drop_vid = d / "video_drop.mp4"
    drop_vid_rej = d / "video_drop.qcreject1.mp4"
    freed += _write(drop_vid, b"X" * 300)
    freed += _write(drop_vid_rej, b"x" * 40)

    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/a.png", status=CharStatus.DONE,
        approved_variant_ids=["variant_keep"],
        images=[
            GeneratedImage(
                variant_id="variant_keep", path=str(keep_img), prompt="p",
                scene_id="s1", status=VariantStatus.READY,
                qc_rejects=[QCReject(path=str(keep_img_rej), kind="swap")],
            ),
            GeneratedImage(
                variant_id="variant_drop", path=str(drop_img), prompt="p",
                scene_id="s1", status=VariantStatus.READY,
                qc_rejects=[
                    QCReject(path=str(drop_img_r1), kind="swap"),
                    QCReject(path=str(drop_img_r2), kind="swap"),
                ],
            ),
        ],
        videos=[
            VideoVariant(
                video_id="video_keep", grok_job_id="g1", status=VideoStatus.DONE,
                final_video_path=str(keep_vid),
                qc_rejects=[QCReject(path=str(keep_vid_rej), kind="video")],
            ),
            VideoVariant(
                video_id="video_drop", grok_job_id="g2", status=VideoStatus.FAILED,
                final_video_path=str(drop_vid),
                qc_rejects=[QCReject(path=str(drop_vid_rej), kind="video")],
            ),
        ],
    )
    job = Job(job_id="j1", title="t", scene_id="s1", scene_image_path="/p.png",
              scene_ids=["s1"], scene_image_paths=["/p.png"], characters={"cA": jc})

    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)

    result = _run(api.compact_job("j1"))

    assert result["ok"] is True
    assert result["bytes_freed"] == freed

    # Approved variant + its reject survive (still shown in the UI).
    assert keep_img.exists() and keep_img_rej.exists()
    # DONE video + its reject survive.
    assert keep_vid.exists() and keep_vid_rej.exists()

    # Purged variant + EVERY sidecar (incl. the unrecorded .qcfail.png) gone.
    assert not drop_img.exists()
    assert not drop_img_r1.exists()
    assert not drop_img_r2.exists()
    assert not drop_img_fail.exists()
    # Purged video + its reject gone.
    assert not drop_vid.exists()
    assert not drop_vid_rej.exists()

    # Rows actually dropped.
    saved = store.get_job("j1").characters["cA"]
    assert [v.variant_id for v in saved.images] == ["variant_keep"]
    assert [vv.video_id for vv in saved.videos] == ["video_keep"]
    assert job.compacted is True


def test_compact_purges_rejects_when_final_video_path_unset(monkeypatch, tmp_path):
    """A clip can error before its final_video_path (the glob stem) is set, yet
    still have a recorded reject sidecar on disk. The recorded-path sweep must
    catch it even though there's no stem to glob from."""
    d = tmp_path / "j2" / "cB"
    err_rej = d / "video_err.qcreject1.mp4"
    freed = _write(err_rej, b"e" * 50)

    jc = JobCharacter(
        char_id="cB", name="B", source_image_path="/b.png", status=CharStatus.FAILED,
        videos=[
            VideoVariant(
                video_id="video_err", grok_job_id="g9", status=VideoStatus.ERROR,
                final_video_path=None,
                qc_rejects=[QCReject(path=str(err_rej), kind="video")],
            ),
        ],
    )
    job = Job(job_id="j2", title="t", scene_id="s1", scene_image_path="/p.png",
              scene_ids=["s1"], scene_image_paths=["/p.png"], characters={"cB": jc})

    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)

    result = _run(api.compact_job("j2"))

    assert result["bytes_freed"] == freed
    assert not err_rej.exists()
    assert store.get_job("j2").characters["cB"].videos == []
