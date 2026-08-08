"""Shared Telegram routing and receipt helpers for finished videos."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from character_swap import video_edit
from character_swap.clients import telegram
from character_swap.config import settings

# In-flight registry (Hugo 2026-08-03): ONE upload at a time per delivery
# target — (run_id, target_id, variant). An upload takes minutes (34 MB finals,
# 600 s timeout × 3 attempts), so the automatic post-build delivery and a
# manual ➤ Telegram click can easily overlap and post the SAME video twice in
# the channel. Everything blocked here is per TARGET: sending another
# character (or another run) while one upload runs is fine and must stay
# possible — the old blunt "whole run is busy" guard was exactly the bug.
# In-process only, mutated from the event loop thread; a restart clears it,
# which is correct (no upload survives the process).
_SENDING: set[tuple[str, str, str]] = set()

# Encoded-bitstream metadata units which can be removed without decoding or
# re-encoding the media.  These units can contain encoder identity/options
# (x264/x265), timecodes and arbitrary user data.  HDR can also live there,
# so `_codec_metadata_filter_args` refuses HDR rather than changing its look.
_CODEC_METADATA_UNITS = {
    "h264": "6",       # SEI
    "hevc": "39|40",  # prefix/suffix SEI
    "av1": "5",       # metadata OBU
}
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}
_HDR_SIDE_DATA = (
    "mastering display", "content light", "dovi", "dolby vision", "hdr10+",
)


class AlreadySending(RuntimeError):
    """Raised when this exact video is already being uploaded right now."""


def send_key(run_id: str, target_id: str, variant: str) -> tuple[str, str, str]:
    return (str(run_id), str(target_id), str(variant or "final"))


def is_sending(run_id: str, target_id: str, variant: str) -> bool:
    return send_key(run_id, target_id, variant) in _SENDING


@contextmanager
def sending(run_id: str, target_id: str, variant: str):
    """Claim a delivery target for the duration of one upload.

    Raises AlreadySending if another sender holds it — callers turn that into
    an honest message ("skickas redan just nu") instead of a build-status lie.
    """
    key = send_key(run_id, target_id, variant)
    if key in _SENDING:
        raise AlreadySending(
            "Den här videon skickas redan till Telegram just nu — "
            "vänta tills den är klar.")
    _SENDING.add(key)
    try:
        yield
    finally:
        _SENDING.discard(key)


def safe_name(name: str) -> str:
    return (name or "").replace("/", "_").replace("\\", "_").strip() or "video"


def character_final_name(char_name: str, base: str, variant: str,
                         run_id: str) -> str:
    suffix = " — repurpose" if variant == "repurpose" else ""
    return f"{safe_name(char_name)} — {safe_name(base)}{suffix} [{run_id}].mp4"


def editor_final_name(base: str, variant: str, edit_id: str) -> str:
    suffix = " — repurpose" if variant == "repurpose" else ""
    return f"{safe_name(base)}{suffix} [{edit_id}].mp4"


def _message_url(chat_id: str, message_id: int | None) -> str | None:
    if not message_id:
        return None
    target = (chat_id or "").strip()
    if target.startswith("@"):
        return f"https://t.me/{target[1:]}/{message_id}"
    return None


def _probe_video_streams(source: Path) -> list[dict]:
    """Return real video codecs + HDR state, with an ffmpeg-only fallback."""
    probe = video_edit._ffprobe()
    if probe:
        proc = subprocess.run(
            [probe, "-v", "error", "-select_streams", "v",
             "-show_streams", "-of", "json", str(source)],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode == 0:
            try:
                streams = []
                for item in json.loads(proc.stdout or "{}").get("streams", []):
                    if (item.get("disposition") or {}).get("attached_pic"):
                        continue
                    side_types = " ".join(
                        str(side.get("side_data_type") or "").lower()
                        for side in item.get("side_data_list") or [])
                    hdr = (
                        str(item.get("color_transfer") or "").lower()
                        in _HDR_TRANSFERS
                        or str(item.get("codec_tag_string") or "").lower()
                        in {"dvh1", "dvhe"}
                        or any(mark in side_types for mark in _HDR_SIDE_DATA)
                    )
                    streams.append({
                        "codec": str(item.get("codec_name") or "").lower(),
                        "hdr": hdr,
                    })
                if streams and all(stream["codec"] for stream in streams):
                    return streams
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

    # imageio-ffmpeg bundles ffmpeg but not ffprobe. A header-only probe still
    # identifies the codec without decoding the final.
    proc = subprocess.run(
        [video_edit._ffmpeg(), "-hide_banner", "-i", str(source)],
        capture_output=True, text=True, check=False,
    )
    header = proc.stderr or ""
    lower_header = header.lower()
    hdr = any(mark in lower_header for mark in (*_HDR_TRANSFERS, *_HDR_SIDE_DATA))
    streams = []
    for line in header.splitlines():
        if " Video: " not in line or "attached pic" in line.lower():
            continue
        codec = line.split(" Video: ", 1)[1].split(",", 1)[0]
        codec = codec.split("(", 1)[0].strip().lower()
        if codec:
            streams.append({"codec": codec, "hdr": hdr})
    if not streams:
        raise RuntimeError(
            "Kunde inte identifiera videocodec före Telegram-publicering.")
    return streams


def _codec_metadata_filter_args(source: Path) -> list[str]:
    """Lossless ffmpeg filters which remove codec-internal metadata.

    Unknown codecs are refused: silently uploading a partially scrubbed file
    would violate the privacy contract. VP8/VP9 have no generic metadata unit
    corresponding to H.26x SEI/AV1 metadata OBUs, so no bitstream filter is
    needed for them.
    """
    args: list[str] = []
    for output_index, stream in enumerate(_probe_video_streams(source)):
        codec = stream["codec"]
        if stream["hdr"] and codec in _CODEC_METADATA_UNITS:
            raise RuntimeError(
                "HDR-videon kan inte göras helt metadatafri utan risk för "
                "ändrad färgåtergivning; Telegram-publiceringen stoppades.")
        remove_types = _CODEC_METADATA_UNITS.get(codec)
        if remove_types:
            args += [
                f"-bsf:v:{output_index}",
                f"filter_units=remove_types={remove_types}",
            ]
        elif codec not in {"vp8", "vp9"}:
            raise RuntimeError(
                f"Codec-metadata kan inte rensas säkert för {codec!r}; "
                "Telegram-publiceringen stoppades.")
    return args


def _write_metadata_free_copy(source: Path, destination: Path) -> None:
    """Remux *source* without descriptive metadata or re-encoding media.

    ``-c copy`` avoids decoding/re-encoding, so this cannot introduce a
    generation loss. Codec metadata units are removed from SDR bitstreams;
    the actual compressed image/audio content remains untouched. Only real
    presentation streams are mapped: cover art, timecode/data tracks,
    attachments and chapters are deliberately omitted. The negative metadata
    maps cover both container and per-stream tags; ``+bitexact`` also stops
    ffmpeg from adding its own encoder/version tag.

    An MP4 still necessarily contains structural fields (codec parameters,
    timing, brands, and any display matrix needed for correct rotation).  They
    are required to decode the file and are not descriptive source metadata.
    """
    args = [
        video_edit._ffmpeg(),
        "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(source),
        # Uppercase V excludes attached cover-art/thumbnail video streams.
        "-map", "0:V?", "-map", "0:a?", "-map", "0:s?",
        "-map_metadata", "-1",
        "-map_metadata:s:v", "-1",
        "-map_metadata:s:a", "-1",
        "-map_metadata:s:s", "-1",
        "-map_chapters", "-1",
        "-c", "copy",
        "-fflags", "+bitexact",
        # Clear common muxer-added fields explicitly as belt and braces.
        "-metadata", "creation_time=",
        "-metadata", "encoder=",
        "-metadata:s:v", "handler_name=",
        "-metadata:s:v", "vendor_id=",
        "-metadata:s:v", "encoder=",
        "-metadata:s:a", "handler_name=",
        "-metadata:s:a", "vendor_id=",
        "-metadata:s:a", "encoder=",
        "-metadata:s:s", "handler_name=",
        "-metadata:s:s", "encoder=",
    ]
    args += _codec_metadata_filter_args(source)
    args.append(str(destination))
    video_edit._run(args)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise RuntimeError("ffmpeg skapade ingen metadatafri Telegram-kopia.")


async def send_file_core(source: Path, *, bot_token: str, chat_id: str,
                         filename: str, caption: str,
                         account: str) -> dict:
    """Snapshot a mutable final, scrub metadata, and return a receipt.

    Metadata removal is fail-closed: Telegram is never handed the untouched
    snapshot when the lossless remux fails.
    """
    if not source.is_file():
        raise FileNotFoundError(f"Video file not found: {source.name}")
    fd, temp_name = tempfile.mkstemp(
        prefix="telegram-source-", suffix=source.suffix or ".mp4")
    os.close(fd)
    snapshot = Path(temp_name)
    clean_fd, clean_name = tempfile.mkstemp(
        prefix="telegram-clean-", suffix=source.suffix or ".mp4")
    os.close(clean_fd)
    clean = Path(clean_name)
    try:
        await asyncio.to_thread(shutil.copyfile, source, snapshot)
        await asyncio.to_thread(_write_metadata_free_copy, snapshot, clean)
        message = await asyncio.to_thread(
            telegram.send_document,
            clean,
            bot_token=bot_token,
            chat_id=chat_id,
            filename=filename,
            caption=caption,
            api_base=settings.telegram_api_base,
        )
    finally:
        snapshot.unlink(missing_ok=True)
        clean.unlink(missing_ok=True)
    document = message.get("document") or {}
    message_id = message.get("message_id")
    return {
        "ok": True,
        "account": account,
        "chat_id": str(chat_id),
        "message_id": message_id,
        "file_id": document.get("file_id"),
        "file_unique_id": document.get("file_unique_id"),
        "url": _message_url(str(chat_id), message_id),
        "name": filename,
        "at": datetime.utcnow().isoformat() + "Z",
    }


async def send_character_final(source: Path, *, chat_id: str,
                               char_name: str, base: str, variant: str,
                               run_id: str) -> dict:
    filename = character_final_name(char_name, base, variant, run_id)
    caption = filename.removesuffix(".mp4")
    return await send_file_core(
        source,
        bot_token=settings.telegram_character_bot_token,
        chat_id=chat_id,
        filename=filename,
        caption=caption,
        account="character",
    )


async def send_editor_final(source: Path, *, base: str, variant: str,
                            edit_id: str) -> dict:
    filename = editor_final_name(base, variant, edit_id)
    caption = filename.removesuffix(".mp4")
    return await send_file_core(
        source,
        bot_token=settings.telegram_editor_bot_token,
        chat_id=settings.telegram_editor_chat_id,
        filename=filename,
        caption=caption,
        account="editor",
    )
