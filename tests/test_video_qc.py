"""Clip-QC pure parts + the runner's generate→QC→retry loops (hermetic)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import runner, swap_qc, video_qc
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


# --------------------------------------------------------------------- pure parts

def test_expected_speech_extracts_quoted_dialogue():
    p = ('Hand-held shot. The person says: "add a teaspoon of baking soda" — '
         'casual delivery. The person says: "stir it well" at the end.')
    assert video_qc.expected_speech(p) == "add a teaspoon of baking soda stir it well"


def test_expected_speech_handles_smart_quotes_and_absence():
    assert video_qc.expected_speech('says: “blend that whole squad”') == "blend that whole squad"
    assert video_qc.expected_speech("no dialogue here") == ""
    assert video_qc.expected_speech("") == ""


def test_speech_similarity_catches_garbled_words():
    ok = video_qc.speech_similarity("add a teaspoon of baking soda",
                                    "add a teaspoon of baking soda")
    garbled = video_qc.speech_similarity("add a teaspoon of baking soda",
                                         "add a teaspoon of baking goda")
    assert ok == 1.0
    assert garbled < 1.0
    # Single-letter slips stay above a total-mismatch:
    assert garbled > video_qc.speech_similarity("add a teaspoon of baking soda",
                                                "completely different words")


def test_repair_prompt_mentions_hint_and_minimal_change():
    p = swap_qc.repair_prompt("the face does not match the reference")
    assert "the face does not match the reference" in p
    assert "as little" in p.lower()


# --------------------------------------------------------------- image QC loop

def _job_one_variant(tmp_path, model="nbp-swap"):
    v = GeneratedImage(variant_id="v1", path=str(tmp_path / "v1.png"),
                       prompt="BASE PROMPT", scene_id="s1",
                       status=VariantStatus.GENERATING)
    jc = JobCharacter(char_id="cA", name="A",
                      source_image_path=str(tmp_path / "char.png"),
                      status=CharStatus.QUEUED, images=[v])
    scene = tmp_path / "scene.png"; scene.write_bytes(b"scene")
    (tmp_path / "char.png").write_bytes(b"char")
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path=str(scene), scene_ids=["s1"],
              scene_image_paths=[str(scene)], image_model=model,
              characters={"cA": jc})
    return job, jc, v


def _stub_runner_persistence(monkeypatch, tmp_path=None):
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_replace_variant", lambda *a, **k: None)
    async def _noop(*a, **k): return None
    monkeypatch.setattr(runner, "_emit", _noop)
    if tmp_path is not None:
        monkeypatch.setattr(runner, "_output_dir",
                            lambda job_id, char_id: tmp_path)


def test_image_qc_fail_then_repair_pass(monkeypatch, tmp_path):
    """First QC failure → repair mode (failed image becomes the scene input,
    repair prompt); pass on attempt 2 → qc_status passed."""
    job, jc, v = _job_one_variant(tmp_path)
    _stub_runner_persistence(monkeypatch)
    gen_calls = []
    def fake_gen(**kw):
        gen_calls.append(kw)
        Path(kw["dest"]).write_bytes(b"img%d" % len(gen_calls))
    monkeypatch.setattr(runner.pipeline, "generate_variant", fake_gen)
    verdicts = [swap_qc.QCVerdict(False, "wrong person", "match the reference face"),
                swap_qc.QCVerdict(True, "", "")]
    monkeypatch.setattr(runner.swap_qc, "inspect_variant",
                        lambda **kw: verdicts.pop(0))

    asyncio.run(runner._generate_one_variant(job, jc, v, asyncio.Semaphore(1)))

    assert v.status == VariantStatus.READY
    assert v.qc_status == "passed"
    assert v.qc_attempts == 2
    assert len(gen_calls) == 2
    # Attempt 1: original scene + base prompt.
    assert gen_calls[0]["scene_image"] == Path(job.scene_image_path)
    assert gen_calls[0]["prompt"] == "BASE PROMPT"
    # Attempt 2 = REPAIR: the PRESERVED reject snapshot becomes the scene input
    # (Hugo 2026-06-20 — the rejected image is kept, not overwritten) + repair
    # prompt with the hint.
    assert ".qcreject" in str(gen_calls[1]["scene_image"])
    assert "match the reference face" in gen_calls[1]["prompt"]
    assert "as little" in gen_calls[1]["prompt"].lower()
    # The rejected attempt-1 image is preserved on disk + recorded.
    assert len(v.qc_rejects) == 1
    assert v.qc_rejects[0].reason == "wrong person"
    assert v.qc_rejects[0].attempt == 1
    assert Path(v.qc_rejects[0].path).exists()


def test_image_qc_exhausted_keeps_image_flagged(monkeypatch, tmp_path):
    job, jc, v = _job_one_variant(tmp_path)
    _stub_runner_persistence(monkeypatch)
    monkeypatch.setattr(runner.pipeline, "generate_variant",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"x"))
    monkeypatch.setattr(runner.swap_qc, "inspect_variant",
                        lambda **kw: swap_qc.QCVerdict(False, "still wrong", "fix"))
    monkeypatch.setattr(type(runner.settings), "swap_qc_max_retries",
                        property(lambda self: 1), raising=False)

    asyncio.run(runner._generate_one_variant(job, jc, v, asyncio.Semaphore(1)))

    assert v.status == VariantStatus.READY      # kept, never destroyed
    assert v.qc_status == "failed"
    assert v.qc_reason == "still wrong"
    assert v.qc_attempts == 2                   # 1 + 1 retry


def test_image_qc_unavailable_skips(monkeypatch, tmp_path):
    job, jc, v = _job_one_variant(tmp_path)
    _stub_runner_persistence(monkeypatch)
    gen_calls = []
    monkeypatch.setattr(runner.pipeline, "generate_variant",
                        lambda **kw: (gen_calls.append(1),
                                      Path(kw["dest"]).write_bytes(b"x")))
    monkeypatch.setattr(runner.swap_qc, "inspect_variant", lambda **kw: None)

    asyncio.run(runner._generate_one_variant(job, jc, v, asyncio.Semaphore(1)))

    assert v.qc_status == "skipped"
    assert len(gen_calls) == 1                  # no retries when QC is off


def test_grok_repair_skipped_falls_back_to_reroll(monkeypatch, tmp_path):
    """grok-image is text-only — repair mode would be ignored, so the first
    retry re-rolls with the hint appended instead."""
    job, jc, v = _job_one_variant(tmp_path, model="grok-image")
    _stub_runner_persistence(monkeypatch)
    gen_calls = []
    def fake_gen(**kw):
        gen_calls.append(kw)
        Path(kw["dest"]).write_bytes(b"x")
    monkeypatch.setattr(runner.pipeline, "generate_variant", fake_gen)
    verdicts = [swap_qc.QCVerdict(False, "wrong person", "fix the face"),
                swap_qc.QCVerdict(True, "", "")]
    monkeypatch.setattr(runner.swap_qc, "inspect_variant",
                        lambda **kw: verdicts.pop(0))

    asyncio.run(runner._generate_one_variant(job, jc, v, asyncio.Semaphore(1)))

    assert ".qcreject" not in str(gen_calls[1]["scene_image"])
    assert "fix the face" in gen_calls[1]["prompt"]
    assert gen_calls[1]["prompt"].startswith("BASE PROMPT")
    # Even on a re-roll the rejected take is still preserved for review.
    assert len(v.qc_rejects) == 1
    assert Path(v.qc_rejects[0].path).exists()


# --------------------------------------------------------------- video QC loop

def test_video_qc_retry_then_pass(monkeypatch, tmp_path):
    from character_swap.models import VideoStatus, VideoVariant
    job, jc, v_img = _job_one_variant(tmp_path)
    v_img.status = VariantStatus.READY
    jc.approved_variant_ids = ["v1"]; jc.approved_variant_id = "v1"
    Path(v_img.path).write_bytes(b"img")
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]
    _stub_runner_persistence(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)

    submits = []
    monkeypatch.setattr(runner.pipeline, "submit_video",
                        lambda **kw: (submits.append(kw), f"req-{len(submits)}")[1])
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))
    verdicts = [video_qc.ClipVerdict(False, 'heard "baking goda"',
                                     'say exactly: "baking soda"'),
                video_qc.ClipVerdict(True, "", "")]
    monkeypatch.setattr(runner.video_qc, "inspect_clip",
                        lambda *a, **kw: verdicts.pop(0))

    asyncio.run(runner._animate_one_video(job, jc, video, "P says: \"baking soda\""))

    from character_swap.models import VideoStatus as VS
    assert video.status == VS.DONE
    assert video.qc_status == "passed"
    assert video.qc_attempts == 2
    assert len(submits) == 2
    assert "baking soda" in submits[1]["movement_prompt"]
    assert "rejected by quality control" in submits[1]["movement_prompt"]
    # The QC-rejected take-1 clip is preserved + recorded (Hugo 2026-06-20).
    assert len(video.qc_rejects) == 1
    assert video.qc_rejects[0].kind == "video"
    assert 'baking goda' in (video.qc_rejects[0].reason or "")
    assert Path(video.qc_rejects[0].path).exists()


def test_video_qc_disabled_single_attempt(monkeypatch, tmp_path):
    from character_swap.models import VideoStatus, VideoVariant
    job, jc, v_img = _job_one_variant(tmp_path)
    v_img.status = VariantStatus.READY
    jc.approved_variant_ids = ["v1"]; jc.approved_variant_id = "v1"
    Path(v_img.path).write_bytes(b"img")
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]
    _stub_runner_persistence(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(type(runner.settings), "video_qc_enabled",
                        property(lambda self: False), raising=False)
    submits = []
    monkeypatch.setattr(runner.pipeline, "submit_video",
                        lambda **kw: (submits.append(1), "req-1")[1])
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))
    monkeypatch.setattr(runner.video_qc, "inspect_clip", lambda *a, **kw: None)

    asyncio.run(runner._animate_one_video(job, jc, video, "prompt"))
    assert len(submits) == 1
    assert video.qc_status == "skipped"


def test_inspect_clip_gating(monkeypatch):
    """inspect_clip: disabled → None; both checks unavailable → None; speech
    mismatch fails with the heard text in the reason."""
    monkeypatch.setattr(type(video_qc.__import__('character_swap.config', fromlist=['settings']).settings) if False else type(__import__('character_swap.config', fromlist=['settings']).settings),
                        "video_qc_enabled", property(lambda self: True), raising=False)
    monkeypatch.setattr(video_qc, "check_speech", lambda *a, **k: None)
    monkeypatch.setattr(video_qc, "check_visual", lambda *a, **k: None)
    assert video_qc.inspect_clip(Path("/x.mp4"), movement_prompt="p") is None

    monkeypatch.setattr(video_qc, "check_speech",
                        lambda *a, **k: (False, "baking goda", 0.93))
    verdict = video_qc.inspect_clip(
        Path("/x.mp4"), movement_prompt='says: "baking soda"')
    assert verdict is not None and not verdict.passed
    assert "baking goda" in verdict.reason
    assert "baking soda" in verdict.corrective_hint
