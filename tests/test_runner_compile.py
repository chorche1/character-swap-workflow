"""Tests for runner_compile — the Step 6 per-character video compile.

Two layers covered:
  1. `_ordered_scene_videos` — pure function that picks the ordered video
     path list per character. No async, no IO except file-exists checks.
  2. `compile_job_videos` target-selection — we replace `_compile_one_character`
     with a stub so we can assert WHICH characters get compiled (the real
     compile_one_character runs ffmpeg / Whisper / ElevenLabs which we don't
     want here).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import runner_compile
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


# --- _ordered_scene_videos -------------------------------------------------------------


def _mkvideo(real_path: Path, source_variant_id: str, status=VideoStatus.DONE) -> VideoVariant:
    return VideoVariant(
        video_id=f"v_{source_variant_id}",
        grok_job_id=f"g_{source_variant_id}",
        status=status,
        source_variant_id=source_variant_id,
        final_video_path=str(real_path),
    )


def _mkvariant(variant_id: str, scene_id: str | None) -> GeneratedImage:
    return GeneratedImage(
        variant_id=variant_id, path=f"/tmp/{variant_id}.png",
        prompt="x", scene_id=scene_id, status=VariantStatus.READY,
    )


def _mkjob(scene_ids: list[str], jc: JobCharacter) -> Job:
    return Job(
        job_id="j1", scene_id=scene_ids[0],
        scene_image_path=f"/tmp/{scene_ids[0]}.png",
        scene_ids=list(scene_ids),
        scene_image_paths=[f"/tmp/{sid}.png" for sid in scene_ids],
        characters={jc.char_id: jc},
    )


def test_ordered_scene_videos_empty_when_no_approved(tmp_path):
    """No approved variants → empty list (compile should bail)."""
    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        approved_variant_ids=[],
    )
    job = _mkjob(["sc1"], jc)
    paths, missing = runner_compile._ordered_scene_videos(job, jc)
    assert paths == []
    assert missing == ["sc1 (no approved variant)"]


def test_ordered_scene_videos_picks_first_done_per_scene(tmp_path):
    """Two scenes, each with one approved variant + one DONE video → ordered list."""
    v1_path = tmp_path / "scene1.mp4"; v1_path.write_text("fake mp4")
    v2_path = tmp_path / "scene2.mp4"; v2_path.write_text("fake mp4")

    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        images=[
            _mkvariant("var_sc1", "sc1"),
            _mkvariant("var_sc2", "sc2"),
        ],
        approved_variant_ids=["var_sc1", "var_sc2"],
        videos=[
            _mkvideo(v1_path, source_variant_id="var_sc1"),
            _mkvideo(v2_path, source_variant_id="var_sc2"),
        ],
    )
    job = _mkjob(["sc1", "sc2"], jc)
    paths, missing = runner_compile._ordered_scene_videos(job, jc)
    assert paths == [v1_path, v2_path]
    assert missing == []


def test_ordered_scene_videos_skips_scene_without_done_video(tmp_path):
    """Scene 2's video is PROCESSING (not DONE) → only scene 1's video included."""
    v1 = tmp_path / "scene1.mp4"; v1.write_text("fake")

    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        images=[
            _mkvariant("var_sc1", "sc1"),
            _mkvariant("var_sc2", "sc2"),
        ],
        approved_variant_ids=["var_sc1", "var_sc2"],
        videos=[
            _mkvideo(v1, source_variant_id="var_sc1"),
            _mkvideo(Path("/tmp/never-existed.mp4"),
                     source_variant_id="var_sc2",
                     status=VideoStatus.PROCESSING),
        ],
    )
    job = _mkjob(["sc1", "sc2"], jc)
    paths, missing = runner_compile._ordered_scene_videos(job, jc)
    assert paths == [v1]
    assert missing == ["sc2 (no finished video)"]


def test_ordered_scene_videos_skips_scene_with_missing_file(tmp_path):
    """A DONE video whose final_video_path doesn't exist on disk gets skipped."""
    v1 = tmp_path / "scene1.mp4"; v1.write_text("fake")

    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        images=[
            _mkvariant("var_sc1", "sc1"),
            _mkvariant("var_sc2", "sc2"),
        ],
        approved_variant_ids=["var_sc1", "var_sc2"],
        videos=[
            _mkvideo(v1, source_variant_id="var_sc1"),
            # Marked DONE but the file doesn't exist (cleaned up / never copied)
            _mkvideo(Path("/tmp/nonexistent-12345.mp4"),
                     source_variant_id="var_sc2"),
        ],
    )
    job = _mkjob(["sc1", "sc2"], jc)
    paths, missing = runner_compile._ordered_scene_videos(job, jc)
    assert paths == [v1]
    assert missing == ["sc2 (video file missing on disk)"]


def test_ordered_scene_videos_legacy_single_scene_uses_approved_variant_id(tmp_path):
    """Old single-scene jobs only have `approved_variant_id` (singular) set."""
    v = tmp_path / "scene.mp4"; v.write_text("fake")
    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        images=[_mkvariant("var_only", None)],  # legacy: variant has no scene_id
        approved_variant_id="var_only",
        approved_variant_ids=[],  # empty — old job
        videos=[_mkvideo(v, source_variant_id="var_only")],
    )
    job = _mkjob(["sc1"], jc)
    assert runner_compile._ordered_scene_videos(job, jc) == ([v], [])


def test_ordered_scene_videos_picks_first_done_when_multiple_videos_per_variant(tmp_path):
    """If a single approved variant has multiple DONE videos (user clicked
    regenerate), the FIRST DONE one wins — that's the contract."""
    v1 = tmp_path / "first.mp4"; v1.write_text("a")
    v2 = tmp_path / "second.mp4"; v2.write_text("b")

    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        images=[_mkvariant("var_sc1", "sc1")],
        approved_variant_ids=["var_sc1"],
        videos=[
            _mkvideo(v1, source_variant_id="var_sc1"),
            _mkvideo(v2, source_variant_id="var_sc1"),
        ],
    )
    job = _mkjob(["sc1"], jc)
    assert runner_compile._ordered_scene_videos(job, jc) == ([v1], [])


def test_compile_persists_missing_scene_warning(monkeypatch, tmp_path):
    """Backlog #9 (audit 2026-06-12): a final that silently skips scenes
    ships with whole lines of dialogue absent and status 'done'. When scenes
    are dropped, the compile must persist compile_warning, emit
    char.compile_warning, and include the warning in char.compile_done."""
    import asyncio
    from types import SimpleNamespace
    from character_swap.config import settings

    v1 = tmp_path / "scene1.mp4"; v1.write_text("fake")
    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        images=[_mkvariant("var_sc1", "sc1"), _mkvariant("var_sc2", "sc2")],
        approved_variant_ids=["var_sc1", "var_sc2"],
        videos=[_mkvideo(v1, source_variant_id="var_sc1")],  # sc2: none
    )
    job = _mkjob(["sc1", "sc2"], jc)

    fake_store = SimpleNamespace(
        get_job=lambda jid: job,
        update_job=lambda j: None,
        get_character=lambda cid: None,
    )
    monkeypatch.setattr(runner_compile, "store", lambda: fake_store)
    monkeypatch.setattr(settings, "output_dir", tmp_path, raising=False)

    events: list[tuple[str, dict]] = []

    async def fake_emit(job_id, kind, **kw):
        events.append((kind, kw))
    monkeypatch.setattr(runner_compile, "_emit", fake_emit)

    final = tmp_path / "result.mp4"; final.write_text("final")

    async def fake_pipeline(paths, **kw):
        assert paths == [v1]                       # only the existing scene
        return runner_compile.EditorResult(final=final, voice_applied=False)
    monkeypatch.setattr(runner_compile, "run_editor_pipeline", fake_pipeline)

    asyncio.run(runner_compile._compile_one_character(
        "j1", "c1", template="submagic-pro", overrides=None,
        enable_trim=True, enable_captions=False, enable_wpm_normalize=False,
        target_wpm=190, threshold_db=-35.0, min_silence_secs=0.35,
        pad_secs=0.06, voice_override=None, enable_voice_swap=False))

    assert jc.compile_status == "done"
    assert "sc2 (no finished video)" in (jc.compile_warning or "")
    kinds = dict(events)
    assert "char.compile_warning" in kinds
    assert "sc2" in kinds["char.compile_warning"]["message"]
    assert kinds["char.compile_done"]["warning"] == jc.compile_warning


def test_compile_clears_stale_warning_on_full_success(monkeypatch, tmp_path):
    """A re-compile where every scene now has a clip must clear the old
    warning — stale caveats are as misleading as silent drops."""
    import asyncio
    from types import SimpleNamespace
    from character_swap.config import settings

    v1 = tmp_path / "s1.mp4"; v1.write_text("fake")
    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        images=[_mkvariant("var_sc1", "sc1")],
        approved_variant_ids=["var_sc1"],
        videos=[_mkvideo(v1, source_variant_id="var_sc1")],
        compile_warning="final is missing 1 scene(s): old",
    )
    job = _mkjob(["sc1"], jc)
    fake_store = SimpleNamespace(get_job=lambda jid: job,
                                 update_job=lambda j: None,
                                 get_character=lambda cid: None)
    monkeypatch.setattr(runner_compile, "store", lambda: fake_store)
    monkeypatch.setattr(settings, "output_dir", tmp_path, raising=False)

    async def fake_emit(*a, **k):
        pass
    monkeypatch.setattr(runner_compile, "_emit", fake_emit)
    final = tmp_path / "r.mp4"; final.write_text("x")

    async def fake_pipeline(paths, **kw):
        return runner_compile.EditorResult(final=final, voice_applied=False)
    monkeypatch.setattr(runner_compile, "run_editor_pipeline", fake_pipeline)

    asyncio.run(runner_compile._compile_one_character(
        "j1", "c1", template="submagic-pro", overrides=None,
        enable_trim=True, enable_captions=False, enable_wpm_normalize=False,
        target_wpm=190, threshold_db=-35.0, min_silence_secs=0.35,
        pad_secs=0.06, voice_override=None, enable_voice_swap=False))

    assert jc.compile_status == "done"
    assert jc.compile_warning is None


# --- compile_job_videos target selection -----------------------------------------------


class _FakeStore:
    """Just-enough store stub for runner_compile's get_job / update_job."""
    def __init__(self, job: Job):
        self.job = job
        self.update_calls: list[Job] = []

    def get_job(self, job_id: str) -> Job | None:
        return self.job if job_id == self.job.job_id else None

    def update_job(self, job: Job) -> None:
        self.update_calls.append(job)

    def get_character(self, char_id: str):
        return None


@pytest.fixture
def stub_compile_one(monkeypatch):
    """Replace _compile_one_character with a stub that records (job, char)."""
    calls: list[tuple[str, str]] = []

    async def fake(job_id: str, char_id: str, **kwargs):
        calls.append((job_id, char_id))

    monkeypatch.setattr(runner_compile, "_compile_one_character", fake)
    return calls


def _eligible_jc(char_id: str, tmp_path: Path) -> JobCharacter:
    """A character with one approved variant + one DONE video on disk."""
    v = tmp_path / f"{char_id}.mp4"; v.write_text("fake")
    return JobCharacter(
        char_id=char_id, name=char_id,
        source_image_path=f"/tmp/{char_id}.png",
        status=CharStatus.ANIMATING,
        images=[_mkvariant(f"var_{char_id}", "sc1")],
        approved_variant_ids=[f"var_{char_id}"],
        videos=[_mkvideo(v, source_variant_id=f"var_{char_id}")],
    )


def test_compile_job_videos_skips_rejected_char(monkeypatch, tmp_path, stub_compile_one):
    """REJECTED char with everything else green should NOT be compiled."""
    good = _eligible_jc("good", tmp_path)
    rejected = _eligible_jc("rejected", tmp_path)
    rejected.status = CharStatus.REJECTED

    job = Job(
        job_id="j1", scene_id="sc1", scene_image_path="/tmp/sc1.png",
        scene_ids=["sc1"],
        characters={good.char_id: good, rejected.char_id: rejected},
    )
    monkeypatch.setattr(runner_compile, "store", lambda: _FakeStore(job))

    asyncio.run(runner_compile.compile_job_videos("j1"))
    char_ids = [c for _, c in stub_compile_one]
    assert "good" in char_ids
    assert "rejected" not in char_ids


def test_compile_job_videos_skips_char_with_no_approved(
    monkeypatch, tmp_path, stub_compile_one,
):
    """Char with no approved variants (user hasn't approved anything yet) skipped."""
    good = _eligible_jc("good", tmp_path)
    no_approval = _eligible_jc("no_approval", tmp_path)
    no_approval.approved_variant_ids = []
    no_approval.approved_variant_id = None

    job = Job(
        job_id="j1", scene_id="sc1", scene_image_path="/tmp/sc1.png",
        scene_ids=["sc1"],
        characters={c.char_id: c for c in (good, no_approval)},
    )
    monkeypatch.setattr(runner_compile, "store", lambda: _FakeStore(job))

    asyncio.run(runner_compile.compile_job_videos("j1"))
    char_ids = [c for _, c in stub_compile_one]
    assert "good" in char_ids
    assert "no_approval" not in char_ids


def test_compile_job_videos_skips_char_with_no_done_videos(
    monkeypatch, tmp_path, stub_compile_one,
):
    """Char approved but Grok hasn't finished any videos yet → skip."""
    good = _eligible_jc("good", tmp_path)
    pending = _eligible_jc("pending", tmp_path)
    # Wipe the DONE videos, leave only PROCESSING
    pending.videos = [
        _mkvideo(Path("/tmp/never.mp4"), "var_pending", status=VideoStatus.PROCESSING),
    ]

    job = Job(
        job_id="j1", scene_id="sc1", scene_image_path="/tmp/sc1.png",
        scene_ids=["sc1"],
        characters={c.char_id: c for c in (good, pending)},
    )
    monkeypatch.setattr(runner_compile, "store", lambda: _FakeStore(job))

    asyncio.run(runner_compile.compile_job_videos("j1"))
    char_ids = [c for _, c in stub_compile_one]
    assert "good" in char_ids
    assert "pending" not in char_ids


def test_compile_job_videos_respects_char_ids_filter(
    monkeypatch, tmp_path, stub_compile_one,
):
    """Passing char_ids=['c1'] should compile only that char (retry-one use case)."""
    c1 = _eligible_jc("c1", tmp_path)
    c2 = _eligible_jc("c2", tmp_path)

    job = Job(
        job_id="j1", scene_id="sc1", scene_image_path="/tmp/sc1.png",
        scene_ids=["sc1"],
        characters={c1.char_id: c1, c2.char_id: c2},
    )
    monkeypatch.setattr(runner_compile, "store", lambda: _FakeStore(job))

    asyncio.run(runner_compile.compile_job_videos("j1", char_ids=["c1"]))
    char_ids = [c for _, c in stub_compile_one]
    assert char_ids == ["c1"]


def test_compile_job_videos_no_targets_no_op(monkeypatch, stub_compile_one):
    """Job with no eligible chars → return without calling compile_one_character."""
    job = Job(
        job_id="j1", scene_id="sc1", scene_image_path="/tmp/sc1.png",
        scene_ids=["sc1"],
        characters={},
    )
    monkeypatch.setattr(runner_compile, "store", lambda: _FakeStore(job))
    asyncio.run(runner_compile.compile_job_videos("j1"))
    assert stub_compile_one == []


def test_compile_job_videos_missing_job_no_op(monkeypatch, stub_compile_one):
    """get_job returns None → silently no-op (don't raise)."""
    class _NoneStore:
        def get_job(self, _): return None

    monkeypatch.setattr(runner_compile, "store", lambda: _NoneStore())
    asyncio.run(runner_compile.compile_job_videos("missing"))
    assert stub_compile_one == []
