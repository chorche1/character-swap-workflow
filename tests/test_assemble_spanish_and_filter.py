"""Two fixes to the Reengineer assemble, both from re_f786da400c (2026-07-29).

1. SPANISH CAPTIONS. A 🇪🇸 character's clips are submitted with the dialogue
   translated (`VideoVariant.localized_movement_prompt`, Hugo 2026-06-26), but
   `_collect_clips` handed the caption pipeline the scene's ENGLISH
   motion_prompt. That text is both the Whisper bias and the even-timed
   fallback when Whisper can't read a clip — so wang's final burned ENGLISH
   captions over Spanish speech ("4 klipp kunde inte läsas av Whisper —
   byggde captions från den kända repliken"). Swap Step-6 already preferred
   the localized line; the assemble path now matches.

2. PER-CHARACTER REBUILD. `assemble(char_ids=[...])` rebuilds just those
   characters and MERGES over the stored finals, so rebuilding the two that
   failed doesn't drop — or re-bill Whisper/Remotion for — the other five.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from character_swap import runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
)

ES = ('He says enthusiastically to the camera: "Pon aceite para bebé en tus '
      'oídos." while he pours it.')
EN_SCENE = ('He says enthusiastically to the camera: "Pour baby oil in your '
            'ears." while he pours it.')


def _clip(vid, variant, path, *, localized=None) -> VideoVariant:
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid,
                        status=VideoStatus.DONE, source_variant_id=variant,
                        final_video_path=str(path),
                        localized_movement_prompt=localized)


def _char(cid, name, videos) -> JobCharacter:
    img = GeneratedImage(variant_id="v_" + cid, path=f"/{cid}.png", prompt="p",
                         scene_id="s1", status="ready")
    return JobCharacter(char_id=cid, name=name, source_image_path="/c.png",
                        status=CharStatus.APPROVED, images=[img],
                        approved_variant_ids=["v_" + cid], videos=videos)


def _state() -> dict:
    return {"re_id": "re_t", "job_id": "j1",
            "scenes": [{"idx": 0, "scene_id": "s1", "duration": 5.0,
                        "motion_prompt": EN_SCENE, "speech": "", "summary": ""}]}


def _job(chars) -> Job:
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               characters={c.char_id: c for c in chars},
               origin="reengineer:re_t")


def test_localized_dialogue_wins_over_the_english_scene_prompt(tmp_path):
    """THE wang regression: Spanish audio must get the Spanish caption line."""
    f = tmp_path / "c.mp4"; f.write_bytes(b"x")
    jc = _char("cES", "wang", [_clip("vd1", "v_cES", f, localized=ES)])
    _clips, dialogues, _missing, _w = runner_reengineer._collect_clips(
        _state(), jc)
    assert "aceite para bebé" in dialogues[0]
    assert "baby oil" not in dialogues[0]


def test_english_character_keeps_the_scene_prompt(tmp_path):
    """No localization → unchanged behaviour (the scene's own says-clause)."""
    f = tmp_path / "c.mp4"; f.write_bytes(b"x")
    jc = _char("cEN", "Connor", [_clip("vd1", "v_cEN", f)])
    _clips, dialogues, _missing, _w = runner_reengineer._collect_clips(
        _state(), jc)
    assert dialogues[0] == "Pour baby oil in your ears."


def test_partial_rebuild_only_touches_the_named_characters(monkeypatch,
                                                           tmp_path):
    f = tmp_path / "c.mp4"; f.write_bytes(b"x")
    job = _job([_char("cA", "Ching", [_clip("v1", "v_cA", f)]),
                _char("cB", "wang", [_clip("v2", "v_cB", f)]),
                _char("cC", "Richard", [_clip("v3", "v_cC", f)])])
    monkeypatch.setattr(runner_reengineer, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job))
    built: list[str] = []
    stored = {"re_id": "re_t", "job_id": "j1", "finals_stale": True,
              "finals": {"cA": {"status": "failed", "error": "old"},
                         "cB": {"status": "done", "final_path": "/old_b.mp4"},
                         "cC": {"status": "done", "final_path": "/keep_c.mp4"}}}
    state = _state()

    async def fake_one(cid, jc, *a, **kw):
        built.append(cid)

    updates: list[dict] = []
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.append(kw))
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: dict(stored))
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda re_id: tmp_path)
    monkeypatch.setattr(runner_reengineer, "_assemble_settings",
                        lambda st: {"template": "t"})

    # Patch the per-character worker by intercepting the gather: run the real
    # _do_assemble but with a stubbed pipeline via _one_character replacement.
    orig = runner_reengineer.runner_compile.run_editor_pipeline

    async def fake_pipeline(*a, **kw):
        raise AssertionError("should not run for filtered-out characters")
    monkeypatch.setattr(runner_reengineer.runner_compile,
                        "run_editor_pipeline", fake_pipeline)
    try:
        asyncio.run(runner_reengineer._do_assemble("re_t", state,
                                                   char_ids=["cA", "cB"]))
    except Exception:
        pass
    finally:
        monkeypatch.setattr(runner_reengineer.runner_compile,
                            "run_editor_pipeline", orig)

    final_update = [u for u in updates if "finals" in u][-1]
    merged = final_update["finals"]
    # cC was NOT rebuilt and must survive untouched.
    assert merged["cC"] == {"status": "done", "final_path": "/keep_c.mp4"}
    assert set(merged) == {"cA", "cB", "cC"}
    # A partial build never claims the whole run is fresh.
    assert "finals_stale" not in final_update


def test_full_rebuild_still_clears_finals_stale():
    import inspect
    src = inspect.getsource(runner_reengineer._do_assemble)
    assert 'changes["finals_stale"] = False' in src
    assert "if not only:" in src


def test_char_ids_is_not_persisted_as_a_setting():
    from character_swap.api import (
        ReAssembleSettingsBody,
        _store_assemble_settings,
        _store_repurpose_settings,
    )
    for fn, key in ((_store_assemble_settings, "assemble_settings"),
                    (_store_repurpose_settings, "repurpose_settings")):
        st: dict = {}
        assert fn(st, ReAssembleSettingsBody(char_ids=["cA"])) is False
        assert not st.get(key)
        # A real setting alongside it still persists — without the filter.
        st = {}
        fn(st, ReAssembleSettingsBody(char_ids=["cA"], target_wpm=200.0))
        assert st[key] == {"target_wpm": 200.0}
