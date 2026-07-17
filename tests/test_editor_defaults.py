"""Editor-wide default settings (Hugo 2026-06-21).

The standard across ALL editor entry points: capcut-bluebox template, caption
size 56 (the bluebox baseline, Hugo 2026-07-04 — was 60), target WPM 190, trim
ON, captions ON, WPM normalize OFF, threshold -24 dB, min silence 0.4 s, padding
0.1 s, word-gap-trim OFF, speed 1.05. Lock the backend Form defaults on every
editor endpoint so a no-settings (or stale-client) call uses the same standard
the UI shows. Voice swap now defaults ON for Reengineer auto-assembly (Hugo
2026-07-17) — see test_reengineer_auto_assemble_voice_swap_on_by_default.
"""
from __future__ import annotations

import inspect

from character_swap import api
from character_swap import runner_reengineer, video_edit


def test_caption_size_standard_is_56_everywhere():
    """The caption-size preset is 56 across every flow (Hugo 2026-07-04): the
    Swap Step-6 compile + standalone Editor read the bluebox template size
    directly, and Reengineer/Repurpose ride a matching size=56 override."""
    assert video_edit.TEMPLATES["capcut-bluebox"].size == 56
    assert runner_reengineer.ASSEMBLE_DEFAULTS["overrides"] == {"size": 56}


def test_reengineer_auto_assemble_voice_swap_on_by_default():
    """Voice swap defaults ON for AUTO-assembled Reengineer finals (Hugo
    2026-07-17) — a run that assembles without the ⚙ panel ever being opened
    swaps to each character's preset voice. voice_override stays None so it's
    the per-character preset (a preset-less character keeps its own voice, since
    runner_compile._resolve_compile_voice returns None for it)."""
    assert runner_reengineer.ASSEMBLE_DEFAULTS["enable_voice_swap"] is True
    assert runner_reengineer.ASSEMBLE_DEFAULTS["voice_override"] is None


def _form_default(fn, name):
    """The effective default of a (possibly Form-wrapped) endpoint param."""
    p = inspect.signature(fn).parameters[name].default
    return getattr(p, "default", p)   # FastAPI Form(...) wraps the value


def test_auto_edit_defaults_are_editor_standard():
    f = api.editor_auto_edit
    assert _form_default(f, "template") == "capcut-bluebox"
    assert _form_default(f, "threshold_db") == -24.0
    assert _form_default(f, "min_silence_secs") == 0.4
    assert _form_default(f, "pad_secs") == 0.1
    assert _form_default(f, "enable_wpm_normalize") is False
    assert _form_default(f, "target_wpm") == 190.0


def test_multi_auto_edit_defaults_are_editor_standard():
    f = api.editor_multi_auto_edit
    assert _form_default(f, "template") == "capcut-bluebox"
    assert _form_default(f, "threshold_db") == -24.0
    assert _form_default(f, "min_silence_secs") == 0.4
    assert _form_default(f, "pad_secs") == 0.1
    assert _form_default(f, "enable_wpm_normalize") is False
    assert _form_default(f, "playback_speed") == 1.05


def test_trim_captions_rerender_defaults_are_editor_standard():
    assert _form_default(api.editor_trim_silences, "threshold_db") == -24.0
    assert _form_default(api.editor_trim_silences, "min_silence_secs") == 0.4
    assert _form_default(api.editor_trim_silences, "pad_secs") == 0.1
    assert _form_default(api.editor_captions, "template") == "capcut-bluebox"
    assert _form_default(api.editor_rerender, "template") == "capcut-bluebox"
