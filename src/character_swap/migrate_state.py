"""
One-shot migration: state.json → state.sqlite3.

Idempotent: if state.json doesn't exist or has already been renamed to
state.json.migrated, this is a noop. Safe to re-run.

Usage:
    uv run character-swap migrate
or programmatically: `from character_swap.migrate_state import migrate; migrate()`.
"""
from __future__ import annotations

import json
from pathlib import Path

from character_swap import db
from character_swap.config import settings
from character_swap.models import AppState


def migrate(json_path: Path | None = None, db_path: Path | None = None) -> dict:
    """Returns a summary dict so the CLI can echo counts."""
    src = json_path or settings.state_file
    if not src.exists():
        return {"migrated": False, "reason": "no state.json found"}

    with src.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    state = AppState.model_validate(raw)

    conn = db.connect(db_path)
    db.ensure_schema(conn)
    # Guard against accidental re-migration with a non-empty DB.
    existing_jobs = conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
    if existing_jobs > 0:
        return {
            "migrated": False,
            "reason": f"state.sqlite3 already has {existing_jobs} job(s); refusing to overwrite",
        }

    with db.transaction(conn) as c:
        for s in state.scenes.values():
            db.upsert_scene(c, s)
        for ch in state.characters.values():
            db.upsert_character(c, ch)
        for p in state.projects.values():
            db.upsert_project(c, p)
        for j in state.jobs.values():
            db.upsert_job(c, j)
        for g in state.generations.values():
            db.upsert_generation(c, g)

    # Rename source so re-runs become noop and the user has a backup.
    src.replace(src.with_suffix(".json.migrated"))

    return {
        "migrated": True,
        "scenes": len(state.scenes),
        "characters": len(state.characters),
        "projects": len(state.projects),
        "jobs": len(state.jobs),
        "generations": len(state.generations),
    }


if __name__ == "__main__":
    result = migrate()
    print(json.dumps(result, indent=2))
