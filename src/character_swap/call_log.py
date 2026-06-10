from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from character_swap.config import settings

_lock = threading.Lock()


def _path() -> Path:
    p = settings.call_log_file
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _cost_usd(phase: str, ok: bool, payload: dict | None = None) -> float:
    """Estimated USD cost for a recorded call. Polls are free; failures don't bill.
    `payload` lets us read per-call extras (e.g. duration_secs for time-billed
    services like fal.ai)."""
    if not ok:
        return 0.0
    payload = payload or {}
    if phase in {"generate", "edit"}:
        return settings.openai_image_price_usd
    if phase == "phase4_submit":
        return settings.grok_video_price_usd
    # Higgsfield Character Swap — one Soul generation per variant.
    if phase == "higgsfield_swap":
        return settings.higgsfield_price_usd
    # fal-hosted swap edits (Qwen Edit+ / Kontext Max / Seedream Edit).
    if phase == "fal_swap":
        return settings.fal_swap_price_usd
    # AI Director — one Opus call per Director invocation (swap or movement).
    # Reengineer's scene analyst is the same one-Claude-call shape.
    if phase in {"director_swap", "director_movement", "reengineer_analyze"}:
        return settings.claude_opus_price_usd
    # Vision QC — one cheap Haiku call per generated swap variant.
    if phase == "swap_qc":
        return settings.swap_qc_price_usd
    # Chat tab — each agent-loop iteration is one Opus call (vision + tool use).
    if phase == "chat":
        return settings.claude_opus_price_usd
    # VEED Subtitle Styling on fal.ai — billed per minute of input video.
    if phase == "fal_caption":
        duration_secs = float(payload.get("duration_secs", 0))
        return round(duration_secs / 60.0 * settings.fal_caption_price_per_minute_usd, 4)
    return 0.0


def append(entry: dict[str, Any]) -> None:
    entry = {"ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z", **entry}
    line = json.dumps(entry, default=str)
    with _lock, _path().open("a", encoding="utf-8") as f:
        f.write(line + "\n")


@contextmanager
def record(phase: str, model: str, **extra: Any):
    started = time.monotonic()
    payload: dict[str, Any] = {"phase": phase, "model": model, **extra}
    err: BaseException | None = None
    try:
        yield payload
    except BaseException as e:
        err = e
        raise
    finally:
        payload["latency_ms"] = round((time.monotonic() - started) * 1000)
        payload["ok"] = err is None
        if err is not None:
            payload["error"] = f"{type(err).__name__}: {err}"
        payload["cost_usd"] = _cost_usd(phase, payload["ok"], payload)
        append(payload)


def _parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.rstrip("Z"))
    except ValueError:
        return None


def read_costs(*, job_id: str | None = None, since: datetime | None = None) -> float:
    """Sum cost_usd in calls.jsonl. Filter by job_id and/or `since` (UTC)."""
    p = _path()
    if not p.exists():
        return 0.0
    total = 0.0
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if job_id is not None and entry.get("job_id") != job_id:
                continue
            if since is not None:
                ts = _parse_ts(entry.get("ts", ""))
                if ts is None or ts < since:
                    continue
            total += float(entry.get("cost_usd") or 0.0)
    return round(total, 4)


def costs_since(days: float) -> float:
    """Convenience: sum costs over the last N days."""
    return read_costs(since=datetime.utcnow() - timedelta(days=days))
