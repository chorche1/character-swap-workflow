from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path

from character_swap.config import settings
from character_swap.models import AppState, CharacterAsset, Job, SceneAsset


class StateStore:
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

    # --- scenes --------------------------------------------------------------
    def add_scene(self, scene: SceneAsset) -> None:
        self._state.scenes[scene.scene_id] = scene
        self.save()

    def get_scene(self, scene_id: str) -> SceneAsset | None:
        return self._state.scenes.get(scene_id)

    # --- characters ----------------------------------------------------------
    def add_character(self, character: CharacterAsset) -> None:
        self._state.characters[character.char_id] = character
        self.save()

    def remove_character(self, char_id: str) -> CharacterAsset | None:
        return self._state.characters.pop(char_id, None)

    def list_characters(self) -> list[CharacterAsset]:
        return list(self._state.characters.values())

    def get_character(self, char_id: str) -> CharacterAsset | None:
        return self._state.characters.get(char_id)

    # --- jobs ----------------------------------------------------------------
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

    def reset(self) -> None:
        self._state = AppState()
        self.save()


_store: StateStore | None = None


def store() -> StateStore:
    global _store
    if _store is None:
        _store = StateStore()
    return _store
