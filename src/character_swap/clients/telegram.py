"""Telegram Bot API wrapper — used to deliver the auto-processed final
video to Hugo as a Telegram message.

Setup:
1. Talk to @BotFather on Telegram, /newbot, get a token. Set
   `TELEGRAM_BOT_TOKEN=...` in `.env`.
2. Send any message to your new bot from your own Telegram account, then
   `curl https://api.telegram.org/bot<TOKEN>/getUpdates` — note the
   `chat.id` of the message. Set `TELEGRAM_CHAT_ID=...`.

Why this client is thin: Telegram has solid Python SDKs but they all
pull in async libs we don't need. The Bot API is a plain HTTP POST and
fits in ~50 lines.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings


_API_BASE = "https://api.telegram.org"

# Target dimensions for the normalized Telegram upload. Higgsfield exports
# at the odd 716×1284 resolution, which Telegram treats as a generic file
# rather than an inline-playable video on mobile clients. We pad to a
# canonical 9:16 reel size before uploading.
_TG_TARGET_W = 1080
_TG_TARGET_H = 1920


def _require_telegram() -> None:
    if not settings.telegram_bot_token:
        raise ProviderNotConfigured(
            "Telegram",
            "Add TELEGRAM_BOT_TOKEN to .env (get one from @BotFather).",
        )
    if not settings.telegram_chat_id:
        raise ProviderNotConfigured(
            "Telegram",
            "Add TELEGRAM_CHAT_ID to .env (curl getUpdates after sending "
            "a message to your bot to find it).",
        )


def configured() -> bool:
    """Soft check used by the watcher to decide whether to attempt
    delivery. Returns False if either env var is missing."""
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def _post_with_retry(url: str, *, data: dict, files: dict | None,
                     timeout: float, max_attempts: int = 3) -> httpx.Response:
    """POST that retries up to `max_attempts` on transient TLS / network
    failures. Telegram's edge occasionally drops connections with
    SSLV3_ALERT_BAD_RECORD_MAC or similar resets — a single retry with
    a short backoff usually clears it. Each attempt seeks file handles
    back to start so the multipart upload re-reads from the beginning."""
    import time as _time
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        # Rewind any file handles in `files` so the retry sends the
        # full body (httpx consumes streams on first send).
        if files:
            for v in files.values():
                if isinstance(v, tuple) and len(v) >= 2 and hasattr(v[1], "seek"):
                    try:
                        v[1].seek(0)
                    except OSError:
                        pass
        try:
            return httpx.post(url, data=data, files=files, timeout=timeout)
        except httpx.HTTPError as e:
            last_err = e
            if attempt < max_attempts - 1:
                _time.sleep(2 ** attempt)  # 1s, 2s, 4s ...
                continue
            raise RuntimeError(f"Telegram POST failed (after {max_attempts} attempts): {e}") from e
    # Unreachable — the raise above handles the last-attempt case.
    raise RuntimeError(f"Telegram POST failed: {last_err}")


@dataclass
class _VideoProbe:
    width: int
    height: int
    duration_secs: float
    pix_fmt: str   # "yuv420p", "yuvj420p", "yuv444p", etc.


def _ffmpeg_bin() -> str:
    """Resolve the bundled ffmpeg binary (same one video_edit uses)."""
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe_for_telegram(path: Path) -> _VideoProbe:
    """Read width/height/duration/pix_fmt from a video by parsing
    `ffmpeg -i`'s stderr (informational output). `imageio_ffmpeg` bundles
    ffmpeg but not ffprobe, so we use the same parsing approach as
    `remotion_render._probe_video`.

    Falls back to sensible defaults (target dimensions, 0 duration) if
    parsing fails — the caller can still send the file, just without
    accurate metadata.
    """
    proc = subprocess.run(
        [_ffmpeg_bin(), "-hide_banner", "-i", str(path)],
        capture_output=True, text=True,
    )
    text = proc.stderr  # ffmpeg writes info to stderr even on success
    width, height = _TG_TARGET_W, _TG_TARGET_H
    duration = 0.0
    pix_fmt = "yuv420p"
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Duration:"):
            try:
                ts = s.split("Duration:")[1].split(",")[0].strip()
                h, m, rest = ts.split(":")
                duration = int(h) * 3600 + int(m) * 60 + float(rest)
            except (ValueError, IndexError):
                pass
        elif "Video:" in s:
            m = re.search(r"(\d{2,5})x(\d{2,5})", s)
            if m:
                width = int(m.group(1))
                height = int(m.group(2))
            pm = re.search(r"\b(yuv\w+)", s)
            if pm:
                pix_fmt = pm.group(1)
    return _VideoProbe(width=width, height=height,
                       duration_secs=duration, pix_fmt=pix_fmt)


@dataclass
class _NormalizedVideo:
    """Result of `_normalize_for_telegram`. `path` is what to upload;
    `width`/`height`/`duration_secs` go into Telegram's sendVideo
    metadata so the mobile client renders an inline preview at the
    correct aspect ratio without having to guess from the file."""
    path: Path
    width: int
    height: int
    duration_secs: int
    re_encoded: bool   # debug — did we run a re-encode pass?
    sidecar: Path | None  # set when path is a sidecar that should be cleaned up


def _needs_normalize(probe: _VideoProbe) -> bool:
    """True when the source video isn't already a clean 1080×1920 yuv420p
    reel. We re-encode for dimension mismatch (the most common Higgsfield
    case) OR pix_fmt mismatch (yuvj* full-range causes color shifts on
    some Telegram clients). +faststart isn't visible in `ffmpeg -i`
    output, so we conservatively re-encode whenever dimensions or pix_fmt
    are off — that pass also adds +faststart for free."""
    if probe.width != _TG_TARGET_W or probe.height != _TG_TARGET_H:
        return True
    if not probe.pix_fmt.startswith("yuv420p"):
        # yuvj420p (JPEG full range), yuv444p, etc.
        return True
    return False


def _normalize_for_telegram(src: Path) -> _NormalizedVideo:
    """Ensure `src` is in a Telegram-friendly format. If the source is
    already 1080×1920 yuv420p we send it as-is. Otherwise we run one
    ffmpeg pass that:

    - Scales to fit inside 1080×1920 preserving the source aspect ratio
    - Pads the remainder with black so the canvas is exactly 1080×1920
      (no crop — the entire source frame is visible)
    - Converts to yuv420p limited-range color (Telegram's expected format)
    - Sets +faststart so the moov atom is at the start, enabling Telegram
      to stream-play without downloading the full file first

    Returns the path to upload + correct width/height/duration for
    Telegram's sendVideo metadata. The caller is responsible for
    cleaning up `sidecar` after upload.
    """
    src = Path(src)
    probe = _probe_for_telegram(src)
    if not _needs_normalize(probe):
        return _NormalizedVideo(
            path=src,
            width=probe.width,
            height=probe.height,
            duration_secs=int(round(probe.duration_secs)),
            re_encoded=False,
            sidecar=None,
        )

    sidecar = src.with_name(f".tg-norm-{src.stem}.mp4")
    vf = (
        f"scale={_TG_TARGET_W}:{_TG_TARGET_H}:"
        f"force_original_aspect_ratio=decrease,"
        f"pad={_TG_TARGET_W}:{_TG_TARGET_H}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,format=yuv420p"
    )
    cmd = [
        _ffmpeg_bin(), "-y",
        "-i", str(src),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(sidecar),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 or not sidecar.exists():
        # Re-encode failed — fall back to sending the source as-is. Bad
        # format is better than no delivery. Caller logs via the caption.
        return _NormalizedVideo(
            path=src,
            width=probe.width,
            height=probe.height,
            duration_secs=int(round(probe.duration_secs)),
            re_encoded=False,
            sidecar=None,
        )

    # Re-probe the normalized output for accurate duration (the encode can
    # shift it by a few cs).
    out_probe = _probe_for_telegram(sidecar)
    return _NormalizedVideo(
        path=sidecar,
        width=_TG_TARGET_W,
        height=_TG_TARGET_H,
        duration_secs=int(round(out_probe.duration_secs or probe.duration_secs)),
        re_encoded=True,
        sidecar=sidecar,
    )


def send_video(file_path: Path, *, caption: str = "",
               chat_id: str | None = None, timeout: float = 300.0) -> dict:
    """Upload `file_path` to Telegram as a video message.

    Returns the Telegram API response dict. Raises RuntimeError on
    network or API-level failure so the caller can log + retry.

    Uses `sendVideo` (not `sendDocument`) so Telegram renders an inline
    preview with the duration/thumbnail bar instead of a generic file
    attachment.

    Pre-upload, runs `_normalize_for_telegram` to coerce odd
    Higgsfield-export dimensions (716×1284 etc.) into a canonical 1080×1920
    yuv420p +faststart reel so Telegram's mobile clients render it
    inline at the correct aspect ratio.

    `timeout` defaults to 5 min — Telegram's API can be slow with large
    uploads on patchy connections. Retries up to 3× on transient
    network/TLS failures via `_post_with_retry`.
    """
    _require_telegram()
    target = chat_id or settings.telegram_chat_id
    url = f"{_API_BASE}/bot{settings.telegram_bot_token}/sendVideo"

    file_path = Path(file_path)
    if not file_path.exists():
        raise RuntimeError(f"Telegram send_video: file missing: {file_path}")

    normalized = _normalize_for_telegram(file_path)
    upload_path = normalized.path
    if upload_path.stat().st_size > 50 * 1024 * 1024:
        # Telegram bots are capped at 50 MB per upload for non-premium
        # bots. Above that, sendDocument with stream-from-URL is needed —
        # but Hugo's final reels rarely exceed this.
        if normalized.sidecar:
            normalized.sidecar.unlink(missing_ok=True)
        raise RuntimeError(
            f"Telegram send_video: file is {upload_path.stat().st_size // 1024 // 1024}MB, "
            "exceeds 50 MB bot upload cap. Use sendDocument with a public "
            "URL for larger files."
        )

    try:
        with upload_path.open("rb") as fh:
            files = {"video": (upload_path.name, fh, "video/mp4")}
            data = {
                "chat_id": target,
                "caption": caption[:1024],   # Telegram cap
                "supports_streaming": "true",
                "width": str(normalized.width),
                "height": str(normalized.height),
                "duration": str(normalized.duration_secs),
            }
            r = _post_with_retry(url, data=data, files=files, timeout=timeout)
    finally:
        if normalized.sidecar:
            normalized.sidecar.unlink(missing_ok=True)

    if r.status_code != 200:
        raise RuntimeError(
            f"Telegram sendVideo returned {r.status_code}: {r.text[:500]}"
        )
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {body.get('description', body)}"
        )
    return body


def send_document(file_path: Path, *, caption: str = "",
                  chat_id: str | None = None, timeout: float = 600.0) -> dict:
    """Like `send_video` but uses `sendDocument` — survives the 50 MB
    sendVideo cap (raises it to ~2 GB) but loses the inline preview."""
    _require_telegram()
    target = chat_id or settings.telegram_chat_id
    url = f"{_API_BASE}/bot{settings.telegram_bot_token}/sendDocument"

    file_path = Path(file_path)
    if not file_path.exists():
        raise RuntimeError(f"Telegram send_document: file missing: {file_path}")

    with file_path.open("rb") as fh:
        files = {"document": (file_path.name, fh, "video/mp4")}
        data = {"chat_id": target, "caption": caption[:1024]}
        r = _post_with_retry(url, data=data, files=files, timeout=timeout)

    if r.status_code != 200:
        raise RuntimeError(
            f"Telegram sendDocument returned {r.status_code}: {r.text[:500]}"
        )
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(
            f"Telegram API error: {body.get('description', body)}"
        )
    return body


def send_text(text: str, *, chat_id: str | None = None) -> dict:
    """Send a plain text message — used for error notifications when
    the auto-pipeline can't deliver a video."""
    _require_telegram()
    target = chat_id or settings.telegram_chat_id
    url = f"{_API_BASE}/bot{settings.telegram_bot_token}/sendMessage"
    r = httpx.post(url, data={"chat_id": target, "text": text[:4096]},
                   timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram sendMessage failed: {r.text[:500]}")
    return r.json()
