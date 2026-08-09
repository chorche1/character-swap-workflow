"""🎞 Versions — an extra video per character, from an edited cut (2026-08-10).

The whole feature rests on one promise: **building a version cannot change the
original reel.** These tests hold that promise from both ends — the cut the
build walks, and the buckets/filenames/locks the delivery uses.

Locked here:
  1. The cut: a version's scenes are invisible to the base build, and a
     version's own build sees exactly its rows, in order, repeats included.
  2. A row whose scene was deleted REFUSES LOUDLY instead of silently
     shortening the video (the 2026-08-06 silent-drop class).
  3. `_do_build` writes only its own bucket — never `finals`, `repurposed`,
     `status` or `finals_stale` (mirrors test_repurpose.py's contract).
  4. Every deliverable gets a DISTINCT filename. A third variant that produced
     the final's name would OVERWRITE the original final on Drive.
  5. The two pre-ticked ✓ boxes resolve a MISSING value to True.
  6. "Ändrad" is derived from clip submit times, not a stored flag.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import (
    deliverables,
    runner_compile,
    runner_reengineer,
    runner_versions,
    telegram_delivery,
    versions,
)
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
)
from character_swap.runner_compile import EditorResult


# --------------------------------------------------------------- fixtures

def _clip(vid, variant, path, **kw):
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid,
                        status=VideoStatus.DONE, source_variant_id=variant,
                        final_video_path=path, **kw)


def _job(tmp_path, n=2):
    paths = []
    for i in range(1, n + 1):
        p = tmp_path / f"c{i}.mp4"
        p.write_bytes(b"x")
        paths.append(p)
    imgs = [GeneratedImage(variant_id=f"v{i}", path=f"/v{i}.png", prompt="p",
                           scene_id=f"s{i}", status="ready")
            for i in range(1, n + 1)]
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/c.png",
                      status=CharStatus.APPROVED, images=imgs,
                      approved_variant_ids=[f"v{i}" for i in range(1, n + 1)],
                      videos=[_clip(f"vd{i}", f"v{i}", str(paths[i - 1]))
                              for i in range(1, n + 1)])
    job = Job(job_id="j1", title="t", scene_id="s1",
              scene_ids=[f"s{i}" for i in range(1, n + 1)],
              scene_image_path="/p.png", scene_image_paths=["/p.png"] * n,
              characters={"cA": jc}, origin="reengineer:re_t")
    return job, paths


def _scene(idx, sid, **kw):
    return {"idx": idx, "scene_id": sid, "duration": 2.0,
            "motion_prompt": "m", "speech": "", "summary": "", **kw}


def _state(rows=None, *, own=(), extra=(), **version_fields):
    """`own` tags EXISTING base scenes as version-owned; `extra` appends new
    version-owned scenes (which is where they always land in production —
    appended, never inserted, so base indices stay contiguous)."""
    scenes = [_scene(0, "s1"), _scene(1, "s2")]
    for e in scenes:
        if e["scene_id"] in own:
            e[versions.OWNER_KEY] = "v_test"
    for sid in extra:
        scenes.append(_scene(len(scenes), sid,
                             **{versions.OWNER_KEY: "v_test"}))
    version = {"id": "v_test", "name": "Version 2",
               "rows": rows if rows is not None
               else [{"row_id": "r1", "scene_id": "s1"},
                     {"row_id": "r2", "scene_id": "s2"}],
               "chars": {}, "building": False, **version_fields}
    return {"re_id": "re_t", "job_id": "j1", "status": "done",
            "scenes": scenes, "versions": {"v_test": version}}


# ------------------------------------------------------------ 1. the cut

def test_base_cut_hides_version_owned_scenes():
    state = _state(extra=["s9"])
    assert [e["scene_id"] for e in versions.base_scenes(state)] == ["s1", "s2"]


def test_version_cut_reorders_renumbers_and_strips_the_owner_tag():
    """A cut's entries must not claim an owner — that is what lets the three
    build readers filter unconditionally without dropping a version's own
    scenes from its own build."""
    state = _state(rows=[{"row_id": "r1", "scene_id": "s2"},
                         {"row_id": "r2", "scene_id": "s9"},
                         {"row_id": "r3", "scene_id": "s1"}],
                   extra=["s9"])
    cut = versions.version_cut(state, state["versions"]["v_test"])
    assert [e["scene_id"] for e in cut["scenes"]] == ["s2", "s9", "s1"]
    # Renumbered to POSITION IN THE VERSION, so "scen N" counts within it.
    assert [e["idx"] for e in cut["scenes"]] == [0, 1, 2]
    assert all(versions.OWNER_KEY not in e for e in cut["scenes"])
    # ...and the stored entries are untouched.
    assert state["scenes"][2][versions.OWNER_KEY] == "v_test"


def test_a_repeated_row_yields_the_clip_twice():
    state = _state(rows=[{"row_id": "r1", "scene_id": "s1"},
                         {"row_id": "r2", "scene_id": "s2"},
                         {"row_id": "r3", "scene_id": "s1"}])
    cut = versions.version_cut(state, state["versions"]["v_test"])
    assert [e["scene_id"] for e in cut["scenes"]] == ["s1", "s2", "s1"]


def test_collect_clips_never_sees_a_version_scene(tmp_path):
    """The base final cannot grow a scene a version added — the promise."""
    job, paths = _job(tmp_path)
    state = _state(own=["s2"])       # s2 now belongs to the version
    clips, _d, missing, _w = runner_reengineer._collect_clips(
        state, job.characters["cA"])
    assert [str(c) for c in clips] == [str(paths[0])]
    assert missing == []               # not "missing" — simply not ours


def test_assembly_gaps_uses_the_same_cut_as_collect_clips(tmp_path):
    """The mirror-exactly invariant now covers the scene LIST too."""
    job, _paths = _job(tmp_path)
    state = _state(own=["s2"])
    gaps = runner_reengineer._assembly_gaps(state, job)
    assert gaps["hard"] == [] and gaps["pending"] == []


# ------------------------------------------------- 2. a dangling row is loud

def test_a_row_whose_scene_was_deleted_refuses_loudly(tmp_path):
    job, _paths = _job(tmp_path)
    state = _state(rows=[{"row_id": "r1", "scene_id": "s1"},
                         {"row_id": "r2", "scene_id": "gone"}])
    with pytest.raises(runner_versions.VersionBuildRefused) as err:
        runner_versions.preflight(state, job, state["versions"]["v_test"])
    # Names the position so the user can find the row, and reports it as data.
    assert "plats 2" in str(err.value)
    assert err.value.detail["dangling"][0]["scene_id"] == "gone"


def test_resolve_rows_reports_rather_than_skips():
    """Skipping is the failure mode: both build readers treat an absent scene
    as 'never this character's' and drop it in silence."""
    state = _state(rows=[{"row_id": "r1", "scene_id": "gone"}])
    scenes, dangling = versions.resolve_rows(
        state, state["versions"]["v_test"])
    assert scenes == []
    assert dangling == [{"row_id": "r1", "scene_id": "gone", "position": 0}]


def test_an_empty_version_refuses(tmp_path):
    job, _paths = _job(tmp_path)
    state = _state(rows=[])
    with pytest.raises(runner_versions.VersionBuildRefused):
        runner_versions.preflight(state, job, state["versions"]["v_test"])


# ------------------------------------------------------------- 3. the build

def _wire(monkeypatch, tmp_path, state, job):
    run_dir = tmp_path / "run"
    run_dir.mkdir(exist_ok=True)

    class _S:
        def get_job(self, jid):
            return job

        def get_character(self, cid):
            return None

    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_compile, "store", lambda: _S())
    monkeypatch.setattr(runner_versions.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    monkeypatch.setattr(runner_versions.reengineer, "load_state",
                        lambda rid: state)
    monkeypatch.setattr(type(runner_versions.settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)

    updates: list[dict] = []

    def _update(re_id, **kw):
        updates.append(kw)
        state.update(kw)

    monkeypatch.setattr(runner_reengineer, "_update", _update)

    captured: dict = {}

    async def fake_pipeline(paths, **kw):
        captured["mirror_h"] = kw.get("mirror_h")
        captured["paths"] = [str(p) for p in paths]
        captured["dialogues"] = kw.get("clip_dialogues")
        out = kw["edit_dir"] / "final.mp4"
        out.write_bytes(b"mp4")
        return EditorResult(final=out, voice_applied=False)

    monkeypatch.setattr(runner_compile, "run_editor_pipeline", fake_pipeline)
    return captured, updates


def test_build_writes_only_its_own_bucket(monkeypatch, tmp_path):
    """THE money test: the original final is untouched, in every field that
    could carry it — mirrors test_repurpose.py's contract one deliverable on."""
    job, paths = _job(tmp_path)
    state = _state(rows=[{"row_id": "r2", "scene_id": "s2"},
                         {"row_id": "r1", "scene_id": "s1"}])
    captured, updates = _wire(monkeypatch, tmp_path, state, job)

    asyncio.run(runner_versions._do_build(
        "re_t", state, state["versions"]["v_test"]))

    # Built the EDITED cut — reversed, both clips.
    assert captured["paths"] == [str(paths[1]), str(paths[0])]
    entry = state["versions"]["v_test"]["chars"]["cA"]
    assert entry["status"] == "done"
    assert entry["final_path"].endswith("version_v_test_cA.mp4")
    assert entry["n_clips"] == 2
    assert state["versions"]["v_test"]["building"] is False
    assert state["versions"]["v_test"]["built_at"]
    # Nothing that belongs to the original reel was written.
    written = set().union(*(u.keys() for u in updates))
    assert "finals" not in written
    assert "repurposed" not in written
    assert "status" not in written
    assert "finals_stale" not in written


def test_build_mirrors_by_default(monkeypatch, tmp_path):
    job, _paths = _job(tmp_path)
    state = _state()
    captured, _u = _wire(monkeypatch, tmp_path, state, job)
    asyncio.run(runner_versions._do_build(
        "re_t", state, state["versions"]["v_test"]))
    assert captured["mirror_h"] is True


def test_build_can_be_unmirrored(monkeypatch, tmp_path):
    job, _paths = _job(tmp_path)
    state = _state(settings={"mirror": False})
    captured, _u = _wire(monkeypatch, tmp_path, state, job)
    asyncio.run(runner_versions._do_build(
        "re_t", state, state["versions"]["v_test"]))
    assert captured["mirror_h"] is False


def test_a_duplicated_row_repeats_the_clip_and_its_dialogue(
        monkeypatch, tmp_path):
    """clip_dialogues must stay index-aligned with clips, or per-clip caption
    alignment burns one scene's line over another's video."""
    job, paths = _job(tmp_path)
    state = _state(rows=[{"row_id": "r1", "scene_id": "s1"},
                         {"row_id": "r2", "scene_id": "s2"},
                         {"row_id": "r3", "scene_id": "s1"}])
    captured, _u = _wire(monkeypatch, tmp_path, state, job)
    asyncio.run(runner_versions._do_build(
        "re_t", state, state["versions"]["v_test"]))
    assert captured["paths"] == [str(paths[0]), str(paths[1]), str(paths[0])]
    assert len(captured["dialogues"]) == 3


# ------------------------------------------------- 4. distinct filenames

def test_every_deliverable_gets_a_distinct_filename():
    """A third variant sharing the final's name OVERWRITES it on Drive —
    google_drive.upload_or_replace keys on the filename."""
    names = {
        telegram_delivery.character_final_name("A", "Reel", v, "re_1",
                                               label=lbl)
        for v, lbl in (("final", None), ("repurpose", None),
                       ("version:v_a", "Version 2"),
                       ("version:v_b", "Version 3"))
    }
    assert len(names) == 4


def test_a_version_without_a_label_still_cannot_collide():
    """The id fallback is the guarantee, not a nicety: a caller that forgets
    the label must not be able to produce the final's name."""
    plain = telegram_delivery.character_final_name("A", "Reel", "final", "r")
    unlabelled = telegram_delivery.character_final_name(
        "A", "Reel", "version:v_a", "r")
    assert plain != unlabelled and "v_a" in unlabelled


def test_final_and_repurpose_names_are_unchanged():
    """Existing Drive files keep their overwrite key."""
    assert telegram_delivery.character_final_name(
        "Ching", "Reel", "final", "re_1") == "Ching — Reel [re_1].mp4"
    assert telegram_delivery.character_final_name(
        "Ching", "Reel", "repurpose",
        "re_1") == "Ching — Reel — repurpose [re_1].mp4"


def test_char_entries_routes_each_variant_to_its_own_bucket():
    state = _state()
    state["finals"] = {"cA": {"status": "done"}}
    state["repurposed"] = {"cA": {"status": "failed"}}
    state["versions"]["v_test"]["chars"] = {"cA": {"status": "compiling"}}
    assert deliverables.char_entries(state, "final")["cA"]["status"] == "done"
    assert deliverables.char_entries(
        state, "repurpose")["cA"]["status"] == "failed"
    assert deliverables.char_entries(
        state, "version:v_test")["cA"]["status"] == "compiling"


def test_an_unknown_version_is_refused_not_defaulted():
    """Falling through to a generic bucket is the silent-misfiling this
    module exists to prevent."""
    state = _state()
    with pytest.raises(deliverables.UnknownVariant):
        deliverables.validate("version:nope", state)
    with pytest.raises(deliverables.UnknownVariant):
        deliverables.validate("nonsense", state)
    assert deliverables.validate("version:v_test", state) == "version:v_test"


def test_the_telegram_lock_is_per_variant():
    """One deliverable uploading must not block its siblings."""
    with telegram_delivery.sending("re_t", "cA", "version:v_test"):
        assert telegram_delivery.is_sending("re_t", "cA", "version:v_test")
        assert not telegram_delivery.is_sending("re_t", "cA", "final")
        assert not telegram_delivery.is_sending("re_t", "cA", "version:v_b")


# ---------------------------------------------- 5. pre-ticked ✓ defaults

@pytest.mark.parametrize("field, reader", [
    ("mirror", runner_versions.mirrored),
    ("auto_telegram_send", runner_versions.auto_telegram_send),
])
def test_a_missing_checkbox_value_means_ticked(field, reader):
    """Both boxes ship PRE-TICKED, so an older cached client that never sends
    the key must not silently flip the behaviour."""
    assert reader({}) is True
    assert reader({"settings": {}}) is True
    assert reader({"settings": {field: False}}) is False
    assert reader({"settings": {field: True}}) is True


# --------------------------------------------------------- 6. staleness

def test_a_version_is_stale_when_a_clip_was_resubmitted_after_the_build(
        tmp_path):
    job, _paths = _job(tmp_path)
    state = _state(built_at="2020-01-01T00:00:00Z")
    assert runner_versions.is_stale(
        state, job, state["versions"]["v_test"]) is True


def test_a_freshly_built_version_is_not_stale(tmp_path):
    job, _paths = _job(tmp_path)
    state = _state(built_at="2999-01-01T00:00:00Z")
    assert runner_versions.is_stale(
        state, job, state["versions"]["v_test"]) is False


def test_an_unbuilt_version_is_not_stale(tmp_path):
    job, _paths = _job(tmp_path)
    state = _state()
    assert runner_versions.is_stale(
        state, job, state["versions"]["v_test"]) is False
