"""Tests for the Step-6 compile voice gate.

The compile's "Voice swap" checkbox (enable_voice_swap) must be able to KEEP the
original generated/Kling audio — even when a character has a library preset
voice or a batch voice_override is set. `_resolve_compile_voice` is the single
decision point the runner uses; testing it directly keeps the test fast and
avoids stubbing the whole concat/trim/transcribe pipeline.
"""
from __future__ import annotations

from character_swap.models import CharacterAsset
from character_swap.runner_compile import _resolve_compile_voice


def _char(voice_id):
    return CharacterAsset(char_id="c", filename="c.png", name="C", voice_id=voice_id)


def test_voice_off_keeps_original_audio_even_with_preset_or_override():
    # Voice swap OFF → None regardless of preset voice or batch override.
    assert _resolve_compile_voice(None, _char("preset_v"), False) is None
    assert _resolve_compile_voice("ov_123", _char("preset_v"), False) is None
    assert _resolve_compile_voice("ov_123", _char(None), False) is None


def test_voice_on_prefers_override_then_preset():
    # Override wins over the character's preset...
    assert _resolve_compile_voice("ov_123", _char("preset_v"), True) == "ov_123"
    # ...preset used when there's no override...
    assert _resolve_compile_voice(None, _char("preset_v"), True) == "preset_v"
    assert _resolve_compile_voice("", _char("preset_v"), True) == "preset_v"


def test_voice_on_but_nothing_set_is_no_swap():
    # On, but neither override nor preset → still no swap (None).
    assert _resolve_compile_voice(None, _char(None), True) is None
    assert _resolve_compile_voice("   ", _char(""), True) is None
    assert _resolve_compile_voice(None, None, True) is None
