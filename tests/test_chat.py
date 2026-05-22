"""Tests for the Claude-driven Chat agent loop in `character_swap/chat.py`.

Focus on the loop mechanics and dispatcher routing — NOT the actual Anthropic
API calls or runner_media generations (those would be slow and require keys).
We mock anthropic_client.messages_with_tools to return synthetic responses.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from character_swap import chat as chat_mod
from character_swap.state import store


def _run(coro):
    """Tiny sync→async bridge so we don't need pytest-asyncio."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake Anthropic responses — control the stop_reason + content blocks the
# agent loop sees, so we can test branches without touching the network.
# ---------------------------------------------------------------------------

class _FakeContentBlock:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _FakeResponse:
    def __init__(self, stop_reason: str, content: list):
        self.stop_reason = stop_reason
        self.content = content


def _text_block(text: str):
    return _FakeContentBlock(type="text", text=text)


def _tool_use_block(name: str, args: dict, block_id: str = "tu_1"):
    return _FakeContentBlock(type="tool_use", id=block_id, name=name, input=args)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Force the JSON state backend onto a tmpdir so each test starts clean."""
    from character_swap.config import settings
    monkeypatch.setattr(settings, "use_sqlite_state", False, raising=False)
    monkeypatch.setattr(settings, "state_dir", tmp_path / "state", raising=False)
    monkeypatch.setattr(settings, "output_dir", tmp_path / "output", raising=False)
    monkeypatch.setattr(settings, "input_dir", tmp_path / "input", raising=False)
    monkeypatch.setattr(settings, "characters_dir", tmp_path / "chars", raising=False)
    # Reset the singleton.
    import character_swap.state as state_mod
    monkeypatch.setattr(state_mod, "_store", None, raising=False)
    yield


# ---------------------------------------------------------------------------
# new_chat + state persistence
# ---------------------------------------------------------------------------

def test_new_chat_creates_session():
    chat = chat_mod.new_chat()
    assert chat.chat_id.startswith("chat_")
    assert chat.title == "New chat"
    assert chat.messages == []
    fetched = store().get_chat(chat.chat_id)
    assert fetched is not None
    assert fetched.chat_id == chat.chat_id


def test_list_chats_returns_newest_first():
    a = chat_mod.new_chat()
    b = chat_mod.new_chat()
    listed = store().list_chats()
    ids = [c.chat_id for c in listed]
    assert ids[0] == b.chat_id  # most-recently-created wins
    assert a.chat_id in ids


# ---------------------------------------------------------------------------
# Agent loop: end_turn (no tool calls) → simple text reply
# ---------------------------------------------------------------------------

def test_run_turn_simple_text_reply(monkeypatch):
    chat = chat_mod.new_chat()

    def fake_messages_with_tools(**kw):
        return _FakeResponse(
            stop_reason="end_turn",
            content=[_text_block("Hello Hugo! What do you want to create?")],
        )
    monkeypatch.setattr(chat_mod.anthropic_client, "messages_with_tools",
                        fake_messages_with_tools)

    result = _run(chat_mod.run_turn(chat.chat_id, "Hi there"))
    assert len(result.messages) == 2
    assert result.messages[0] == {"role": "user", "content": "Hi there"}
    asst = result.messages[1]
    assert asst["role"] == "assistant"
    assert asst["content"][0]["type"] == "text"
    assert "Hello Hugo" in asst["content"][0]["text"]
    # Title auto-set from first user message.
    assert result.title == "Hi there"


# ---------------------------------------------------------------------------
# Agent loop: tool_use → execute → loop → end_turn
# ---------------------------------------------------------------------------

def test_run_turn_tool_use_then_end(monkeypatch):
    chat = chat_mod.new_chat()
    call_count = {"n": 0}

    def fake_messages_with_tools(**kw):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call: ask to list characters.
            return _FakeResponse(
                stop_reason="tool_use",
                content=[_tool_use_block("list_characters", {}, block_id="tu_a")],
            )
        # Second call: end the turn with a summary.
        return _FakeResponse(
            stop_reason="end_turn",
            content=[_text_block("You have 0 characters.")],
        )
    monkeypatch.setattr(chat_mod.anthropic_client, "messages_with_tools",
                        fake_messages_with_tools)

    result = _run(chat_mod.run_turn(chat.chat_id, "List my chars"))

    # Expect 4 messages: user, asst(tool_use), user(tool_result), asst(text)
    assert len(result.messages) == 4
    assert result.messages[0]["role"] == "user"
    assert result.messages[1]["role"] == "assistant"
    assert result.messages[1]["content"][0]["type"] == "tool_use"
    assert result.messages[1]["content"][0]["name"] == "list_characters"
    # Tool result block in next user message.
    tr = result.messages[2]
    assert tr["role"] == "user"
    assert tr["content"][0]["type"] == "tool_result"
    assert tr["content"][0]["tool_use_id"] == "tu_a"
    parsed = json.loads(tr["content"][0]["content"])
    assert parsed["count"] == 0
    # Final assistant text.
    assert "0 characters" in result.messages[3]["content"][0]["text"]


# ---------------------------------------------------------------------------
# Tool dispatcher routes correctly
# ---------------------------------------------------------------------------

def test_dispatch_unknown_tool_returns_error():
    chat = chat_mod.new_chat()
    result = _run(chat_mod._dispatch_tool("does_not_exist", {}, chat))
    assert "error" in result
    assert "unknown tool" in result["error"]


def test_dispatch_list_characters_runs():
    chat = chat_mod.new_chat()
    result = _run(chat_mod._dispatch_tool("list_characters", {}, chat))
    assert result == {"characters": [], "count": 0}


def test_dispatch_list_scenes_runs():
    chat = chat_mod.new_chat()
    result = _run(chat_mod._dispatch_tool("list_scenes", {}, chat))
    assert result == {"scenes": [], "count": 0}


def test_dispatch_list_available_models_returns_provider_flags(monkeypatch):
    chat = chat_mod.new_chat()
    from character_swap.config import settings
    monkeypatch.setattr(settings, "openai_api_key", "test", raising=False)
    monkeypatch.setattr(settings, "fal_api_key", "test", raising=False)
    result = _run(chat_mod._dispatch_tool("list_available_models", {}, chat))
    assert "providers" in result
    assert "active" in result
    assert "openai" in result["active"]
    assert "fal" in result["active"]


# ---------------------------------------------------------------------------
# Tool defs are well-formed JSON schemas
# ---------------------------------------------------------------------------

def test_all_tool_defs_have_required_fields():
    for t in chat_mod.TOOL_DEFS:
        assert "name" in t
        assert "description" in t
        assert "input_schema" in t
        sch = t["input_schema"]
        assert sch.get("type") == "object"
        assert "properties" in sch


def test_every_tool_def_has_a_dispatcher():
    for t in chat_mod.TOOL_DEFS:
        assert t["name"] in chat_mod.TOOL_DISPATCHERS, \
            f"tool {t['name']} declared but no dispatcher registered"


# ---------------------------------------------------------------------------
# Loop guard: stop after max_iterations
# ---------------------------------------------------------------------------

def test_run_turn_caps_runaway_loops(monkeypatch):
    chat = chat_mod.new_chat()

    def fake_messages_with_tools(**kw):
        # Forever ask for a list_scenes (cheap, no side effects).
        return _FakeResponse(
            stop_reason="tool_use",
            content=[_tool_use_block("list_scenes", {})],
        )
    monkeypatch.setattr(chat_mod.anthropic_client, "messages_with_tools",
                        fake_messages_with_tools)

    result = _run(chat_mod.run_turn(chat.chat_id, "loop pls", max_iterations=3))
    last = result.messages[-1]
    assert last["role"] == "assistant"
    assert "Stopped after 3 tool iterations" in last["content"][0]["text"]
