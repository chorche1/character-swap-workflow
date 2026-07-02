"""JsonStateStore corrupt-state handling (audit 2026-07-01).

`_load` used to catch ValueError — which INCLUDES pydantic ValidationError —
and silently sideline a corrupt/schema-invalid state.json to a FIXED-name
backup with zero logging: the user booted into a blank app with no
explanation, and a repeat corruption clobbered the previous backup. This
locks the loud path: the unreadable file is COPIED to
`state.json.corrupt-<UTC timestamp>` and an ERROR log names the backup + the
parse error. The server must stay bootable either way (never raise).
"""
from __future__ import annotations

import json
import logging

from character_swap.models import SceneAsset
from character_swap.state import JsonStateStore

_TRUNCATED = '{"jobs": {  TRUNCATED MID-WRITE'


def _error_messages(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.ERROR]


def test_truncated_json_backed_up_and_logged(tmp_path, caplog):
    p = tmp_path / "state.json"
    p.write_text(_TRUNCATED, encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="character_swap.state"):
        s = JsonStateStore(path=p)

    # Boots EMPTY but boots — the server must still start.
    assert s.state.jobs == {}
    assert s.state.characters == {}

    # The unreadable file is preserved byte-for-byte under a TIMESTAMPED
    # name (a second corruption must never clobber the first backup).
    backups = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == _TRUNCATED

    # An ERROR names both the backup and the parse error — never silent.
    msgs = _error_messages(caplog)
    assert any(str(backups[0]) in m for m in msgs)
    assert any("JSONDecodeError" in m for m in msgs)


def test_schema_invalid_state_backed_up_and_logged(tmp_path, caplog):
    """Valid JSON but schema-invalid — pydantic ValidationError subclasses
    ValueError, so this hits the SAME catch (e.g. an older checkout reading
    a state.json written by a newer worktree). The whole library used to be
    silently discarded here."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"jobs": "notadict"}), encoding="utf-8")

    with caplog.at_level(logging.ERROR, logger="character_swap.state"):
        s = JsonStateStore(path=p)

    assert s.state.jobs == {}
    backups = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {
        "jobs": "notadict"}
    msgs = _error_messages(caplog)
    assert any("ValidationError" in m and str(backups[0]) in m for m in msgs)

    # The store stays fully usable: the next save writes a fresh state.json
    # while the backup survives untouched.
    s.add_scene(SceneAsset(scene_id="sc1", filename="sc1.png",
                           original_name="sc1.png"))
    assert "sc1" in json.loads(p.read_text(encoding="utf-8"))["scenes"]
    assert backups[0].read_text(encoding="utf-8") == json.dumps(
        {"jobs": "notadict"})


def test_healthy_state_loads_without_backup_or_error(tmp_path, caplog):
    """Counterfactual: a clean load must create no backup and log no error."""
    p = tmp_path / "state.json"
    s1 = JsonStateStore(path=p)
    s1.add_scene(SceneAsset(scene_id="sc1", filename="sc1.png",
                            original_name="sc1.png"))

    with caplog.at_level(logging.ERROR, logger="character_swap.state"):
        s2 = JsonStateStore(path=p)

    assert "sc1" in s2.state.scenes
    assert list(tmp_path.glob("state.json.corrupt-*")) == []
    assert _error_messages(caplog) == []
