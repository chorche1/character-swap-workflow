"""Round-trip persistence tests for SqliteStateStore + JsonStateStore.

These tests would have caught yesterday's voice_id-doesn't-persist bug
(2026-05-18). The pattern: mutate via the store's API, throw the store
away, build a fresh one against the same DB file, verify the mutation
is still visible.

Each store-backend test gets its own tmp_path so they're hermetic.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from character_swap.models import (
    CharacterAsset,
    Job,
    JobCharacter,
    ProjectAsset,
    SceneAsset,
)
from character_swap.state import JsonStateStore, SqliteStateStore


# --- SqliteStateStore round-trips ------------------------------------------------------


@pytest.fixture
def sqlite_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


def _fresh_sqlite(db_path: Path) -> SqliteStateStore:
    """Build a store against a specific DB file. Same file = same data."""
    return SqliteStateStore(db_path=db_path)


def test_sqlite_character_voice_id_round_trip(sqlite_db_path):
    """The exact bug from 2026-05-18: PATCH set voice_id in memory, save()
    didn't flush characters, server restart wiped it. After the fix,
    update_character writes the row inline."""
    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_character(CharacterAsset(char_id="c1", name="Silas", filename="x.png"))
    ch = s1.get_character("c1")
    ch.voice_id = "voice_abc123"
    ch.voice_provider = "elevenlabs"
    s1.update_character(ch)

    # Throw the store away. Build a fresh one against the same file.
    s2 = _fresh_sqlite(sqlite_db_path)
    loaded = s2.get_character("c1")
    assert loaded is not None
    assert loaded.voice_id == "voice_abc123"
    assert loaded.voice_provider == "elevenlabs"


def test_sqlite_character_voice_id_cleared_to_none(sqlite_db_path):
    """Empty-string clear (user picks '— none —' in the dropdown) → NULL in DB."""
    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_character(CharacterAsset(
        char_id="c1", name="Silas", filename="x.png",
        voice_id="voice_abc", voice_provider="elevenlabs",
    ))
    ch = s1.get_character("c1")
    ch.voice_id = None
    ch.voice_provider = None
    s1.update_character(ch)

    s2 = _fresh_sqlite(sqlite_db_path)
    loaded = s2.get_character("c1")
    assert loaded.voice_id is None
    assert loaded.voice_provider is None


def test_sqlite_character_rename_round_trip(sqlite_db_path):
    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_character(CharacterAsset(char_id="c1", name="Old", filename="x.png"))
    ch = s1.get_character("c1")
    ch.name = "New"
    s1.update_character(ch)

    s2 = _fresh_sqlite(sqlite_db_path)
    assert s2.get_character("c1").name == "New"


def test_sqlite_update_character_does_not_need_save(sqlite_db_path):
    """update_character is the right mutator — no extra save() call should be
    needed for the change to persist (the bug yesterday was that callers had
    to remember to call save() AND save() didn't actually flush characters)."""
    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_character(CharacterAsset(char_id="c1", name="A", filename="x.png"))
    ch = s1.get_character("c1")
    ch.voice_id = "v1"
    s1.update_character(ch)
    # NOTE: no s1.save() call.

    s2 = _fresh_sqlite(sqlite_db_path)
    assert s2.get_character("c1").voice_id == "v1"


def test_sqlite_remove_character_deletes_row(sqlite_db_path):
    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_character(CharacterAsset(char_id="c1", name="A", filename="x.png"))
    s1.add_character(CharacterAsset(char_id="c2", name="B", filename="y.png"))
    s1.remove_character("c1")

    s2 = _fresh_sqlite(sqlite_db_path)
    assert s2.get_character("c1") is None
    assert s2.get_character("c2") is not None  # untouched


def test_sqlite_project_character_pruning_round_trip(sqlite_db_path):
    """When DELETE /api/characters runs, it prunes project.character_ids in
    memory. The fix yesterday made that update_project()-able. This test
    ensures the pruned list survives a restart."""
    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_character(CharacterAsset(char_id="c1", name="A", filename="a.png"))
    s1.add_character(CharacterAsset(char_id="c2", name="B", filename="b.png"))
    s1.add_project(ProjectAsset(
        project_id="p1", name="My project",
        character_ids=["c1", "c2"],
    ))

    # Simulate DELETE /api/characters/c1
    s1.remove_character("c1")
    proj = s1.get_project("p1")
    proj.character_ids = [c for c in proj.character_ids if c != "c1"]
    s1.update_project(proj)

    s2 = _fresh_sqlite(sqlite_db_path)
    assert s2.get_project("p1").character_ids == ["c2"]


def test_sqlite_job_round_trip(sqlite_db_path):
    """Job + nested JobCharacter survive a store restart."""
    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_scene(SceneAsset(scene_id="sc1", filename="s.png", original_name="s.png"))
    job = Job(
        job_id="j1",
        scene_id="sc1",
        scene_image_path="/tmp/s.png",
        scene_ids=["sc1"],
        scene_image_paths=["/tmp/s.png"],
        characters={
            "ch_1": JobCharacter(
                char_id="ch_1", name="Alex",
                source_image_path="/tmp/a.png",
            )
        },
        movement_prompt="walk forward",
    )
    s1.add_job(job)

    s2 = _fresh_sqlite(sqlite_db_path)
    loaded = s2.get_job("j1")
    assert loaded is not None
    assert loaded.scene_id == "sc1"
    assert loaded.movement_prompt == "walk forward"
    assert "ch_1" in loaded.characters
    assert loaded.characters["ch_1"].name == "Alex"


def test_sqlite_load_app_state_after_restart(sqlite_db_path):
    """Multi-entity sanity check: scenes + characters + projects + jobs all
    co-exist in the same store and reload together."""
    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_scene(SceneAsset(scene_id="sc1", filename="s.png", original_name="s.png"))
    s1.add_character(CharacterAsset(char_id="c1", name="A", filename="x.png", voice_id="v"))
    s1.add_project(ProjectAsset(project_id="p1", name="P", character_ids=["c1"]))
    s1.add_job(Job(
        job_id="j1", scene_id="sc1", scene_image_path="/tmp/x.png",
        project_id="p1",
    ))

    s2 = _fresh_sqlite(sqlite_db_path)
    assert s2.get_scene("sc1") is not None
    assert s2.get_character("c1").voice_id == "v"
    assert s2.get_project("p1").character_ids == ["c1"]
    assert s2.get_job("j1").project_id == "p1"


# --- JsonStateStore parallel coverage --------------------------------------------------


@pytest.fixture
def json_state_path(tmp_path: Path) -> Path:
    return tmp_path / "state.json"


def test_json_character_voice_id_round_trip(json_state_path):
    s1 = JsonStateStore(path=json_state_path)
    s1.add_character(CharacterAsset(char_id="c1", name="Silas", filename="x.png"))
    ch = s1.get_character("c1")
    ch.voice_id = "voice_xyz"
    ch.voice_provider = "elevenlabs"
    s1.update_character(ch)

    s2 = JsonStateStore(path=json_state_path)
    loaded = s2.get_character("c1")
    assert loaded.voice_id == "voice_xyz"
    assert loaded.voice_provider == "elevenlabs"


def test_json_project_pruning_round_trip(json_state_path):
    s1 = JsonStateStore(path=json_state_path)
    s1.add_character(CharacterAsset(char_id="c1", name="A", filename="a.png"))
    s1.add_project(ProjectAsset(
        project_id="p1", name="P", character_ids=["c1"],
    ))
    s1.remove_character("c1")
    proj = s1.get_project("p1")
    proj.character_ids = []
    s1.update_project(proj)

    s2 = JsonStateStore(path=json_state_path)
    assert s2.get_project("p1").character_ids == []
