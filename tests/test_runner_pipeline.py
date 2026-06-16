"""Tests for runner_pipeline — Phase 4 (compile → spawn automate.py → Drive).

What's testable without actually running Resolve or spawning subprocesses:
  - The stdout-marker regexes pick up the right status transitions
  - The fatal-marker detection fires on the right error lines
  - The temp-dir + credentials-path helpers resolve sensibly

The actual subprocess management (asyncio.create_subprocess_exec, full
char-pipeline run) needs Resolve + real files; skip those here. Smoke-test
manually after Resolve setup.
"""
from __future__ import annotations

import pytest

from character_swap import runner_pipeline


# --- Status marker mapping --------------------------------------------------------------

def _first_match(line: str):
    """Return the (new_status, capture_value) the marker table picks for `line`,
    or (None, None) if nothing matches."""
    for pat, new_status, capture_name in runner_pipeline._STATUS_MARKERS:
        m = pat.search(line)
        if m:
            detail = m.group(capture_name) if capture_name else None
            return new_status, detail
    return None, None


@pytest.mark.parametrize("line,expected_status,expected_detail", [
    # The Project-ready banner is what automate.py prints right after
    # importing the video — that's the first signal the user's actually
    # in the rendering phase.
    ("Project ready: 'ed_abc123'", "rendering", None),
    ("Rendering → /Users/h/foo/ed-final.mp4", "rendering", None),
    # render OK marker captures the rendered filename so we can show it.
    ("  render OK    : ed_abc-final.mp4 (12.3 MB)", "uploading", "ed_abc-final.mp4"),
    # Upload-starting line — Hugo sees "uploading" badge as soon as it fires.
    ("  drive       : uploading ed_abc-final.mp4 (12.3 MB)…", "uploading", None),
    # Success line carries the Drive URL; status flips to done.
    ("  drive OK    : https://drive.google.com/file/d/xxx/view", "done",
     "https://drive.google.com/file/d/xxx/view"),
    # Skip line (no credentials) still counts as done — pipeline completed.
    ("  drive SKIP   : no credentials.json next to automate.py", "done", None),
    # Error line — also "done" status because rendering succeeded; Drive
    # failure surfaces via the absence of pipeline_drive_link, not via failed.
    ("  drive ERROR : quota exceeded", "done", None),
    # Final banner — last-line safety net.
    ("Done. Project 'ed_abc' is open in Resolve.", "done", None),
])
def test_status_markers_map_known_lines(line, expected_status, expected_detail):
    status, detail = _first_match(line)
    assert status == expected_status, f"line={line!r}"
    assert detail == expected_detail, f"line={line!r}"


@pytest.mark.parametrize("line", [
    "",
    "some random debug print from python",
    "  auto color   : opened Color page · 2 clip(s) tagged Rec.709",  # info-only
    "  render WARN  : no output found matching foo*.mp4",            # warn but not transition
    "import DaVinciResolveScript",                                    # arbitrary import line
])
def test_status_markers_ignore_unrelated_lines(line):
    status, detail = _first_match(line)
    assert status is None, f"line={line!r} should not transition status"


# --- Fatal marker detection -------------------------------------------------------------

def _is_fatal(line: str) -> bool:
    return any(p.search(line) for p in runner_pipeline._FATAL_MARKERS)


@pytest.mark.parametrize("line", [
    "Open DaVinci Resolve first, then re-run this script.",
    "Could not import DaVinciResolveScript. Install Resolve from",
    "Could not create/open project 'ed_abc'",
])
def test_fatal_markers_fire(line):
    assert _is_fatal(line), f"expected fatal: {line!r}"


@pytest.mark.parametrize("line", [
    "Done. Project 'ed_abc' is open in Resolve.",
    "  render OK    : ed_abc-final.mp4 (5.0 MB)",
    "Open this folder in Finder",   # superstring of "Open" but not the fatal one
    "Resolve says hello",
])
def test_fatal_markers_dont_false_positive(line):
    assert not _is_fatal(line), f"should not be fatal: {line!r}"


# --- Helpers ---------------------------------------------------------------------------

def test_pipeline_root_uses_data_dir_alongside_state(monkeypatch, tmp_path):
    """`_pipeline_root` is the parent of state_dir + 'pipeline-runs' — same
    parent as state/, characters/, output/ in the shared data store."""
    # Mock settings.state_dir so the helper isn't tied to the actual install.
    from character_swap import config
    monkeypatch.setattr(config.settings, "state_dir", tmp_path / "state")
    (tmp_path / "state").mkdir()
    root = runner_pipeline._pipeline_root()
    assert root.name == "pipeline-runs"
    assert root.parent == tmp_path
    assert root.is_dir()


def test_shared_credentials_returns_none_when_missing(monkeypatch, tmp_path):
    from character_swap import config
    monkeypatch.setattr(config.settings, "state_dir", tmp_path / "state")
    assert runner_pipeline._shared_credentials() is None


def test_shared_credentials_returns_path_when_present(monkeypatch, tmp_path):
    from character_swap import config
    (tmp_path / "credentials.json").write_text('{"installed": {}}')
    monkeypatch.setattr(config.settings, "state_dir", tmp_path / "state")
    found = runner_pipeline._shared_credentials()
    assert found is not None
    assert found.name == "credentials.json"
    assert found.read_text().startswith('{"installed"')


def test_shared_drive_token_returns_path_even_when_missing(monkeypatch, tmp_path):
    """`_shared_drive_token` returns the target path regardless of whether the
    file exists — the subprocess will create it on first run."""
    from character_swap import config
    monkeypatch.setattr(config.settings, "state_dir", tmp_path / "state")
    token = runner_pipeline._shared_drive_token()
    assert token is not None
    assert token.name == "token.json"
    assert not token.exists()


# --- run_full_pipeline target selection ------------------------------------------------

def _pipeline_eligible_jc(char_id: str, tmp_path):
    """A character eligible for the Resolve pipeline: one approved variant +
    one DONE video on disk (mirrors compile eligibility)."""
    from pathlib import Path
    from character_swap.models import (
        CharStatus, GeneratedImage, JobCharacter, VariantStatus,
        VideoStatus, VideoVariant,
    )
    v = tmp_path / f"{char_id}.mp4"; v.write_text("fake")
    return JobCharacter(
        char_id=char_id, name=char_id, source_image_path=f"/tmp/{char_id}.png",
        status=CharStatus.ANIMATING,
        images=[GeneratedImage(variant_id=f"var_{char_id}",
                               path=f"/tmp/{char_id}.png", prompt="x",
                               scene_id="sc1", status=VariantStatus.READY)],
        approved_variant_ids=[f"var_{char_id}"],
        videos=[VideoVariant(video_id=f"vid_{char_id}",
                             grok_job_id=f"g_{char_id}", status=VideoStatus.DONE,
                             source_variant_id=f"var_{char_id}",
                             final_video_path=str(v))],
    )


def test_run_full_pipeline_selects_only_eligible_chars(monkeypatch, tmp_path):
    """run_full_pipeline shares compile eligibility (via _eligible_for_compile):
    only not-rejected + approved + has-DONE-video chars get a pipeline task."""
    import asyncio
    from types import SimpleNamespace
    from character_swap.models import CharStatus, Job, VideoStatus, VideoVariant

    good = _pipeline_eligible_jc("good", tmp_path)
    rejected = _pipeline_eligible_jc("rejected", tmp_path)
    rejected.status = CharStatus.REJECTED
    no_approval = _pipeline_eligible_jc("no_approval", tmp_path)
    no_approval.approved_variant_ids = []
    no_approval.approved_variant_id = None
    no_video = _pipeline_eligible_jc("no_video", tmp_path)
    no_video.videos = [VideoVariant(video_id="v_nv", grok_job_id="g_nv",
                                    status=VideoStatus.PROCESSING,
                                    source_variant_id="var_no_video",
                                    final_video_path="/tmp/never.mp4")]

    chars = {c.char_id: c for c in (good, rejected, no_approval, no_video)}
    job = Job(job_id="j1", scene_id="sc1", scene_image_path="/tmp/sc1.png",
              scene_ids=["sc1"], characters=chars)

    monkeypatch.setattr(runner_pipeline, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job))
    monkeypatch.setattr(runner_pipeline, "_persist_pipeline",
                        lambda *a, **k: None)

    scheduled: list[str] = []

    async def fake_run_char(job_id, cid):
        scheduled.append(cid)
    monkeypatch.setattr(runner_pipeline, "_run_char_pipeline", fake_run_char)

    asyncio.run(runner_pipeline.run_full_pipeline("j1"))
    assert scheduled == ["good"]
