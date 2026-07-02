"""Deleting an entity mid-generation must WIN over stale runner writes
(2026-07-03 re-audit).

Every update_* method in both backends was an unconditional upsert: a runner
holding a stale Job across awaits RESURRECTED a job the user deleted from the
sidebar (JSON backend / SQLite full update_job path — the job reappeared in
the sidebar with 404 thumbnails and survived restarts), or crashed the
generation task with a sqlite3.IntegrityError (the SQLite granular fast paths
INSERT job_characters/variants/videos rows whose job_id FK references the
deleted jobs row, PRAGMA foreign_keys=ON). Now: update_job /
update_job_character / update_variant / update_video / update_generation
log-and-skip when the entity no longer exists — deletion wins, loudly.

Counterfactuals: the guards key on EXISTENCE only — every legitimate
add-then-update flow (including appending brand-new variants to a live job
via the fast path) still persists; those are locked by
test_state_persistence.py and re-checked here after a skipped write.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    MediaGeneration,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)
from character_swap.state import JsonStateStore, SqliteStateStore


@pytest.fixture
def sqlite_db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.sqlite3"


def _job() -> Job:
    images = [GeneratedImage(variant_id="v0", path="/v0.png", prompt="p",
                             scene_id="s1", status=VariantStatus.GENERATING)]
    videos = [VideoVariant(video_id="vd0", grok_job_id="g0",
                           status=VideoStatus.PENDING)]
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/src.png",
                      status=CharStatus.GENERATING, images=images,
                      videos=videos)
    return Job(job_id="j_del", title="t", scene_id="s1",
               scene_image_path="/scene.png", characters={"cA": jc})


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING]


# --- JSON backend ----------------------------------------------------------

def test_json_update_job_does_not_resurrect_deleted_job(tmp_path, caplog):
    s1 = JsonStateStore(path=tmp_path / "state.json")
    job = _job()
    s1.add_job(job)
    assert s1.remove_job("j_del") is not None

    with caplog.at_level(logging.WARNING, logger="character_swap.state"):
        s1.update_job(job)                       # stale runner write

    assert s1.get_job("j_del") is None           # no in-memory zombie
    # Not silently dropped — the skip is logged.
    assert any("j_del" in m for m in _warnings(caplog))
    # Not written back to disk either: a fresh store sees no job.
    s2 = JsonStateStore(path=tmp_path / "state.json")
    assert s2.get_job("j_del") is None


def test_json_granular_fast_paths_do_not_resurrect(tmp_path):
    """update_variant / update_job_character / update_video delegate to
    update_job on the JSON backend — all three must inherit the guard."""
    s1 = JsonStateStore(path=tmp_path / "state.json")
    job = _job()
    s1.add_job(job)
    s1.remove_job("j_del")

    jc = job.characters["cA"]
    s1.update_variant(job, jc, jc.images[0])
    s1.update_job_character(job, jc)
    s1.update_video(job, jc, jc.videos[0])

    assert s1.get_job("j_del") is None
    s2 = JsonStateStore(path=tmp_path / "state.json")
    assert s2.get_job("j_del") is None


def test_json_update_generation_does_not_resurrect(tmp_path):
    s1 = JsonStateStore(path=tmp_path / "state.json")
    gen = MediaGeneration(gen_id="g_del", kind="image", model="gpt-image-2",
                          prompt="p")
    s1.add_generation(gen)
    s1.delete_generation("g_del")

    s1.update_generation(gen)                    # stale runner write

    assert s1.get_generation("g_del") is None
    s2 = JsonStateStore(path=tmp_path / "state.json")
    assert s2.get_generation("g_del") is None


# --- SQLite backend --------------------------------------------------------

def test_sqlite_update_variant_after_delete_no_integrity_error(sqlite_db_path):
    """The exact crash: the granular fast path INSERTed a job_characters row
    with a dangling job_id FK → IntegrityError killed the generation task and
    the pre-transaction in-memory re-add left a zombie job in the sidebar."""
    s1 = SqliteStateStore(db_path=sqlite_db_path)
    job = _job()
    s1.add_job(job)
    assert s1.remove_job("j_del") is not None

    jc = job.characters["cA"]
    jc.images[0].status = VariantStatus.READY
    s1.update_variant(job, jc, jc.images[0])     # must NOT raise

    assert s1.get_job("j_del") is None           # no in-memory zombie
    s2 = SqliteStateStore(db_path=sqlite_db_path)
    assert s2.get_job("j_del") is None           # no DB rows either


def test_sqlite_update_job_after_delete_no_resurrection(sqlite_db_path):
    """The full update_job path re-INSERTed the jobs row, fully resurrecting
    the deleted job to disk."""
    s1 = SqliteStateStore(db_path=sqlite_db_path)
    job = _job()
    s1.add_job(job)
    s1.remove_job("j_del")

    s1.update_job(job)

    assert s1.get_job("j_del") is None
    s2 = SqliteStateStore(db_path=sqlite_db_path)
    assert s2.get_job("j_del") is None


def test_sqlite_update_job_character_and_video_after_delete(sqlite_db_path):
    s1 = SqliteStateStore(db_path=sqlite_db_path)
    job = _job()
    s1.add_job(job)
    s1.remove_job("j_del")

    jc = job.characters["cA"]
    s1.update_job_character(job, jc)             # must NOT raise
    jc.videos[0].status = VideoStatus.DONE
    s1.update_video(job, jc, jc.videos[0])       # must NOT raise

    assert s1.get_job("j_del") is None
    s2 = SqliteStateStore(db_path=sqlite_db_path)
    assert s2.get_job("j_del") is None


def test_sqlite_update_generation_after_delete(sqlite_db_path):
    s1 = SqliteStateStore(db_path=sqlite_db_path)
    gen = MediaGeneration(gen_id="g_del", kind="image", model="gpt-image-2",
                          prompt="p")
    s1.add_generation(gen)
    s1.delete_generation("g_del")

    s1.update_generation(gen)

    assert s1.get_generation("g_del") is None
    s2 = SqliteStateStore(db_path=sqlite_db_path)
    assert s2.get_generation("g_del") is None


# --- counterfactual: live entities still persist after a skipped write ------

def test_updates_on_live_entities_still_persist(sqlite_db_path, tmp_path):
    """The guard keys on existence only — a NORMAL add-then-update flow (and
    a fast-path append of a brand-new variant) must keep persisting exactly
    as before, even after a skipped write on a deleted sibling."""
    for s1, reopen in (
        (SqliteStateStore(db_path=sqlite_db_path),
         lambda: SqliteStateStore(db_path=sqlite_db_path)),
        (JsonStateStore(path=tmp_path / "state.json"),
         lambda: JsonStateStore(path=tmp_path / "state.json")),
    ):
        dead = _job()
        s1.add_job(dead)
        s1.remove_job("j_del")
        s1.update_job(dead)                      # skipped

        live = _job()
        live.job_id = "j_live"
        s1.add_job(live)
        jc = live.characters["cA"]
        jc.images[0].status = VariantStatus.READY
        s1.update_variant(live, jc, jc.images[0])
        new = GeneratedImage(variant_id="v_new", path="/n.png", prompt="p",
                             scene_id="s1", status=VariantStatus.GENERATING)
        jc.images.append(new)
        s1.update_variant(live, jc, new)         # fast-path append still lands

        loaded = reopen().get_job("j_live")
        assert loaded is not None
        assert loaded.characters["cA"].images[0].status == VariantStatus.READY
        assert [v.variant_id for v in loaded.characters["cA"].images] == \
            ["v0", "v_new"]
