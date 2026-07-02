"""transcribe_words scratch-wav cleanup (2026-07-02 reliability audit).

The extracted `<video>.audio.wav` was only unlinked on the success path — a
failing Whisper API call (quota, 5xx, network) leaked one wav per attempt,
next to the user's clips, forever. The extraction now sits in a try/finally.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from character_swap import video_edit


def _clip(dest: Path, *, secs: float = 1.0) -> Path:
    """Tiny solid-color clip WITH a tone track (transcribe needs audio)."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-y",
         "-f", "lavfi", "-i", f"color=c=red:s=160x284:d={secs}:r=12",
         "-f", "lavfi", "-i",
         f"sine=frequency=440:sample_rate=44100:duration={secs}",
         "-map", "0:v", "-map", "1:a",
         "-pix_fmt", "yuv420p", "-shortest", str(dest)],
        check=True, capture_output=True)
    return dest


class _FailingClient:
    class audio:
        class transcriptions:
            @staticmethod
            def create(**kw):
                raise RuntimeError("whisper 500")


class _OkClient:
    class audio:
        class transcriptions:
            @staticmethod
            def create(**kw):
                class _R:
                    words = [{"word": "hi", "start": 0.0, "end": 0.4}]
                return _R()


def test_failed_whisper_call_cleans_up_extracted_wav(tmp_path, monkeypatch):
    src = _clip(tmp_path / "talk.mp4")
    monkeypatch.setattr(video_edit.openai_image, "_client",
                        lambda: _FailingClient())

    with pytest.raises(RuntimeError, match="whisper 500"):
        video_edit.transcribe_words(src)

    # Fail-loud is preserved (the error propagated) AND the scratch wav is gone.
    assert not src.with_suffix(".audio.wav").exists()


def test_successful_whisper_call_still_cleans_up_wav(tmp_path, monkeypatch):
    src = _clip(tmp_path / "talk.mp4")
    monkeypatch.setattr(video_edit.openai_image, "_client", lambda: _OkClient())

    words = video_edit.transcribe_words(src)

    assert [w.text for w in words] == ["hi"]
    assert not src.with_suffix(".audio.wav").exists()
