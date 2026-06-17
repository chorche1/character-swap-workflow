"""The Step-6 compile must default to capcut-purple-pill everywhere.

The UI default is capcut-purple-pill, but the backend used to default to
submagic-pro — so any compile path that didn't explicitly send a template (or a
stale client) fell back to the wrong style. Lock both backend defaults so they
stay in sync with the UI.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi import BackgroundTasks

from character_swap import api, runner_compile
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
    VideoStatus, VideoVariant,
)


def test_compile_body_default_template_is_purple_pill():
    assert api.CompileVideosBody().template == "capcut-purple-pill"


def test_compile_runner_default_template_is_purple_pill():
    default = inspect.signature(
        runner_compile.compile_job_videos
    ).parameters["template"].default
    assert default == "capcut-purple-pill"


# --- per-job compile settings (Hugo 2026-06-17) ----------------------------

class _FakeStore:
    def __init__(self, job: Job) -> None:
        self._job = job

    def get_job(self, jid):
        return self._job if jid == self._job.job_id else None

    def update_job(self, job: Job) -> None:
        self._job = job


def _eligible_job() -> Job:
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/c.png",
        status=CharStatus.APPROVED,
        images=[GeneratedImage(variant_id="vA", path="/a.png", prompt="p",
                               scene_id="s1", status=VariantStatus.READY)],
        approved_variant_ids=["vA"],
        videos=[VideoVariant(video_id="vid", grok_job_id="g", status=VideoStatus.DONE,
                             source_variant_id="vA", final_video_path="/v.mp4")])
    return Job(job_id="jc1", title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               movement_prompt="x", characters={"cA": jc})


def test_compile_persists_settings_on_job_and_exposes_them(monkeypatch):
    """The compile endpoint stores the ⚙ settings on the job (minus char_ids)
    so each job keeps its own editable preset, and _job_to_dict surfaces them
    for the frontend to rehydrate the panel per job."""
    store = _FakeStore(_eligible_job())
    monkeypatch.setattr(api, "store", lambda: store)
    monkeypatch.setattr(type(api.settings), "require_keys",
                        lambda self, *a, **k: None, raising=False)

    body = api.CompileVideosBody(threshold_db=-19.0, pad_secs=0.08,
                                 enable_gap_trim=True, gap_max_secs=0.5,
                                 char_ids=["cA"])
    out = asyncio.run(api.compile_job_videos("jc1", body, BackgroundTasks()))

    saved = store.get_job("jc1").compile_settings
    assert saved is not None
    assert saved["threshold_db"] == -19.0
    assert saved["pad_secs"] == 0.08
    assert saved["enable_gap_trim"] is True
    assert saved["gap_max_secs"] == 0.5
    assert "char_ids" not in saved          # per-click filter, not a setting
    assert "persist_settings" not in saved  # control flag, not a setting
    # Surfaced for the frontend.
    assert out["compile_settings"]["threshold_db"] == -19.0


def test_compile_resolve_oneoff_does_not_persist_settings(monkeypatch):
    """persist_settings=False (the Resolve one-off, which forces captions OFF)
    must NOT overwrite the job's remembered preset — else the panel silently
    reopens captions-off next session (review 2026-06-17)."""
    store = _FakeStore(_eligible_job())
    monkeypatch.setattr(api, "store", lambda: store)
    monkeypatch.setattr(type(api.settings), "require_keys",
                        lambda self, *a, **k: None, raising=False)

    body = api.CompileVideosBody(enable_captions=False, persist_settings=False)
    asyncio.run(api.compile_job_videos("jc1", body, BackgroundTasks()))
    assert store.get_job("jc1").compile_settings is None   # unchanged
