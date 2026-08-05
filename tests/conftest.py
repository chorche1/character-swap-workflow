"""Global test isolation — keep the suite out of the REAL data store.

config.py loads `.env`, which in every worktree is a symlink to the shared
PRODUCTION data store (~/character-swap-data): USE_SQLITE_STATE=1 plus
CHARACTERS_DIR / INPUT_DIR / OUTPUT_DIR / STATE_DIR pointing at the live
SQLite DB, call log, and media dirs. Before this conftest existed, any test
that touched state.store(), call_log, or the data dirs without explicit
monkeypatching wrote into production (observed 2026-06-11: junk job "j_ef"
in state.sqlite3 + dozens of test entries in calls.jsonl).

The fix relies on two load-order facts:

1. pydantic-settings gives real environment variables priority over `.env`
   file values, and
2. pytest imports tests/conftest.py before any test module — i.e. before
   character_swap.config builds its module-level `settings` singleton.

So exporting the four data-dir overrides at conftest import time guarantees
every Settings() constructed during the session points at a throwaway tmp
tree, no matter what the .env symlink says.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="charswap-test-data-"))

_ENV_OVERRIDES = {
    "STATE_DIR": TEST_DATA_ROOT / "state",
    "CHARACTERS_DIR": TEST_DATA_ROOT / "characters",
    "INPUT_DIR": TEST_DATA_ROOT / "input",
    "OUTPUT_DIR": TEST_DATA_ROOT / "output",
}
for _var, _dir in _ENV_OVERRIDES.items():
    _dir.mkdir(parents=True, exist_ok=True)
    os.environ[_var] = str(_dir)
# api.py's StaticFiles mounts validate scenes_dir at import.
(TEST_DATA_ROOT / "input" / "scenes").mkdir(parents=True, exist_ok=True)

# Production runs the SQLite backend, and the suite has effectively always
# run under USE_SQLITE_STATE=1 via the shared .env — pin it so the suite
# behaves the same on a checkout without that symlink.
os.environ["USE_SQLITE_STATE"] = "1"

_DIR_ATTRS = {
    "state_dir": _ENV_OVERRIDES["STATE_DIR"],
    "characters_dir": _ENV_OVERRIDES["CHARACTERS_DIR"],
    "input_dir": _ENV_OVERRIDES["INPUT_DIR"],
    "output_dir": _ENV_OVERRIDES["OUTPUT_DIR"],
}


@pytest.fixture(scope="session", autouse=True)
def _isolated_data_dirs():
    """Belt and braces: verify (and if needed repair) the settings singleton.

    If character_swap.config was somehow imported before this conftest set
    the env overrides (it shouldn't be — conftest loads first), the singleton
    would have captured the production paths. Repair it in place and drop any
    store() singleton built against the wrong paths.
    """
    from character_swap import state
    from character_swap.config import settings

    repaired = False
    for attr, path in _DIR_ATTRS.items():
        if Path(getattr(settings, attr)) != path:
            setattr(settings, attr, path)
            repaired = True
    if repaired:
        state._store = None

    for attr, path in _DIR_ATTRS.items():
        assert Path(getattr(settings, attr)) == path, (
            f"settings.{attr} escaped test isolation: {getattr(settings, attr)}"
        )
    yield


@pytest.fixture(autouse=True)
def _no_real_phone_push(monkeypatch):
    """No test may fire a REAL phone push (ntfy HTTP POST).

    The shared `.env` sets NTFY_TOPIC, so `push.enabled()` is True on any
    checkout with the symlink. Several tests reach `push.notify` indirectly
    (e.g. the compile target-selection tests run `compile_job_videos`, whose
    batch push fires with ok=0) — without this, those would buzz Hugo's phone
    on every `pytest` run from the main checkout. Setting the env var empty
    won't help (config uses `env_ignore_empty=True`, so an empty override is
    ignored and the .env value wins), so pin the singleton attr directly
    before each test. Tests that exercise the push path turn it back on
    explicitly via monkeypatch + a stubbed `_send`/`notify`."""
    from character_swap.config import settings
    monkeypatch.setattr(settings, "ntfy_topic", "", raising=False)


@pytest.fixture(autouse=True)
def _no_real_telegram_send(monkeypatch):
    """No test may deliver a REAL video to a REAL Telegram channel.

    Observed 2026-08-04: Hugo's "Character Swap – Editor Finals" channel had
    filled up with dozens of 1-byte .mp4 "finals" named after test fixtures
    ("hello world this is the script — repurpose [ed_…]"). Every one came from
    `pytest`, not from the app. The PostToolUse hook fires on any .py edit —
    including edits inside a worktree — and runs the suite from the MAIN
    checkout, whose `.env` carries the live TELEGRAM_EDITOR_BOT_TOKEN +
    TELEGRAM_EDITOR_CHAT_ID. Five tests reached delivery for real on every
    run: the two `repurpose_editor_job` workers (which stub
    `run_editor_pipeline` but not the send that follows it) and the three
    TestClient calls to `/api/editor/auto_edit` + `/api/editor/multi_auto_edit`
    (both endpoints auto-send the finished reel).

    `_post_with_retry` is the single HTTP egress in `clients/telegram.py`, so
    blocking it here stops delivery no matter which layer a test enters
    through, while leaving `send_document`'s own logic (token/limit checks,
    multipart shape) testable: the tests that exercise the transport patch
    `_post_with_retry` or `send_document` themselves, and a per-test patch is
    applied after this fixture, so it wins."""
    from character_swap.clients import telegram

    def _blocked(*_a, **_k):
        raise RuntimeError(
            "A test tried to reach the real Telegram Bot API. Stub the send in "
            "the test itself — monkeypatch telegram_delivery.send_editor_final "
            "/ send_character_final for a delivery path, or "
            "clients.telegram.send_document / _post_with_retry for the "
            "transport."
        )

    monkeypatch.setattr(telegram, "_post_with_retry", _blocked)
