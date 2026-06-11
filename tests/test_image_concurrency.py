"""Per-provider image concurrency + semaphore released during QC.

The single global IMAGE_CONCURRENCY=2 semaphore was the measured wall-clock
bottleneck for Reengineer runs (effective concurrency 1.78 on a 65-image
burst). The runner now sizes the semaphore per PROVIDER of the job's
effective swap model, and holds a lane only for the generation call itself —
QC and retry bookkeeping run with the lane released.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from character_swap import runner
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)
from character_swap.swap_qc import QCVerdict


# ------------------------------------------------- per-provider resolution

@pytest.mark.parametrize("slug,expected", [
    ("nbp-swap", 8),            # fal
    ("nb2-swap", 8),            # fal
    ("seedream-edit-swap", 8),  # fal
    ("gpt-image", 4),           # openai
    ("gpt2-id-swap", 4),        # openai
    ("nano-banana-pro", 3),     # gemini
    ("grok-image", 2),          # xai → global fallback
    ("totally-unknown", 2),     # unknown slug → global fallback
])
def test_concurrency_resolves_per_provider(monkeypatch, slug, expected):
    """Provider → setting RESOLUTION (settings pinned — the dev .env may
    override the shipped defaults, e.g. Hugo runs OPENAI=8/FAL=10)."""
    for name, val in (("image_concurrency_fal", 8),
                      ("image_concurrency_openai", 4),
                      ("image_concurrency_gemini", 3),
                      ("image_concurrency", 2)):
        monkeypatch.setattr(type(settings), name,
                            property(lambda self, v=val: v), raising=False)
    assert runner._image_concurrency_for_model(slug) == expected


def test_concurrency_env_override(monkeypatch):
    monkeypatch.setattr(type(settings), "image_concurrency_fal",
                        property(lambda self: 3), raising=False)
    assert runner._image_concurrency_for_model("nbp-swap") == 3


def test_concurrency_never_below_one(monkeypatch):
    monkeypatch.setattr(type(settings), "image_concurrency_openai",
                        property(lambda self: 0), raising=False)
    # 0 is falsy → falls back to the global setting, floored at 1.
    monkeypatch.setattr(type(settings), "image_concurrency",
                        property(lambda self: 0), raising=False)
    assert runner._image_concurrency_for_model("gpt-image") == 1


# ------------------------------------------------- semaphore vs QC overlap

def _job(tmp_path, n_variants=2):
    variants, chars = [], {}
    for i in range(n_variants):
        dest = tmp_path / f"variant_v{i}.png"
        variants.append(GeneratedImage(
            variant_id=f"v{i}", path=str(dest), prompt="P",
            scene_id="s1", status=VariantStatus.GENERATING))
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/char.png",
                      status=CharStatus.GENERATING, images=variants)
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path="/scene.png", scene_ids=["s1"],
              scene_image_paths=["/scene.png"], characters={"cA": jc})
    return job, jc, variants


def _quiet(monkeypatch):
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_variant", lambda *a, **k: None)
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(runner, "_scene_path_for_variant",
                        lambda j, v: Path("/scene.png"))


def test_semaphore_released_during_qc(monkeypatch, tmp_path):
    """With Semaphore(1), variant #2's GENERATION must start while variant
    #1 is still inside QC — impossible before (QC held the lane)."""
    job, jc, variants = _job(tmp_path, n_variants=2)
    _quiet(monkeypatch)

    gen_started: list[str] = []
    v1_in_qc = threading.Event()
    release_qc = threading.Event()

    def fake_gen(**kw):
        gen_started.append(kw["dest"])
        Path(kw["dest"]).write_bytes(b"img")
    monkeypatch.setattr(runner.pipeline, "generate_variant", fake_gen)

    def fake_qc(**kw):
        if kw["result_image"] == Path(variants[0].path) and not v1_in_qc.is_set():
            v1_in_qc.set()
            assert release_qc.wait(timeout=5), "test deadlock: QC never released"
        return QCVerdict(True, "", "")
    monkeypatch.setattr(runner.swap_qc, "inspect_variant", fake_qc)

    async def run():
        sem = asyncio.Semaphore(1)
        t1 = asyncio.create_task(
            runner._generate_one_variant(job, jc, variants[0], sem))
        t2 = asyncio.create_task(
            runner._generate_one_variant(job, jc, variants[1], sem))
        # Wait until v1 is parked inside QC (lane must be free now).
        await asyncio.to_thread(v1_in_qc.wait, 5)
        # Give t2 a chance to grab the lane and start generating.
        for _ in range(50):
            if len(gen_started) >= 2:
                break
            await asyncio.sleep(0.02)
        overlap = len(gen_started) >= 2
        release_qc.set()
        await asyncio.gather(t1, t2)
        return overlap

    assert asyncio.run(run()), "generation #2 never started while #1 was in QC"
    assert variants[0].status == VariantStatus.READY
    assert variants[1].status == VariantStatus.READY


def test_retry_reacquires_semaphore(monkeypatch, tmp_path):
    """A QC-failed slot re-enters the semaphore queue for its retry: with
    Semaphore(1) we must see one acquisition per generation attempt."""
    job, jc, variants = _job(tmp_path, n_variants=1)
    _quiet(monkeypatch)
    v = variants[0]

    acquisitions = 0

    class CountingSem(asyncio.Semaphore):
        async def __aenter__(self):
            nonlocal acquisitions
            acquisitions += 1
            return await super().__aenter__()

    def fake_gen(**kw):
        Path(kw["dest"]).write_bytes(b"img")
    monkeypatch.setattr(runner.pipeline, "generate_variant", fake_gen)
    verdicts = iter([QCVerdict(False, "wrong person", "fix"),
                     QCVerdict(True, "", "")])
    monkeypatch.setattr(runner.swap_qc, "inspect_variant",
                        lambda **kw: next(verdicts))

    asyncio.run(runner._generate_one_variant(job, jc, v, CountingSem(1)))
    assert v.status == VariantStatus.READY
    assert v.qc_attempts == 2
    assert acquisitions == 2
