"""Restart repoll must route to the PER-CLIP effective video model.

2026-07-01 audit gap #5: `runner.resume_pending → _resume_video →
pipeline.wait_for_video(model=_eff_video_model(job, jc, video))` was the only
untested `_eff_video_model` call site (submit is covered by
test_per_clip_video_model.py; strand-marking by test_video_resume_and_orphans).
Per-clip overrides (`Job.video_models_by_scene`) mean a resumed kling-v3 /
veo-3.1-fast clip must be polled against ITS OWN provider's endpoint — a
regression here makes a restart mid-animation poll the wrong provider, the
fal request_id 404s, and the clip errors even though the render finished and
was billed.
"""
from __future__ import annotations

import asyncio

from character_swap import runner
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus, VideoStatus,
    VideoVariant,
)


class _FakeStore:
    """update_job-only store (like the JSON backend) — _persist/_replace_video
    fall back to the full update path on it."""

    def __init__(self, job: Job):
        self.job = job

    def get_job(self, jid):
        return self.job if jid == self.job.job_id else None

    def update_job(self, job):
        self.job = job


def _two_scene_inflight_job(**overrides) -> Job:
    """One char, two scenes; one IN-FLIGHT clip per scene (has a provider id,
    non-terminal) — the exact shape resume_pending re-polls."""
    imgs = [
        GeneratedImage(variant_id="v_a", path="/tmp/a.png", prompt="(p)",
                       scene_id="scA", status=VariantStatus.READY),
        GeneratedImage(variant_id="v_b", path="/tmp/b.png", prompt="(p)",
                       scene_id="scB", status=VariantStatus.READY),
    ]
    vids = [
        VideoVariant(video_id="vid_a", grok_job_id="req_a",
                     status=VideoStatus.PROCESSING, source_variant_id="v_a"),
        VideoVariant(video_id="vid_b", grok_job_id="req_b",
                     status=VideoStatus.PROCESSING, source_variant_id="v_b"),
    ]
    jc = JobCharacter(char_id="c1", name="T", source_image_path="/tmp/a.png",
                      status=CharStatus.ANIMATING, images=imgs, videos=vids,
                      approved_variant_ids=["v_a", "v_b"],
                      approved_variant_id="v_a")
    overrides.setdefault("video_model", "grok-imagine")
    return Job(job_id="j_resume", title="t", scene_id="scA",
               scene_image_path="/tmp/s.png",
               scene_ids=["scA", "scB"],
               scene_image_paths=["/tmp/a.png", "/tmp/b.png"],
               movement_prompt="go",
               movement_prompts={"scA": "go", "scB": "go"},
               characters={"c1": jc}, **overrides)


def _resume_and_drain(job_id: str) -> None:
    async def main():
        await runner.resume_pending(job_id)
        # _resume_video runs via create_task — wait for every spawned task.
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(main())


def _capture_wait(monkeypatch) -> dict[str, str | None]:
    """provider request id → the `model` kwarg wait_for_video was polled with."""
    polled: dict[str, str | None] = {}

    def fake_wait(*, job_id, character_name, dest, on_progress=None,
                  app_job_id=None, model=None, **kw):
        polled[job_id] = model
        return {"status": "done"}

    monkeypatch.setattr(runner.pipeline, "wait_for_video", fake_wait)
    return polled


def test_resume_polls_per_scene_override_model(monkeypatch):
    """Job default grok-imagine, scB overridden to kling-v3: the resumed scB
    clip must poll kling-v3 (fal) while its sibling on scA polls the default."""
    job = _two_scene_inflight_job(video_models_by_scene={"scB": "kling-v3"})
    monkeypatch.setattr(runner, "store", lambda: _FakeStore(job))
    polled = _capture_wait(monkeypatch)

    _resume_and_drain("j_resume")

    assert polled == {"req_a": "grok-imagine", "req_b": "kling-v3"}
    # Both clips finished the resume happy path.
    jc = job.characters["c1"]
    assert {v.status for v in jc.videos} == {VideoStatus.DONE}
    assert jc.status == CharStatus.DONE


def test_resume_without_override_polls_job_default(monkeypatch):
    """Back-compat: no per-scene dict → every resumed clip polls the job-wide
    default provider."""
    job = _two_scene_inflight_job()
    assert job.video_models_by_scene == {}
    monkeypatch.setattr(runner, "store", lambda: _FakeStore(job))
    polled = _capture_wait(monkeypatch)

    _resume_and_drain("j_resume")

    assert polled == {"req_a": "grok-imagine", "req_b": "grok-imagine"}


def test_resume_legacy_none_scene_variant_uses_job_default(monkeypatch):
    """A legacy clip whose approved image has scene_id=None must resolve to
    the job default — never KeyError into a per-scene dict."""
    img = GeneratedImage(variant_id="v_l", path="/tmp/l.png", prompt="(p)",
                         scene_id=None, status=VariantStatus.READY)
    vid = VideoVariant(video_id="vid_l", grok_job_id="req_l",
                       status=VideoStatus.PROCESSING, source_variant_id="v_l")
    jc = JobCharacter(char_id="c1", name="T", source_image_path="/tmp/l.png",
                      status=CharStatus.ANIMATING, images=[img], videos=[vid],
                      approved_variant_ids=["v_l"], approved_variant_id="v_l")
    job = Job(job_id="j_resume", title="t", scene_id="s1",
              scene_image_path="/tmp/s.png", movement_prompt="go",
              video_model="veo-3.1-fast",
              video_models_by_scene={"s1": "kling-v3"},   # keyed by scene_id;
              characters={"c1": jc})                      # None must NOT match
    monkeypatch.setattr(runner, "store", lambda: _FakeStore(job))
    polled = _capture_wait(monkeypatch)

    _resume_and_drain("j_resume")

    assert polled == {"req_l": "veo-3.1-fast"}
