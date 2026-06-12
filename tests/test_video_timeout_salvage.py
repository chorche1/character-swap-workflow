"""Backlog #22 (2026-06-12): timed-out clips are re-polled, not re-billed.

The local wait gives up after VIDEO_TIMEOUT_SECS (600s default) — inside
Kling's measured completion tail — while the provider job often finishes
minutes later. ↻ retry on such a clip now re-polls the EXISTING provider
job first (free); only if that fails does it fall back to the normal
fresh (billed) submit. Non-timeout errors and prompt-edited retries go
straight to fresh submit as before.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from character_swap import runner
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


def _setup(monkeypatch, tmp_path, *, error: str, grok_job_id: str = "prov_1"):
    variant = GeneratedImage(variant_id="var1", path=str(tmp_path / "v.png"),
                             prompt="p", scene_id="s1",
                             status=VariantStatus.READY)
    video = VideoVariant(video_id="vd1", grok_job_id=grok_job_id,
                         status=VideoStatus.ERROR, error=error,
                         source_variant_id="var1")
    jc = JobCharacter(char_id="c1", name="A", source_image_path="/a.png",
                      status=CharStatus.ANIMATING, images=[variant],
                      approved_variant_ids=["var1"], videos=[video])
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path="/s.png", scene_ids=["s1"],
              scene_image_paths=["/s.png"], characters={"c1": jc},
              movement_prompt="walk", movement_prompts={"s1": "walk"})

    monkeypatch.setattr(runner, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job,
                                                update_job=lambda j: None))
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_output_dir",
                        lambda jid, cid: tmp_path)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)

    fresh_submits: list = []

    async def fake_animate(job_, jc_, video_, *a, **kw):
        fresh_submits.append(video_.video_id)
    monkeypatch.setattr(runner, "_animate_one_video", fake_animate)
    return job, jc, video, fresh_submits


def test_timed_out_clip_is_salvaged_by_repoll(monkeypatch, tmp_path):
    job, jc, video, fresh = _setup(
        monkeypatch, tmp_path,
        error="fal Kling v3 job prov_1 timed out after 600s")
    polled: list = []

    def fake_wait(*, job_id, dest, **kw):
        polled.append(job_id)
        Path(dest).write_bytes(b"clip")
        return Path(dest)
    monkeypatch.setattr(runner.pipeline, "wait_for_video", fake_wait)

    asyncio.run(runner.retry_one_video("j1", "c1", "vd1"))

    assert polled == ["prov_1"]             # re-polled the EXISTING job
    assert fresh == []                      # no new billed submit
    assert jc.videos[0].video_id == "vd1"   # entry kept in place
    assert jc.videos[0].status == VideoStatus.DONE
    assert jc.videos[0].final_video_path.endswith("video_vd1.mp4")
    assert jc.videos[0].qc_status == "skipped"


def test_failed_salvage_falls_back_to_fresh_submit(monkeypatch, tmp_path):
    job, jc, video, fresh = _setup(
        monkeypatch, tmp_path,
        error="grok job timed out after 600s")

    def fake_wait(**kw):
        raise RuntimeError("request not found")
    monkeypatch.setattr(runner.pipeline, "wait_for_video", fake_wait)

    asyncio.run(runner.retry_one_video("j1", "c1", "vd1"))

    assert len(fresh) == 1                  # normal billed retry happened
    assert jc.videos[0].video_id != "vd1"   # fresh VideoVariant swapped in


def test_non_timeout_error_skips_salvage(monkeypatch, tmp_path):
    job, jc, video, fresh = _setup(
        monkeypatch, tmp_path, error="content policy rejection")
    called: list = []

    def fake_wait(**kw):
        called.append(1)
    monkeypatch.setattr(runner.pipeline, "wait_for_video", fake_wait)

    asyncio.run(runner.retry_one_video("j1", "c1", "vd1"))

    assert called == []                     # no salvage attempt
    assert len(fresh) == 1                  # straight to fresh submit


def test_prompt_edit_skips_salvage(monkeypatch, tmp_path):
    job, jc, video, fresh = _setup(
        monkeypatch, tmp_path, error="timed out after 600s")
    called: list = []

    def fake_wait(**kw):
        called.append(1)
    monkeypatch.setattr(runner.pipeline, "wait_for_video", fake_wait)

    asyncio.run(runner.retry_one_video("j1", "c1", "vd1",
                                       prompt_override="new prompt"))

    assert called == []                     # new prompt → new generation
    assert len(fresh) == 1
