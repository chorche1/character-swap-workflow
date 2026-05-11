from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from character_swap.config import settings

_lock = threading.Lock()


def _path() -> Path:
    p = settings.call_log_file
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


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
        append(payload)
