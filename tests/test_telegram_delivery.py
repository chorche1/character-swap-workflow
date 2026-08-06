"""Telegram routing, receipts, and manual resend endpoints."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from character_swap import api, telegram_delivery, video_edit
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


def test_no_test_can_deliver_to_a_real_telegram_channel(monkeypatch, tmp_path):
    """The autouse conftest guard dead-ends every unstubbed send.

    Hugo's real "Character Swap – Editor Finals" channel had filled up with
    1-byte .mp4 "finals" named after test fixtures (2026-08-04). They all came
    from `pytest`: the PostToolUse hook runs the suite from the MAIN checkout,
    whose `.env` carries the live editor bot token + chat id, and five tests
    reached delivery for real on every run — the two `repurpose_editor_job`
    workers (which stub `run_editor_pipeline` but not the send that follows
    it) and the three TestClient calls to `/api/editor/auto_edit` +
    `/api/editor/multi_auto_edit`, both of which auto-send the finished reel.

    Credentials are pinned PRESENT here on purpose: a guard that only holds
    because the tokens happen to be empty would pass in a worktree and still
    post from the main checkout — exactly the asymmetry that hid this bug.
    Both public delivery helpers must dead-end, so the next auto-send site is
    covered the day it is written.
    """
    src = tmp_path / "04-final.mp4"
    src.write_bytes(b"\x00")
    monkeypatch.setattr(settings, "telegram_editor_bot_token", "token-live")
    monkeypatch.setattr(settings, "telegram_editor_chat_id", "-1001234567890")
    monkeypatch.setattr(settings, "telegram_character_bot_token", "token-live")

    def fake_scrub(source, destination):
        destination.write_bytes(source.read_bytes())

    monkeypatch.setattr(
        telegram_delivery, "_write_metadata_free_copy", fake_scrub)

    with pytest.raises(RuntimeError, match="real Telegram Bot API"):
        _run(telegram_delivery.send_editor_final(
            src, base="regression", variant="final", edit_id="ed_guard"))
    with pytest.raises(RuntimeError, match="real Telegram Bot API"):
        _run(telegram_delivery.send_character_final(
            src, chat_id="@chang", char_name="Chang", base="regression",
            variant="final", run_id="j_guard"))


def test_send_file_core_snapshots_and_builds_public_message_url(
        monkeypatch, tmp_path):
    src = tmp_path / "live.mp4"
    src.write_bytes(b"stable bytes")
    seen = {}

    def fake_scrub(source, destination):
        seen["snapshot"] = Path(source)
        seen["snapshot_bytes"] = Path(source).read_bytes()
        assert Path(source) != src
        destination.write_bytes(b"metadata-free bytes")

    def fake_send(path, **kwargs):
        seen["path"] = Path(path)
        seen["bytes"] = Path(path).read_bytes()
        seen["kwargs"] = kwargs
        assert Path(path) != src
        return {
            "message_id": 9,
            "document": {"file_id": "f9", "file_unique_id": "u9"},
        }

    monkeypatch.setattr(
        telegram_delivery, "_write_metadata_free_copy", fake_scrub)
    monkeypatch.setattr(telegram_delivery.telegram, "send_document", fake_send)
    receipt = _run(telegram_delivery.send_file_core(
        src, bot_token="tok", chat_id="@public_channel",
        filename="final.mp4", caption="Final", account="character"))

    assert seen["snapshot_bytes"] == b"stable bytes"
    assert seen["bytes"] == b"metadata-free bytes"
    assert receipt["url"] == "https://t.me/public_channel/9"
    assert receipt["file_id"] == "f9"
    assert not seen["snapshot"].exists()      # both temporary files cleaned up
    assert not seen["path"].exists()


def _packet_hash(path: Path, stream: str) -> str:
    proc = subprocess.run(
        [
            video_edit._ffmpeg(), "-v", "error", "-i", str(path),
            "-map", stream, "-c", "copy", "-f", "hash",
            "-hash", "sha256", "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _decoded_hash(path: Path, stream: str) -> str:
    proc = subprocess.run(
        [
            video_edit._ffmpeg(), "-v", "error", "-i", str(path),
            "-map", stream, "-f", "hash", "-hash", "sha256", "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_metadata_free_copy_strips_tags_without_reencoding(tmp_path):
    source = tmp_path / "tagged.mp4"
    clean = tmp_path / "clean.mp4"
    video_edit._run([
        video_edit._ffmpeg(),
        "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=black:s=64x64:r=10:d=0.5",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-shortest", "-c:v", "libx264", "-c:a", "aac",
        "-metadata", "title=Private title",
        "-metadata", "artist=Private artist",
        "-metadata", "comment=Private comment",
        "-metadata", "creation_time=2026-08-05T01:02:03Z",
        "-metadata:s:v:0", "handler_name=Private video track",
        "-metadata:s:a:0", "handler_name=Private audio track",
        str(source),
    ])
    original_bytes = source.read_bytes()

    telegram_delivery._write_metadata_free_copy(source, clean)

    probe = subprocess.run(
        [video_edit._ffmpeg(), "-hide_banner", "-i", str(clean)],
        capture_output=True,
        text=True,
        check=False,
    ).stderr
    assert "Private title" not in probe
    assert "Private artist" not in probe
    assert "Private comment" not in probe
    assert "Private video track" not in probe
    assert "Private audio track" not in probe
    assert source.read_bytes() == original_bytes
    # x264 writes its identity/options into a User Data Unregistered SEI unit.
    # That codec metadata is gone, so encoded packet hashes intentionally
    # differ — but decoded frames are byte-identical (zero generation loss).
    assert b"x264 - core" in source.read_bytes()
    assert b"x264 - core" not in clean.read_bytes()
    assert _packet_hash(source, "0:v:0") != _packet_hash(clean, "0:v:0")
    assert _decoded_hash(source, "0:v:0") == _decoded_hash(clean, "0:v:0")
    assert _packet_hash(source, "0:a:0") == _packet_hash(clean, "0:a:0")


def test_codec_metadata_scrub_refuses_hdr_instead_of_changing_appearance(
        monkeypatch, tmp_path):
    source = tmp_path / "hdr.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        telegram_delivery, "_probe_video_streams",
        lambda _path: [{"codec": "hevc", "hdr": True}],
    )

    with pytest.raises(RuntimeError, match="HDR-videon"):
        telegram_delivery._codec_metadata_filter_args(source)


def test_unknown_codec_fails_closed(monkeypatch, tmp_path):
    source = tmp_path / "unknown.mp4"
    source.write_bytes(b"fixture")
    monkeypatch.setattr(
        telegram_delivery, "_probe_video_streams",
        lambda _path: [{"codec": "prores", "hdr": False}],
    )

    with pytest.raises(RuntimeError, match="prores"):
        telegram_delivery._codec_metadata_filter_args(source)


def test_send_file_core_never_uploads_when_metadata_scrub_fails(
        monkeypatch, tmp_path):
    source = tmp_path / "final.mp4"
    source.write_bytes(b"original remains local")
    sent = False

    def fail_scrub(_source, _destination):
        raise RuntimeError("metadata scrub failed")

    def fake_send(*_args, **_kwargs):
        nonlocal sent
        sent = True

    monkeypatch.setattr(
        telegram_delivery, "_write_metadata_free_copy", fail_scrub)
    monkeypatch.setattr(telegram_delivery.telegram, "send_document", fake_send)

    with pytest.raises(RuntimeError, match="metadata scrub failed"):
        _run(telegram_delivery.send_file_core(
            source, bot_token="tok", chat_id="@channel",
            filename="final.mp4", caption="Final", account="editor"))

    assert sent is False
    assert source.read_bytes() == b"original remains local"


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
