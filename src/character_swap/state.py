"""
State store. Two backends behind one API:

- JsonStateStore  — original. Rewrites state.json on every save().
- SqliteStateStore — opt-in via USE_SQLITE_STATE=1. Keeps AppState in memory
  for fast reads but persists per-row to state/state.sqlite3.

The public surface (`store()`, `store().state`, the add_/get_/list_/update_/
delete_ methods) is identical across both — so callers in api.py / runner.py /
cli.py don't change.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from character_swap import db
from character_swap.config import settings
from character_swap.models import AppState, CharacterAsset, Job, ProjectAsset, SceneAsset


# --- JSON backend (original) ---------------------------------------------------------

class JsonStateStore:
    def __init__(self, path: Path | None = None):
        self.path = path or settings.state_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._state: AppState = self._load()

    def _load(self) -> AppState:
        if not self.path.exists():
            return AppState()
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return AppState.model_validate(data)
        except (json.JSONDecodeError, ValueError):
            backup = self.path.with_suffix(".json.corrupt")
            self.path.replace(backup)
            return AppState()

    @property
    def state(self) -> AppState:
        return self._state

    def save(self) -> None:
        with self._lock:
            self._state.last_updated = datetime.utcnow()
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._state.model_dump(mode="json"), f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)

    # scenes
    def add_scene(self, scene: SceneAsset) -> None:
        self._state.scenes[scene.scene_id] = scene
        self.save()

    def get_scene(self, scene_id: str) -> SceneAsset | None:
        return self._state.scenes.get(scene_id)

    # characters
    def add_character(self, character: CharacterAsset) -> None:
        self._state.characters[character.char_id] = character
        self.save()

    def remove_character(self, char_id: str) -> CharacterAsset | None:
        return self._state.characters.pop(char_id, None)

    def list_characters(self) -> list[CharacterAsset]:
        return list(self._state.characters.values())

    def get_character(self, char_id: str) -> CharacterAsset | None:
        return self._state.characters.get(char_id)

    # jobs
    def add_job(self, job: Job) -> None:
        self._state.jobs[job.job_id] = job
        self.save()

    def get_job(self, job_id: str) -> Job | None:
        return self._state.jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        return list(self._state.jobs.values())

    def update_job(self, job: Job) -> None:
        job.updated_at = datetime.utcnow()
        self._state.jobs[job.job_id] = job
        self.save()

    # projects
    def add_project(self, project: ProjectAsset) -> None:
        self._state.projects[project.project_id] = project
        self.save()

    def get_project(self, project_id: str) -> ProjectAsset | None:
        return self._state.projects.get(project_id)

    def list_projects(self) -> list[ProjectAsset]:
        return list(self._state.projects.values())

    def update_project(self, project: ProjectAsset) -> None:
        project.updated_at = datetime.utcnow()
        self._state.projects[project.project_id] = project
        self.save()

    def delete_project(self, project_id: str) -> list[str]:
        removed: list[str] = [
            jid for jid, j in self._state.jobs.items() if j.project_id == project_id
        ]
        for jid in removed:
            self._state.jobs.pop(jid, None)
        self._state.projects.pop(project_id, None)
        self.save()
        return removed

    def reset(self) -> None:
        self._state = AppState()
        self.save()


# --- SQLite backend (opt-in) ---------------------------------------------------------

class SqliteStateStore:
    def __init__(self, db_path: Path | None = None):
        self._conn = db.connect(db_path)
        db.ensure_schema(self._conn)
        self._lock = threading.Lock()
        self._state: AppState = db.load_app_state(self._conn)

    @property
    def state(self) -> AppState:
        return self._state

    def save(self) -> None:
        # No-op: each mutator below already writes its own row(s). Kept so
        # rare callers that explicitly call `save()` (e.g. character rename
        # propagation in api.py) re-flush every job that may have changed.
        with self._lock, db.transaction(self._conn) as conn:
            for j in self._state.jobs.values():
                db.upsert_job(conn, j)
            self._state.last_updated = datetime.utcnow()

    # scenes
    def add_scene(self, scene: SceneAsset) -> None:
        self._state.scenes[scene.scene_id] = scene
        with self._lock, db.transaction(self._conn) as conn:
            db.upsert_scene(conn, scene)

    def get_scene(self, scene_id: str) -> SceneAsset | None:
        return self._state.scenes.get(scene_id)

    # characters
    def add_character(self, character: CharacterAsset) -> None:
        self._state.characters[character.char_id] = character
        with self._lock, db.transaction(self._conn) as conn:
            db.upsert_character(conn, character)

    def remove_character(self, char_id: str) -> CharacterAsset | None:
        out = self._state.characters.pop(char_id, None)
        if out is not None:
            with self._lock, db.transaction(self._conn) as conn:
                db.delete_character(conn, char_id)
        return out

    def list_characters(self) -> list[CharacterAsset]:
        return list(self._state.characters.values())

    def get_character(self, char_id: str) -> CharacterAsset | None:
        return self._state.characters.get(char_id)

    # jobs
    def add_job(self, job: Job) -> None:
        self._state.jobs[job.job_id] = job
        with self._lock, db.transaction(self._conn) as conn:
            db.upsert_job(conn, job)

    def get_job(self, job_id: str) -> Job | None:
        return self._state.jobs.get(job_id)

    def list_jobs(self) -> list[Job]:
        return list(self._state.jobs.values())

    def update_job(self, job: Job) -> None:
        job.updated_at = datetime.utcnow()
        self._state.jobs[job.job_id] = job
        with self._lock, db.transaction(self._conn) as conn:
            db.upsert_job(conn, job)

    # projects
    def add_project(self, project: ProjectAsset) -> None:
        self._state.projects[project.project_id] = project
        with self._lock, db.transaction(self._conn) as conn:
            db.upsert_project(conn, project)

    def get_project(self, project_id: str) -> ProjectAsset | None:
        return self._state.projects.get(project_id)

    def list_projects(self) -> list[ProjectAsset]:
        return list(self._state.projects.values())

    def update_project(self, project: ProjectAsset) -> None:
        project.updated_at = datetime.utcnow()
        self._state.projects[project.project_id] = project
        with self._lock, db.transaction(self._conn) as conn:
            db.upsert_project(conn, project)

    def delete_project(self, project_id: str) -> list[str]:
        removed = [
            jid for jid, j in self._state.jobs.items() if j.project_id == project_id
        ]
        for jid in removed:
            self._state.jobs.pop(jid, None)
        self._state.projects.pop(project_id, None)
        with self._lock, db.transaction(self._conn) as conn:
            db.delete_project(conn, project_id)
        return removed

    def reset(self) -> None:
        self._state = AppState()
        with self._lock, db.transaction(self._conn) as conn:
            db.reset_all(conn)


# Type alias kept for any external code that imported StateStore directly.
StateStore = JsonStateStore | SqliteStateStore  # type: ignore[valid-type]


_store: JsonStateStore | SqliteStateStore | None = None


def store() -> JsonStateStore | SqliteStateStore:
    global _store
    if _store is None:
        if settings.use_sqlite_state:
            _store = SqliteStateStore()
        else:
            _store = JsonStateStore()
    return _store
