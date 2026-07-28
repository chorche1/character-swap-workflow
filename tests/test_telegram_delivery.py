"""Telegram routing, receipts, and manual resend endpoints."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from character_swap import api, telegram_delivery
from character_swap.clients import telegram
from character_swap.config import settings
from character_swap.models import CharacterAsset, Job, JobCharacter


def _run(coro):
    return asyncio.run(coro)


class _Response:
    status_code = 200
    text = ""

    def json(self):
        return {
            "ok": True,
            "result": {
                "message_id": 42,
                "document": {
                    "file_id": "file-1",
                    "file_unique_id": "uniq-1",
                },
            },
        }


def test_client_sends_exact_document_with_explicit_token_and_channel(
        monkeypatch, tmp_path):
    src = tmp_path / "final.mp4"
    original = b"exact original video bytes"
    src.write_bytes(original)
    seen = {}

    def fake_post(url, *, data, files, timeout, max_attempts=3):
        item = files["document"]
        seen.update(
            url=url,
            data=data,
            name=item[0],
            mime=item[2],
            uploaded=item[1].read(),
        )
        return _Response()

    monkeypatch.setattr(telegram, "_post_with_retry", fake_post)
    result = telegram.send_document(
        src, bot_token="token-A", chat_id="@chang",
        filename="Chang final.mp4")

    assert seen["url"].endswith("/bottoken-A/sendDocument")
    assert seen["data"]["chat_id"] == "@chang"
    assert seen["data"]["disable_content_type_detection"] == "true"
    assert seen["name"] == "Chang final.mp4"
    assert seen["mime"] == "application/octet-stream"
    assert seen["uploaded"] == original
    assert src.read_bytes() == original
    assert result["message_id"] == 42


def test_client_never_compresses_oversized_cloud_file(monkeypatch, tmp_path):
    src = tmp_path / "large.mp4"
    original = b"unaltered-source"
    src.write_bytes(original)
    monkeypatch.setattr(telegram, "_CLOUD_LIMIT", len(original) - 1)
    called = False

    def fake_post(*args, **kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(telegram, "_post_with_retry", fake_post)
    with pytest.raises(RuntimeError, match="komprimeras aldrig"):
        telegram.send_document(
            src, bot_token="token-A", chat_id="@chang")

    assert called is False
    assert src.read_bytes() == original


def test_send_file_core_snapshots_and_builds_public_message_url(
        monkeypatch, tmp_path):
    src = tmp_path / "live.mp4"
    src.write_bytes(b"stable bytes")
    seen = {}

    def fake_send(path, **kwargs):
        seen["path"] = Path(path)
        seen["bytes"] = Path(path).read_bytes()
        seen["kwargs"] = kwargs
        assert Path(path) != src
        return {
            "message_id": 9,
            "document": {"file_id": "f9", "file_unique_id": "u9"},
        }

    monkeypatch.setattr(telegram_delivery.telegram, "send_document", fake_send)
    receipt = _run(telegram_delivery.send_file_core(
        src, bot_token="tok", chat_id="@public_channel",
        filename="final.mp4", caption="Final", account="character"))

    assert seen["bytes"] == b"stable bytes"
    assert receipt["url"] == "https://t.me/public_channel/9"
    assert receipt["file_id"] == "f9"
    assert not seen["path"].exists()          # snapshot cleaned up


class _Store:
    def __init__(self, job: Job, asset: CharacterAsset):
        self.job = job
        self.asset = asset
        self.updated = []

    def get_job(self, job_id):
        return self.job if job_id == self.job.job_id else None

    def get_character(self, char_id):
        return self.asset if char_id == self.asset.char_id else None

    def update_job(self, job):
        self.updated.append(job)


def _job(tmp_path) -> tuple[Job, CharacterAsset]:
    final = tmp_path / "final.mp4"
    final.write_bytes(b"video")
    jc = JobCharacter(
        char_id="c1", name="Chang", source_image_path="x.png",
        compiled_video_path=str(final), compile_status="done")
    job = Job(
        job_id="j1", title="Honey run", scene_id="s1",
        scene_image_path="scene.png", characters={"c1": jc})
    asset = CharacterAsset(
        char_id="c1", name="Chang", filename="x.png",
        telegram_chat_id="@chang")
    return job, asset


def test_job_endpoint_routes_to_character_channel_and_persists(
        monkeypatch, tmp_path):
    job, asset = _job(tmp_path)
    store = _Store(job, asset)
    monkeypatch.setattr(api, "store", lambda: store)
    monkeypatch.setattr(settings, "telegram_character_bot_token", "token-A")
    seen = {}

    async def fake_send(path, **kwargs):
        seen.update(path=Path(path), **kwargs)
        return {"ok": True, "message_id": 7, "file_id": "f7"}

    monkeypatch.setattr(
        telegram_delivery, "send_character_final", fake_send)
    receipt = _run(api.job_char_telegram_send(
        "j1", "c1", api.TelegramSendBody(variant="final")))

    assert seen["chat_id"] == "@chang"
    assert seen["base"] == "Honey run"
    assert receipt["message_id"] == 7
    assert job.characters["c1"].telegram_sends["final"]["file_id"] == "f7"
    assert store.updated


def test_job_endpoint_fails_loud_when_character_channel_missing(
        monkeypatch, tmp_path):
    job, asset = _job(tmp_path)
    asset.telegram_chat_id = None
    monkeypatch.setattr(api, "store", lambda: _Store(job, asset))
    monkeypatch.setattr(settings, "telegram_character_bot_token", "token-A")
    with pytest.raises(api.HTTPException) as error:
        _run(api.job_char_telegram_send(
            "j1", "c1", api.TelegramSendBody(variant="final")))
    assert error.value.status_code == 409
    assert "Telegram-kanal saknas" in str(error.value.detail)


def test_character_patch_roundtrips_telegram_channel(monkeypatch, tmp_path):
    _job_value, asset = _job(tmp_path)

    class _CharacterStore:
        def get_character(self, char_id):
            return asset

        def update_character(self, value):
            self.saved = value

    fake = _CharacterStore()
    monkeypatch.setattr(api, "store", lambda: fake)
    out = _run(api.rename_character(
        "c1", api.RenameCharacterBody(telegram_chat_id="@new_channel")))
    assert asset.telegram_chat_id == "@new_channel"
    assert out["telegram_chat_id"] == "@new_channel"


def test_editor_endpoint_uses_shared_editor_channel_and_persists(
        monkeypatch, tmp_path):
    edit_id = "ed_editor"
    edit_dir = tmp_path / "editor" / edit_id
    edit_dir.mkdir(parents=True)
    final = edit_dir / "04-final.mp4"
    final.write_bytes(b"lossless editor bytes")
    generation = SimpleNamespace(
        prompt="Multiclip script", editor_meta={})

    class _EditorStore:
        def __init__(self):
            self.updated = []

        def get_generation(self, gen_id):
            return generation if gen_id == edit_id else None

        def update_generation(self, gen):
            self.updated.append(gen)

    fake_store = _EditorStore()
    monkeypatch.setattr(api, "store", lambda: fake_store)
    monkeypatch.setattr(settings, "output_dir", tmp_path)
    monkeypatch.setattr(settings, "telegram_editor_bot_token", "token-B")
    monkeypatch.setattr(
        settings, "telegram_editor_chat_id", "-1004389149468")
    seen = {}

    async def fake_send(path, **kwargs):
        seen.update(path=Path(path), **kwargs)
        return {"ok": True, "account": "editor", "message_id": 11}

    monkeypatch.setattr(
        telegram_delivery, "send_editor_final", fake_send)

    receipt = _run(api.editor_telegram_send(
        edit_id, api.TelegramSendBody(gen_id=edit_id, slot="final")))

    assert seen["path"] == final
    assert seen["base"] == "Multiclip script"
    assert seen["variant"] == "final"
    assert receipt["account"] == "editor"
    assert generation.editor_meta["telegram"]["final"]["message_id"] == 11
    assert fake_store.updated
