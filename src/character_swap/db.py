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
    CharacterImage,
    GeneratedImage,
    GenKind,
    GenStatus,
    Job,
    JobCharacter,
    MediaGeneration,
    ProjectAsset,
    SceneAsset,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)
# Historical name kept for stable JSON serialization across migrations
# (originally added for the reel feature, now used by all JSON-serialized
# columns like movement_prompts_json + approved_variant_ids_json).
import json as _reel_json


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
    primary_image_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character_images (
    image_id TEXT PRIMARY KEY,
    char_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    filename TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (char_id) REFERENCES characters(char_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_char_images_char ON character_images(char_id);

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
    prompt TEXT,
    image_model TEXT NOT NULL DEFAULT 'gpt-image',
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

CREATE TABLE IF NOT EXISTS generations (
    gen_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt TEXT NOT NULL,
    aspect_ratio TEXT,
    duration_secs INTEGER,
    avatar_id TEXT,
    voice_id TEXT,
    voice_provider TEXT,
    status TEXT NOT NULL,
    output_path TEXT,
    provider_job_id TEXT,
    cost_usd REAL,
    error TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_gens_kind ON generations(kind);
CREATE INDEX IF NOT EXISTS idx_gens_created ON generations(created_at);

CREATE TABLE IF NOT EXISTS gen_reference_paths (
    gen_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    path TEXT NOT NULL,
    PRIMARY KEY (gen_id, position),
    FOREIGN KEY (gen_id) REFERENCES generations(gen_id) ON DELETE CASCADE
);
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
    # Idempotent column additions for installs that came up under an older schema.
    job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "prompt" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN prompt TEXT")
    if "image_model" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN image_model TEXT NOT NULL DEFAULT 'gpt-image'")
    char_cols = {r["name"] for r in conn.execute("PRAGMA table_info(characters)")}
    if "primary_image_id" not in char_cols:
        conn.execute("ALTER TABLE characters ADD COLUMN primary_image_id TEXT")
    gen_cols = {r["name"] for r in conn.execute("PRAGMA table_info(generations)")}
    if "avatar_id" not in gen_cols:
        conn.execute("ALTER TABLE generations ADD COLUMN avatar_id TEXT")
    if "voice_id" not in gen_cols:
        conn.execute("ALTER TABLE generations ADD COLUMN voice_id TEXT")
    if "voice_provider" not in gen_cols:
        conn.execute("ALTER TABLE generations ADD COLUMN voice_provider TEXT")
    # Prompt-enrichment columns (added when prompt_enrich.py landed).
    if "enrich_prompt" not in gen_cols:
        conn.execute("ALTER TABLE generations ADD COLUMN enrich_prompt INTEGER NOT NULL DEFAULT 0")
    if "enriched_prompt" not in gen_cols:
        conn.execute("ALTER TABLE generations ADD COLUMN enriched_prompt TEXT")
    job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "enrich_prompt" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN enrich_prompt INTEGER NOT NULL DEFAULT 0")
    if "enriched_image_prompt" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN enriched_image_prompt TEXT")
    if "enriched_movement_prompt" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN enriched_movement_prompt TEXT")
    # Per-job video provider — picker added to Step 4 UI so users can swap
    # Grok for Kling / Veo / Runway / Luma / Pika / etc. without leaving the
    # swap flow. Old jobs read NULL → defaults to 'grok-imagine' in code.
    if "video_model" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN video_model TEXT NOT NULL DEFAULT 'grok-imagine'")
    # Multi-variant approval: JSON-encoded list of variant_ids the user
    # picked. With N scenes a character can have N approvals (one per scene)
    # so all of them animate in parallel in Step 4. Old jobs read NULL →
    # the loader falls back to wrapping `approved_variant_id` in a list.
    jc_cols = {r["name"] for r in conn.execute("PRAGMA table_info(job_characters)")}
    if "approved_variant_ids_json" not in jc_cols:
        conn.execute("ALTER TABLE job_characters ADD COLUMN approved_variant_ids_json TEXT")
    # Per-scene movement prompts (scene_id → prompt) for Step 4. Old jobs
    # only have the singular `movement_prompt` field — the loader broadcasts
    # it across every scene so the dict is always populated for callers.
    job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "movement_prompts_json" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN movement_prompts_json TEXT")
    if "enriched_movement_prompts_json" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN enriched_movement_prompts_json TEXT")
    # AI Director — opt-in Claude/Opus agent that writes tailored per-variant
    # prompts. `use_director` flips the runner into the director path;
    # `director_prompts_json` caches the SwapDirectorPlan so retries don't
    # re-bill the Anthropic API.
    if "use_director" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN use_director INTEGER NOT NULL DEFAULT 0")
    if "director_prompts_json" not in job_cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN director_prompts_json TEXT")
    # Same fields on generations (freeform Image / Video tabs).
    gen_cols = {r["name"] for r in conn.execute("PRAGMA table_info(generations)")}
    if "use_director" not in gen_cols:
        conn.execute("ALTER TABLE generations ADD COLUMN use_director INTEGER NOT NULL DEFAULT 0")
    if "director_prompt" not in gen_cols:
        conn.execute("ALTER TABLE generations ADD COLUMN director_prompt TEXT")
    # Per-character preset ElevenLabs voice. Auto-applied when generating a
    # video for the character via the Editor tab's optional "Character"
    # dropdown OR the Swap-flow Step 6 compile feature.
    char_cols = {r["name"] for r in conn.execute("PRAGMA table_info(characters)")}
    if "voice_id" not in char_cols:
        conn.execute("ALTER TABLE characters ADD COLUMN voice_id TEXT")
    if "voice_provider" not in char_cols:
        conn.execute("ALTER TABLE characters ADD COLUMN voice_provider TEXT")
    # Step 6 (Compile) per-character output. Concatenated + editor-processed
    # final video for each character. Status null = never compiled.
    jc_cols2 = {r["name"] for r in conn.execute("PRAGMA table_info(job_characters)")}
    if "compiled_video_path" not in jc_cols2:
        conn.execute("ALTER TABLE job_characters ADD COLUMN compiled_video_path TEXT")
    if "compile_edit_id" not in jc_cols2:
        conn.execute("ALTER TABLE job_characters ADD COLUMN compile_edit_id TEXT")
    if "compile_status" not in jc_cols2:
        conn.execute("ALTER TABLE job_characters ADD COLUMN compile_status TEXT")
    if "compile_error" not in jc_cols2:
        conn.execute("ALTER TABLE job_characters ADD COLUMN compile_error TEXT")
    # Per-video movement-prompt override. Set when the user regenerates a
    # specific DONE video with a tweaked prompt (Step 5 regen button).
    video_cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    if "movement_prompt_override" not in video_cols:
        conn.execute("ALTER TABLE videos ADD COLUMN movement_prompt_override TEXT")
    # One-time cleanup migration: the Reel feature has been removed. Drop
    # its tables if a prior schema left them behind. No-op on fresh DBs.
    conn.execute("DROP TABLE IF EXISTS reel_jobs")
    conn.execute("DROP TABLE IF EXISTS reel_presets")


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


def _char_from_row(r: sqlite3.Row, images: list[CharacterImage]) -> CharacterAsset:
    keys = r.keys()
    return CharacterAsset(
        char_id=r["char_id"],
        filename=r["filename"],
        name=r["name"],
        images=images,
        primary_image_id=r["primary_image_id"] if "primary_image_id" in keys else None,
        voice_id=r["voice_id"] if "voice_id" in keys else None,
        voice_provider=r["voice_provider"] if "voice_provider" in keys else None,
        created_at=_parse_iso(r["created_at"]),
    )


def _char_image_from_row(r: sqlite3.Row) -> CharacterImage:
    return CharacterImage(
        image_id=r["image_id"],
        filename=r["filename"],
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
    keys = r.keys()
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
        movement_prompt_override=(
            r["movement_prompt_override"]
            if "movement_prompt_override" in keys else None
        ),
    )


def _jc_from_row(r: sqlite3.Row, images: list[GeneratedImage],
                 videos: list[VideoVariant]) -> JobCharacter:
    from character_swap.models import CharStatus
    keys = r.keys()
    # Multi-variant approval: prefer the new JSON column. Falls back to the
    # legacy single `approved_variant_id` so jobs created before the
    # migration ran are still readable.
    approved_ids: list[str] = []
    if "approved_variant_ids_json" in keys and r["approved_variant_ids_json"]:
        try:
            parsed = _reel_json.loads(r["approved_variant_ids_json"])
            if isinstance(parsed, list):
                approved_ids = [str(x) for x in parsed if x]
        except (ValueError, TypeError):
            approved_ids = []
    if not approved_ids and r["approved_variant_id"]:
        approved_ids = [r["approved_variant_id"]]
    return JobCharacter(
        char_id=r["char_id"],
        name=r["name"],
        source_image_path=r["source_image_path"],
        status=CharStatus(r["status"]),
        approved_variant_id=approved_ids[0] if approved_ids else None,
        approved_variant_ids=approved_ids,
        error=r["error"],
        images=images,
        videos=videos,
        compiled_video_path=r["compiled_video_path"] if "compiled_video_path" in keys else None,
        compile_edit_id=r["compile_edit_id"] if "compile_edit_id" in keys else None,
        compile_status=r["compile_status"] if "compile_status" in keys else None,
        compile_error=r["compile_error"] if "compile_error" in keys else None,
        updated_at=_parse_iso(r["updated_at"]),
    )


# --- bulk load ------------------------------------------------------------------------

def load_app_state(conn: sqlite3.Connection) -> AppState:
    """Read every row and build an AppState. Cheap up to thousands of jobs."""
    state = AppState()

    for r in conn.execute("SELECT * FROM scenes"):
        sa = _scene_from_row(r)
        state.scenes[sa.scene_id] = sa

    imgs_by_char: dict[str, list[CharacterImage]] = {}
    for r in conn.execute(
        "SELECT * FROM character_images ORDER BY char_id, position"
    ):
        imgs_by_char.setdefault(r["char_id"], []).append(_char_image_from_row(r))
    for r in conn.execute("SELECT * FROM characters"):
        ca = _char_from_row(r, imgs_by_char.get(r["char_id"], []))
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
    has_prompt = "prompt" in job_cols
    has_image_model = "image_model" in job_cols
    has_video_model = "video_model" in job_cols
    has_enrich = "enrich_prompt" in job_cols
    has_movement_prompts = "movement_prompts_json" in job_cols
    has_director = "use_director" in job_cols

    def _parse_prompts_dict(raw: str | None) -> dict[str, str]:
        if not raw:
            return {}
        try:
            parsed = _reel_json.loads(raw)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items() if v}
        except (ValueError, TypeError):
            pass
        return {}

    for r in conn.execute("SELECT * FROM jobs ORDER BY created_at"):
        movement_prompts = (
            _parse_prompts_dict(r["movement_prompts_json"])
            if has_movement_prompts else {}
        )
        enriched_movement_prompts = (
            _parse_prompts_dict(r["enriched_movement_prompts_json"])
            if has_movement_prompts else {}
        )
        j = Job(
            job_id=r["job_id"],
            title=r["title"],
            project_id=r["project_id"],
            scene_id=r["scene_id"],
            scene_image_path=r["scene_image_path"],
            prompt=r["prompt"] if has_prompt else None,
            image_model=r["image_model"] if has_image_model else "gpt-image",
            video_model=(r["video_model"] if has_video_model else None) or "grok-imagine",
            movement_prompt=r["movement_prompt"],
            movement_prompts=movement_prompts,
            images_per_character=r["images_per_character"],
            videos_per_character=r["videos_per_character"],
            compacted=bool(r["compacted"]) if has_compacted else False,
            enrich_prompt=bool(r["enrich_prompt"]) if has_enrich else False,
            enriched_image_prompt=r["enriched_image_prompt"] if has_enrich else None,
            enriched_movement_prompt=r["enriched_movement_prompt"] if has_enrich else None,
            enriched_movement_prompts=enriched_movement_prompts,
            use_director=bool(r["use_director"]) if has_director else False,
            director_prompts_json=r["director_prompts_json"] if has_director else None,
            characters=jc_by_job.get(r["job_id"], {}),
            created_at=_parse_iso(r["created_at"]),
            updated_at=_parse_iso(r["updated_at"]),
        )
        state.jobs[j.job_id] = j

    refs_by_gen: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT gen_id, path FROM gen_reference_paths ORDER BY gen_id, position"
    ):
        refs_by_gen.setdefault(r["gen_id"], []).append(r["path"])
    for r in conn.execute("SELECT * FROM generations ORDER BY created_at"):
        g = _gen_from_row(r, refs_by_gen.get(r["gen_id"], []))
        state.generations[g.gen_id] = g

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
        """INSERT INTO characters (char_id, filename, name, primary_image_id,
                                   voice_id, voice_provider, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(char_id) DO UPDATE SET
             filename = excluded.filename,
             name = excluded.name,
             primary_image_id = excluded.primary_image_id,
             voice_id = excluded.voice_id,
             voice_provider = excluded.voice_provider""",
        (c.char_id, c.filename, c.name, c.primary_image_id,
         c.voice_id, c.voice_provider, _iso(c.created_at)),
    )
    # Replace the image rows atomically.
    conn.execute("DELETE FROM character_images WHERE char_id = ?", (c.char_id,))
    conn.executemany(
        """INSERT INTO character_images
             (image_id, char_id, position, filename, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (img.image_id, c.char_id, i, img.filename, _iso(img.created_at))
            for i, img in enumerate(c.images)
        ],
    )


def delete_character(conn: sqlite3.Connection, char_id: str) -> None:
    conn.execute("DELETE FROM project_characters WHERE char_id = ?", (char_id,))
    # character_images rows cascade via FK.
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
                             prompt, image_model, video_model,
                             movement_prompt, movement_prompts_json,
                             images_per_character, videos_per_character,
                             compacted, enrich_prompt, enriched_image_prompt,
                             enriched_movement_prompt,
                             enriched_movement_prompts_json,
                             use_director, director_prompts_json,
                             created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET
             title = excluded.title,
             project_id = excluded.project_id,
             scene_id = excluded.scene_id,
             scene_image_path = excluded.scene_image_path,
             prompt = excluded.prompt,
             image_model = excluded.image_model,
             video_model = excluded.video_model,
             movement_prompt = excluded.movement_prompt,
             movement_prompts_json = excluded.movement_prompts_json,
             images_per_character = excluded.images_per_character,
             videos_per_character = excluded.videos_per_character,
             compacted = excluded.compacted,
             enrich_prompt = excluded.enrich_prompt,
             enriched_image_prompt = excluded.enriched_image_prompt,
             enriched_movement_prompt = excluded.enriched_movement_prompt,
             enriched_movement_prompts_json = excluded.enriched_movement_prompts_json,
             use_director = excluded.use_director,
             director_prompts_json = excluded.director_prompts_json,
             updated_at = excluded.updated_at""",
        (
            j.job_id, j.title, j.project_id, j.scene_id, j.scene_image_path,
            j.prompt, j.image_model, j.video_model,
            j.movement_prompt,
            _reel_json.dumps(dict(j.movement_prompts or {})),
            j.images_per_character, j.videos_per_character,
            1 if j.compacted else 0,
            1 if j.enrich_prompt else 0,
            j.enriched_image_prompt, j.enriched_movement_prompt,
            _reel_json.dumps(dict(j.enriched_movement_prompts or {})),
            1 if j.use_director else 0,
            j.director_prompts_json,
            _iso(j.created_at), _iso(j.updated_at),
        ),
    )
    # Reset children — simplest correctness model for an in-place job update.
    conn.execute("DELETE FROM job_characters WHERE job_id = ?", (j.job_id,))
    conn.execute("DELETE FROM variants WHERE job_id = ?", (j.job_id,))
    conn.execute("DELETE FROM videos WHERE job_id = ?", (j.job_id,))
    for char_pos, (cid, jc) in enumerate(j.characters.items()):
        # Keep the legacy `approved_variant_id` column in sync with the
        # first entry of the list, so older queries / migrations still see
        # a sensible value. Source of truth is `approved_variant_ids_json`.
        approved_ids = list(jc.approved_variant_ids or [])
        if not approved_ids and jc.approved_variant_id:
            approved_ids = [jc.approved_variant_id]
        legacy_single = approved_ids[0] if approved_ids else None
        conn.execute(
            """INSERT INTO job_characters
                 (job_id, char_id, position, name, source_image_path,
                  status, approved_variant_id, approved_variant_ids_json,
                  error, compiled_video_path, compile_edit_id,
                  compile_status, compile_error, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                j.job_id, cid, char_pos, jc.name, jc.source_image_path,
                str(jc.status), legacy_single,
                _reel_json.dumps(approved_ids),
                jc.error,
                jc.compiled_video_path, jc.compile_edit_id,
                jc.compile_status, jc.compile_error,
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
                     final_video_path, source_variant_id, error,
                     movement_prompt_override)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    vv.video_id, j.job_id, cid, i, vv.grok_job_id,
                    str(vv.status), _iso(vv.submitted_at),
                    _iso(vv.completed_at), vv.download_url,
                    vv.final_video_path, vv.source_variant_id, vv.error,
                    vv.movement_prompt_override,
                ),
            )


def delete_job(conn: sqlite3.Connection, job_id: str) -> None:
    # FK ON DELETE CASCADE handles children.
    conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))


def upsert_generation(conn: sqlite3.Connection, g: MediaGeneration) -> None:
    conn.execute(
        """INSERT INTO generations (gen_id, kind, model, prompt, aspect_ratio,
                                    duration_secs, avatar_id, voice_id, voice_provider,
                                    enrich_prompt, enriched_prompt,
                                    use_director, director_prompt,
                                    status, output_path,
                                    provider_job_id, cost_usd, error,
                                    created_at, completed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(gen_id) DO UPDATE SET
             kind = excluded.kind,
             model = excluded.model,
             prompt = excluded.prompt,
             aspect_ratio = excluded.aspect_ratio,
             duration_secs = excluded.duration_secs,
             avatar_id = excluded.avatar_id,
             voice_id = excluded.voice_id,
             voice_provider = excluded.voice_provider,
             enrich_prompt = excluded.enrich_prompt,
             enriched_prompt = excluded.enriched_prompt,
             use_director = excluded.use_director,
             director_prompt = excluded.director_prompt,
             status = excluded.status,
             output_path = excluded.output_path,
             provider_job_id = excluded.provider_job_id,
             cost_usd = excluded.cost_usd,
             error = excluded.error,
             completed_at = excluded.completed_at""",
        (
            g.gen_id, str(g.kind), g.model, g.prompt, g.aspect_ratio,
            g.duration_secs, g.avatar_id, g.voice_id, g.voice_provider,
            1 if g.enrich_prompt else 0, g.enriched_prompt,
            1 if g.use_director else 0, g.director_prompt,
            str(g.status), g.output_path, g.provider_job_id,
            g.cost_usd, g.error,
            _iso(g.created_at), _iso(g.completed_at),
        ),
    )
    conn.execute("DELETE FROM gen_reference_paths WHERE gen_id = ?", (g.gen_id,))
    conn.executemany(
        "INSERT INTO gen_reference_paths (gen_id, position, path) VALUES (?, ?, ?)",
        [(g.gen_id, i, p) for i, p in enumerate(g.reference_paths)],
    )


def delete_generation(conn: sqlite3.Connection, gen_id: str) -> None:
    conn.execute("DELETE FROM generations WHERE gen_id = ?", (gen_id,))


def _gen_from_row(r: sqlite3.Row, ref_paths: list[str]) -> MediaGeneration:
    keys = r.keys()
    return MediaGeneration(
        gen_id=r["gen_id"],
        kind=GenKind(r["kind"]),
        model=r["model"],
        prompt=r["prompt"],
        reference_paths=ref_paths,
        aspect_ratio=r["aspect_ratio"],
        duration_secs=r["duration_secs"],
        avatar_id=r["avatar_id"] if "avatar_id" in keys else None,
        voice_id=r["voice_id"] if "voice_id" in keys else None,
        voice_provider=r["voice_provider"] if "voice_provider" in keys else None,
        enrich_prompt=bool(r["enrich_prompt"]) if "enrich_prompt" in keys else False,
        enriched_prompt=r["enriched_prompt"] if "enriched_prompt" in keys else None,
        use_director=bool(r["use_director"]) if "use_director" in keys else False,
        director_prompt=r["director_prompt"] if "director_prompt" in keys else None,
        status=GenStatus(r["status"]),
        output_path=r["output_path"],
        provider_job_id=r["provider_job_id"],
        cost_usd=r["cost_usd"],
        error=r["error"],
        created_at=_parse_iso(r["created_at"]),
        completed_at=_parse_iso(r["completed_at"]) if r["completed_at"] else None,
    )


def reset_all(conn: sqlite3.Connection) -> None:
    for table in ("gen_reference_paths", "generations",
                  "videos", "variants", "job_characters", "jobs",
                  "project_characters", "projects",
                  "character_images", "characters", "scenes"):
        conn.execute(f"DELETE FROM {table}")


