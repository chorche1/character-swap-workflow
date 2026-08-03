"""Clip-QC pure parts + the runner's generate→QC→retry loops (hermetic)."""
from __future__ import annotations

import asyncio
import subprocess
import tempfile
import types
from pathlib import Path

import pytest

from character_swap import reengineer, runner, swap_qc, video_edit, video_qc
from character_swap.models import (
    CharacterAsset,
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)
from character_swap.state import store


# --------------------------------------------------------------------- pure parts

def test_expected_speech_extracts_quoted_dialogue():
    p = ('Hand-held shot. The person says: "add a teaspoon of baking soda" — '
         'casual delivery. The person says: "stir it well" at the end.')
    assert video_qc.expected_speech(p) == "add a teaspoon of baking soda stir it well"


def test_expected_speech_handles_smart_quotes_and_absence():
    assert video_qc.expected_speech('says: “blend that whole squad”') == "blend that whole squad"
    assert video_qc.expected_speech("no dialogue here") == ""
    assert video_qc.expected_speech("") == ""


def test_expected_speech_recognizes_director_labeled_dialogue():
    """2026-07-02 audit: the AI Director writes `AUDIO — … Dialogue: "…"`
    with no `says` verb (the re_87e851d21f/Chang shape, fixed for captions in
    f68651e). The QC's private extraction returned "" for such prompts, so
    the speech check silently disengaged. expected_speech must see the line."""
    p = ('Cinematic kitchen shot, warm light. AUDIO — upbeat tone. '
         'Dialogue: "This is raw honey." No music.')
    assert video_qc.expected_speech(p) == "This is raw honey."


def test_expected_speech_strips_stage_directions():
    """`(while he points at the jar)` inside the quotes is never SPOKEN
    (70f3537) — leaving it in false-fails the Whisper similarity check."""
    p = ('The person says: "This is store-bought honey (while he points at '
         'the jar), and this is raw honey"')
    assert video_qc.expected_speech(p) == (
        "This is store-bought honey, and this is raw honey")


def test_expected_speech_delegates_to_canonical_extractor(monkeypatch):
    """expected_speech must BE video_edit.extract_dialogue — a local re-
    implementation drifted twice (06-26 CTA truncation, 07-02 labeled-form
    miss). Lock the delegation so it can never drift a third time."""
    monkeypatch.setattr(video_edit, "extract_dialogue",
                        lambda text: f"CANON<{text}>")
    assert video_qc.expected_speech("some prompt") == "CANON<some prompt>"


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


def test_speech_similarity_long_transcript_no_autojunk_collapse():
    """2026-07-03 audit: speech_similarity used a CHAR-level SequenceMatcher
    with default autojunk — on transcripts >200 chars, frequent letters become
    'junk' and matching collapses, so a near-perfect Whisper transcript of a
    longer dialogue line scored below the 0.7 threshold and burned a full
    video regeneration on a good clip. This exact pair scored 0.414 under the
    old matcher; word-level + autojunk=False (the same fix as
    video_edit.caption_transcript_ratio, 2026-06-22) keeps it high."""
    expected = (
        "this is store-bought honey and this is raw honey straight from "
        "the hive most people have no idea what the difference does to "
        "your body the processed one spikes your blood sugar like candy "
        "while the raw one feeds the good bacteria in your gut save this "
        "before it gets taken down")
    # Whisper-typical mishearings scattered through an otherwise-correct read:
    heard = (
        "this is store-bought hunny and this is raw honey straight from "
        "the hive most people have no idea what the differents does to "
        "your body the processed one spike your blood sugar like canned "
        "while the raw one feeds the good bacteria in your gut save this "
        "before it gets taking down")
    assert len(expected) > 200          # the autojunk trigger zone
    sim = video_qc.speech_similarity(expected, heard)
    from character_swap.config import settings
    assert sim >= settings.video_qc_speech_threshold, (
        f"near-perfect long transcript must PASS the speech check, got {sim}")
    # A genuinely wrong long transcript still fails:
    wrong = ("thanks for watching everyone i hope you found this video "
             "helpful please like and subscribe and see you in the next one "
             "bye " * 3)
    assert video_qc.speech_similarity(expected, wrong) < \
        settings.video_qc_speech_threshold


def test_repair_prompt_mentions_hint_and_minimal_change():
    p = swap_qc.repair_prompt("the face does not match the reference")
    assert "the face does not match the reference" in p
    assert "as little" in p.lower()


# ------------------------------------------------- _sample_frames cleanup

def _lavfi_clip(dest: Path, secs: float = 1.0) -> Path:
    """Tiny real video for frame sampling (no audio needed)."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y",
         "-f", "lavfi", "-i", f"color=c=red:s=160x284:d={secs}:r=12",
         "-pix_fmt", "yuv420p", str(dest)],
        check=True, capture_output=True)
    return dest


def test_sample_frames_removes_temp_dir_after_use(tmp_path):
    """2026-07-02 audit: every visual-QC inspection leaked a vqc_* temp dir
    with up to 4 JPEGs. The context manager must reclaim it on exit."""
    clip = _lavfi_clip(tmp_path / "clip.mp4")
    with video_qc._sample_frames(clip, n=2) as frames:
        assert frames, "expected at least one sampled frame"
        tmp_dir = frames[0].parent
        assert tmp_dir.name.startswith("vqc_")
        assert all(f.exists() for f in frames)
    assert not tmp_dir.exists()


def test_sample_frames_cleans_up_on_exception(tmp_path):
    clip = _lavfi_clip(tmp_path / "clip2.mp4")
    with pytest.raises(RuntimeError, match="boom"):
        with video_qc._sample_frames(clip, n=1) as frames:
            tmp_dir = frames[0].parent
            raise RuntimeError("boom")
    assert not tmp_dir.exists()


def test_sample_frames_cleans_up_when_no_frames_extracted(tmp_path, monkeypatch):
    """Zero extracted frames (undecodable clip) must not leave an empty
    vqc_* dir behind either."""
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 1.0)
    monkeypatch.setattr(
        video_qc.subprocess, "run",
        lambda *a, **kw: types.SimpleNamespace(returncode=1, stdout="", stderr=""))
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*a, **kw):
        d = real_mkdtemp(*a, **kw)
        made.append(d)
        return d

    monkeypatch.setattr(video_qc.tempfile, "mkdtemp", tracking_mkdtemp)

    with video_qc._sample_frames(tmp_path / "broken.mp4", n=2) as frames:
        assert frames == []
    assert made and not Path(made[0]).exists()


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
    # This test exercises the QC retry path, so QC must be ON *and* retries > 0
    # regardless of the ambient .env (Hugo's shared env is VIDEO_QC=0 pre-2026-07-03
    # and VIDEO_QC_MAX_RETRIES=0 after — flag-only — either of which would force a
    # single take and defeat the retry assertion).
    monkeypatch.setattr(type(runner.settings), "video_qc_enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(runner.settings), "video_qc_max_retries",
                        property(lambda self: 1), raising=False)

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


def test_video_qc_flag_only_no_retry(monkeypatch, tmp_path):
    """VIDEO_QC on + VIDEO_QC_MAX_RETRIES=0 = FLAG-ONLY (Hugo 2026-07-03 "bara
    flagga, behåll"): a dialogue-mismatch clip is KEPT (status DONE) and marked
    qc_status='failed' with the reason, but the provider is called exactly
    ONCE — no re-render, and no qcreject snapshot (the shown clip IS the take)."""
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
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(runner.settings), "video_qc_max_retries",
                        property(lambda self: 0), raising=False)
    submits = []
    monkeypatch.setattr(runner.pipeline, "submit_video",
                        lambda **kw: (submits.append(kw), "req-1")[1])
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))
    monkeypatch.setattr(runner.video_qc, "inspect_clip",
                        lambda *a, **kw: video_qc.ClipVerdict(
                            False, 'dialogue mismatch: heard "something else"',
                            'say the line'))

    asyncio.run(runner._animate_one_video(job, jc, video, 'P says: "baking soda"'))

    assert len(submits) == 1                       # NO re-render
    assert video.status == VideoStatus.DONE        # clip kept, usable downstream
    assert video.qc_status == "failed"             # but flagged with the reason
    assert "something else" in (video.qc_reason or "")
    assert video.qc_attempts == 1
    assert video.qc_rejects == []                  # nothing snapshotted; take IS the clip


def _animate_capture(monkeypatch, tmp_path, prompt, char_id="cA"):
    """Drive _animate_one_video once with QC passing on the first take; return the
    (submits, qc_seen) capture lists so the caller can assert on what reached the
    video provider AND the QC judge. `char_id` is overridable (the test store is
    session-scoped, so each language test uses a unique id to avoid leakage)."""
    from character_swap.models import VideoStatus, VideoVariant
    job, jc, v_img = _job_one_variant(tmp_path)
    if char_id != "cA":
        jc.char_id = char_id
        job.characters = {char_id: jc}
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
                        property(lambda self: True), raising=False)
    submits, qc_seen = [], []
    monkeypatch.setattr(runner.pipeline, "submit_video",
                        lambda **kw: (submits.append(kw), "req-1")[1])
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))
    monkeypatch.setattr(runner.video_qc, "inspect_clip",
                        lambda *a, **kw: (qc_seen.append(kw.get("movement_prompt")),
                                          video_qc.ClipVerdict(True, "", ""))[1])
    asyncio.run(runner._animate_one_video(job, jc, video, prompt))
    return submits, qc_seen


def test_animate_localizes_for_flagged_character(monkeypatch, tmp_path):
    """End-to-end: a 🇪🇸-flagged character's clip is translated at submit time, and
    the SAME localized prompt reaches both submit_video AND video_qc (so the QC
    expected-dialogue matches the Spanish audio)."""
    # Flag the LIBRARY character (unique id — the test store is session-scoped).
    store().add_character(CharacterAsset(
        char_id="cA_es_flag", name="A", filename="cA.png", language="es"))
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, *, language="es", re_id=None: ["¡Hola amigos!"])
    p = ('He waves. The person says to the camera with an American accent: '
         '"Hello friends."')
    submits, qc_seen = _animate_capture(monkeypatch, tmp_path, p,
                                        char_id="cA_es_flag")

    assert len(submits) == 1
    mp = submits[0]["movement_prompt"]
    assert "¡Hola amigos!" in mp and "Hello friends." not in mp
    assert "Latin American Spanish accent" in mp
    assert "american accent" not in mp.lower()        # inline EN accent stripped
    assert qc_seen and qc_seen[0] == mp                # QC saw the SAME prompt


def test_animate_localizes_for_german_character(monkeypatch, tmp_path):
    """Same chokepoint, other language (Hugo 2026-08-02): a 🇩🇪-flagged
    character's clip is translated at submit time and the SAME German prompt
    reaches submit_video AND video_qc."""
    store().add_character(CharacterAsset(
        char_id="cA_de_flag", name="A", filename="cA.png", language="de"))
    seen_lang = []
    monkeypatch.setattr(
        reengineer, "translate_dialogue",
        lambda lines, *, language="es", re_id=None: (
            seen_lang.append(language), ["Hallo Freunde!"])[1])
    p = ('He waves. The person says to the camera with an American accent: '
         '"Hello friends."')
    submits, qc_seen = _animate_capture(monkeypatch, tmp_path, p,
                                        char_id="cA_de_flag")

    assert seen_lang == ["de"]
    assert len(submits) == 1
    mp = submits[0]["movement_prompt"]
    assert "Hallo Freunde!" in mp and "Hello friends." not in mp
    assert "standard German (Hochdeutsch)" in mp
    assert "american accent" not in mp.lower()        # inline EN accent stripped
    assert qc_seen and qc_seen[0] == mp                # QC saw the SAME prompt


def test_animate_fails_the_clip_loudly_when_german_translation_fails(
        monkeypatch, tmp_path):
    """Translation failure for a flagged character must ERROR the clip with the
    language named — never ship an English take under a 🇩🇪 flag."""
    from character_swap.models import VideoStatus, VideoVariant
    store().add_character(CharacterAsset(
        char_id="cA_de_fail", name="A", filename="cA.png", language="de"))
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, *, language="es", re_id=None: None)
    job, jc, v_img = _job_one_variant(tmp_path)
    jc.char_id = "cA_de_fail"; job.characters = {"cA_de_fail": jc}
    v_img.status = VariantStatus.READY
    jc.approved_variant_ids = ["v1"]; jc.approved_variant_id = "v1"
    Path(v_img.path).write_bytes(b"img")
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]
    _stub_runner_persistence(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(runner.pipeline, "submit_video",
                        lambda **kw: (_ for _ in ()).throw(
                            AssertionError("must not submit a failed localization")))
    asyncio.run(runner._animate_one_video(
        job, jc, video, 'He waves. The person says: "Hello friends."'))
    assert video.status == VideoStatus.ERROR
    assert "German localization failed" in (video.error or "")


def test_animate_passes_through_for_unflagged_character(monkeypatch, tmp_path):
    """An unflagged (English) character's prompt reaches the provider untouched —
    no translation, no accent rewrite. Uses a unique id never registered in the
    session-scoped store so the 'no flag' state is explicit, not incidental."""
    assert runner._character_language("cA_unflagged") is None
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("must not translate for unflagged char")))
    p = 'He waves. The person says: "Hello friends."'
    submits, _ = _animate_capture(monkeypatch, tmp_path, p, char_id="cA_unflagged")
    assert len(submits) == 1
    assert submits[0]["movement_prompt"] == p


def test_animate_keeps_spanish_through_qc_retry(monkeypatch, tmp_path):
    """On a QC retry the prompt is re-based on the already-localized (Spanish)
    movement_prompt — only an English QC instruction hint is appended — so the
    Spanish dialogue + accent survive take 2 (guards against a regression that
    re-derived the retry prompt from a pre-localization source)."""
    from character_swap.models import VideoStatus, VideoVariant
    job, jc, v_img = _job_one_variant(tmp_path)
    jc.char_id = "cA_retry_es"; job.characters = {"cA_retry_es": jc}
    v_img.status = VariantStatus.READY
    jc.approved_variant_ids = ["v1"]; jc.approved_variant_id = "v1"
    Path(v_img.path).write_bytes(b"img")
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]
    store().add_character(CharacterAsset(
        char_id="cA_retry_es", name="A", filename="cA.png", language="es"))
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda lines, *, language="es", re_id=None: ["¡Hola amigos!"])
    _stub_runner_persistence(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    monkeypatch.setattr(type(runner.settings), "video_qc_enabled",
                        property(lambda self: True), raising=False)
    # Retry path — pin retries on regardless of the ambient flag-only .env.
    monkeypatch.setattr(type(runner.settings), "video_qc_max_retries",
                        property(lambda self: 1), raising=False)
    submits = []
    monkeypatch.setattr(runner.pipeline, "submit_video",
                        lambda **kw: (submits.append(kw), f"req-{len(submits)}")[1])
    monkeypatch.setattr(runner.pipeline, "wait_for_video",
                        lambda **kw: Path(kw["dest"]).write_bytes(b"clip"))
    verdicts = [video_qc.ClipVerdict(False, 'heard "hello"', 'pronounce it clearly'),
                video_qc.ClipVerdict(True, "", "")]
    monkeypatch.setattr(runner.video_qc, "inspect_clip",
                        lambda *a, **kw: verdicts.pop(0))

    asyncio.run(runner._animate_one_video(
        job, jc, video,
        'He waves. The person says to the camera with an American accent: '
        '"Hello friends."'))

    assert len(submits) == 2
    for s in submits:                                   # BOTH takes stay Spanish
        assert "¡Hola amigos!" in s["movement_prompt"]
        assert "Latin American Spanish accent" in s["movement_prompt"]
        assert "american accent" not in s["movement_prompt"].lower()
    assert "rejected by quality control" in submits[1]["movement_prompt"]


def test_animate_fails_loudly_on_localization_failure(monkeypatch, tmp_path):
    """If Spanish translation fails for a 🇪🇸-flagged character, the clip FAILS
    with a clear error (Hugo 2026-06-27) — it never submits an English take."""
    from character_swap.models import VideoStatus, VideoVariant
    job, jc, v_img = _job_one_variant(tmp_path)
    jc.char_id = "cA_loc_fail"; job.characters = {"cA_loc_fail": jc}
    v_img.status = VariantStatus.READY
    jc.approved_variant_ids = ["v1"]; jc.approved_variant_id = "v1"
    Path(v_img.path).write_bytes(b"img")
    video = VideoVariant(video_id="vd1", grok_job_id="",
                         status=VideoStatus.PENDING, source_variant_id="v1")
    jc.videos = [video]
    store().add_character(CharacterAsset(
        char_id="cA_loc_fail", name="A", filename="cA.png", language="es"))
    monkeypatch.setattr(reengineer, "translate_dialogue",
                        lambda *a, **k: None)            # translation fails
    _stub_runner_persistence(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "_replace_video", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_maybe_complete_char", lambda *a, **k: None)
    submits = []
    monkeypatch.setattr(runner.pipeline, "submit_video",
                        lambda **kw: (submits.append(kw), "req")[1])

    asyncio.run(runner._animate_one_video(
        job, jc, video, 'He waves. The person says: "Hello friends."'))

    assert video.status == VideoStatus.ERROR
    assert "Spanish localization failed" in (video.error or "")
    assert submits == []                                 # never submitted English


def test_inspect_clip_gating(monkeypatch):
    """inspect_clip: disabled → None; both checks unavailable → None; speech
    mismatch fails with the heard text in the reason."""
    monkeypatch.setattr(type(video_qc.__import__('character_swap.config', fromlist=['settings']).settings) if False else type(__import__('character_swap.config', fromlist=['settings']).settings),
                        "video_qc_enabled", property(lambda self: True), raising=False)
    # `_transcribe` is the ONE Whisper pass inspect_clip runs — it returns
    # (ok, heard, similarity, detected_language) so the wrong-language check
    # rides along without a second call. check_speech is the 3-tuple public
    # view of the same pass.
    monkeypatch.setattr(video_qc, "_transcribe", lambda *a, **k: None)
    monkeypatch.setattr(video_qc, "check_visual", lambda *a, **k: None)
    assert video_qc.inspect_clip(Path("/x.mp4"), movement_prompt="p") is None

    monkeypatch.setattr(video_qc, "_transcribe",
                        lambda *a, **k: (False, "baking goda", 0.93, "english", "baking goda"))
    verdict = video_qc.inspect_clip(
        Path("/x.mp4"), movement_prompt='says: "baking soda"')
    assert verdict is not None and not verdict.passed
    assert "baking goda" in verdict.reason
    assert "baking soda" in verdict.corrective_hint


def test_inspect_clip_visual_disabled_runs_speech_only(monkeypatch):
    """VIDEO_QC_VISUAL=0 → the anatomy vision call is never made; only the
    dialogue check decides the verdict (Hugo 2026-07-03 'bara repliken'). A
    would-fail visual check must be skipped entirely when speech passes."""
    cfg = __import__('character_swap.config', fromlist=['settings']).settings
    monkeypatch.setattr(type(cfg), "video_qc_enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(cfg), "video_qc_visual_enabled",
                        property(lambda self: False), raising=False)
    visual_calls = []
    monkeypatch.setattr(video_qc, "check_visual",
                        lambda *a, **k: (visual_calls.append(1),
                                         video_qc.ClipVerdict(False, "extra arm", "fix"))[1])
    monkeypatch.setattr(video_qc, "_transcribe",
                        lambda *a, **k: (True, "baking soda", 0.99, "english", "baking soda"))
    verdict = video_qc.inspect_clip(
        Path("/x.mp4"), movement_prompt='says: "baking soda"')
    assert verdict is not None and verdict.passed     # speech ran + passed
    assert visual_calls == []                         # visual never consulted


def test_inspect_clip_visual_enabled_still_runs(monkeypatch):
    """Default (VIDEO_QC_VISUAL unset → True) keeps the anatomy check wired: a
    failing visual verdict fails the clip even when speech is unavailable."""
    cfg = __import__('character_swap.config', fromlist=['settings']).settings
    monkeypatch.setattr(type(cfg), "video_qc_enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(type(cfg), "video_qc_visual_enabled",
                        property(lambda self: True), raising=False)
    monkeypatch.setattr(video_qc, "_transcribe", lambda *a, **k: None)
    monkeypatch.setattr(video_qc, "check_visual",
                        lambda *a, **k: video_qc.ClipVerdict(False, "extra arm", "fix"))
    verdict = video_qc.inspect_clip(Path("/x.mp4"), movement_prompt="no dialogue")
    assert verdict is not None and not verdict.passed
    assert "extra arm" in verdict.reason
