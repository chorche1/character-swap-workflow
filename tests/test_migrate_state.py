"""migrate_state.py (state.json → state.sqlite3) — full-fidelity migration.

2026-07-01 audit gap #6: the only migration path behind `character-swap
migrate` had zero tests, and it runs exactly ONCE against the user's entire
history — a silently dropped field (qc_rejects, editor_meta, approved_variant
lists, video_models_by_scene, legacy single-scene jobs) is unrecoverable data
loss discovered weeks later.

Locked here:
  - a MAXIMAL legacy state.json (modern multi-scene job with per-scene model
    overrides + qc_rejects on image AND video, a LEGACY job with only the
    singular scene_id/approved_variant_id, chars/projects/scenes with every
    optional field, an editor MediaGeneration with nested editor_meta)
    migrates into SQLite and loads back with model_dump equality PER ENTITY;
  - the source file is renamed to state.json.migrated (re-runs become noop);
  - re-running against a non-empty DB REFUSES instead of duplicating rows;
  - no state.json at all is a clean noop.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from character_swap.migrate_state import migrate
from character_swap.models import (
    AppState, CharacterAsset, CharacterImage, CharStatus, GeneratedImage,
    GenKind, GenStatus, Job, JobCharacter, MediaGeneration, ProjectAsset,
    QCReject, SceneAsset, VariantStatus, VideoStatus, VideoVariant,
)
from character_swap.state import SqliteStateStore

_T0 = datetime(2026, 5, 1, 10, 20, 30, 123456)
_T1 = datetime(2026, 6, 2, 8, 15, 0, 654321)


def _maximal_state() -> AppState:
    scene = SceneAsset(scene_id="sc1", filename="sc_ab.png",
                       original_name="kitchen.png", created_at=_T0)
    char = CharacterAsset(
        char_id="ch1", filename="ch1.png", name="Cooper",
        images=[CharacterImage(image_id="img1", filename="ch1.png",
                               created_at=_T0),
                CharacterImage(image_id="img2", filename="ch1_b.png",
                               created_at=_T1)],
        primary_image_id="img1", voice_id="v_eleven", voice_provider="elevenlabs",
        language="es", created_at=_T0,
    )
    project = ProjectAsset(project_id="p1", name="Reels",
                           character_ids=["ch1"],
                           default_prompt="Custom project swap prompt.",
                           created_at=_T0, updated_at=_T1)

    modern_img = GeneratedImage(
        variant_id="v_m1", path="/tmp/v_m1.png", prompt="(swap)",
        scene_id="sc1", status=VariantStatus.READY,
        qc_status="passed", qc_reason=None, qc_attempts=2,
        qc_intent="byt muggen mot ett glas",
        qc_rejects=[QCReject(path="/tmp/v_m1.qcreject1.png",
                             reason="WRONG PERSON", attempt=1, kind="swap",
                             created_at=_T0)],
        fallback_model="nbp-swap", moderation_rewritten=True, created_at=_T0,
    )
    modern_vid = VideoVariant(
        video_id="vd_m1", grok_job_id="req_1", status=VideoStatus.DONE,
        submitted_at=_T0, completed_at=_T1,
        download_url="https://x/1.mp4", final_video_path="/tmp/vd_m1.mp4",
        source_variant_id="v_m1",
        movement_prompt_override="He waves slowly",
        localized_movement_prompt='Él dice "hola"',
        qc_status="failed", qc_reason="garbled TTS", qc_attempts=2,
        qc_rejects=[QCReject(path="/tmp/vd_m1.qcreject1.mp4",
                             reason="garbled TTS", attempt=1, kind="video",
                             created_at=_T1)],
        imported=False,
    )
    modern_jc = JobCharacter(
        char_id="ch1", name="Cooper", source_image_path="/tmp/ch1.png",
        status=CharStatus.DONE, images=[modern_img], videos=[modern_vid],
        approved_variant_ids=["v_m1"], approved_variant_id="v_m1",
        end_frame_paths={"sc1": "/tmp/end_ch1_sc1.png"},
        end_frame_errors={"sc2": "content policy"},
        compiled_video_path="/tmp/compiled.mp4", compile_edit_id="ed_1",
        compile_status="done", compile_warning="final is missing 1 scene(s)",
        drive_pushes={"final": {"file_id": "f1", "url": "https://d/x",
                                "name": "cooper.mp4", "at": "2026-06-02"}},
        updated_at=_T1,
    )
    modern_job = Job(
        job_id="j_modern", title="Modern job", project_id="p1",
        scene_id="sc1", scene_image_path="/tmp/sc1.png",
        scene_ids=["sc1", "sc2"],
        scene_image_paths=["/tmp/sc1.png", "/tmp/sc2.png"],
        prompt="custom swap prompt", image_model="gpt2-id-swap",
        video_model="grok-imagine",
        video_models_by_scene={"sc2": "kling-v3"},
        movement_prompt="walks", movement_prompts={"sc1": "walks",
                                                   "sc2": "nods"},
        characters={"ch1": modern_jc},
        created_at=_T0, updated_at=_T1,
    )

    legacy_jc = JobCharacter(
        char_id="ch1", name="Cooper", source_image_path="/tmp/ch1.png",
        status=CharStatus.APPROVED,
        images=[GeneratedImage(variant_id="v_old", path="/tmp/v_old.png",
                               prompt="(old)", scene_id=None,
                               status=VariantStatus.READY, created_at=_T0)],
        approved_variant_ids=[], approved_variant_id="v_old",  # singular only
        updated_at=_T0,
    )
    legacy_job = Job(
        job_id="j_legacy", scene_id="sc1", scene_image_path="/tmp/sc1.png",
        characters={"ch1": legacy_jc}, created_at=_T0, updated_at=_T0,
    )

    editor_gen = MediaGeneration(
        gen_id="g_ed1", kind=GenKind.EDITOR, model="editor-multiclip",
        prompt="the full script text", reference_paths=["/tmp/c1.mp4",
                                                        "/tmp/c2.mp4"],
        status=GenStatus.DONE, output_path="/tmp/final.mp4",
        editor_meta={
            "edit_id": "ed_77", "n_clips": 2,
            "clip_paths": ["/tmp/c1.mp4", "/tmp/c2.mp4"],
            "settings": {"template": "submagic-pro", "target_wpm": 190,
                         "overrides": None},
            "repurpose": {"status": "done", "edit_id": "ed_78",
                          "video_path": "/tmp/flip.mp4", "error": None},
        },
        created_at=_T0, completed_at=_T1,
    )
    image_gen = MediaGeneration(
        gen_id="g_im1", kind=GenKind.IMAGE, model="gpt-image",
        prompt="a red fox", reference_paths=[], aspect_ratio="9:16",
        enrich_prompt=True, enriched_prompt="a majestic red fox at dusk",
        use_director=True, director_prompt="cinematic fox portrait",
        status=GenStatus.FAILED, error="quota", cost_usd=0.05,
        created_at=_T1,
    )

    return AppState(
        scenes={"sc1": scene}, characters={"ch1": char},
        projects={"p1": project},
        jobs={"j_modern": modern_job, "j_legacy": legacy_job},
        generations={"g_ed1": editor_gen, "g_im1": image_gen},
    )


@pytest.fixture
def legacy_json(tmp_path: Path) -> tuple[Path, AppState]:
    """Write the maximal state as a LEGACY state.json: the old-shape job gets
    its post-hoc keys stripped so the file looks like a genuinely old dump."""
    raw = json.loads(_maximal_state().model_dump_json())
    for key in ("scene_ids", "scene_image_paths", "video_models_by_scene",
                "movement_prompts", "end_frames_by_scene"):
        raw["jobs"]["j_legacy"].pop(key, None)
    raw["jobs"]["j_legacy"]["characters"]["ch1"].pop("approved_variant_ids",
                                                     None)
    json_path = tmp_path / "state.json"
    json_path.write_text(json.dumps(raw), encoding="utf-8")
    # What a faithful migration must reproduce (defaults re-applied on load).
    expected = AppState.model_validate(raw)
    return json_path, expected


def _dump_maps(state: AppState) -> dict[str, dict[str, dict]]:
    return {
        section: {k: v.model_dump(mode="json")
                  for k, v in getattr(state, section).items()}
        for section in ("scenes", "characters", "projects", "jobs",
                        "generations")
    }


def test_migrate_full_fidelity_round_trip(legacy_json, tmp_path: Path):
    json_path, expected = legacy_json
    db_path = tmp_path / "state.sqlite3"

    result = migrate(json_path=json_path, db_path=db_path)

    assert result["migrated"] is True
    assert result == {"migrated": True, "scenes": 1, "characters": 1,
                      "projects": 1, "jobs": 2, "generations": 2}
    # Source renamed → re-runs are noop, user keeps a backup.
    assert not json_path.exists()
    assert json_path.with_suffix(".json.migrated").exists()

    loaded = SqliteStateStore(db_path=db_path).state
    want, got = _dump_maps(expected), _dump_maps(loaded)
    for section in want:
        assert set(got[section]) == set(want[section]), section
        for key in want[section]:
            assert got[section][key] == want[section][key], (
                f"{section}[{key}] lost fidelity through migration"
            )


def test_migrate_preserves_the_risk_fields_explicitly(legacy_json,
                                                      tmp_path: Path):
    """Belt and braces on the exact fields the audit called out — so a future
    schema change that breaks ONE of them names it directly."""
    json_path, _ = legacy_json
    db_path = tmp_path / "state.sqlite3"
    migrate(json_path=json_path, db_path=db_path)
    loaded = SqliteStateStore(db_path=db_path).state

    jc = loaded.jobs["j_modern"].characters["ch1"]
    assert jc.images[0].qc_rejects[0].path == "/tmp/v_m1.qcreject1.png"
    assert jc.images[0].qc_rejects[0].reason == "WRONG PERSON"
    assert jc.videos[0].qc_rejects[0].kind == "video"
    assert jc.videos[0].movement_prompt_override == "He waves slowly"
    assert jc.videos[0].localized_movement_prompt == 'Él dice "hola"'
    assert jc.approved_variant_ids == ["v_m1"]
    assert loaded.jobs["j_modern"].video_models_by_scene == {"sc2": "kling-v3"}

    legacy = loaded.jobs["j_legacy"]
    assert legacy.scene_id == "sc1"                      # singular preserved
    assert legacy.characters["ch1"].approved_variant_id == "v_old"

    ed = loaded.generations["g_ed1"]
    assert ed.kind == GenKind.EDITOR
    assert ed.editor_meta["repurpose"]["edit_id"] == "ed_78"
    assert ed.editor_meta["settings"]["template"] == "submagic-pro"
    assert ed.reference_paths == ["/tmp/c1.mp4", "/tmp/c2.mp4"]

    assert loaded.projects["p1"].default_prompt == "Custom project swap prompt."
    assert loaded.characters["ch1"].voice_id == "v_eleven"
    assert loaded.characters["ch1"].language == "es"
    assert loaded.characters["ch1"].primary_image_id == "img1"


def test_migrate_missing_json_is_noop(tmp_path: Path):
    result = migrate(json_path=tmp_path / "state.json",
                     db_path=tmp_path / "state.sqlite3")
    assert result == {"migrated": False, "reason": "no state.json found"}


def test_migrate_refuses_nonempty_db(legacy_json, tmp_path: Path):
    """Re-migrating over a live DB must refuse loudly, never duplicate or
    overwrite rows — the DB may have months of post-migration history."""
    json_path, _ = legacy_json
    db_path = tmp_path / "state.sqlite3"
    assert migrate(json_path=json_path, db_path=db_path)["migrated"] is True

    # Simulate a state.json reappearing (e.g. restored from a backup).
    renamed = json_path.with_suffix(".json.migrated")
    renamed.rename(json_path)

    result = migrate(json_path=json_path, db_path=db_path)
    assert result["migrated"] is False
    assert "refusing" in result["reason"]
    assert json_path.exists()                            # source NOT renamed

    # Nothing was duplicated.
    loaded = SqliteStateStore(db_path=db_path).state
    assert len(loaded.jobs) == 2
    assert len(loaded.generations) == 2


def test_migrate_rerun_after_success_is_noop(legacy_json, tmp_path: Path):
    json_path, _ = legacy_json
    db_path = tmp_path / "state.sqlite3"
    assert migrate(json_path=json_path, db_path=db_path)["migrated"] is True
    result = migrate(json_path=json_path, db_path=db_path)
    assert result == {"migrated": False, "reason": "no state.json found"}
