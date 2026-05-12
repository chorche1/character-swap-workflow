"""
SQLite backing store for AppState.

Stdlib-only (no SQLModel/SQLAlchemy dep) — schema is a flat 8 tables that
mirror models.py. Connections are opened per-call by `connect()` so the
StateStore stays thread-safe under uvicorn's worker threads.

Design notes:
- The application keeps `AppState` fully in memory (see state.py). This file's
  job is *persistence*: every CRUD method on `StateStore` writes a small set of
  rows here so we don't rewrite a multi-MB JSON file per progress event.
- `update_job` is the one expensive op: it DELETEs and re-INSERTs all child
  rows (job_characters, variants, videos) inside a transaction. Bounded by
  the size of that one job, which is small (5 chars × 4 variants + 4 videos).
- The DB file lives at `settings.state_db` (`state/state.sqlite3`).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from character_swap.config import settings
from character_swap.models import (
    AppState,
    CharacterAsset,
    GeneratedImage,
    Job,
    JobCharacter,
    ProjectAsset,
    SceneAsset,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS scenes (
    scene_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    original_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS characters (
    char_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_characters (
    project_id TEXT NOT NULL,
    char_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (project_id, char_id),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT,
    project_id TEXT,
    scene_id TEXT NOT NULL,
    scene_image_path TEXT NOT NULL,
    movement_prompt TEXT,
    images_per_character INTEGER NOT NULL DEFAULT 1,
    videos_per_character INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_project ON jobs(project_id);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);

CREATE TABLE IF NOT EXISTS job_characters (
    job_id TEXT NOT NULL,
    char_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    source_image_path TEXT NOT NULL,
    status TEXT NOT NULL,
    approved_variant_id TEXT,
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (job_id, char_id),
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS variants (
    variant_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    char_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    path TEXT NOT NULL,
    prompt TEXT NOT NULL,
    parent_variant_id TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_variants_jc ON variants(job_id, char_id);

CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    char_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    grok_job_id TEXT NOT NULL,
    status TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    completed_at TEXT,
    download_url TEXT,
    final_video_path TEXT,
    source_variant_id TEXT,
    error TEXT,
    FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_videos_jc ON videos(job_id, char_id);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_iso(s: str | None) -> datetime:
    if not s:
        return datetime.utcnow()
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.utcnow()


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or settings.state_db
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Use immediate transactions so concurrent writers serialize cleanly."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise


# --- row → model conversions ----------------------------------------------------------

def _scene_from_row(r: sqlite3.Row) -> SceneAsset:
    return SceneAsset(
        scene_id=r["scene_id"],
        filename=r["filename"],
        original_name=r["original_name"],
        created_at=_parse_iso(r["created_at"]),
    )


def _char_from_row(r: sqlite3.Row) -> CharacterAsset:
    return CharacterAsset(
        char_id=r["char_id"],
        filename=r["filename"],
        name=r["name"],
        created_at=_parse_iso(r["created_at"]),
    )


def _project_from_row(r: sqlite3.Row, character_ids: list[str]) -> ProjectAsset:
    return ProjectAsset(
        project_id=r["project_id"],
        name=r["name"],
        character_ids=character_ids,
        created_at=_parse_iso(r["created_at"]),
        updated_at=_parse_iso(r["updated_at"]),
    )


def _variant_from_row(r: sqlite3.Row) -> GeneratedImage:
    return GeneratedImage(
        variant_id=r["variant_id"],
        path=r["path"],
        prompt=r["prompt"],
        parent_variant_id=r["parent_variant_id"],
        status=VariantStatus(r["status"]),
        error=r["error"],
        created_at=_parse_iso(r["created_at"]),
    )


def _video_from_row(r: sqlite3.Row) -> VideoVariant:
    try:
        st = VideoStatus(r["status"])
    except ValueError:
        st = VideoStatus.PROCESSING
    return VideoVariant(
        video_id=r["video_id"],
        grok_job_id=r["grok_job_id"],
        status=st,
        submitted_at=_parse_iso(r["submitted_at"]),
        completed_at=_parse_iso(r["completed_at"]) if r["completed_at"] else None,
        download_url=r["download_url"],
        final_video_path=r["final_video_path"],
        source_variant_id=r["source_variant_id"],
        error=r["error"],
    )


def _jc_from_row(r: sqlite3.Row, images: list[GeneratedImage],
                 videos: list[VideoVariant]) -> JobCharacter:
    from character_swap.models import CharStatus
    return JobCharacter(
        char_id=r["char_id"],
        name=r["name"],
        source_image_path=r["source_image_path"],
        status=CharStatus(r["status"]),
        approved_variant_id=r["approved_variant_id"],
        error=r["error"],
        images=images,
        videos=videos,
        updated_at=_parse_iso(r["updated_at"]),
    )


# --- bulk load ------------------------------------------------------------------------

def load_app_state(conn: sqlite3.Connection) -> AppState:
    """Read every row and build an AppState. Cheap up to thousands of jobs."""
    state = AppState()

    for r in conn.execute("SELECT * FROM scenes"):
        sa = _scene_from_row(r)
        state.scenes[sa.scene_id] = sa

    for r in conn.execute("SELECT * FROM characters"):
        ca = _char_from_row(r)
        state.characters[ca.char_id] = ca

    project_chars: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT project_id, char_id FROM project_characters ORDER BY project_id, position"
    ):
        project_chars.setdefault(r["project_id"], []).append(r["char_id"])
    for r in conn.execute("SELECT * FROM projects"):
        pa = _project_from_row(r, project_chars.get(r["project_id"], []))
        state.projects[pa.project_id] = pa

    # Group variants/videos by (job, char) for assembling JobCharacter.
    var_by_jc: dict[tuple[str, str], list[GeneratedImage]] = {}
    for r in conn.execute(
        "SELECT * FROM variants ORDER BY job_id, char_id, position"
    ):
        var_by_jc.setdefault((r["job_id"], r["char_id"]), []).append(_variant_from_row(r))

    vid_by_jc: dict[tuple[str, str], list[VideoVariant]] = {}
    for r in conn.execute(
        "SELECT * FROM videos ORDER BY job_id, char_id, position"
    ):
        vid_by_jc.setdefault((r["job_id"], r["char_id"]), []).append(_video_from_row(r))

    jc_by_job: dict[str, dict[str, JobCharacter]] = {}
    for r in conn.execute(
        "SELECT * FROM job_characters ORDER BY job_id, position"
    ):
        jc = _jc_from_row(
            r,
            images=var_by_jc.get((r["job_id"], r["char_id"]), []),
            videos=vid_by_jc.get((r["job_id"], r["char_id"]), []),
        )
        jc_by_job.setdefault(r["job_id"], {})[r["char_id"]] = jc

    job_cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    has_compacted = "compacted" in job_cols
    for r in conn.execute("SELECT * FROM jobs ORDER BY created_at"):
        j = Job(
            job_id=r["job_id"],
            title=r["title"],
            project_id=r["project_id"],
            scene_id=r["scene_id"],
            scene_image_path=r["scene_image_path"],
            movement_prompt=r["movement_prompt"],
            images_per_character=r["images_per_character"],
            videos_per_character=r["videos_per_character"],
            compacted=bool(r["compacted"]) if has_compacted else False,
            characters=jc_by_job.get(r["job_id"], {}),
            created_at=_parse_iso(r["created_at"]),
            updated_at=_parse_iso(r["updated_at"]),
        )
        state.jobs[j.job_id] = j

    return state


# --- per-entity upserts ---------------------------------------------------------------

def upsert_scene(conn: sqlite3.Connection, s: SceneAsset) -> None:
    conn.execute(
        """INSERT INTO scenes (scene_id, filename, original_name, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(scene_id) DO UPDATE SET
             filename = excluded.filename,
             original_name = excluded.original_name""",
        (s.scene_id, s.filename, s.original_name, _iso(s.created_at)),
    )


def upsert_character(conn: sqlite3.Connection, c: CharacterAsset) -> None:
    conn.execute(
        """INSERT INTO characters (char_id, filename, name, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(char_id) DO UPDATE SET
             filename = excluded.filename,
             name = excluded.name""",
        (c.char_id, c.filename, c.name, _iso(c.created_at)),
    )


def delete_character(conn: sqlite3.Connection, char_id: str) -> None:
    conn.execute("DELETE FROM project_characters WHERE char_id = ?", (char_id,))
    conn.execute("DELETE FROM characters WHERE char_id = ?", (char_id,))


def upsert_project(conn: sqlite3.Connection, p: ProjectAsset) -> None:
    conn.execute(
        """INSERT INTO projects (project_id, name, created_at, updated_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(project_id) DO UPDATE SET
             name = excluded.name,
             updated_at = excluded.updated_at""",
        (p.project_id, p.name, _iso(p.created_at), _iso(p.updated_at)),
    )
    # Replace the project_characters mapping atomically.
    conn.execute("DELETE FROM project_characters WHERE project_id = ?", (p.project_id,))
    conn.executemany(
        "INSERT INTO project_characters (project_id, char_id, position) VALUES (?, ?, ?)",
        [(p.project_id, cid, i) for i, cid in enumerate(p.character_ids)],
    )


def delete_project(conn: sqlite3.Connection, project_id: str) -> list[str]:
    """FK ON DELETE CASCADE handles project_characters. Job deletion is handled
    by the caller (so disk cleanup is sequenced correctly)."""
    job_ids = [r["job_id"] for r in conn.execute(
        "SELECT job_id FROM jobs WHERE project_id = ?", (project_id,)
    )]
    for jid in job_ids:
        delete_job(conn, jid)
    conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))
    return job_ids


def upsert_job(conn: sqlite3.Connection, j: Job) -> None:
    conn.execute(
        """INSERT INTO jobs (job_id, title, project_id, scene_id, scene_image_path,
                             movement_prompt, images_per_character, videos_per_character,
                             compacted, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET
             title = excluded.title,
             project_id = excluded.project_id,
             scene_id = excluded.scene_id,
             scene_image_path = excluded.scene_image_path,
             movement_prompt = excluded.movement_prompt,
             images_per_character = excluded.images_per_character,
             videos_per_character = excluded.videos_per_character,
             compacted = excluded.compacted,
             updated_at = excluded.updated_at""",
        (
            j.job_id, j.title, j.project_id, j.scene_id, j.scene_image_path,
            j.movement_prompt, j.images_per_character, j.videos_per_character,
            1 if j.compacted else 0,
            _iso(j.created_at), _iso(j.updated_at),
        ),
    )
    # Reset children — simplest correctness model for an in-place job update.
    conn.execute("DELETE FROM job_characters WHERE job_id = ?", (j.job_id,))
    conn.execute("DELETE FROM variants WHERE job_id = ?", (j.job_id,))
    conn.execute("DELETE FROM videos WHERE job_id = ?", (j.job_id,))
    for char_pos, (cid, jc) in enumerate(j.characters.items()):
        conn.execute(
            """INSERT INTO job_characters
                 (job_id, char_id, position, name, source_image_path,
                  status, approved_variant_id, error, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                j.job_id, cid, char_pos, jc.name, jc.source_image_path,
                str(jc.status), jc.approved_variant_id, jc.error,
                _iso(jc.updated_at),
            ),
        )
        for i, v in enumerate(jc.images):
            conn.execute(
                """INSERT INTO variants
                    (variant_id, job_id, char_id, position, path, prompt,
                     parent_variant_id, status, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    v.variant_id, j.job_id, cid, i, v.path, v.prompt,
                    v.parent_variant_id, str(v.status), v.error,
                    _iso(v.created_at),
                ),
            )
        for i, vv in enumerate(jc.videos):
            conn.execute(
                """INSERT INTO videos
                    (video_id, job_id, char_id, position, grok_job_id,
                     status, submitted_at, completed_at, download_url,
                     final_video_path, source_variant_id, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    vv.video_id, j.job_id, cid, i, vv.grok_job_id,
                    str(vv.status), _iso(vv.submitted_at),
                    _iso(vv.completed_at), vv.download_url,
                    vv.final_video_path, vv.source_variant_id, vv.error,
                ),
            )


def delete_job(conn: sqlite3.Connection, job_id: str) -> None:
    # FK ON DELETE CASCADE handles children.
    conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))


def reset_all(conn: sqlite3.Connection) -> None:
    for table in ("videos", "variants", "job_characters", "jobs",
                  "project_characters", "projects", "characters", "scenes"):
        conn.execute(f"DELETE FROM {table}")
