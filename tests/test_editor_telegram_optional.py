"""A machine with no Telegram destination must not report a failed delivery.

2026-08-04, found while packaging the Editor for another Mac: the automatic
post-render delivery called editor_telegram_send unconditionally, so on an
install without TELEGRAM_EDITOR_* every successful render came back with
`telegram: {ok: false, error: "TELEGRAM_EDITOR_BOT_TOKEN saknas i .env."}`
— which app.js turns into a red "Finalen är klar men Telegram misslyckades"
toast. Nothing was ever meant to be sent there.

"Configured and the send failed" must stay loud; "never configured here"
must be silent. Manual ➤ clicks keep answering with the 409 either way —
there the user explicitly asked.
"""
from __future__ import annotations

import inspect

from character_swap import api
from character_swap.config import settings


def _set(monkeypatch, token: str, chat: str) -> None:
    monkeypatch.setattr(settings, "telegram_editor_bot_token", token)
    monkeypatch.setattr(settings, "telegram_editor_chat_id", chat)


def test_configured_requires_both_token_and_chat(monkeypatch):
    _set(monkeypatch, "", "")
    assert api._editor_telegram_configured() is False
    _set(monkeypatch, "123:AA", "")
    assert api._editor_telegram_configured() is False
    _set(monkeypatch, "", "-100123")
    assert api._editor_telegram_configured() is False
    _set(monkeypatch, "123:AA", "-100123")
    assert api._editor_telegram_configured() is True


def test_manual_send_still_refuses_loudly(monkeypatch):
    """The 409 is the right answer for an explicit click — only the
    AUTOMATIC path goes quiet."""
    import pytest
    from fastapi import HTTPException
    _set(monkeypatch, "", "")
    with pytest.raises(HTTPException) as e:
        api._require_editor_telegram()
    assert e.value.status_code == 409
    assert "TELEGRAM_EDITOR_BOT_TOKEN" in str(e.value.detail)


def test_both_auto_send_sites_are_guarded():
    """Guard the automatic delivery in BOTH editor pipelines — single-clip
    and multi-clip. A new pipeline that forgets it re-opens the bug."""
    for fn in (api.editor_auto_edit, api.editor_multi_auto_edit):
        src = inspect.getsource(fn)
        assert "editor_telegram_send" in src, fn.__name__
        guard = src.index("_editor_telegram_configured()")
        send = src.index("editor_telegram_send")
        assert guard < send, (
            f"{fn.__name__}: the auto-send must be gated on "
            "_editor_telegram_configured() BEFORE it is attempted")
