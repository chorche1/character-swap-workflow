"""Regression tests for the process-wide Remotion render gate.

2026-06-10: a 12-character Step-6 compile launched ~12 simultaneous
`npx remotion render` headless Chromes (one per character — PurplePill is
the batch-wide default template). Measured from calls.jsonl: 430s median
per render at 11-concurrent vs 71s solo, plus per-frame delayRender 30s
timeouts (`Timeout (30000ms) exceeded rendering the component at frame
427`) and one Chrome launch crash. The fix gates renders process-wide in
remotion_render.py and raises per-render --concurrency / --timeout.
"""
from __future__ import annotations

import contextlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from character_swap import remotion_render
from character_swap.config import settings


@contextlib.contextmanager
def _no_record(**_kw):
    """Stand-in for call_log.record — tests must not pollute calls.jsonl."""
    yield {}


def _cache_mp4_arg(cmd: list[str]) -> Path:
    return Path(next(a for a in cmd if a.endswith(".mp4")))


@pytest.fixture()
def render_env(tmp_path, monkeypatch):
    """Isolate cache dir, skip ffprobe, silence call logging, reset gate."""
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(remotion_render, "_cache_dir", lambda: cache)
    monkeypatch.setattr(
        remotion_render, "_probe_video",
        lambda _p: remotion_render.VideoProbe(
            duration_secs=1.0, width=1080, height=1920),
    )
    monkeypatch.setattr(remotion_render, "record", _no_record)
    remotion_render._gate = None
    yield tmp_path
    remotion_render._gate = None


def _make_input(tmp_path: Path, name: str) -> Path:
    p = tmp_path / name
    p.write_bytes(b"\x00" * 64)
    return p


WORDS = [{"text": "hi", "start": 0.0, "end": 0.5}]


def test_render_cmd_uses_settings_concurrency_and_timeout(render_env, monkeypatch):
    """--concurrency / --timeout come from settings, not hardcoded 1 / 30s."""
    monkeypatch.setattr(settings, "remotion_concurrency", 4)
    monkeypatch.setattr(settings, "remotion_timeout_ms", 120_000)
    captured: list[list[str]] = []

    def fake_run(cmd, **_kw):
        captured.append(cmd)
        _cache_mp4_arg(cmd).write_bytes(b"fake")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(remotion_render.subprocess, "run", fake_run)
    inp = _make_input(render_env, "in.mp4")
    out = render_env / "out.mp4"
    summary = remotion_render.render_remotion(
        inp, out, composition_id="CapCutPurplePill",
        props={"accent": "#8B5CF6"}, words=WORDS)
    assert summary["cached"] is False
    assert out.is_file()
    (cmd,) = captured
    assert "--concurrency=4" in cmd
    assert "--timeout=120000" in cmd
    assert "--concurrency=1" not in cmd


def test_gate_caps_simultaneous_render_subprocesses(render_env, monkeypatch):
    """8 parallel render calls never run more than the configured 2 at once."""
    monkeypatch.setattr(settings, "remotion_max_concurrent_renders", 2)
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def fake_run(cmd, **_kw):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.15)
        _cache_mp4_arg(cmd).write_bytes(b"fake")
        with lock:
            state["active"] -= 1
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(remotion_render.subprocess, "run", fake_run)

    def one(i: int) -> dict:
        inp = _make_input(render_env, f"in-{i}.mp4")
        return remotion_render.render_remotion(
            inp, render_env / f"out-{i}.mp4",
            composition_id="CapCutPurplePill",
            props={"accent": "#8B5CF6"}, words=WORDS)

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(one, range(8)))

    assert all(r["cached"] is False for r in results)
    assert all((render_env / f"out-{i}.mp4").is_file() for i in range(8))
    assert state["max_active"] == 2, (
        f"expected exactly 2 concurrent renders, saw {state['max_active']}")


def test_cache_rechecked_after_waiting_for_gate(render_env, monkeypatch):
    """A sibling render that fills the cache while we queue means no re-render."""
    inp = _make_input(render_env, "in.mp4")
    props = {"accent": "#8B5CF6"}
    # Reconstruct the exact cache key render_remotion will compute (probe is
    # mocked to fixed values) so the fake gate can fill it on acquire.
    full_props = {
        "videoSrc": f"local://{inp.name}",
        "words": WORDS,
        "videoDurationSecs": 1.0,
        "videoWidth": 1080,
        "videoHeight": 1920,
        **props,
    }
    cache_key = remotion_render._hash_render_inputs(
        "CapCutPurplePill", full_props, inp.resolve())
    cache_path = remotion_render._cache_dir() / f"{cache_key}.mp4"

    @contextlib.contextmanager
    def sibling_fills_cache_while_queued():
        cache_path.write_bytes(b"sibling render output")
        yield

    monkeypatch.setattr(
        remotion_render, "_render_gate", sibling_fills_cache_while_queued)

    def must_not_run(*_a, **_kw):  # pragma: no cover - failure path
        raise AssertionError("subprocess ran despite warm cache")

    monkeypatch.setattr(remotion_render.subprocess, "run", must_not_run)
    out = render_env / "out.mp4"
    summary = remotion_render.render_remotion(
        inp, out, composition_id="CapCutPurplePill", props=props, words=WORDS)
    assert summary["cached"] is True
    assert out.read_bytes() == b"sibling render output"
