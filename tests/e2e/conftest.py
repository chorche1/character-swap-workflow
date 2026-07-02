"""Fixtures for the stubbed-provider e2e flow tests.

Isolation: tests/conftest.py (the parent conftest, imported first) already
forces STATE_DIR / CHARACTERS_DIR / INPUT_DIR / OUTPUT_DIR to a throwaway tmp
tree and pins ntfy off, so nothing here can touch the real data store. The
`_assert_isolated` fixture double-checks that guarantee per test.

The `client` fixture builds a TestClient WITHOUT the lifespan context —
startup recovery / resume machinery stays off, and FastAPI BackgroundTasks
run INLINE before each request returns, which is exactly what makes these
flows deterministic (no real polling loops against a live server).
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "e2e: stubbed-provider full-flow end-to-end test (runs by default — "
        "deselect with `-m 'not e2e'`)",
    )


@pytest.fixture(autouse=True)
def _assert_isolated():
    """Belt-and-braces: refuse to run against anything but the tmp data tree
    that tests/conftest.py set up."""
    from character_swap.config import settings
    tmp_root = Path(tempfile.gettempdir()).resolve()
    for attr in ("state_dir", "characters_dir", "input_dir", "output_dir"):
        p = Path(getattr(settings, attr)).resolve()
        assert tmp_root in p.parents or p == tmp_root, (
            f"settings.{attr} is not isolated to a tmp dir: {p} — "
            "refusing to run e2e flows against a real data store")
    yield


@pytest.fixture()
def ledger(monkeypatch):
    """Install all provider fakes; returns the call ledger."""
    from e2e import fakes
    return fakes.apply_fakes(monkeypatch)


@pytest.fixture()
def client(ledger):
    """TestClient over the real app. Depends on `ledger` so the fakes are in
    place before any request can schedule background work."""
    from character_swap import api
    return TestClient(api.app)
