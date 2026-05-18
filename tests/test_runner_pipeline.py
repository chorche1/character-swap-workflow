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
