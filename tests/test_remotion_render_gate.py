"""Regression tests for the process-wide Remotion render gate.

2026-06-10: a 12-character Step-6 compile launched ~12 simultaneous
`npx remotion render` headless Chromes (one per character — PurplePill is
the batch-wide default template). Measured from calls.jsonl: 430s median
per render at 11-concurrent vs 71s solo, plus per-frame delayRender 30s
timeouts (`Timeout (30000ms) exceeded rendering the component at frame
427`) and one Chrome launch crash. The fix gates renders process-wide in
remotion_render.py and raises per-render --concurrency / --timeout.

2026-07-01 additions: the render subprocess now runs in its OWN process
group so the timeout kills the whole npx → node → Chrome tree (not just
npx), and each render writes to a UNIQUE .partial temp file so two
concurrent renders with the same cache key can't interleave writes into
one file and promote garbage into the cache.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from character_swap import remotion_render
from character_swap.config import settings


@contextlib.contextmanager
def _no_record(**_kw):
    """Stand-in for call_log.record — tests must not pollute calls.jsonl."""
    yield {}


def _cache_mp4_arg(cmd: list[str]) -> Path:
    return Path(next(a for a in cmd if a.endswith(".mp4")))


def _props_arg(cmd: list[str]) -> str:
    return next(a for a in cmd if a.startswith("--props="))


def _install_popen(monkeypatch, render, spawn_kwargs: list | None = None):
    """Patch subprocess.Popen inside remotion_render with a fake process.

    `render(cmd, timeout)` runs inside communicate() and returns
    (returncode, stdout, stderr); it may write output files or raise
    subprocess.TimeoutExpired to simulate a hung Chrome.
    """
    class FakeProc:
        def __init__(self, cmd, **kwargs):
            self.cmd = cmd
            self.pid = 999_999  # never a real pid — killpg is stubbed in tests
            self.returncode = None
            self._timed_out = False
            if spawn_kwargs is not None:
                spawn_kwargs.append(kwargs)

        def communicate(self, timeout=None):
            if self._timed_out:
                # post-kill reap inside _kill_render_tree
                return "", ""
            try:
                rc, out, err = render(self.cmd, timeout)
            except subprocess.TimeoutExpired:
                self._timed_out = True
                raise
            self.returncode = rc
            return out, err

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(remotion_render.subprocess, "Popen", FakeProc)
    return FakeProc


def _ok_render(cmd, _timeout, payload: bytes = b"fake"):
    _cache_mp4_arg(cmd).write_bytes(payload)
    return 0, "", ""


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
    # The install preflight (2026-08-04) would otherwise refuse every render
    # here — node_modules/ is gitignored, so a checkout that never ran
    # `remotion-install` has none. Its own behavior is covered below.
    monkeypatch.setattr(remotion_render, "_ensure_installed", lambda _d: None)
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

    def fake_render(cmd, timeout):
        captured.append(cmd)
        return _ok_render(cmd, timeout)

    _install_popen(monkeypatch, fake_render)
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


def test_render_spawns_in_own_process_group(render_env, monkeypatch):
    """The subprocess must be its own session/process-group leader so a
    timeout can kill the whole npx → node → Chrome tree via killpg."""
    spawn_kwargs: list[dict] = []
    _install_popen(monkeypatch, _ok_render, spawn_kwargs=spawn_kwargs)
    inp = _make_input(render_env, "in.mp4")
    remotion_render.render_remotion(
        inp, render_env / "out.mp4", composition_id="CapCutPurplePill",
        props={"accent": "#8B5CF6"}, words=WORDS)
    (kwargs,) = spawn_kwargs
    assert kwargs.get("start_new_session") is True, (
        "render subprocess must run in its own process group — without it a "
        "timeout kills only npx and leaks the node/Chrome descendants")


def test_failed_render_never_poisons_the_cache(render_env, monkeypatch):
    """Backlog #8 (2026-06-12): the render used to write straight to the
    cache key — a failed/killed Chrome left a truncated MP4 there, served
    as a successful render on every future hit. Now it renders to a
    .partial temp and promotes atomically only on success."""
    def dying_render(cmd, _timeout):
        _cache_mp4_arg(cmd).write_bytes(b"truncated-by-crash")
        return 1, "", "chrome crashed"

    _install_popen(monkeypatch, dying_render)
    inp = _make_input(render_env, "in.mp4")
    with pytest.raises(RuntimeError, match="remotion render failed"):
        remotion_render.render_remotion(
            inp, render_env / "out.mp4", composition_id="CapCutPurplePill",
            props={"accent": "#8B5CF6"}, words=WORDS)
    # Nothing at the cache key, no orphaned .partial either.
    assert list(remotion_render._cache_dir().iterdir()) == []

    # The same render retried after the transient failure succeeds cleanly
    # (cache miss, not a poisoned hit).
    _install_popen(monkeypatch, _ok_render)
    summary = remotion_render.render_remotion(
        inp, render_env / "out.mp4", composition_id="CapCutPurplePill",
        props={"accent": "#8B5CF6"}, words=WORDS)
    assert summary["cached"] is False
    assert (render_env / "out.mp4").read_bytes() == b"fake"


def test_render_subprocess_timeout_kills_tree_and_releases_gate(render_env, monkeypatch):
    """Backlog #11 (2026-06-12): no subprocess timeout meant a hung headless
    Chrome held 1 of the 2 gate slots forever. The run is now bounded by
    settings.remotion_render_timeout_secs; on expiry the ENTIRE process
    group is SIGKILLed (2026-07-01 — subprocess.run's own timeout only
    killed npx, leaking the wedged node/Chrome tree), the cache stays clean
    and the gate slot is released."""
    monkeypatch.setattr(settings, "remotion_render_timeout_secs", 60)
    seen_timeouts: list[float] = []
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(remotion_render.os, "killpg",
                        lambda pgid, sig: kills.append((pgid, sig)))

    def hung_render(cmd, timeout):
        seen_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(cmd, timeout)

    proc_cls = _install_popen(monkeypatch, hung_render)
    inp = _make_input(render_env, "in.mp4")
    with pytest.raises(RuntimeError, match="timed out"):
        remotion_render.render_remotion(
            inp, render_env / "out.mp4", composition_id="CapCutPurplePill",
            props={"accent": "#8B5CF6"}, words=WORDS)
    assert seen_timeouts == [60]
    assert list(remotion_render._cache_dir().iterdir()) == []
    # The WHOLE process group was killed, not just the direct child.
    assert kills == [(999_999, signal.SIGKILL)], (
        "timeout must os.killpg the render's process group")
    assert proc_cls is not None

    # Gate slot was released: the next render goes straight through.
    _install_popen(monkeypatch, _ok_render)
    summary = remotion_render.render_remotion(
        inp, render_env / "out.mp4", composition_id="CapCutPurplePill",
        props={"accent": "#8B5CF6"}, words=WORDS)
    assert summary["cached"] is False


def test_kill_render_tree_kills_grandchildren():
    """Real-OS check: _kill_render_tree must reach DESCENDANTS of the direct
    child (the npx → node → Chrome chain), not just the child itself. A
    plain proc.kill() would orphan the grandchild `sleep` here."""
    proc = subprocess.Popen(
        ["/bin/sh", "-c", "sleep 30 & echo $!; wait"],
        stdout=subprocess.PIPE, text=True,
        start_new_session=True,
    )
    try:
        grandchild_pid = int(proc.stdout.readline().strip())
        remotion_render._kill_render_tree(proc)
        # Direct child reaped.
        assert proc.returncode is not None
        # Grandchild dead too — killpg reached the whole group.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("grandchild survived the process-tree kill")
    finally:
        # Belt and braces — never leave a stray sleep behind on failure.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)


def test_gate_caps_simultaneous_render_subprocesses(render_env, monkeypatch):
    """8 parallel render calls never run more than the configured 2 at once."""
    monkeypatch.setattr(settings, "remotion_max_concurrent_renders", 2)
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def slow_render(cmd, _timeout):
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.15)
        _cache_mp4_arg(cmd).write_bytes(b"fake")
        with lock:
            state["active"] -= 1
        return 0, "", ""

    _install_popen(monkeypatch, slow_render)

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


def test_concurrent_identical_key_renders_use_distinct_partial_files(render_env, monkeypatch):
    """2026-07-01: two renders with the SAME cache key admitted by the gate
    simultaneously (the in-gate re-check only catches a FINISHED sibling)
    used to write into ONE shared .partial file — interleaved garbage was
    promoted into the cache permanently, and the first finisher unlinked
    the shared props file out from under the sibling. Each render must get
    its own temp files; each promoted output must be one COMPLETE render."""
    monkeypatch.setattr(settings, "remotion_max_concurrent_renders", 2)
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    payloads = [b"A" * 4096, b"B" * 4096]
    seen: list[dict] = []

    def overlapping_render(cmd, _timeout):
        with lock:
            payload = payloads[len(seen)]
            seen.append({
                "partial": _cache_mp4_arg(cmd),
                "props": _props_arg(cmd),
                "payload": payload,
            })
        # Both renders are mid-flight at the same time — deterministic
        # reproduction of the double-submit race.
        barrier.wait(timeout=10)
        _cache_mp4_arg(cmd).write_bytes(payload)
        return 0, "", ""

    _install_popen(monkeypatch, overlapping_render)
    inp = _make_input(render_env, "in.mp4")

    def one(i: int) -> dict:
        return remotion_render.render_remotion(
            inp, render_env / f"out-{i}.mp4",
            composition_id="CapCutPurplePill",
            props={"accent": "#8B5CF6"}, words=WORDS)

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(one, range(2)))

    assert len(seen) == 2
    # THE fix: no shared temp files between the two in-flight renders.
    assert seen[0]["partial"] != seen[1]["partial"], (
        "concurrent same-key renders must not share a .partial file")
    assert seen[0]["props"] != seen[1]["props"], (
        "concurrent same-key renders must not share a props file")
    assert all(r["cached"] is False for r in results)

    # The cache holds exactly ONE complete render (either one), no leftovers.
    cache_files = list(remotion_render._cache_dir().iterdir())
    assert len(cache_files) == 1
    assert cache_files[0].suffix == ".mp4"
    assert cache_files[0].read_bytes() in (payloads[0], payloads[1])
    # Both outputs are COMPLETE payloads — never interleaved garbage.
    for i in range(2):
        assert (render_env / f"out-{i}.mp4").read_bytes() in (
            payloads[0], payloads[1])
    # No orphaned props files either.
    assert not list(render_env.glob(".remotion-props-*"))


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

    def must_not_spawn(*_a, **_kw):  # pragma: no cover - failure path
        raise AssertionError("subprocess spawned despite warm cache")

    monkeypatch.setattr(remotion_render.subprocess, "Popen", must_not_spawn)
    out = render_env / "out.mp4"
    summary = remotion_render.render_remotion(
        inp, out, composition_id="CapCutPurplePill", props=props, words=WORDS)
    assert summary["cached"] is True
    assert out.read_bytes() == b"sibling render output"


def test_render_cmd_caps_offthread_video_cache(render_env, monkeypatch):
    """The OffthreadVideo frame cache must carry an EXPLICIT byte cap.

    2026-07-31 OOM: Remotion's default for this option is null = "half of
    system memory", read per render process at ITS OWN start. Four
    concurrent renders of a ~110s 1080x1920 reel therefore each grew a
    decoded-frame cache toward ~20GB (3330 frames x 6.2MB, which fits under
    "half of 64GB" so nothing ever evicted) — 64GB RAM exhausted, 65GB swap,
    all four compositors SIGKILLed. The total budget is split across the
    gate so the ceiling holds no matter how many renders fan out.
    """
    monkeypatch.setattr(settings, "remotion_offthread_cache_bytes", 8 * 1024**3)
    monkeypatch.setattr(settings, "remotion_max_concurrent_renders", 4)
    captured: list[list[str]] = []

    def fake_render(cmd, timeout):
        captured.append(cmd)
        return _ok_render(cmd, timeout)

    _install_popen(monkeypatch, fake_render)
    inp = _make_input(render_env, "in.mp4")
    remotion_render.render_remotion(
        inp, render_env / "out.mp4", composition_id="CapCutBlueBox",
        props={}, words=WORDS)

    (cmd,) = captured
    flag = next(
        (a for a in cmd if a.startswith("--offthreadvideo-cache-size-in-bytes=")),
        None)
    assert flag is not None, "render must cap the OffthreadVideo cache"
    per_render = int(flag.split("=", 1)[1])
    # Each render gets its share; total across the gate stays at the budget.
    assert per_render == (8 * 1024**3) // 4
    assert per_render * settings.remotion_max_concurrent_renders <= 8 * 1024**3


def test_offthread_cache_budget_scales_with_gate_and_has_a_floor():
    """Budget splits across the gate, but never below a workable floor."""
    from character_swap.config import settings as s

    orig_total = s.remotion_offthread_cache_bytes
    orig_gate = s.remotion_max_concurrent_renders
    try:
        s.remotion_offthread_cache_bytes = 8 * 1024**3
        s.remotion_max_concurrent_renders = 2
        assert remotion_render._offthread_cache_budget() == 4 * 1024**3
        s.remotion_max_concurrent_renders = 8
        assert remotion_render._offthread_cache_budget() == 1024**3
        # A tiny total (or a huge gate) must not starve a render to 0 bytes.
        s.remotion_offthread_cache_bytes = 1024**2
        assert (remotion_render._offthread_cache_budget()
                == remotion_render._MIN_OFFTHREAD_CACHE_BYTES)
    finally:
        s.remotion_offthread_cache_bytes = orig_total
        s.remotion_max_concurrent_renders = orig_gate


# ---------------------------------------------------------------------------
# Install preflight (2026-08-04). Packaging the app for a machine that never
# ran `character-swap remotion-install` exposed this: remotion/package.json is
# COMMITTED but node_modules/ is gitignored, so the old guard passed and the
# render fell through to Popen(["npx", ...]) → FileNotFoundError. That's an
# OSError, and api.py's caption endpoints catch only RuntimeError, so the user
# got an opaque 500 — on the app's own DEFAULT template, which is a Remotion
# one. Both the preflight and the Popen-time backstop must refuse LOUDLY.
# ---------------------------------------------------------------------------

def test_render_refuses_loudly_when_node_modules_missing(render_env, monkeypatch):
    monkeypatch.undo()  # drop render_env's _ensure_installed stub
    missing = render_env / "remotion-no-modules"
    (missing).mkdir()
    (missing / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(remotion_render, "_remotion_dir", lambda: missing)
    monkeypatch.setattr(remotion_render, "record", _no_record)

    def _never_spawn(*_a, **_kw):
        raise AssertionError("must refuse BEFORE spawning npx")

    monkeypatch.setattr(remotion_render.subprocess, "Popen", _never_spawn)
    with pytest.raises(RuntimeError, match="remotion-install"):
        remotion_render.render_remotion(
            _make_input(render_env, "in.mp4"), render_env / "out.mp4",
            composition_id="CapCutPurplePill", props={}, words=WORDS)


def test_render_refuses_loudly_when_node_is_absent(render_env, monkeypatch, tmp_path):
    """node_modules/ present but no `node` on PATH — half-finished install."""
    monkeypatch.undo()
    have_modules = tmp_path / "remotion-ok"
    (have_modules / "node_modules").mkdir(parents=True)
    (have_modules / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(remotion_render, "_remotion_dir", lambda: have_modules)
    monkeypatch.setattr(remotion_render, "record", _no_record)
    monkeypatch.setattr(remotion_render.shutil, "which", lambda _n: None)
    with pytest.raises(RuntimeError, match="remotion-install"):
        remotion_render.render_remotion(
            _make_input(render_env, "in2.mp4"), render_env / "out.mp4",
            composition_id="CapCutPurplePill", props={}, words=WORDS)


def test_missing_npx_at_spawn_becomes_runtimeerror_not_oserror(render_env, monkeypatch):
    """Backstop: PATH changed between preflight and spawn. A raw
    FileNotFoundError here would surface as an opaque 500 (api.py catches
    RuntimeError only), so it must be translated."""
    def _no_npx(*_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory: 'npx'")

    monkeypatch.setattr(remotion_render.subprocess, "Popen", _no_npx)
    with pytest.raises(RuntimeError, match="remotion-install"):
        remotion_render.render_remotion(
            _make_input(render_env, "in3.mp4"), render_env / "out.mp4",
            composition_id="CapCutPurplePill", props={}, words=WORDS)
