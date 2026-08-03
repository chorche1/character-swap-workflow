"""
Remotion-based caption rendering — the new path for caption engines that
need animation, kinetic text, glow, spring physics, etc. (Things ASS
can't do.)

Python invokes a `npx remotion render` subprocess against the React
project at `<repo>/remotion/`. Word-level Whisper timestamps and the
template's visual params are passed as JSON props.

Calls are wrapped in `call_log.record(phase="remotion_render", ...)` to
match the existing logging pattern for OpenAI/Grok calls.

A SHA-256 cache under `output/cache/remotion/<hash>.mp4` short-circuits
re-renders when the same (composition, props, words, input file) tuple
recurs — common while a user tweaks accent color or position.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import imageio_ffmpeg

from character_swap.call_log import record
from character_swap.config import settings


# Process-wide cap on simultaneous `npx remotion render` subprocesses. Every
# caller (Step-6 compile, /rerender, auto_edit, timeline) funnels through
# render_remotion via asyncio.to_thread, so a threading semaphore here gates
# them all. Without it, compile's per-character fan-out launches one headless
# Chrome per character at once and the machine thrashes: 11 concurrent
# renders measured 430s median each (vs 71s solo) with per-frame delayRender
# timeouts. Built lazily so tests can override the setting before first use.
_gate_lock = threading.Lock()
_gate: threading.BoundedSemaphore | None = None


def _render_gate() -> threading.BoundedSemaphore:
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = threading.BoundedSemaphore(
                max(1, settings.remotion_max_concurrent_renders))
        return _gate


# Floor per render. Remotion renders frames roughly sequentially per tab, so
# the working set is small — this only needs to cover in-flight frames, not
# the whole video. 512MB ≈ 80 decoded 1080x1920 frames.
_MIN_OFFTHREAD_CACHE_BYTES = 512 * 1024**2


def _offthread_cache_budget() -> int:
    """Each render's share of the process-wide OffthreadVideo cache budget.

    Divided by the gate size so the TOTAL across concurrent renders stays at
    `remotion_offthread_cache_bytes` — Remotion's own default would instead
    give EVERY render half the machine's memory (see the config comment)."""
    gate = max(1, settings.remotion_max_concurrent_renders)
    return max(_MIN_OFFTHREAD_CACHE_BYTES,
               settings.remotion_offthread_cache_bytes // gate)


# Path to the Remotion project. Resolved relative to the repo root via the
# settings.project_root anchor (set in config.py).
def _remotion_dir() -> Path:
    return settings.project_root / "remotion"


REMOTION_MISSING_MSG = (
    "Remotion är inte installerat på den här datorn (Node och/eller "
    "remotion/node_modules saknas). Kör `character-swap remotion-install`, "
    "eller välj en textremsmall utan animation."
)


def _ensure_installed(remotion_dir: Path) -> None:
    """Refuse LOUDLY when the Remotion toolchain isn't installed.

    `remotion/package.json` is COMMITTED but `node_modules/` is not, so the
    package.json guard alone passed on a fresh checkout and we fell through
    to `Popen(["npx", ...])`. Without node that raises FileNotFoundError —
    an OSError, not a RuntimeError — and the caption endpoints in api.py
    only catch RuntimeError, so the user got an opaque 500 instead of being
    told to run the installer. The app's own DEFAULT caption template is a
    Remotion one, which makes this the most likely first failure on a
    machine that skipped `remotion-install` (2026-08-04).

    Mirrors api._remotion_available(), which gates the template LIST the
    same way — this is the render-side half of the same rule.
    """
    if not (remotion_dir / "node_modules").is_dir() or shutil.which("node") is None:
        raise RuntimeError(REMOTION_MISSING_MSG)


def _kill_render_tree(proc: subprocess.Popen) -> None:
    """SIGKILL the render's entire process group. The render is spawned with
    start_new_session=True so its pgid == its pid and the group holds every
    descendant (npx → node remotion CLI → headless Chrome + its per-tab
    renderer processes). Killing only the direct child — what
    subprocess.run(timeout=...) did — SIGKILLed npx while the wedged Chrome
    tree survived, reparented, and kept burning CPU forever (one leaked tree
    per timeout)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        # Group already gone (or unkillable) — fall back to the direct child.
        proc.kill()
    try:
        # Reap the child + drain/close its pipes; bounded so a kernel-stuck
        # zombie can't re-wedge the gate slot the timeout just freed.
        proc.communicate(timeout=10)
    except Exception:
        pass


def _cache_dir() -> Path:
    p = settings.output_dir / "cache" / "remotion"
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class VideoProbe:
    duration_secs: float
    width: int
    height: int


def _probe_video(video_path: Path) -> VideoProbe:
    """Read duration + dimensions via ffmpeg's stderr output.

    `imageio_ffmpeg` bundles ffmpeg but not ffprobe, so we parse ffmpeg's
    informational output instead. ffmpeg -i prints a 'Duration: H:MM:SS.cs'
    line and a 'Stream #0:0 ... Video: ... <W>x<H>' line.
    """
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-i", str(video_path)],
        capture_output=True, text=True,
    )
    text = proc.stderr  # ffmpeg writes info to stderr even on success
    duration = 0.0
    width = 1080
    height = 1920
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Duration:"):
            try:
                ts = s.split("Duration:")[1].split(",")[0].strip()
                h, m, rest = ts.split(":")
                duration = int(h) * 3600 + int(m) * 60 + float(rest)
            except Exception:
                pass
        elif "Video:" in s:
            # Look for WxH pattern, e.g. "1080x1920" or "1080x1920 [SAR ..."
            import re
            m = re.search(r"(\d{2,5})x(\d{2,5})", s)
            if m:
                width = int(m.group(1))
                height = int(m.group(2))
    return VideoProbe(duration_secs=duration, width=width, height=height)


def _hash_render_inputs(
    composition_id: str,
    full_props: dict[str, Any],
    input_path: Path,
) -> str:
    """SHA-256 over (composition, props JSON, input file stat).

    File content isn't hashed — too slow for multi-MB videos. Path + size
    + mtime is sufficient because each upload gets its own `edit_id` dir
    with a fresh path, so collisions across uploads are not possible.
    """
    st = input_path.stat()
    payload = {
        "composition": composition_id,
        "props": full_props,
        "input": {
            "path": str(input_path),
            "size": st.st_size,
            "mtime": int(st.st_mtime),
        },
        # Encode quality is part of the cache identity — without this, a
        # quality bump (REMOTION_CRF/REMOTION_JPEG_QUALITY) would silently
        # serve old lower-quality renders from the cache.
        "encode": {
            "crf": settings.remotion_crf,
            "jpeg_quality": settings.remotion_jpeg_quality,
        },
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def render_remotion(
    input_video: Path,
    output_video: Path,
    *,
    composition_id: str,
    props: dict[str, Any],
    words: list[dict[str, Any]],
    job_id: str | None = None,
) -> dict:
    """Render captions over `input_video` using a Remotion composition.

    Parameters
    ----------
    composition_id : str
        Must match a `<Composition id="...">` in `remotion/src/Root.tsx`.
    props : dict
        Visual params (accent, fontFamily, sizeScale, positionPct,
        allCaps, wordsPerCard). Will be merged with auto-detected video
        metadata before being passed to React.
    words : list[dict]
        Word-level timestamps from Whisper. Each item has
        `{text, start, end}`. Must be plain JSON-serializable (the
        caller is responsible for converting from `Word` dataclass).
    """
    remotion_dir = _remotion_dir()
    if not (remotion_dir / "package.json").is_file():
        raise RuntimeError(
            f"Remotion project missing at {remotion_dir}. "
            f"Run `character-swap remotion-install` first."
        )
    _ensure_installed(remotion_dir)

    probe = _probe_video(input_video)
    input_video = input_video.resolve()
    # Remotion 4 OffthreadVideo no longer accepts file:// URLs — the
    # renderer's headless Chrome refuses anything that's not http(s).
    # We use a placeholder URL when computing the cache key so the hash
    # is stable across runs (the port number is random per process), then
    # if it's a cache miss we spin up an ephemeral HTTP server below and
    # substitute the real URL before running the render subprocess.
    placeholder_url = f"local://{input_video.name}"
    full_props: dict[str, Any] = {
        "videoSrc": placeholder_url,
        "words": words,
        "videoDurationSecs": probe.duration_secs,
        "videoWidth": probe.width,
        "videoHeight": probe.height,
        **props,
    }

    cache_key = _hash_render_inputs(composition_id, full_props, input_video)
    cache_path = _cache_dir() / f"{cache_key}.mp4"
    output_video.parent.mkdir(parents=True, exist_ok=True)

    if cache_path.is_file():
        shutil.copy2(cache_path, output_video)
        return {
            "engine": "remotion",
            "composition": composition_id,
            "n_words": len(words),
            "cached": True,
        }

    queue_t0 = time.monotonic()
    with _render_gate():
        queue_wait = time.monotonic() - queue_t0
        # A queued sibling (e.g. a compile retry with identical inputs) may
        # have filled the cache while we waited for a render slot.
        if cache_path.is_file():
            shutil.copy2(cache_path, output_video)
            return {
                "engine": "remotion",
                "composition": composition_id,
                "n_words": len(words),
                "cached": True,
            }
        return _render_locked(
            input_video, output_video,
            composition_id=composition_id, full_props=full_props,
            words=words, cache_key=cache_key, cache_path=cache_path,
            queue_wait=queue_wait, job_id=job_id,
        )


def _render_locked(
    input_video: Path,
    output_video: Path,
    *,
    composition_id: str,
    full_props: dict[str, Any],
    words: list[dict[str, Any]],
    cache_key: str,
    cache_path: Path,
    queue_wait: float,
    job_id: str | None,
) -> dict:
    remotion_dir = _remotion_dir()
    with record(phase="remotion_render", model=composition_id,
                character="editor", job_id=job_id) as entry:
        # Queue wait is logged but excluded from latency_ms (record starts
        # after the gate) so per-render stats stay comparable over time.
        entry["queue_wait_secs"] = round(queue_wait, 2)
        # Spin up an ephemeral HTTP server serving the directory that
        # contains the input video; rewrite videoSrc to its real URL so
        # the Remotion renderer's headless Chrome can fetch the file.
        import functools
        import http.server
        import socketserver
        import urllib.parse as _urlparse
        serve_dir = input_video.parent

        # Quiet handler — silences the per-request access log so render
        # subprocess output stays clean. Still surfaces errors via HTTP codes.
        class _QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *_a, **_k) -> None:  # type: ignore[override]
                return
        handler = functools.partial(_QuietHandler, directory=str(serve_dir))
        httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        server_port = httpd.server_address[1]
        server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        server_thread.start()
        safe_name = _urlparse.quote(input_video.name)
        served_url = f"http://127.0.0.1:{server_port}/{safe_name}"
        real_props = {**full_props, "videoSrc": served_url}

        # Write props JSON to a temp file alongside the output; Remotion CLI
        # reads it with `--props=<path>`.
        # Both temp names carry a per-render token: the gate admits 2 renders
        # at once and the in-gate cache re-check only catches a FINISHED
        # sibling, so two concurrent renders with the SAME cache key used to
        # write into ONE shared .partial file — the interleaved garbage was
        # then promoted into the cache permanently, and the first finisher's
        # cleanup unlinked the shared props file out from under the sibling.
        render_token = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        props_path = output_video.parent / (
            f".remotion-props-{cache_key}-{render_token}.json")
        props_path.write_text(json.dumps(real_props), encoding="utf-8")
        # Backlog #8 (2026-06-12): render to a .partial temp file and
        # promote atomically on success. Rendering straight to cache_path
        # meant a failed/killed render left a truncated MP4 at the cache
        # key — served as a successful render on every future hit.
        partial_path = cache_path.with_name(
            f"{cache_path.name}.partial-{render_token}.mp4")
        try:
            cmd = [
                "npx", "--prefix", str(remotion_dir),
                "remotion", "render",
                composition_id,
                str(partial_path),
                f"--props={props_path}",
                # Tabs-per-render × the process-wide gate is the real
                # parallelism budget: 4 × 2 = 8 Chrome tabs on an 18-core
                # machine. Overrides remotion.config.ts.
                f"--concurrency={max(1, settings.remotion_concurrency)}",
                # Per-frame delayRender budget. The Remotion default (30s)
                # is too tight for a cold OffthreadVideo seek in the long
                # concat videos Step-6 compile produces.
                f"--timeout={max(30_000, settings.remotion_timeout_ms)}",
                # HARD cap on the compositor's decoded-frame cache. Without
                # it Remotion defaults to "half of system memory" measured
                # per render process at its own start, so concurrent renders
                # of a full-length reel each grew to ~20GB and OOM-killed the
                # machine (2026-07-31). Never affects OUTPUT — deliberately
                # excluded from the cache key in _hash_render_inputs.
                f"--offthreadvideo-cache-size-in-bytes={_offthread_cache_budget()}",
                # Encode quality (2026-06-12): Remotion's defaults (h264 CRF
                # ~23-equivalent + JPEG-80 frame captures) were the last lossy
                # hop in the chain — measured ~3.2 Mbps 1080x1920 finals.
                # CRF 16 + lossless-ish JPEG 100 captures make the caption
                # pass nearly transparent.
                f"--crf={settings.remotion_crf}",
                f"--jpeg-quality={settings.remotion_jpeg_quality}",
                "--log=info",
            ]
            entry["concurrency"] = max(1, settings.remotion_concurrency)
            # Whole-subprocess backstop (backlog #11): a hung headless
            # Chrome used to hold one of the gate slots forever. The render
            # runs in its OWN process group (start_new_session) so on
            # timeout we can kill the WHOLE tree — subprocess.run's own
            # timeout only SIGKILLed the direct child (npx), leaving the
            # wedged node/Chrome descendants alive (see _kill_render_tree).
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(remotion_dir),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    start_new_session=True,
                )
            except FileNotFoundError as e:
                # npx vanished between the preflight guard above and here
                # (PATH change, half-finished install). Same loud message —
                # never let an OSError surface as an opaque 500.
                raise RuntimeError(REMOTION_MISSING_MSG) from e
            timeout_secs = max(60, settings.remotion_render_timeout_secs)
            try:
                stdout, stderr = proc.communicate(timeout=timeout_secs)
            except subprocess.TimeoutExpired as e:
                _kill_render_tree(proc)
                raise RuntimeError(
                    f"remotion render timed out after {timeout_secs:.0f}s — "
                    "render process tree (npx/node/Chrome) killed, gate slot "
                    "released") from e
            if proc.returncode != 0:
                detail = (stderr or stdout or "").strip()[-2000:]
                raise RuntimeError(
                    f"remotion render failed (exit {proc.returncode}): {detail}"
                )
            # Success → promote atomically into the cache.
            os.replace(partial_path, cache_path)
        finally:
            partial_path.unlink(missing_ok=True)
            props_path.unlink(missing_ok=True)
            httpd.shutdown()
            httpd.server_close()

    shutil.copy2(cache_path, output_video)
    return {
        "engine": "remotion",
        "composition": composition_id,
        "n_words": len(words),
        "cached": False,
    }
