"""Reengineer scene analyst must get a generous per-call timeout (Hugo
2026-06-20).

A multi-scene recipe sends ~40 images to Opus and routinely needs >120s. The
shared Anthropic client caps every call at 120s, so the analyst timed out
(2 attempts ≈ 250s → APITimeoutError) and SILENTLY fell back to a generic
"The person continues the action visible in the image naturally" motion prompt
that doesn't describe what the subject actually does. Fix = a longer per-call
timeout (passed through messages_with_tools) + a trimmed frame budget.
"""
from __future__ import annotations

from character_swap import reengineer
from character_swap.clients import anthropic_client
from character_swap.config import settings
from character_swap.video_edit import Word


def test_default_analyst_timeout_is_generous():
    # Must clear the real ~250s the giant request was observed to need.
    assert settings.reengineer_analyst_timeout_secs >= 250.0


def test_analyze_scenes_passes_the_longer_timeout(monkeypatch, tmp_path):
    """analyze_scenes must hand messages_with_tools the analyst timeout so the
    one big vision call isn't killed by the shared 120s client default."""
    seen: dict = {}

    def fake_call(**kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(anthropic_client, "messages_with_tools", fake_call)
    monkeypatch.setattr(
        anthropic_client, "extract_tool_call",
        lambda resp, name: {"scenes": [
            {"idx": 0, "motion_prompt": "He pours baking soda into the glass.",
             "speech": "add baking soda", "summary": "pour"}]})
    monkeypatch.setattr(anthropic_client, "_file_to_image_block",
                        lambda p: {"type": "image_stub", "path": str(p)})

    frame = tmp_path / "scene-00.png"
    frame.write_bytes(b"x")
    plans = reengineer.analyze_scenes(
        frames=[frame],
        spans=[(0.0, 3.6)],
        words=[Word("add", 0.2, 0.6), Word("baking", 0.6, 1.0),
               Word("soda", 1.0, 1.4)],
        re_id="re_test",
        motion_frames=[[(frame, 0.0)]],
    )
    assert plans is not None
    assert seen.get("timeout") == settings.reengineer_analyst_timeout_secs
    assert seen["timeout"] >= 250.0


def test_messages_with_tools_applies_per_call_timeout(monkeypatch):
    """The timeout kwarg must reach the SDK via with_options(timeout=...) —
    overriding the shared client's 120s cap for this call only."""
    captured: dict = {}

    class FakeMessages:
        def create(self, **kw):
            return object()

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

        def with_options(self, **kw):
            captured.update(kw)
            return self

    monkeypatch.setattr(anthropic_client, "_client", lambda: FakeClient())
    anthropic_client.messages_with_tools(
        system="s", messages=[], tools=[], phase="reengineer_analyze",
        timeout=300.0)
    assert captured.get("timeout") == 300.0


def test_no_timeout_leaves_client_untouched(monkeypatch):
    """Callers that don't pass timeout (Director, QC) keep the shared 120s
    client — with_options must NOT be invoked for them."""
    called = {"with_options": False}

    class FakeMessages:
        def create(self, **kw):
            return object()

    class FakeClient:
        def __init__(self):
            self.messages = FakeMessages()

        def with_options(self, **kw):
            called["with_options"] = True
            return self

    monkeypatch.setattr(anthropic_client, "_client", lambda: FakeClient())
    anthropic_client.messages_with_tools(
        system="s", messages=[], tools=[], phase="director_swap")
    assert called["with_options"] is False
