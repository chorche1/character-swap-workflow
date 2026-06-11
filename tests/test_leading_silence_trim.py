"""Always-on audio-onset start trim (Hugo, 2026-06-11).

Every clip entering any pipeline — Editor auto_edit, multi_auto_edit per
clip, Step-6 compile per scene, Reengineer assemble per scene — is first cut
to AUDIO onset, unconditionally (the enable_trim toggle governs interior
pauses only). The marker is audio ENERGY (silencedetect), deliberately not
Whisper's first word.

These tests exercise the primitive end-to-end with real ffmpeg-synthesized
clips, plus the Reengineer assemble integration.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from character_swap import runner_reengineer, video_edit
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
)


def _clip(dest: Path, *, lead_silence: float = 0.0, tone_secs: float = 2.0,
          tone_volume_db: float = 0.0, no_audio: bool = False) -> Path:
    """Solid-color clip with `lead_silence` s of digital silence followed by
    `tone_secs` of a 440Hz tone (at `tone_volume_db`)."""
    total = lead_silence + tone_secs
    args = ["ffmpeg", "-hide_banner", "-y",
            "-f", "lavfi", "-i", f"color=c=red:s=160x284:d={total}:r=12"]
    if not no_audio:
        afilter = (f"volume={tone_volume_db}dB,"
                   f"adelay={int(lead_silence * 1000)}:all=1,"
                   f"apad=whole_dur={total}")
        args += ["-f", "lavfi", "-i",
                 f"sine=frequency=440:sample_rate=44100:duration={tone_secs}",
                 "-filter_complex", f"[1:a]{afilter}[a]",
                 "-map", "0:v", "-map", "[a]"]
    args += ["-pix_fmt", "yuv420p", "-shortest", str(dest)]
    subprocess.run(args, check=True, capture_output=True)
    return dest


# ----------------------------------------------------------- the primitive

def test_leading_silence_is_cut_to_audio_onset(tmp_path):
    src = _clip(tmp_path / "lead.mp4", lead_silence=1.0, tone_secs=2.0)
    out = tmp_path / "cut.mp4"
    info = video_edit.trim_leading_silence(src, out, min_silence_secs=0.05)
    assert info["leading_silence_secs"] == pytest.approx(1.0, abs=0.3)
    assert info["trimmed_duration"] == pytest.approx(2.0, abs=0.4)
    assert out.exists()


def test_clip_already_starting_on_audio_is_untouched(tmp_path):
    src = _clip(tmp_path / "nolead.mp4", lead_silence=0.0, tone_secs=2.0)
    out = tmp_path / "cut.mp4"
    info = video_edit.trim_leading_silence(src, out, min_silence_secs=0.05)
    assert info["leading_silence_secs"] == 0.0
    assert info["trimmed_duration"] == pytest.approx(info["original_duration"])


def test_video_only_clip_passes_through(tmp_path):
    src = _clip(tmp_path / "mute.mp4", tone_secs=2.0, no_audio=True)
    out = tmp_path / "cut.mp4"
    info = video_edit.trim_leading_silence(src, out, min_silence_secs=0.05)
    assert info["leading_silence_secs"] == 0.0
    assert out.exists()


def test_sub_threshold_audio_counts_as_silence(tmp_path):
    """Quiet room tone below threshold_db is NOT "enough audio" — a clip
    whose first second is a -50dB murmur still gets cut to where the real
    sound starts when judged against the -30dB default."""
    quiet = _clip(tmp_path / "quiet-part.mp4", tone_secs=1.0,
                  tone_volume_db=-50.0)
    loud = _clip(tmp_path / "loud-part.mp4", tone_secs=2.0)
    src = tmp_path / "combo.mp4"
    listfile = tmp_path / "concat.txt"
    listfile.write_text(f"file '{quiet}'\nfile '{loud}'\n")
    subprocess.run(["ffmpeg", "-hide_banner", "-y", "-f", "concat",
                    "-safe", "0", "-i", str(listfile),
                    "-c:v", "libx264", "-c:a", "aac", str(src)],
                   check=True, capture_output=True)
    out = tmp_path / "cut.mp4"
    info = video_edit.trim_leading_silence(src, out, threshold_db=-30.0,
                                           min_silence_secs=0.05)
    assert info["leading_silence_secs"] == pytest.approx(1.0, abs=0.35)


# ------------------------------------------------- Reengineer assemble

def test_assemble_cuts_each_scene_clip_to_audio_onset(tmp_path, monkeypatch):
    """Two Kling clips with 1s of dead air each → the per-character final
    is ~2s shorter than the raw clips combined."""
    run_dir = tmp_path / "re_run"
    run_dir.mkdir()
    clip_a = _clip(tmp_path / "scene-a.mp4", lead_silence=1.0, tone_secs=2.0)
    clip_b = _clip(tmp_path / "scene-b.mp4", lead_silence=1.0, tone_secs=2.0)

    v_a = GeneratedImage(variant_id="va", path="/a.png", prompt="p",
                         scene_id="s1", status="ready")
    v_b = GeneratedImage(variant_id="vb", path="/b.png", prompt="p",
                         scene_id="s2", status="ready")
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/c.png",
        status=CharStatus.DONE, images=[v_a, v_b],
        approved_variant_ids=["va", "vb"],
        videos=[
            VideoVariant(video_id="vidA", grok_job_id="g1",
                         status=VideoStatus.DONE, source_variant_id="va",
                         final_video_path=str(clip_a)),
            VideoVariant(video_id="vidB", grok_job_id="g2",
                         status=VideoStatus.DONE, source_variant_id="vb",
                         final_video_path=str(clip_b)),
        ])
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path="/p.png", characters={"cA": jc})

    class _S:
        def get_job(self, jid):
            return job if jid == "j1" else None
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    updates: dict = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.update(kw))

    # Original scene durations = the full 3s (lead + tone): the onset trim is
    # what shortens the final, NOT the duration cap.
    state = {"re_id": "re_t", "job_id": "j1",
             "scenes": [{"scene_id": "s1", "duration": 3.0},
                        {"scene_id": "s2", "duration": 3.0}]}

    asyncio.run(runner_reengineer._do_assemble("re_t", state))

    assert updates["status"] == "done"
    assert updates["finals_stale"] is False        # edit-mode flag cleared
    final = Path(updates["finals"]["cA"]["final_path"])
    assert final.exists()
    # 2 clips × ~2s tone after the ~1s lead is cut from each.
    assert video_edit._probe_duration(final) == pytest.approx(4.0, abs=0.8)


def test_assemble_still_caps_at_original_scene_duration(tmp_path, monkeypatch):
    """A clip with no lead but 3s of tone against a 2s original scene is
    still capped at the original duration (never longer than the original)."""
    run_dir = tmp_path / "re_run"
    run_dir.mkdir()
    clip_a = _clip(tmp_path / "scene-a.mp4", lead_silence=0.0, tone_secs=3.0)

    v_a = GeneratedImage(variant_id="va", path="/a.png", prompt="p",
                         scene_id="s1", status="ready")
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/c.png",
        status=CharStatus.DONE, images=[v_a],
        approved_variant_ids=["va"],
        videos=[VideoVariant(video_id="vidA", grok_job_id="g1",
                             status=VideoStatus.DONE, source_variant_id="va",
                             final_video_path=str(clip_a))])
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_image_path="/p.png", characters={"cA": jc})

    class _S:
        def get_job(self, jid):
            return job
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    updates: dict = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.update(kw))

    state = {"re_id": "re_t", "job_id": "j1",
             "scenes": [{"scene_id": "s1", "duration": 2.0}]}
    asyncio.run(runner_reengineer._do_assemble("re_t", state))

    assert updates["status"] == "done"
    final = Path(updates["finals"]["cA"]["final_path"])
    assert video_edit._probe_duration(final) == pytest.approx(2.0, abs=0.4)
