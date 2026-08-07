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


# --- UI reachability ---------------------------------------------------------
#
# The resolver above is only reachable if the user can actually SET a preset
# voice. The 🎤 picker shipped in 36b7b5e and was silently dropped from the
# markup in 8131edf when the library card was rewritten — leaving the model
# field, the PATCH endpoint, the SQLite column, `setCharacterVoice()` and this
# very resolver all alive but unreachable from the UI. These tests lock the
# whole chain, not just its last link.

def _web(name: str) -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "web" / name).read_text()


def test_library_card_has_a_preset_voice_picker():
    """A character's preset voice must be settable from the library card."""
    html = _web("index.html")
    assert "setCharacterVoice(ch.char_id" in html, (
        "the 🎤 preset-voice picker is missing from the library card — "
        "the preset becomes unreachable even though the backend still honors it"
    )
    # It must be populated from the loaded ElevenLabs voices, not hardcoded.
    assert "elevenlabsVoices" in html
    # ...and offer a way back to 'no preset' (keep the clip's own audio).
    assert 'ingen preset' in html


def test_set_character_voice_patches_the_character():
    """The picker's handler must PATCH the character, clearing on empty."""
    app_js = _web("app.js")
    assert "async setCharacterVoice(charId, voiceId)" in app_js
    body = app_js.split("async setCharacterVoice(charId, voiceId)")[1][:600]
    assert "'/api/characters/' + charId" in body
    assert "PATCH" in body
    # Empty selection clears the preset rather than sending undefined.
    assert "voiceId || ''" in body
