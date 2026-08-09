"""Telegram delivery must never be blocked by a BUILD guard (Hugo 2026-08-03).

re_4906fac466: the repurpose build finished, all 7 mirrored finals were on
disk and visible in the UI — and every ➤ Telegram click was refused with
"Bygget pågår — vänta tills finalen är klar". The build was NOT running. The
automatic delivery was: `_do_repurpose` called the Telegram auto-send from
INSIDE the `_REPURPOSING` in-flight guard, so 7 × ~34 MB of sequential uploads
(600 s timeout × 3 attempts each) held the "a build is running" flag for as
long as the delivery took, and the endpoint reported that as a build.

`assemble()` already had this right — it sends AFTER releasing `_ASSEMBLING`.
These tests lock the same contract for `repurpose()`, plus the per-target
send lock that replaced the blunt run-wide block: only the ONE video being
uploaded right now is refused (and honestly), never its run-mates.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import api, auto_finalize, runner_reengineer, telegram_delivery
from character_swap.config import settings
from character_swap.models import CharacterAsset


def _run(coro):
    return asyncio.run(coro)


# --- 1. the build guard is released before the delivery starts -------------------------


def test_repurpose_releases_build_guard_before_telegram_send(monkeypatch):
    """The auto-send must run with `_REPURPOSING` already cleared — otherwise
    every manual send is refused for the whole (multi-minute) delivery."""
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: {"re_id": rid, "job_id": "j1"})
    seen: dict = {}

    async def fake_build(re_id, state, **kw):
        seen["guard_during_build"] = re_id in runner_reengineer._REPURPOSING
        return ["c1"]   # what it BUILT — the caller delivers exactly these

    async def fake_send(re_id, **kw):
        seen["guard_during_send"] = re_id in runner_reengineer._REPURPOSING

    monkeypatch.setattr(runner_reengineer, "_do_repurpose", fake_build)
    monkeypatch.setattr(auto_finalize, "send_reengineer_repurposed", fake_send)

    _run(runner_reengineer.repurpose("re_t"))

    assert seen["guard_during_build"] is True     # builds still coalesce
    assert seen["guard_during_send"] is False     # deliveries never block
    assert "re_t" not in runner_reengineer._REPURPOSING


def test_repurpose_skips_send_when_the_build_failed(monkeypatch):
    """A failed build has nothing to deliver — and must still free the guard."""
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: {"re_id": rid, "job_id": "j1"})
    calls: list[str] = []

    async def boom(re_id, state):
        raise RuntimeError("ffmpeg died")

    async def fake_send(re_id):
        calls.append(re_id)

    monkeypatch.setattr(runner_reengineer, "_do_repurpose", boom)
    monkeypatch.setattr(auto_finalize, "send_reengineer_repurposed", fake_send)
    errors: dict = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: errors.update(kw))

    _run(runner_reengineer.repurpose("re_t"))

    assert calls == []
    assert errors["repurposing"] is False
    assert "repurpose misslyckades" in errors["error"]
    assert "re_t" not in runner_reengineer._REPURPOSING


def test_do_repurpose_no_longer_sends_telegram_itself(monkeypatch):
    """Guard against the send creeping back inside the guarded build."""
    import inspect
    source = inspect.getsource(runner_reengineer._do_repurpose)
    assert "send_reengineer_repurposed" not in source


# --- 2. the per-target send lock -------------------------------------------------------


def test_sending_lock_is_per_target_and_released_on_error():
    with telegram_delivery.sending("re_t", "cA", "repurpose"):
        assert telegram_delivery.is_sending("re_t", "cA", "repurpose")
        # Different character, different variant, different run: all free.
        assert not telegram_delivery.is_sending("re_t", "cB", "repurpose")
        assert not telegram_delivery.is_sending("re_t", "cA", "final")
        assert not telegram_delivery.is_sending("re_other", "cA", "repurpose")
        with pytest.raises(telegram_delivery.AlreadySending):
            with telegram_delivery.sending("re_t", "cA", "repurpose"):
                pass
    assert not telegram_delivery.is_sending("re_t", "cA", "repurpose")

    with pytest.raises(ValueError):
        with telegram_delivery.sending("re_t", "cA", "final"):
            raise ValueError("upload blew up")
    assert not telegram_delivery.is_sending("re_t", "cA", "final")


class _CharStore:
    def __init__(self, *assets):
        self.assets = {a.char_id: a for a in assets}

    def get_character(self, char_id):
        return self.assets.get(char_id)

    def get_job(self, job_id):
        return None


def _wire_manual_send(monkeypatch):
    monkeypatch.setattr(settings, "telegram_character_bot_token", "token-A")
    monkeypatch.setattr(api, "store", lambda: _CharStore(
        CharacterAsset(char_id="cA", name="Ching", filename="a.png",
                       telegram_chat_id="@ching"),
        CharacterAsset(char_id="cB", name="Chang", filename="b.png",
                       telegram_chat_id="@chang")))
    sent: list[str] = []

    async def fake_send(path, *, chat_id, char_name, base, variant, run_id):
        sent.append(f"{char_name}:{variant}")
        return {"ok": True, "message_id": 1}

    monkeypatch.setattr(telegram_delivery, "send_character_final", fake_send)
    return sent


def test_manual_send_blocked_only_for_the_video_being_uploaded(
        monkeypatch, tmp_path):
    """The heart of the bug report: with one upload in flight, its run-mates
    must still be sendable, and the refusal must name the real reason."""
    sent = _wire_manual_send(monkeypatch)
    source = tmp_path / "repurpose_cA.mp4"
    source.write_bytes(b"video")

    async def send(char_id: str, variant: str):
        return await api._telegram_character_file(
            source, char_id=char_id, char_name=char_id, base="run",
            variant=variant, run_id="re_t")

    with telegram_delivery.sending("re_t", "cA", "repurpose"):
        with pytest.raises(api.HTTPException) as error:
            _run(send("cA", "repurpose"))
        assert error.value.status_code == 409
        detail = str(error.value.detail)
        assert "skickas redan" in detail
        assert "Bygget" not in detail          # never a build-status lie
        # Everything else stays sendable while that one upload runs.
        _run(send("cB", "repurpose"))
        _run(send("cA", "final"))

    assert sent == ["Chang:repurpose", "Ching:final"]
    _run(send("cA", "repurpose"))              # free again afterwards
    assert sent[-1] == "Ching:repurpose"


# --- 3. the auto-send never duplicates a manual upload ---------------------------------


def test_auto_send_skips_a_target_being_sent_manually(monkeypatch, tmp_path):
    """Both senders may now run at once, so the automatic delivery must skip
    (not repost) the video a manual click is already uploading."""
    finals = {}
    for cid in ("cA", "cB"):
        path = tmp_path / f"repurpose_{cid}.mp4"
        path.write_bytes(b"video")
        finals[cid] = {"status": "done", "final_path": str(path)}
    state = {"re_id": "re_t", "job_id": "j1", "status": "done",
             "repurposed": finals}

    monkeypatch.setattr("character_swap.reengineer.load_state",
                        lambda rid: dict(state))
    saved: list[dict] = []
    monkeypatch.setattr("character_swap.reengineer.save_state",
                        lambda st: saved.append(st))
    monkeypatch.setattr(auto_finalize, "store", lambda: _CharStore(
        CharacterAsset(char_id="cA", name="Ching", filename="a.png",
                       telegram_chat_id="@ching"),
        CharacterAsset(char_id="cB", name="Chang", filename="b.png",
                       telegram_chat_id="@chang")))

    async def _anoop(*a, **k):
        return None

    monkeypatch.setattr(auto_finalize, "_emit", _anoop)
    monkeypatch.setattr(auto_finalize, "_notify_telegram_result",
                        lambda *a, **k: None)
    sent: list[str] = []

    async def fake_send(path, *, chat_id, char_name, base, variant, run_id):
        sent.append(char_name)
        return {"ok": True, "message_id": 2}

    monkeypatch.setattr(telegram_delivery, "send_character_final", fake_send)

    with telegram_delivery.sending("re_t", "cA", "repurpose"):
        _run(auto_finalize.send_reengineer_repurposed("re_t"))

    assert sent == ["Chang"]                       # cA skipped, not duplicated
    receipts = saved[-1]["repurposed"]
    assert receipts["cB"].get("telegram")
    assert not receipts["cA"].get("telegram")      # the manual send owns it
