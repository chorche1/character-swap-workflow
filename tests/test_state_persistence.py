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


# --- full-fidelity round-trip (the 2026-06-10 scene_ids bug class) ---------------------
#
# db.py used to hydrate Job/JobCharacter/GeneratedImage/VideoVariant from an
# ENUMERATED column list, silently dropping every field the schema didn't
# know (Job.scene_ids → multi-scene jobs collapsed to one scene after a
# restart). Rows now carry a complete `model_json` dump. This test builds
# models with EVERY field set to a non-default synthetic value — derived from
# model_fields, so a field added NEXT MONTH is exercised automatically — and
# asserts a byte-perfect round-trip. If someone reintroduces column
# enumeration, this fails on the first dropped field.

def _synth_value(name: str, annotation, idx: int):
    """Non-default synthetic value for a field annotation."""
    import typing
    from datetime import datetime
    from enum import Enum

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is typing.Union or str(origin) == "types.UnionType":   # Optional[X] / X | None
        inner = next((a for a in args if a is not type(None)), str)
        return _synth_value(name, inner, idx)
    if annotation is str:
        return f"synth_{name}_{idx}"
    if annotation is bool:
        return True
    if annotation is int:
        return 7 + idx
    if annotation is float:
        return 7.5 + idx
    if annotation is datetime:
        return datetime(2026, 6, 10, 12, 0, idx % 60)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return list(annotation)[-1]                                   # non-first member
    if origin is list:
        return [_synth_value(name, args[0] if args else str, idx)]
    if origin is dict:
        k = _synth_value(name + "_k", args[0] if args else str, idx)
        v = _synth_value(name + "_v", args[1] if len(args) > 1 else str, idx)
        return {k: v}
    raise NotImplementedError(
        f"_synth_value: add a synthesizer for field '{name}': {annotation!r} "
        "(a new model field type needs round-trip coverage)")


def _populated(model_cls, skip: set[str] = frozenset(), **overrides):
    vals = {}
    for i, (fname, finfo) in enumerate(model_cls.model_fields.items()):
        if fname in skip or fname in overrides:
            continue
        vals[fname] = _synth_value(fname, finfo.annotation, i)
    return model_cls(**vals, **overrides)


def test_sqlite_full_fidelity_job_round_trip(sqlite_db_path):
    from character_swap.models import GeneratedImage, VideoVariant

    variant = _populated(GeneratedImage)
    video = _populated(VideoVariant)
    jc = _populated(JobCharacter, skip={"images", "videos"},
                    images=[variant], videos=[video],
                    approved_variant_ids=[variant.variant_id],
                    approved_variant_id=variant.variant_id)
    job = _populated(Job, skip={"characters"},
                     characters={jc.char_id: jc})

    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_job(job)

    s2 = _fresh_sqlite(sqlite_db_path)
    loaded = s2.get_job(job.job_id)
    assert loaded is not None
    assert loaded.model_dump() == job.model_dump(), (
        "SQLite round-trip dropped or mutated a field — check db.py model_json paths")


def test_sqlite_multi_scene_survives_restart(sqlite_db_path):
    """The user-visible 2026-06-10 bug: a 2-scene job restarted into a
    1-scene job (scene_ids + variant.scene_id were dropped)."""
    from character_swap.models import GeneratedImage, VariantStatus

    v1 = GeneratedImage(variant_id="v1", path="/v1.png", prompt="p",
                        scene_id="sc_A", status=VariantStatus.READY)
    v2 = GeneratedImage(variant_id="v2", path="/v2.png", prompt="p",
                        scene_id="sc_B", status=VariantStatus.READY)
    jc = JobCharacter(char_id="c1", name="N", source_image_path="/c.png",
                      images=[v1, v2], approved_variant_ids=["v1", "v2"])
    job = Job(job_id="j_ms", title="t", scene_id="sc_A",
              scene_image_path="/a.png",
              scene_ids=["sc_A", "sc_B"],
              scene_image_paths=["/a.png", "/b.png"],
              video_audio=True, origin="reengineer:re_x",
              characters={"c1": jc})

    s1 = _fresh_sqlite(sqlite_db_path)
    s1.add_job(job)
    s2 = _fresh_sqlite(sqlite_db_path)
    loaded = s2.get_job("j_ms")
    assert loaded.scene_ids == ["sc_A", "sc_B"]
    assert loaded.scene_image_paths == ["/a.png", "/b.png"]
    assert loaded.video_audio is True
    assert loaded.origin == "reengineer:re_x"
    lv = loaded.characters["c1"].images
    assert [v.scene_id for v in lv] == ["sc_A", "sc_B"]


# --- Granular fast paths (update_variant / update_job_character / update_video) --------
#
# A variant status flip on a 45-slot job used to DELETE+reinsert every child
# row (~550KB × ~65 writes per Reengineer run). The fast paths write single
# rows; these tests prove they round-trip AND leave every sibling intact.

from character_swap.models import (   # noqa: E402 — grouped with their tests
    CharStatus,
    GeneratedImage,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


def _two_char_job() -> Job:
    chars = {}
    for c in ("cA", "cB"):
        images = [GeneratedImage(variant_id=f"{c}-v{i}", path=f"/{c}-v{i}.png",
                                 prompt=f"prompt-{c}-{i}", scene_id="s1",
                                 status=VariantStatus.GENERATING)
                  for i in range(2)]
        videos = [VideoVariant(video_id=f"{c}-vid0", grok_job_id="g0",
                               status=VideoStatus.PENDING)]
        chars[c] = JobCharacter(char_id=c, name=c.upper(),
                                source_image_path="/src.png",
                                status=CharStatus.GENERATING,
                                images=images, videos=videos)
    return Job(job_id="j1", title="t", scene_id="s1",
               scene_image_path="/scene.png", characters=chars)


def test_sqlite_update_variant_fast_path_round_trip(sqlite_db_path):
    s1 = _fresh_sqlite(sqlite_db_path)
    job = _two_char_job()
    s1.add_job(job)

    jc = job.characters["cA"]
    v = jc.images[1]
    v.status = VariantStatus.READY
    v.qc_status = "passed"
    v.qc_attempts = 2
    v.fallback_model = "nbp-swap"
    s1.update_variant(job, jc, v)

    s2 = _fresh_sqlite(sqlite_db_path)
    loaded = s2.get_job("j1")
    lv = loaded.characters["cA"].images[1]
    assert lv.status == VariantStatus.READY
    assert lv.qc_status == "passed"
    assert lv.qc_attempts == 2
    assert lv.fallback_model == "nbp-swap"
    # Every sibling untouched, order preserved.
    assert [x.variant_id for x in loaded.characters["cA"].images] == ["cA-v0", "cA-v1"]
    assert loaded.characters["cA"].images[0].status == VariantStatus.GENERATING
    assert [x.variant_id for x in loaded.characters["cB"].images] == ["cB-v0", "cB-v1"]
    assert len(loaded.characters["cB"].videos) == 1


def test_sqlite_update_job_character_fast_path(sqlite_db_path):
    s1 = _fresh_sqlite(sqlite_db_path)
    job = _two_char_job()
    s1.add_job(job)

    jc = job.characters["cB"]
    jc.status = CharStatus.AWAITING_APPROVAL
    jc.images[0].status = VariantStatus.READY
    s1.update_job_character(job, jc)

    s2 = _fresh_sqlite(sqlite_db_path)
    loaded = s2.get_job("j1")
    assert loaded.characters["cB"].status == CharStatus.AWAITING_APPROVAL
    assert loaded.characters["cB"].images[0].status == VariantStatus.READY
    assert loaded.characters["cA"].status == CharStatus.GENERATING


def test_sqlite_update_video_fast_path(sqlite_db_path):
    s1 = _fresh_sqlite(sqlite_db_path)
    job = _two_char_job()
    s1.add_job(job)

    jc = job.characters["cA"]
    vv = jc.videos[0]
    vv.status = VideoStatus.DONE
    vv.final_video_path = "/final.mp4"
    s1.update_video(job, jc, vv)

    s2 = _fresh_sqlite(sqlite_db_path)
    loaded = s2.get_job("j1")
    assert loaded.characters["cA"].videos[0].status == VideoStatus.DONE
    assert loaded.characters["cA"].videos[0].final_video_path == "/final.mp4"
    assert loaded.characters["cB"].videos[0].status == VideoStatus.PENDING


def test_sqlite_appended_variant_via_fast_path(sqlite_db_path):
    """_replace_variant appends NEW variants (e.g. edits) then fast-paths —
    the insert must land."""
    s1 = _fresh_sqlite(sqlite_db_path)
    job = _two_char_job()
    s1.add_job(job)

    jc = job.characters["cA"]
    new = GeneratedImage(variant_id="cA-edit", path="/e.png", prompt="edit",
                         scene_id="s1", status=VariantStatus.GENERATING)
    jc.images.append(new)
    s1.update_variant(job, jc, new)

    s2 = _fresh_sqlite(sqlite_db_path)
    assert [x.variant_id for x in s2.get_job("j1").characters["cA"].images] \
        == ["cA-v0", "cA-v1", "cA-edit"]


def test_sqlite_wipe_still_deletes_rows_via_full_update(sqlite_db_path):
    """The _kick_char wipe path uses the FULL update_job — old variant rows
    must actually disappear (the fast paths can't delete)."""
    s1 = _fresh_sqlite(sqlite_db_path)
    job = _two_char_job()
    s1.add_job(job)

    jc = job.characters["cA"]
    jc.images = []
    jc.videos = []
    s1.update_job(job)

    s2 = _fresh_sqlite(sqlite_db_path)
    loaded = s2.get_job("j1")
    assert loaded.characters["cA"].images == []
    assert loaded.characters["cA"].videos == []
    assert len(loaded.characters["cB"].images) == 2


def test_json_store_fast_paths_delegate(tmp_path):
    s1 = JsonStateStore(path=tmp_path / "state.json")
    job = _two_char_job()
    s1.add_job(job)
    jc = job.characters["cA"]
    v = jc.images[0]
    v.status = VariantStatus.READY
    s1.update_variant(job, jc, v)

    s2 = JsonStateStore(path=tmp_path / "state.json")
    assert s2.get_job("j1").characters["cA"].images[0].status == VariantStatus.READY


def test_sqlite_remove_job_deletes_rows(sqlite_db_path):
    """The exact bug from 2026-06-12: DELETE /api/jobs popped the job from
    memory and called save(), but save() only re-upserts jobs still in
    memory — the deleted job's rows stayed in the DB and the job resurrected
    on the next server restart (junk job "j_ef"). remove_job must delete the
    row (children cascade via FK)."""
    s1 = _fresh_sqlite(sqlite_db_path)
    job = _two_char_job()
    s1.add_job(job)

    removed = s1.remove_job("j1")
    assert removed is not None and removed.job_id == "j1"
    assert s1.get_job("j1") is None
    # Removing a job that's already gone is a harmless no-op.
    assert s1.remove_job("j1") is None

    # Restart-equivalent: a fresh store on the same DB must NOT resurrect it.
    s2 = _fresh_sqlite(sqlite_db_path)
    assert s2.get_job("j1") is None
    # Child rows are gone too, not orphaned.
    for table in ("jobs", "job_characters", "variants", "videos"):
        n = s2._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE job_id = 'j1'"  # noqa: S608
        ).fetchone()[0]
        assert n == 0, f"orphaned rows left in {table}"


def test_json_remove_job_persists(tmp_path):
    s1 = JsonStateStore(path=tmp_path / "state.json")
    s1.add_job(_two_char_job())
    assert s1.remove_job("j1").job_id == "j1"

    s2 = JsonStateStore(path=tmp_path / "state.json")
    assert s2.get_job("j1") is None
