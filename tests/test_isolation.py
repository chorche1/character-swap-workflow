"""Regression tests for tests/conftest.py's production-data isolation.

2026-06-11: a pytest run inserted junk job "j_ef" into the production SQLite
DB and appended test entries to the production calls.jsonl, because config.py
loads the shared .env and nothing redirected the data dirs for tests. These
tests fail loudly if that isolation ever regresses.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

_TMP_ROOT = Path(tempfile.gettempdir()).resolve()


def _assert_under_tmp(label: str, p: Path) -> None:
    resolved = Path(p).resolve()
    assert resolved.is_relative_to(_TMP_ROOT), (
        f"{label} = {resolved} — tests must only ever touch throwaway tmp "
        f"dirs, never a real data store (see tests/conftest.py)"
    )


def test_settings_point_at_throwaway_dirs():
    from character_swap.config import settings

    for attr in ("state_dir", "characters_dir", "input_dir", "output_dir"):
        _assert_under_tmp(f"settings.{attr}", getattr(settings, attr))
    # Derived paths used by state.store() and call_log.
    _assert_under_tmp("settings.state_db", settings.state_db)
    _assert_under_tmp("settings.state_file", settings.state_file)
    _assert_under_tmp("settings.call_log_file", settings.call_log_file)


def test_active_store_backing_file_is_isolated():
    from character_swap import state

    s = state.store()
    if hasattr(s, "_conn"):  # SqliteStateStore
        # PRAGMA database_list row: (seq, name, file)
        backing = s._conn.execute("PRAGMA database_list").fetchone()[2]
        # ":memory:" or "" would also be fine — only a real path needs checking.
        if backing:
            _assert_under_tmp("sqlite store backing file", Path(backing))
    else:  # JsonStateStore
        _assert_under_tmp("json store path", s.path)
