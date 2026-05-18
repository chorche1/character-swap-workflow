"""Tests for the Resolve project exporter (SRT writer + zip builder).

Pure stdlib — no Resolve needed, no network, no ffmpeg. The starter script
goes in the zip as a string so we don't need Resolve to verify the bundle.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from character_swap.exporter import (
    _format_srt_timestamp,
    build_export_zip,
    write_srt,
)


# --- SRT timestamp formatting ----------------------------------------------------------


@pytest.mark.parametrize("secs,expected", [
    (0.0,        "00:00:00,000"),
    (1.5,        "00:00:01,500"),
    (61.234,     "00:01:01,234"),
    (3725.999,   "01:02:05,999"),
    (-1.0,       "00:00:00,000"),   # negatives clamp to zero
    (0.0001,     "00:00:00,000"),   # sub-millisecond rounds down
    (0.0006,     "00:00:00,001"),   # sub-millisecond rounds up
])
def test_format_srt_timestamp_shapes(secs, expected):
    assert _format_srt_timestamp(secs) == expected


# --- SRT writer -------------------------------------------------------------------------


def test_write_srt_empty_words_produces_empty_file(tmp_path):
    out = tmp_path / "captions.srt"
    write_srt([], out)
    assert out.read_text(encoding="utf-8") == ""


def test_write_srt_groups_three_words_per_cue_by_default(tmp_path):
    words = [
        {"text": "Never", "start": 0.0, "end": 0.4},
        {"text": "buy",   "start": 0.5, "end": 0.8},
        {"text": "honey", "start": 0.9, "end": 1.4},
        {"text": "from",  "start": 1.5, "end": 1.8},
        {"text": "the",   "start": 1.9, "end": 2.0},
        {"text": "store", "start": 2.1, "end": 2.7},
    ]
    out = tmp_path / "captions.srt"
    write_srt(words, out)
    text = out.read_text(encoding="utf-8")
    # Two cues (3 + 3 words), numbered 1 and 2.
    assert "1\n00:00:00,000 --> 00:00:01,400\nNever buy honey" in text
    assert "2\n00:00:01,500 --> 00:00:02,700\nfrom the store" in text


def test_write_srt_respects_words_per_line_override(tmp_path):
    words = [{"text": f"w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(6)]
    out = tmp_path / "captions.srt"
    write_srt(words, out, words_per_line=2)
    text = out.read_text(encoding="utf-8")
    # 6 words / 2 = 3 cues
    assert text.count("-->") == 3
    assert "w0 w1" in text
    assert "w2 w3" in text
    assert "w4 w5" in text


def test_write_srt_handles_end_before_start_safely(tmp_path):
    """Bad whisper output where end <= start shouldn't crash; cue duration
    should fall back to a small positive value."""
    words = [{"text": "oops", "start": 1.0, "end": 0.5}]
    out = tmp_path / "captions.srt"
    write_srt(words, out)
    text = out.read_text(encoding="utf-8")
    assert "1\n00:00:01,000 --> 00:00:01,300\noops" in text


def test_write_srt_skips_cues_with_only_whitespace(tmp_path):
    """If every word in a group is blank/whitespace the cue shouldn't appear."""
    words = [
        {"text": "real", "start": 0.0, "end": 0.5},
        {"text": "talk", "start": 0.5, "end": 1.0},
        {"text": "ok",   "start": 1.0, "end": 1.5},
        {"text": "  ",   "start": 1.5, "end": 2.0},  # blank
        {"text": "",     "start": 2.0, "end": 2.5},  # empty
        {"text": "\t",   "start": 2.5, "end": 3.0},  # whitespace
    ]
    out = tmp_path / "captions.srt"
    write_srt(words, out)
    text = out.read_text(encoding="utf-8")
    assert text.count("-->") == 1  # only the first cue (real talk ok)
    assert "real talk ok" in text


# --- Zip builder ------------------------------------------------------------------------


@pytest.fixture
def fake_videos(tmp_path: Path) -> tuple[Path, Path]:
    """Create two tiny fake mp4 files (any bytes — zip only reads them)."""
    final = tmp_path / "video-final.mp4"
    final.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    pre = tmp_path / "video-pre.mp4"
    pre.write_bytes(b"\x00\x00\x00\x18ftypmp42-pre")
    return final, pre


def test_build_export_zip_minimal_includes_required_files(fake_videos):
    final, _ = fake_videos
    zip_bytes = build_export_zip(
        final_video=final,
        pre_caption_video=None,
        words=None,
        project_name="my-edit",
    )
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert "my-edit/video-final.mp4" in names
        assert "my-edit/automate.py" in names
        assert "my-edit/README.md" in names
        # Optional bits omitted when inputs are missing:
        assert not any(n.endswith(".srt") for n in names)
        assert not any(n.endswith("words.json") for n in names)
        assert "my-edit/video-pre-captions.mp4" not in names


def test_build_export_zip_full_bundle_includes_srt_and_words(fake_videos):
    final, pre = fake_videos
    words = [
        {"text": "hello", "start": 0.0, "end": 0.5},
        {"text": "world", "start": 0.5, "end": 1.0},
    ]
    zip_bytes = build_export_zip(
        final_video=final,
        pre_caption_video=pre,
        words=words,
        project_name="full-export",
    )
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        assert {
            "full-export/video-final.mp4",
            "full-export/video-pre-captions.mp4",
            "full-export/captions.srt",
            "full-export/words.json",
            "full-export/automate.py",
            "full-export/README.md",
        } <= set(names)
        # words.json round-trips correctly
        loaded = json.loads(zf.read("full-export/words.json").decode("utf-8"))
        assert loaded == words
        # captions.srt is valid SRT (has the cue separator)
        srt = zf.read("full-export/captions.srt").decode("utf-8")
        assert "-->" in srt
        assert "hello world" in srt


def test_build_export_zip_starter_script_imports_resolve_module(fake_videos):
    """The starter script should reference DaVinciResolveScript so users know
    the integration is real, not a placeholder."""
    final, _ = fake_videos
    zip_bytes = build_export_zip(
        final_video=final, pre_caption_video=None,
        words=None, project_name="probe",
    )
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        script = zf.read("probe/automate.py").decode("utf-8")
        assert "DaVinciResolveScript" in script
        assert "GetProjectManager" in script
        assert "AppendToTimeline" in script or "CreateTimelineFromClips" in script


def test_build_export_zip_uses_project_name_as_folder_root(fake_videos):
    """Every file in the zip should be nested under the project_name folder
    so `unzip` produces a clean directory instead of dumping everything."""
    final, _ = fake_videos
    zip_bytes = build_export_zip(
        final_video=final, pre_caption_video=None,
        words=None, project_name="nested-name-test",
    )
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        assert all(n.startswith("nested-name-test/") for n in zf.namelist())
