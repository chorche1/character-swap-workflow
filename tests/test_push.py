"""Tests for the ntfy phone-push helper (src/character_swap/push.py).

Pure request-building is tested directly; the network send is monkeypatched so
nothing leaves the machine.
"""
from __future__ import annotations

import character_swap.push as push
from character_swap.config import settings


def _set_topic(monkeypatch, topic="", server="https://ntfy.sh", click=""):
    monkeypatch.setattr(settings, "ntfy_topic", topic, raising=False)
    monkeypatch.setattr(settings, "ntfy_server", server, raising=False)
    monkeypatch.setattr(settings, "ntfy_click", click, raising=False)


def test_disabled_when_topic_unset(monkeypatch):
    _set_topic(monkeypatch, topic="")
    assert push.enabled() is False
    assert push.build_request("hi", "body") is None


def test_enabled_when_topic_set(monkeypatch):
    _set_topic(monkeypatch, topic="abc123")
    assert push.enabled() is True


def test_build_request_shape(monkeypatch):
    _set_topic(monkeypatch, topic="abc123", server="https://ntfy.sh/")
    req = push.build_request("Klar", "fyra scener", priority=4,
                             tags=["white_check_mark"])
    assert req is not None
    url, headers, body = req
    assert url == "https://ntfy.sh/abc123"          # trailing slash stripped
    assert headers["Title"] == "Klar"
    assert headers["Priority"] == "4"
    assert headers["Tags"] == "white_check_mark"
    assert body == "fyra scener".encode("utf-8")


def test_title_transliterated_to_ascii(monkeypatch):
    _set_topic(monkeypatch, topic="t")
    _, headers, _ = push.build_request("Granska klippen — välj förälder")
    # å/ä/ö transliterated, em-dash normalized, all ASCII.
    assert headers["Title"].isascii()
    assert headers["Title"] == "Granska klippen - valj foralder"


def test_body_keeps_utf8(monkeypatch):
    _set_topic(monkeypatch, topic="t")
    _, _, body = push.build_request("x", "förälder å ä ö 🎉")
    assert body.decode("utf-8") == "förälder å ä ö 🎉"


def test_priority_clamped(monkeypatch):
    _set_topic(monkeypatch, topic="t")
    _, h1, _ = push.build_request("x", priority=99)
    _, h2, _ = push.build_request("x", priority=-3)
    assert h1["Priority"] == "5"
    assert h2["Priority"] == "1"


def test_click_from_settings_and_override(monkeypatch):
    _set_topic(monkeypatch, topic="t", click="https://app.example.ts.net")
    _, h, _ = push.build_request("x")
    assert h["Click"] == "https://app.example.ts.net"
    # explicit click wins
    _, h2, _ = push.build_request("x", click="https://other")
    assert h2["Click"] == "https://other"


def test_notify_noop_when_disabled(monkeypatch):
    _set_topic(monkeypatch, topic="")
    sent: list = []
    monkeypatch.setattr(push, "_send", lambda req: sent.append(req))
    push.notify("hi", "body")
    assert sent == []


def test_notify_submits_when_enabled(monkeypatch):
    _set_topic(monkeypatch, topic="abc")
    sent: list = []
    monkeypatch.setattr(push, "_send", lambda req: sent.append(req))
    # Run the submitted work synchronously so the assertion is deterministic
    # (no thread-timing flakiness).
    monkeypatch.setattr(push._EXECUTOR, "submit",
                        lambda fn, req: fn(req))
    push.notify("Klar", "body", tags=["white_check_mark"])
    assert len(sent) == 1
    url, headers, body = sent[0]
    assert url.endswith("/abc")
    assert headers["Title"] == "Klar"


def test_notify_never_raises_on_send_error(monkeypatch):
    _set_topic(monkeypatch, topic="abc")

    def _boom(req):
        raise RuntimeError("network down")

    monkeypatch.setattr(push, "_send", _boom)
    monkeypatch.setattr(push._EXECUTOR, "submit", lambda fn, req: fn(req))
    # Must swallow — a push failure can never bubble into a render.
    push.notify("x", "y")


# --- runner integration: the milestone helpers call push.notify -------------

def test_reengineer_push_status_fires_on_gate(monkeypatch):
    from character_swap import runner_reengineer as rr

    calls: list = []
    monkeypatch.setattr(rr.push, "notify",
                        lambda title, body="", **kw: calls.append((title, body, kw)))
    rr._push_status({"re_id": "re_x", "scenes": [1, 2, 3]}, "awaiting_approval")
    assert len(calls) == 1
    title, body, kw = calls[0]
    assert title == "Granska klippen"
    assert "3 scener" in body
    assert kw["priority"] == 4


def test_reengineer_push_status_ignores_intermediate(monkeypatch):
    from character_swap import runner_reengineer as rr

    calls: list = []
    monkeypatch.setattr(rr.push, "notify",
                        lambda *a, **k: calls.append(a))
    # swapping/animating are progress, not milestones — no push.
    rr._push_status({"re_id": "re_x"}, "swapping")
    rr._push_status({"re_id": "re_x"}, "animating")
    assert calls == []


def test_reengineer_push_failed_includes_error(monkeypatch):
    from character_swap import runner_reengineer as rr

    calls: list = []
    monkeypatch.setattr(rr.push, "notify",
                        lambda title, body="", **kw: calls.append((title, body, kw)))
    rr._push_status({"re_id": "re_x", "error": "Kling timeout"}, "failed")
    assert calls
    title, body, kw = calls[0]
    assert "misslyckades" in title.lower()
    assert "Kling timeout" in body
    assert kw["priority"] == 5
