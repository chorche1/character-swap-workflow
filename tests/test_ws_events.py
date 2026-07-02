"""WebSocket /ws/jobs/{job_id} + events.py fan-out — the live-update contract.

2026-07-01 audit gap #4: the entire live UI (variant thumbnails, "k/N images"
counters, video.progress, compile chimes) rides on dicts published through
`events.publish` and relayed VERBATIM by `api.ws_job`. Before this file, zero
tests connected a WebSocket or inspected the subscriber registry — a renamed
event kind, a dropped payload key, or a subscribe/unsubscribe leak would ship
with the whole suite green and the UI would just silently stop updating.

Locked here:
  - a connected client receives a published event exactly as published;
  - a job with state sends a `snapshot` first; an unknown job doesn't crash;
  - events for OTHER job_ids are never delivered;
  - disconnect removes the subscriber (registry pruned — no leak);
  - events.py registry semantics: fan-out to N queues, empty-set pruning,
    publish-to-nobody is a no-op, full queues drop instead of raising.

Note on threading: TestClient runs each WS session's app in its own portal
loop. Publishing from the test thread therefore goes through
`run_coroutine_threadsafe` onto THAT loop (captured via an events.subscribe
spy) — same shape as runner code publishing via `publish_threadsafe`.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from character_swap import api, events
from character_swap.models import Job

client = TestClient(api.app)


class _FakeStore:
    def __init__(self, jobs: dict[str, Job] | None = None):
        self.jobs = jobs or {}

    def get_job(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)


@pytest.fixture
def app_loop(monkeypatch) -> dict:
    """Capture the WS session's event loop the moment the endpoint subscribes,
    so the test thread can schedule `events.publish` on it."""
    captured: dict = {}
    orig = events.subscribe

    async def spy(job_id: str):
        captured["loop"] = asyncio.get_running_loop()
        return await orig(job_id)

    monkeypatch.setattr(events, "subscribe", spy)
    return captured


def _publish(loop: asyncio.AbstractEventLoop, job_id: str, event: dict) -> None:
    asyncio.run_coroutine_threadsafe(
        events.publish(job_id, event), loop
    ).result(timeout=5.0)


def _wait_subscribed(job_id: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if events._subscribers.get(job_id):
            return
        time.sleep(0.01)
    raise AssertionError(f"WS endpoint never subscribed to {job_id!r}")


# --- endpoint: delivery ----------------------------------------------------------------


def test_ws_snapshot_then_relays_published_event_verbatim(monkeypatch, app_loop):
    job = Job(job_id="j_ws1", scene_id="s1", scene_image_path="/tmp/scene.png")
    monkeypatch.setattr(api, "store", lambda: _FakeStore({"j_ws1": job}))

    evt = {"kind": "variant.ready", "job_id": "j_ws1", "char_id": "c1",
           "variant_id": "v1", "path": "/tmp/v1.png"}
    with client.websocket_connect("/ws/jobs/j_ws1") as ws:
        snap = ws.receive_json()
        assert snap["kind"] == "snapshot"
        assert snap["job"]["job_id"] == "j_ws1"

        _publish(app_loop["loop"], "j_ws1", evt)
        # Payload must arrive EXACTLY as published — app.js keys on kind /
        # char_id / variant_id / path; any dropped key silently kills the UI.
        assert ws.receive_json() == evt
    assert "j_ws1" not in events._subscribers


def test_ws_unknown_job_id_no_snapshot_no_crash(monkeypatch, app_loop):
    """Connecting to a job the store doesn't know must not crash the endpoint
    — no snapshot is sent, but published events still flow (the job may be
    created a moment later)."""
    monkeypatch.setattr(api, "store", lambda: _FakeStore())

    evt = {"kind": "variant.ready", "job_id": "j_does_not_exist"}
    with client.websocket_connect("/ws/jobs/j_does_not_exist") as ws:
        _wait_subscribed("j_does_not_exist")
        _publish(app_loop["loop"], "j_does_not_exist", evt)
        # FIRST message is the published event — proving no snapshot was sent
        # for the unknown job (and nothing blew up before the relay loop).
        assert ws.receive_json() == evt
    assert "j_does_not_exist" not in events._subscribers


def test_ws_other_jobs_events_not_delivered(monkeypatch, app_loop):
    monkeypatch.setattr(api, "store", lambda: _FakeStore())

    mine = {"kind": "video.ready", "job_id": "j_ws_mine"}
    with client.websocket_connect("/ws/jobs/j_ws_mine") as ws:
        _wait_subscribed("j_ws_mine")
        # Publish to a DIFFERENT job first, then to ours. Publishes are
        # sequential (each .result() blocks), so if the foreign event leaked
        # into our queue it would arrive FIRST.
        _publish(app_loop["loop"], "j_ws_other", {"kind": "video.ready",
                                                  "job_id": "j_ws_other"})
        _publish(app_loop["loop"], "j_ws_mine", mine)
        assert ws.receive_json() == mine


def test_ws_disconnect_removes_subscriber_no_leak(monkeypatch, app_loop):
    """The endpoint's finally-unsubscribe must run on disconnect — otherwise
    every page reload leaks a queue that publish() fills forever."""
    monkeypatch.setattr(api, "store", lambda: _FakeStore())

    with client.websocket_connect("/ws/jobs/j_ws_leak"):
        _wait_subscribed("j_ws_leak")
        assert len(events._subscribers["j_ws_leak"]) == 1
    # WebSocketTestSession.__exit__ waits for the app task to finish, so the
    # endpoint's `finally: unsubscribe(...)` has completed by now.
    assert "j_ws_leak" not in events._subscribers


# --- events.py registry semantics --------------------------------------------------------


def test_events_fanout_to_multiple_subscribers_and_pruning():
    async def main():
        q1 = await events.subscribe("j_ev_fan")
        q2 = await events.subscribe("j_ev_fan")
        evt = {"kind": "char.approved", "job_id": "j_ev_fan", "char_id": "c1"}
        await events.publish("j_ev_fan", evt)
        assert q1.get_nowait() == evt
        assert q2.get_nowait() == evt

        await events.unsubscribe("j_ev_fan", q1)
        assert "j_ev_fan" in events._subscribers          # q2 still live
        await events.unsubscribe("j_ev_fan", q2)
        # Last unsubscribe prunes the empty set — the registry must not grow
        # one dead key per job forever.
        assert "j_ev_fan" not in events._subscribers

    asyncio.run(main())


def test_events_publish_without_subscribers_is_noop():
    async def main():
        await events.publish("j_ev_nobody", {"kind": "x"})
        # defaultdict must NOT have materialized an entry on publish.
        assert "j_ev_nobody" not in events._subscribers

    asyncio.run(main())


def test_events_unsubscribe_twice_is_safe():
    async def main():
        q = await events.subscribe("j_ev_dbl")
        await events.unsubscribe("j_ev_dbl", q)
        await events.unsubscribe("j_ev_dbl", q)           # no KeyError
        assert "j_ev_dbl" not in events._subscribers

    asyncio.run(main())


def test_events_full_queue_drops_instead_of_raising():
    """Slow consumer: publish is best-effort — a full queue (maxsize=128)
    drops the event instead of raising/blocking the runner."""
    async def main():
        q = await events.subscribe("j_ev_full")
        try:
            for i in range(200):
                await events.publish("j_ev_full", {"i": i})
            assert q.qsize() == 128                       # capped, no overflow
            assert q.get_nowait() == {"i": 0}             # oldest kept (FIFO)
        finally:
            await events.unsubscribe("j_ev_full", q)

    asyncio.run(main())
