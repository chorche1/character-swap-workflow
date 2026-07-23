"""Regression: an ElevenLabs voice-swap failure must not kill an Editor render.

2026-07-24 bug — with voice swap ON by default (Jeff Bridges preset) and the
ElevenLabs account out of credits, every `/api/editor/multi_auto_edit` call
500'd at the voice-swap step ("Voice swap failed: ElevenLabsAccountError:
quota_exceeded") AFTER the clips were already trimmed, matched, and concat'd
— so multi-clip editing was completely dead while the Step-6 compile and
Reengineer assemble (which skip a failed voice swap with a loud warning)
kept delivering finals. Hugo's directive 2026-07-24: the Editor endpoints get
the same contract — skip the voice swap LOUDLY, keep rendering, and report
the reason on `voice_swap.skipped` / `voice_swap.error` so the UI can toast
it. Locks both `/api/editor/multi_auto_edit` and `/api/editor/auto_edit`.
"""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from character_swap import api, video_edit
from character_swap.clients import elevenlabs
from character_swap.clients.elevenlabs import ElevenLabsAccountError
from character_swap.config import settings

_SCRIPT = "hello there this is a test script for the reel"


def _words(*_a, **_k) -> list:
    return [video_edit.Word(text=t, start=i * 0.4, end=i * 0.4 + 0.4)
            for i, t in enumerate(_SCRIPT.split())]


def _touch(_src, dst, *_a, **_k):
    Path(dst).write_bytes(b"\x00")
    return {}


def _quota_exceeded(*_a, **_k):
    raise ElevenLabsAccountError(
        "Voice changer failed (401): quota_exceeded — 16 credits remaining")


def _common_mocks(monkeypatch, tmp_path):
    monkeypatch.setattr(video_edit, "transcribe_words", _words)
    monkeypatch.setattr(video_edit, "trim_leading_silence", _touch)
    monkeypatch.setattr(video_edit, "render_captions", _touch)
    monkeypatch.setattr(video_edit, "_probe_duration", lambda *_a, **_k: 4.0)
    # The wav-extract ffmpeg call inside the voice-swap step.
    monkeypatch.setattr(video_edit, "_run", lambda *_a, **_k: None)
    monkeypatch.setattr(elevenlabs, "voice_changer", _quota_exceeded)
    monkeypatch.setattr(type(settings), "require_keys",
                        lambda self, *_a, **_k: None)
    monkeypatch.setattr(type(settings), "has_provider",
                        lambda self, *_a, **_k: True)
    monkeypatch.setattr(settings, "output_dir", tmp_path, raising=False)


def test_multi_auto_edit_survives_voice_swap_failure(monkeypatch, tmp_path):
    _common_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(
        video_edit, "match_clips_by_transcript",
        lambda transcripts, script: [
            {"idx": i, "score": 1.0, "unmatched": False}
            for i in range(len(transcripts))],
    )
    monkeypatch.setattr(video_edit, "concat_videos",
                        lambda paths, out, **k: Path(out).write_bytes(b"\x00"))

    client = TestClient(api.app)
    resp = client.post(
        "/api/editor/multi_auto_edit",
        data={
            "script": _SCRIPT,
            "template": "capcut-bluebox",
            "voice_id": "4J4I0vMcRYqgQRmwxubI",
            "enable_trim": "false",
            "enable_captions": "true",
            "enable_wpm_normalize": "false",
            "enable_gap_trim": "false",
            "playback_speed": "1.0",
        },
        files=[
            ("files", ("clip-00.mp4", b"\x00\x00clip0", "video/mp4")),
            ("files", ("clip-01.mp4", b"\x00\x00clip1", "video/mp4")),
        ],
    )
    # The render must SUCCEED — the voice swap is skipped, not fatal…
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["output_url"]
    # …and the skip is LOUD: reason surfaced for the UI toast.
    assert data["voice_swap"]["skipped"] is True
    assert "ElevenLabsAccountError" in data["voice_swap"]["error"]


def test_auto_edit_survives_voice_swap_failure(monkeypatch, tmp_path):
    _common_mocks(monkeypatch, tmp_path)

    client = TestClient(api.app)
    resp = client.post(
        "/api/editor/auto_edit",
        data={
            "template": "capcut-bluebox",
            "voice_id": "4J4I0vMcRYqgQRmwxubI",
            "enable_trim": "false",
            "enable_captions": "true",
            "enable_wpm_normalize": "false",
            "enable_gap_trim": "false",
        },
        files=[("file", ("clip.mp4", b"\x00\x00clip", "video/mp4"))],
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["voice_swap"]["skipped"] is True
    assert "ElevenLabsAccountError" in data["voice_swap"]["error"]
