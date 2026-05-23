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
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import imageio_ffmpeg

from character_swap.call_log import record
from character_swap.config import settings


# Path to the Remotion project. Resolved relative to the repo root via the
# settings.project_root anchor (set in config.py).
def _remotion_dir() -> Path:
    return settings.project_root / "remotion"


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

    with record(phase="remotion_render", model=composition_id,
                character="editor", job_id=job_id):
        # Spin up an ephemeral HTTP server serving the directory that
        # contains the input video; rewrite videoSrc to its real URL so
        # the Remotion renderer's headless Chrome can fetch the file.
        import functools
        import http.server
        import socketserver
        import threading
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
        props_path = output_video.parent / f".remotion-props-{cache_key}.json"
        props_path.write_text(json.dumps(real_props), encoding="utf-8")
        try:
            cmd = [
                "npx", "--prefix", str(remotion_dir),
                "remotion", "render",
                composition_id,
                str(cache_path),
                f"--props={props_path}",
                "--concurrency=1",
                "--log=info",
            ]
            proc = subprocess.run(
                cmd, cwd=str(remotion_dir),
                capture_output=True, text=True,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
                raise RuntimeError(
                    f"remotion render failed (exit {proc.returncode}): {detail}"
                )
        finally:
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
