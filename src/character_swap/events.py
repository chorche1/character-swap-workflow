"""
In-process pub/sub for live job updates.

Multiple WebSocket clients can subscribe to the same job_id; each subscriber
gets its own asyncio.Queue. The runner publishes events; the API broadcasts.

This is intentionally tiny: single-process, single-user. If we ever need to
scale beyond that, swap this out for Redis pub/sub without changing callers.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from typing import Any

# job_id -> set of subscriber queues
_subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
_lock = asyncio.Lock()


async def subscribe(job_id: str) -> asyncio.Queue:
    """Open a subscription. Caller MUST call `unsubscribe(job_id, queue)` when done."""
    q: asyncio.Queue = asyncio.Queue(maxsize=128)
    async with _lock:
        _subscribers[job_id].add(q)
    return q


async def unsubscribe(job_id: str, queue: asyncio.Queue) -> None:
    async with _lock:
        _subscribers[job_id].discard(queue)
        if not _subscribers[job_id]:
            _subscribers.pop(job_id, None)


async def publish(job_id: str, event: dict[str, Any]) -> None:
    """Best-effort broadcast. Drops events on full queues — clients then re-fetch state."""
    async with _lock:
        queues = list(_subscribers.get(job_id, ()))
    for q in queues:
        # Slow consumer — let it drop. Client should reload state on reconnect.
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(event)


def publish_threadsafe(loop: asyncio.AbstractEventLoop, job_id: str, event: dict[str, Any]) -> None:
    """Called from sync code running in `asyncio.to_thread`. Schedules publish on the loop."""
    asyncio.run_coroutine_threadsafe(publish(job_id, event), loop)
