"""Regression: the Editor multi-clip endpoint must rescue lost captions.

2026-06-23 bug — `/api/editor/multi_auto_edit` burned captions from a single
PLAIN Whisper transcribe of the stitched reel, with no script-based safety
net. On a 4-clip Kling reel Whisper caught only the first clip's words, so
~18 s of a 21 s video had NO captions. The Step-6 compile / Reengineer
assemble already guard this via `runner_compile._resolve_caption_words`
(best-of-both transcript + even-timed script fallback); the Editor multi-clip
endpoint and its Drive-watcher twin bypassed it. This locks the wiring: when
Whisper drops most of the audio but the full script is known, the persisted
`words.json` is rebuilt from the script so captions span the whole video.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from character_swap import api, video_edit
from character_swap.config import settings

# A realistic multi-clip ad script (4 clips' worth of dialogue).
_SCRIPT = (
    "Pour Listerine directly on your skin tags and just watch what happens. "
    "The antiseptic compounds in Listerine help break down the tissue around "
    "the tag. The skin tag starts to dry out and shrink. Do this a few times "
    "a month, no cutting, no freezing, no office visit needed."
)
# What Whisper actually returns on the stitched reel: ONLY the first clip's
# opening words (the skip failure), every call.
_SKIP_TEXT = "Pour Listerine directly on your skin tags"


def _skip_words(*_a, **_k) -> list:
    return [video_edit.Word(text=t, start=float(i) * 0.3, end=float(i) * 0.3 + 0.3)
            for i, t in enumerate(_SKIP_TEXT.split())]


def _touch(_src, dst, *_a, **_k):
    Path(dst).write_bytes(b"\x00")
    return {}


def test_multi_clip_captions_fall_back_to_script_when_whisper_skips(
        monkeypatch, tmp_path):
    # Whisper always returns the short skip transcript (per-clip + the caption
    # pass + the internal unprompted pass in _resolve_caption_words).
    monkeypatch.setattr(video_edit, "transcribe_words", _skip_words)
    monkeypatch.setattr(
        video_edit, "match_clips_by_transcript",
        lambda transcripts, script: [
            {"idx": i, "score": 1.0, "unmatched": False}
            for i in range(len(transcripts))],
    )
    monkeypatch.setattr(video_edit, "trim_leading_silence", _touch)
    monkeypatch.setattr(video_edit, "concat_videos",
                        lambda paths, out, **k: Path(out).write_bytes(b"\x00"))
    monkeypatch.setattr(video_edit, "_probe_duration", lambda *_a, **_k: 21.0)
    monkeypatch.setattr(video_edit, "render_captions", _touch)
    monkeypatch.setattr(type(settings), "require_keys",
                        lambda self, *_a, **_k: None)
    monkeypatch.setattr(settings, "output_dir", tmp_path, raising=False)

    client = TestClient(api.app)
    files = [
        ("files", ("clip-00.mp4", b"\x00\x00clip0", "video/mp4")),
        ("files", ("clip-01.mp4", b"\x00\x00clip1", "video/mp4")),
        ("files", ("clip-02.mp4", b"\x00\x00clip2", "video/mp4")),
        ("files", ("clip-03.mp4", b"\x00\x00clip3", "video/mp4")),
    ]
    resp = client.post(
        "/api/editor/multi_auto_edit",
        data={
            "script": _SCRIPT,
            "template": "capcut-bluebox",
            "enable_trim": "false",
            "enable_captions": "true",
            "enable_wpm_normalize": "false",
            "enable_gap_trim": "false",
            "playback_speed": "1.0",   # skip the time-stretch branch
        },
        files=files,
    )
    assert resp.status_code == 200, resp.text
    edit_id = resp.json()["edit_id"]

    words = json.loads((tmp_path / "editor" / edit_id / "words.json")
                       .read_text(encoding="utf-8"))
    # The skip transcript is 8 words; the script is far longer. The fallback
    # must have rebuilt the FULL script (every whitespace token, matching
    # video_edit.script_fallback_words), evenly timed across the whole 21 s —
    # not the 8-word tail that left most of the video uncaptioned.
    assert len(words) == len(_SCRIPT.split())
    assert words[0]["start"] == 0.0
    assert words[-1]["end"] >= 20.0           # captions reach the END of the reel
    assert len(words) > len(_SKIP_TEXT.split())
