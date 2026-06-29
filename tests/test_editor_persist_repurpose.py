"""Saved multi-clip Editor reels + 🔁 Repurpose (Hugo 2026-06-29).

Multi-clip Editor jobs used to vanish on reload — only files on disk, no state
record. They are now persisted as MediaGeneration(kind=editor) with an
`editor_meta` JSON bag (ordered source clips + settings + repurpose pointer), so
the user can come back later and press 🔁 Repurpose to get a mirror-flipped copy
exactly like Swap/Reengineer finals.

These lock: the SQLite round-trip of `editor_meta`, the `_gen_to_dict` editor
branch, the repurpose endpoint's validation (404 / 409 / no-clips), the
flip-to-compiling, and the worker's success wiring (mirror_h=True) +
fail-loud-on-missing-clips contract.
"""
from __future__ import annotations

import asyncio

import pytest

from character_swap import api, runner_compile
from character_swap.config import settings
from character_swap.models import GenKind, GenStatus, MediaGeneration
from character_swap.state import SqliteStateStore, store


def _clip_paths(n: int) -> list[str]:
    """Create n real dummy clip files under output_dir and return their paths."""
    d = settings.output_dir / "editor" / "_srcclips"
    d.mkdir(parents=True, exist_ok=True)
    out = []
    for i in range(n):
        p = d / f"clip-{i:02d}.mp4"
        p.write_bytes(b"\x00\x00\x00")  # not real video — only existence matters
        out.append(str(p))
    return out


def _make_editor_gen(edit_id: str, *, clips: list[str] | None = None,
                     repurpose: dict | None = None) -> MediaGeneration:
    gen = MediaGeneration(
        gen_id=edit_id,
        kind=GenKind.EDITOR,
        model="editor-multiclip",
        prompt="hello world this is the script",
        reference_paths=clips or [],
        status=GenStatus.DONE,
        output_path=str(settings.output_dir / "editor" / edit_id / "04-final.mp4"),
        editor_meta={
            "edit_id": edit_id,
            "n_clips": len(clips or []),
            "clip_paths": clips or [],
            "settings": {"template": "submagic-pro", "voice_id": "",
                         "enable_captions": True, "playback_speed": 1.05,
                         "overrides": {"size": 72}},
            "repurpose": repurpose,
        },
    )
    store().add_generation(gen)
    return gen


# --- persistence --------------------------------------------------------------

def test_editor_meta_survives_sqlite_round_trip():
    clips = _clip_paths(3)
    _make_editor_gen("ed_rt0001", clips=clips)
    # A fresh store reads the SAME on-disk DB → editor_meta must come back whole.
    reloaded = SqliteStateStore().get_generation("ed_rt0001")
    assert reloaded is not None
    assert reloaded.kind is GenKind.EDITOR
    meta = reloaded.editor_meta
    assert meta["edit_id"] == "ed_rt0001"
    assert meta["n_clips"] == 3
    assert meta["clip_paths"] == clips
    assert meta["settings"]["template"] == "submagic-pro"
    assert meta["settings"]["overrides"] == {"size": 72}
    assert meta["repurpose"] is None


def test_list_generations_filters_editor_kind():
    _make_editor_gen("ed_list01", clips=_clip_paths(1))
    rows = asyncio.run(api.list_generations(kind="editor"))
    assert any(r["gen_id"] == "ed_list01" and r["kind"] == "editor" for r in rows)


# --- serialization ------------------------------------------------------------

def test_gen_to_dict_exposes_editor_block_with_repurpose():
    rep_path = str(settings.output_dir / "editor" / "ed_rep" / "04-final.mp4")
    gen = _make_editor_gen(
        "ed_ser001", clips=_clip_paths(2),
        repurpose={"status": "done", "edit_id": "ed_rep",
                   "video_path": rep_path, "error": None,
                   "settings": {"template": "mrbeast-bold"}},
    )
    out = api._gen_to_dict(gen)
    assert "editor" in out
    e = out["editor"]
    assert e["edit_id"] == "ed_ser001"
    assert e["n_clips"] == 2
    assert e["settings"]["template"] == "submagic-pro"
    assert e["repurpose_status"] == "done"
    assert e["repurpose_url"]  # mapped to a /files URL (path is under output_dir)
    assert e["repurpose_settings"]["template"] == "mrbeast-bold"


# --- endpoint validation ------------------------------------------------------

def _body(**kw) -> api.EditorRepurposeBody:
    return api.EditorRepurposeBody(**kw)


def test_repurpose_unknown_id_404(monkeypatch):
    monkeypatch.setattr(api.settings, "openai_api_key", "sk-test")
    with pytest.raises(api.HTTPException) as ei:
        asyncio.run(api.editor_repurpose("ed_nope", _body(), api.BackgroundTasks()))
    assert ei.value.status_code == 404


def test_repurpose_non_editor_gen_404(monkeypatch):
    monkeypatch.setattr(api.settings, "openai_api_key", "sk-test")
    store().add_generation(MediaGeneration(
        gen_id="g_img1", kind=GenKind.IMAGE, model="gpt-image",
        prompt="x", status=GenStatus.DONE))
    with pytest.raises(api.HTTPException) as ei:
        asyncio.run(api.editor_repurpose("g_img1", _body(), api.BackgroundTasks()))
    assert ei.value.status_code == 404


def test_repurpose_no_clip_paths_409(monkeypatch):
    monkeypatch.setattr(api.settings, "openai_api_key", "sk-test")
    _make_editor_gen("ed_noclips", clips=[])
    with pytest.raises(api.HTTPException) as ei:
        asyncio.run(api.editor_repurpose("ed_noclips", _body(), api.BackgroundTasks()))
    assert ei.value.status_code == 409


def test_repurpose_already_compiling_409(monkeypatch):
    monkeypatch.setattr(api.settings, "openai_api_key", "sk-test")
    _make_editor_gen("ed_busy", clips=_clip_paths(1),
                     repurpose={"status": "compiling"})
    with pytest.raises(api.HTTPException) as ei:
        asyncio.run(api.editor_repurpose("ed_busy", _body(), api.BackgroundTasks()))
    assert ei.value.status_code == 409


def test_repurpose_endpoint_flips_to_compiling(monkeypatch):
    monkeypatch.setattr(api.settings, "openai_api_key", "sk-test")
    _make_editor_gen("ed_go", clips=_clip_paths(2))
    out = asyncio.run(api.editor_repurpose(
        "ed_go", _body(template="capcut-glow"), api.BackgroundTasks()))
    # The record immediately reads compiling (background task not run here).
    assert out["editor"]["repurpose_status"] == "compiling"
    persisted = store().get_generation("ed_go")
    rep = persisted.editor_meta["repurpose"]
    assert rep["status"] == "compiling"
    assert rep["settings"]["template"] == "capcut-glow"


# --- worker contract ----------------------------------------------------------

def test_worker_fails_loudly_on_missing_clips():
    # clip_paths point at files that don't exist → must FAIL, never half-build.
    _make_editor_gen("ed_miss", clips=["/no/such/clip-00.mp4",
                                       "/no/such/clip-01.mp4"])
    asyncio.run(runner_compile.repurpose_editor_job("ed_miss"))
    rep = store().get_generation("ed_miss").editor_meta["repurpose"]
    assert rep["status"] == "failed"
    assert "saknar" in rep["error"]
    assert rep["video_path"] is None


def test_worker_happy_path_passes_mirror_h(monkeypatch):
    clips = _clip_paths(2)
    _make_editor_gen("ed_mirror", clips=clips)

    seen = {}

    async def _fake_pipeline(paths, **kwargs):
        seen["paths"] = list(paths)
        seen["mirror_h"] = kwargs.get("mirror_h")
        seen["script_hint"] = kwargs.get("script_hint")
        final = settings.output_dir / "editor" / kwargs["edit_id"] / "04-final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"\x00")
        return runner_compile.EditorResult(final=final, voice_applied=False)

    monkeypatch.setattr(runner_compile, "run_editor_pipeline", _fake_pipeline)
    asyncio.run(runner_compile.repurpose_editor_job(
        "ed_mirror", template="submagic-pro", playback_speed=1.0))

    # The mirror transform was requested + the saved script biases captions.
    assert seen["mirror_h"] is True
    assert seen["script_hint"] == "hello world this is the script"
    assert [str(p) for p in seen["paths"]] == clips
    rep = store().get_generation("ed_mirror").editor_meta["repurpose"]
    assert rep["status"] == "done"
    assert rep["video_path"].endswith("04-final.mp4")
    assert rep["edit_id"] and rep["edit_id"] != "ed_mirror"


def test_worker_voice_swap_gated_on_explicit_override(monkeypatch):
    clips = _clip_paths(1)
    _make_editor_gen("ed_voice", clips=clips)

    seen = {}

    async def _fake_pipeline(paths, **kwargs):
        seen["voice_id"] = kwargs.get("voice_id")
        final = settings.output_dir / "editor" / kwargs["edit_id"] / "04-final.mp4"
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"\x00")
        return runner_compile.EditorResult(final=final, voice_applied=False)

    monkeypatch.setattr(runner_compile, "run_editor_pipeline", _fake_pipeline)
    # Box ticked but no override → no voice swap (editor reels have no preset).
    asyncio.run(runner_compile.repurpose_editor_job(
        "ed_voice", enable_voice_swap=True, voice_override=""))
    assert seen["voice_id"] is None
