"""The pad kept around speech is ASYMMETRIC (Hugo 2026-08-10).

One "Padding around speech" preset used to be applied to BOTH sides of every
cut. Hugo's directive: keep **0.1 s before speech resumes** and **0.15 s after
it stops** — a phrase's tail needs more breathing room than its head.

Two invariants this file locks, beyond the numbers:

  * `pad_end_secs=None` MIRRORS `pad_secs` in every primitive, so a caller
    written before the split is never silently retuned.
  * the exact-start contract survives: the FIRST keep range still gets ZERO
    pre-pad, so a clip begins exactly on its own audio onset (Hugo confirmed
    2026-08-10 that the split must not change how a hook starts).
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

from character_swap import api, auto_finalize, runner_compile, runner_reengineer
from character_swap.video_edit import (
    Word,
    _invert_silences,
    _word_gap_keep_ranges,
)

WEB = Path(__file__).resolve().parents[1] / "web"


# ------------------------------------------------------ the pure keep-builders

def test_invert_silences_pads_more_after_speech_than_before():
    """An INTERIOR keep range starts `pad_secs` early and ends `pad_end_secs`
    late — the two sides are independent."""
    # speech 0–1, pause 1–3, speech 3–5, pause 5–7, speech 7–10
    keep = _invert_silences([(1.0, 3.0), (5.0, 7.0)], total_duration=10.0,
                            pad_secs=0.1, pad_end_secs=0.15)
    assert len(keep) == 3
    # First keep: exact start (no pre-pad), 0.15 s of room after the last word.
    assert keep[0] == (0.0, 1.15)
    # Interior keep: 0.1 s BEFORE speech resumes, 0.15 s AFTER it stops.
    assert keep[1][0] == 2.9
    assert round(keep[1][1], 3) == 5.15
    # Tail keep: pre-pad only; trailing silence was already consumed.
    assert keep[2][0] == 6.9


def test_invert_silences_none_end_pad_mirrors_the_start_pad():
    """A caller that predates the split (no `pad_end_secs`) keeps its exact
    symmetric behaviour — the fallback is `pad_secs`, never a hardcoded 0.15."""
    sil = [(1.0, 3.0)]
    assert (_invert_silences(sil, 10.0, 0.07)
            == _invert_silences(sil, 10.0, 0.07, None)
            == _invert_silences(sil, 10.0, 0.07, 0.07))


def test_invert_silences_keeps_the_exact_start_contract():
    """Even with a wide end pad, the FIRST keep starts at 0.0 — leading
    silence is fully discarded (Hugo: "behåll exakt start")."""
    keep = _invert_silences([(2.0, 4.0)], total_duration=10.0,
                            pad_secs=0.5, pad_end_secs=0.9)
    assert keep[0][0] == 0.0


def test_word_gap_ranges_use_the_end_pad_after_a_word():
    """The word-gap trim cuts `pad_end_secs` after the previous word and
    `pad_secs` before the next one — and the trailing room tone survives for
    `pad_end_secs`, not `pad_secs`."""
    words = [Word("a", 0.5, 1.0), Word("b", 3.0, 3.5)]
    keep = _word_gap_keep_ranges(words, 6.0, max_gap_secs=0.35,
                                 pad_secs=0.1, pad_end_secs=0.15)
    # Leading silence before the first word is cut to pad_secs (start side).
    assert round(keep[0][0], 3) == 0.4
    # The pause is cut from (1.0 + end_pad) to (3.0 - start_pad).
    assert round(keep[0][1], 3) == 1.15
    assert round(keep[1][0], 3) == 2.9
    # Trailing room tone kept for the END pad.
    assert round(keep[1][1], 3) == 3.65


def test_word_gap_ranges_none_end_pad_mirrors_the_start_pad():
    words = [Word("a", 0.5, 1.0), Word("b", 3.0, 3.5)]
    assert (_word_gap_keep_ranges(words, 6.0, pad_secs=0.07)
            == _word_gap_keep_ranges(words, 6.0, pad_secs=0.07,
                                     pad_end_secs=0.07))


# ---------------------------------------------------------------- the presets

def _form_default(fn, name):
    p = inspect.signature(fn).parameters[name].default
    return getattr(p, "default", p)   # FastAPI Form(...) wraps the value


def test_editor_endpoints_preset_0_1_before_and_0_15_after():
    for fn in (api.editor_trim_silences, api.editor_auto_edit,
               api.editor_multi_auto_edit):
        assert _form_default(fn, "pad_secs") == 0.1, fn.__name__
        assert _form_default(fn, "pad_end_secs") == 0.15, fn.__name__


def test_request_bodies_preset_0_1_before_and_0_15_after():
    for body in (api.CompileVideosBody, api.RepurposeVideosBody,
                 api.EditorRepurposeBody):
        m = body()
        assert m.pad_secs == 0.1, body.__name__
        assert m.pad_end_secs == 0.15, body.__name__
    # The Reengineer ⚙ panel body is all-None ("keep what's stored").
    assert api.ReAssembleSettingsBody().pad_end_secs is None
    assert "pad_end_secs" in api.ReAssembleSettingsBody.model_fields


def test_runner_presets_are_asymmetric():
    assert runner_reengineer.ASSEMBLE_DEFAULTS["pad_secs"] == 0.1
    assert runner_reengineer.ASSEMBLE_DEFAULTS["pad_end_secs"] == 0.15
    assert auto_finalize._DEFAULT_COMPILE_SETTINGS["pad_secs"] == 0.1
    assert auto_finalize._DEFAULT_COMPILE_SETTINGS["pad_end_secs"] == 0.15
    # auto_finalize only forwards keys it lists — a preset it doesn't accept
    # would be dropped on the way to compile_job_videos.
    assert "pad_end_secs" in auto_finalize._COMPILE_KEYS
    for fn in (runner_compile.compile_job_videos,
               runner_compile.repurpose_editor_job):
        sig = inspect.signature(fn).parameters
        assert sig["pad_secs"].default == 0.1, fn.__name__
        assert sig["pad_end_secs"].default == 0.15, fn.__name__


def test_stored_reengineer_settings_can_override_the_end_pad():
    """`_assemble_settings` only accepts keys present in ASSEMBLE_DEFAULTS —
    the new one must be accepted, or the ⚙ panel silently loses the value."""
    cfg = runner_reengineer._assemble_settings(
        {"assemble_settings": {"pad_end_secs": 0.3}})
    assert cfg["pad_end_secs"] == 0.3
    assert cfg["pad_secs"] == 0.1          # untouched sibling


# ------------------------------------------------------------ the wiring path

def test_run_editor_pipeline_forwards_the_end_pad_to_assemble_clips(
        tmp_path, monkeypatch):
    """The shared pipeline (Step-6 compile + Reengineer assemble + repurpose)
    must hand BOTH pads to the single-encode assemble — dropping the second
    one would silently restore symmetric padding in every final."""
    import asyncio

    seen: dict = {}

    def fake_assemble(paths, out, **kw):
        seen.update(kw)
        Path(out).write_bytes(b"mp4")
        return {"clip_keeps": None}

    monkeypatch.setattr(runner_compile.video_edit, "assemble_clips",
                        fake_assemble)
    # Stop right after the assemble — a missing transcription raises, and the
    # kwargs we care about are already captured.
    monkeypatch.setattr(runner_compile.video_edit, "transcribe_words",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"x")
    try:
        asyncio.run(runner_compile.run_editor_pipeline(
            [clip], edit_id="ed_test", edit_dir=tmp_path / "ed",
            template="capcut-bluebox", overrides=None,
            enable_trim=True, enable_captions=False,
            enable_wpm_normalize=False, target_wpm=190.0,
            threshold_db=-24.0, min_silence_secs=0.4,
            pad_secs=0.1, pad_end_secs=0.15, voice_id=None,
            enable_transcribe=False,
        ))
    except Exception:                       # noqa: BLE001 — later steps aren't the point
        pass
    assert seen["pad_secs"] == 0.1
    assert seen["pad_end_secs"] == 0.15


# ------------------------------------------------------------- the JS mirror

def _js() -> str:
    return (WEB / "app.js").read_text()


def test_frontend_seeds_the_end_pad_everywhere_it_seeds_the_start_pad():
    """Every settings seed carrying `padSecs` must carry `padEndSecs` too —
    an undefined value renders as a crash in the `.toFixed(2)` slider readout
    and sends `undefined` to the server."""
    js = _js()
    assert js.count("padEndSecs: 0.15") == 4     # editor + compile + reAsm + repurpose
    assert js.count("padSecs:") == 4


def test_frontend_sends_the_end_pad_on_every_request_that_sends_the_start_pad():
    js = _js()
    assert js.count("fd.append('pad_secs'") == js.count("fd.append('pad_end_secs'") == 3
    assert len(re.findall(r"\bpad_secs:", js)) == len(re.findall(r"\bpad_end_secs:", js))
    # Stored settings rehydrate the panel (a run built before the split has no
    # stored value and must fall back to the seed, not to undefined).
    assert "if ('pad_end_secs' in s) o.padEndSecs = s.pad_end_secs;" in js


def test_ui_exposes_both_pads_wherever_it_exposed_the_single_one():
    html = (WEB / "index.html").read_text()
    assert html.count("padEndSecs") == html.count("padSecs")
