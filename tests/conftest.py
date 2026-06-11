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
