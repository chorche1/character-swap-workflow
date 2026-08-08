"""Kör om en körning med ETT NYTT CAST (Hugo 2026-08-08).

`POST /api/reengineer/{re_id}/rerun` clones a run's scene plan onto a new set
of characters as a BRAND NEW run. The parent must come out byte-identical.

The tests are grouped by the failure each one exists to prevent — the
mapping pass over this codebase turned up seven distinct ways a naive clone
silently produces a wrong-but-plausible run (copied job_id rewrites the
parent; a copied Director plan is keyed by the OLD char_ids so every slot
falls back to the generic prompt; a copied `awaiting_person` deadlocks the
build; per-character `language_model_redirect` outranks the live 🗣 lookup;
referencing the parent's files makes deleting it break this run; …).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, reengineer as reengineer_mod, rerun as rerun_mod
from character_swap import runner, runner_reengineer
from character_swap.config import settings
from character_swap.models import (CharacterAsset, GeneratedImage, Job,
                                   JobCharacter, SceneAsset)

PNG = b"\x89PNG\r\n\x1a\n" + b"parent-scene-bytes"


class _Store:
    def __init__(self, box):
        self.box = box

    def get_character(self, cid):
        return self.box["chars"].get(cid)

    def get_scene(self, sid):
        return self.box["scenes"].get(sid)

    def add_scene(self, scene):
        self.box["scenes"][scene.scene_id] = scene

    def get_job(self, jid):
        return self.box["jobs"].get(jid)

    def add_job(self, job):
        self.box["jobs"][job.job_id] = job

    def update_job(self, job):
        self.box["jobs"][job.job_id] = job


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """A parent run on disk: 3 scenes (one with a 🎯 end pose), a finished job,
    an old cast of two, plus a library holding the NEW cast."""
    box = {"chars": {}, "scenes": {}, "jobs": {}, "started": []}

    out = tmp_path / "output"
    # scenes_dir is a computed property (input_dir / "scenes") — patch the
    # field it derives from.
    monkeypatch.setattr(settings, "output_dir", out)
    monkeypatch.setattr(settings, "input_dir", tmp_path / "input")
    scenes_dir = settings.scenes_dir
    scenes_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    # has_provider is a method → patch on the class.
    monkeypatch.setattr(type(settings), "has_provider", lambda self, p: True)

    st = _Store(box)
    for mod in (api, rerun_mod, runner_reengineer, runner):
        monkeypatch.setattr(mod, "store", lambda _st=st: _st, raising=False)

    # Characters: the parent's cast, plus a new English / Spanish / German one.
    for cid, name, lang in (("ch_old1", "Old A", None), ("ch_old2", "Old B", None),
                            ("ch_new_en", "Nya EN", None),
                            ("ch_new_es", "Nya ES", "es"),
                            ("ch_new_de", "Nya DE", "de")):
        box["chars"][cid] = CharacterAsset(
            char_id=cid, name=name, filename=f"{cid}.png", language=lang)

    # Three scenes registered in the content-addressed library.
    sids = []
    for i in range(3):
        sid = f"sc_parent{i}"
        (scenes_dir / f"{sid}.png").write_bytes(PNG + bytes([i]))
        box["scenes"][sid] = SceneAsset(scene_id=sid, filename=f"{sid}.png",
                                        original_name=f"scene{i}.png")
        sids.append(sid)

    parent_dir = out / "reengineer" / "re_parent"
    (parent_dir / "end_frames").mkdir(parents=True, exist_ok=True)
    pose = parent_dir / "end_frames" / "img_01.png"
    pose.write_bytes(b"end-pose-bytes")

    parent_job = Job(
        job_id="j_parent", scene_id=sids[0], scene_ids=list(sids),
        scene_image_path=str(scenes_dir / f"{sids[0]}.png"),
        scene_image_paths=[str(scenes_dir / f"{s}.png") for s in sids],
        video_model="kling-v3",
        end_frames_by_scene={sids[1]: str(pose)},
        director_prompts_json=json.dumps({"characters": ["ch_old1"]}),
        scene_people_counts={sids[2]: 2},
        characters={
            "ch_old1": JobCharacter(char_id="ch_old1", name="Old A",
                                    source_image_path="x.png",
                                    images=[GeneratedImage(
                                        variant_id="v1", scene_id=sids[0],
                                        path="p.png", prompt="old")]),
        })
    box["jobs"]["j_parent"] = parent_job

    parent_state = {
        "re_id": "re_parent",
        "created_at": "2026-08-01T00:00:00Z", "updated_at": "2026-08-01T00:00:00Z",
        "status": "done", "error": None, "from_images": True,
        "source_name": "5 bilder", "character_ids": ["ch_old1", "ch_old2"],
        "image_model": "gpt2-id-swap", "video_model": "kling-v3",
        "auto_mode": False, "outfit_mode": "character", "outfit_text": "",
        "use_director": True, "skip_qc": False, "auto_telegram_send": True,
        "background_path": None, "background_source": "character",
        "character_source_image_ids": {"ch_old1": "img_old"},
        "job_id": "j_parent",
        # Runtime residue a naive clone would drag along:
        "finals": {"ch_old1": {"status": "done", "final_path": "/tmp/f.mp4",
                               "telegram": {"ok": True}}},
        "finals_stale": True, "completed_at": "2026-08-01T05:00:00Z",
        "consistency_warnings": {"ch_old1": [{"issue": "x"}]},
        "assemble_settings": {"template": "capcut-bluebox"},
        "scenes": [
            {"idx": 0, "scene_id": sids[0], "start": 0.0, "end": 5.0,
             "duration": 5.0, "kling_secs": 5, "source": "image",
             "motion_prompt": 'He says to the camera: "Hello there friend."',
             "speech": "Hello there friend.", "summary": "scen ett",
             "dirty": True, "dirty_at": "2026-08-01T04:00:00Z"},
            {"idx": 1, "scene_id": sids[1], "start": 0.0, "end": 8.0,
             "duration": 8.0, "kling_secs": 8, "source": "image",
             "motion_prompt": "Scene two prompt", "speech": "",
             "summary": "scen två", "two_person": True,
             "end_frame_path": str(pose), "video_model": "veo-3.1-fast"},
            {"idx": 2, "scene_id": sids[2], "start": 0.0, "end": 6.0,
             "duration": 6.0, "kling_secs": 6, "source": "image",
             "motion_prompt": "Scene three prompt", "speech": "",
             "summary": "scen tre",
             # The multi-person gate, already answered on the parent.
             "multi_person": True, "swap_person_idx": 1,
             "people": [{"position": "left", "description": "man"},
                        {"position": "right", "description": "woman"}]},
        ],
    }
    states = {"re_parent": dict(parent_state)}

    def load_state(re_id):
        s = states.get(re_id)
        return json.loads(json.dumps(s)) if s else None

    def save_state(s):
        states[s["re_id"]] = json.loads(json.dumps(s))

    def re_dir(re_id):
        p = out / "reengineer" / re_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    for mod in (reengineer_mod, runner_reengineer.reengineer):
        monkeypatch.setattr(mod, "load_state", load_state)
        monkeypatch.setattr(mod, "save_state", save_state)
        monkeypatch.setattr(mod, "reengineer_dir", re_dir)
    monkeypatch.setattr(rerun_mod.reengineer_mod, "reengineer_dir", re_dir)

    async def _noop(re_id):
        box["started"].append(re_id)
    monkeypatch.setattr(runner_reengineer, "run_reengineer_from_images", _noop)

    return {"box": box, "states": states, "sids": sids, "out": out,
            "scenes_dir": scenes_dir, "pose": pose,
            "parent_snapshot": json.loads(json.dumps(parent_state))}


def _rows(plan, **over):
    rows = []
    for sc in plan["scenes"]:
        row = {"idx": sc["idx"], "motion_prompt": sc["motion_prompt"],
               "secs": sc["secs"], "direct": sc["direct"],
               "two_person": sc["two_person"], "keep_end_frame": True,
               "reuse_direct_clip": True, "video_model": sc["video_model"]}
        row.update(over)
        rows.append(row)
    return rows


def _rerun(re_id, body, bg=None):
    return asyncio.run(api.reengineer_rerun(re_id, api.RerunBody(**body),
                                            bg if bg is not None
                                            else BackgroundTasks()))


def _plan(re_id="re_parent"):
    return asyncio.run(api.reengineer_rerun_plan(re_id))


# --------------------------------------------------------------- prefill


def test_plan_exposes_editable_fields_and_off_entry_facts(wired):
    plan = _plan()
    assert [s["idx"] for s in plan["scenes"]] == [0, 1, 2]
    assert plan["scenes"][0]["motion_prompt"].startswith("He says")
    # Effective seconds — the stored override, NOT the card's displayed
    # klingDuration() (which adds +2 and would inflate every re-run).
    assert [s["secs"] for s in plan["scenes"]] == [5, 8, 6]
    # `has_end_frame` lives on the JOB, not the scene entry.
    assert [s["has_end_frame"] for s in plan["scenes"]] == [False, True, False]
    assert plan["scenes"][1]["two_person"] is True
    assert plan["scenes"][1]["video_model"] == "veo-3.1-fast"
    assert plan["settings"]["image_model"] == "gpt2-id-swap"
    assert plan["character_ids"] == ["ch_old1", "ch_old2"]
    # Thumbnails, or the modal renders three broken images.
    assert all(s.get("frame_url") for s in plan["scenes"])


def test_plan_trusts_the_job_over_a_stale_entry_end_frame(wired):
    """A 🎯 pose CLEARED after the gate must not be resurrected: the endpoints
    that clear it write only to Job.end_frames_by_scene, leaving the scene
    entry's write-once `end_frame_path` behind."""
    wired["box"]["jobs"]["j_parent"].end_frames_by_scene = {}
    plan = _plan()
    assert [s["has_end_frame"] for s in plan["scenes"]] == [False, False, False]


# --------------------------------------------------------------- the parent is untouched


def test_parent_state_and_job_are_untouched(wired):
    before_job = wired["box"]["jobs"]["j_parent"].model_dump_json()
    _rerun("re_parent", {"character_ids": ["ch_new_en"],
                         "scenes": _rows(_plan())})
    assert wired["states"]["re_parent"] == wired["parent_snapshot"]
    assert wired["box"]["jobs"]["j_parent"].model_dump_json() == before_job


def test_new_run_drops_every_runtime_key(wired):
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())})
    st = wired["states"][view["re_id"]]
    assert st["re_id"] != "re_parent"
    assert st["rerun_of"] == "re_parent"
    assert st["status"] == "queued"
    # A copied job_id would make _do_create_from_images RE-ATTACH to the
    # parent's job and rewrite it.
    assert st["job_id"] is None
    assert st["finals"] == {}
    for dead in ("finals_stale", "completed_at", "consistency_warnings",
                 "repurposed", "resume_status", "auto_assemble_blocked"):
        assert dead not in st, dead
    # Per-scene runtime residue is gone too.
    for e in st["scenes"]:
        for dead in ("dirty", "dirty_at", "awaiting_person", "multi_person",
                     "people", "swap_person_idx"):
            assert dead not in e, dead


def test_person_choice_is_not_carried_to_a_new_cast(wired):
    """The parent's answer is baked PER CHARACTER with that character's gender
    clause, so it is wrong for a new cast in exactly the way the gate exists
    to prevent (re_a5613a883e). And a carried `awaiting_person` is a HARD
    assembly gap that would deadlock the child's build."""
    view = _rerun("re_parent", {"character_ids": ["ch_new_es"],
                                "scenes": _rows(_plan())})
    st = wired["states"][view["re_id"]]
    assert "swap_person_idx" not in st["scenes"][2]
    assert "awaiting_person" not in st["scenes"][2]


def test_character_source_overrides_are_not_inherited(wired):
    """The parent's map is keyed by the OLD char_ids — dead weight at best,
    and it would pin an unintended reference image if an id ever recurred."""
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())})
    assert wired["states"][view["re_id"]]["character_source_image_ids"] == {}


# --------------------------------------------------------------- the language rule


def test_language_flagged_characters_still_get_their_model(wired):
    """Hugo's explicit requirement. The inherited run model is kling-v3 and
    scene 2 even carries a per-scene override — neither may defeat the
    redirect, because `_eff_video_model_for_variant` returns the language
    model on its first line, before both."""
    view = _rerun("re_parent", {"character_ids": ["ch_new_en", "ch_new_es",
                                                  "ch_new_de"],
                                "scenes": _rows(_plan())})
    st = wired["states"][view["re_id"]]
    assert st["video_model"] == "kling-v3"

    job = Job(job_id="j_child", scene_id=st["scenes"][0]["scene_id"],
              scene_image_path="x.png",
              scene_ids=[e["scene_id"] for e in st["scenes"]],
              video_model=st["video_model"],
              video_models_by_scene={e["scene_id"]: e["video_model"]
                                     for e in st["scenes"] if e.get("video_model")},
              characters={cid: JobCharacter(char_id=cid, name=cid,
                                            source_image_path="x.png")
                          for cid in st["character_ids"]})
    lang_model = api.runner_media.SPOKEN_LANGUAGE_VIDEO_MODEL
    for cid in ("ch_new_es", "ch_new_de"):
        assert runner._eff_video_model_for_variant(
            job, job.characters[cid], None) == lang_model
    # …and an English character in a run cloned from a Spanish one is NOT
    # dragged onto the language model.
    assert runner._eff_video_model_for_variant(
        job, job.characters["ch_new_en"], None) == "kling-v3"


def test_no_per_character_clip_state_is_cloned(wired):
    """`VideoVariant.language_model_redirect` / `.fallback_model` outrank the
    live 🗣 lookup by design (fal request_ids are endpoint-scoped). Cloning a
    clip row would therefore pin a new character to the wrong provider."""
    view = _rerun("re_parent", {"character_ids": ["ch_new_es"],
                                "scenes": _rows(_plan())})
    st = wired["states"][view["re_id"]]
    blob = json.dumps(st)
    for leaked in ("language_model_redirect", "fallback_model", "variant_id",
                   "imported", "director_prompts_json", "ch_old1", "ch_old2"):
        assert leaked not in blob, leaked


def test_preflight_refuses_when_the_redirect_target_has_no_key(wired,
                                                               monkeypatch):
    """A 🗣 cast renders on the redirect target whatever the run's own model
    is, so its key is what has to be checked. Before this, an all-Spanish run
    passed every gate and died inside a worker thread an hour later."""
    lang_model = api.runner_media.SPOKEN_LANGUAGE_VIDEO_MODEL
    missing = api.runner_media.VIDEO_MODELS[lang_model]["provider"]
    monkeypatch.setattr(type(settings), "has_provider",
                        lambda self, p: p != missing)
    with pytest.raises(HTTPException) as ei:
        _rerun("re_parent", {"character_ids": ["ch_new_es"],
                             "scenes": _rows(_plan())})
    assert ei.value.status_code == 503
    assert lang_model in ei.value.detail or "Veo" in ei.value.detail


def test_preflight_ignores_the_language_model_for_an_english_cast(wired,
                                                                  monkeypatch):
    """The mirror case: an English-only cast must not be blocked by a key it
    will never use. (Kling and Veo happen to share the fal provider, so the
    run is moved onto a non-fal model to make the two separable.)"""
    lang_model = api.runner_media.SPOKEN_LANGUAGE_VIDEO_MODEL
    missing = api.runner_media.VIDEO_MODELS[lang_model]["provider"]
    wired["states"]["re_parent"]["video_model"] = "grok-imagine"
    for e in wired["states"]["re_parent"]["scenes"]:
        e.pop("video_model", None)
    monkeypatch.setattr(type(settings), "has_provider",
                        lambda self, p: p != missing)
    _rerun("re_parent", {"character_ids": ["ch_new_en"],
                         "scenes": _rows(_plan())})


# --------------------------------------------------------------- files are copied, not referenced


def test_end_frame_is_copied_into_the_child_run_dir(wired):
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())})
    st = wired["states"][view["re_id"]]
    pose = Path(st["scenes"][1]["end_frame_path"])
    assert pose.exists() and pose.read_bytes() == b"end-pose-bytes"
    assert view["re_id"] in pose.parts and "re_parent" not in pose.parts


def test_deleting_the_parent_leaves_the_child_intact(wired):
    """No refcount anywhere is cross-run, and DELETE rmtree's the whole run
    dir — a referenced pose would vanish SILENTLY (`_resolve_end_image` just
    returns None for a missing file)."""
    import shutil
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())})
    st = wired["states"][view["re_id"]]
    shutil.rmtree(wired["out"] / "reengineer" / "re_parent")
    assert Path(st["scenes"][1]["end_frame_path"]).exists()
    for e in st["scenes"]:
        assert Path(settings.scenes_dir / f"{e['scene_id']}.png").exists()


def test_keep_end_frame_false_drops_the_pose(wired):
    rows = _rows(_plan())
    rows[1]["keep_end_frame"] = False
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"], "scenes": rows})
    assert "end_frame_path" not in wired["states"][view["re_id"]]["scenes"][1]


def test_missing_scene_file_refuses_loudly(wired):
    (wired["scenes_dir"] / f"{wired['sids'][1]}.png").unlink()
    with pytest.raises(HTTPException) as ei:
        _rerun("re_parent", {"character_ids": ["ch_new_en"],
                             "scenes": _rows(_plan())})
    assert ei.value.status_code == 400
    assert "saknas" in ei.value.detail
    # …and the half-built run dir is cleaned up, not left as a ghost.
    dirs = {p.name for p in (wired["out"] / "reengineer").iterdir()}
    assert dirs == {"re_parent"}


# --------------------------------------------------------------- editing the plan


def test_scenes_can_be_subset_reordered_and_edited(wired):
    plan = _plan()
    rows = _rows(plan)
    picked = [rows[2], rows[0]]
    picked[0]["motion_prompt"] = "Helt ny prompt"
    picked[0]["secs"] = 12
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": picked})
    st = wired["states"][view["re_id"]]
    assert st["n_scenes"] == 2
    assert [e["idx"] for e in st["scenes"]] == [0, 1]
    assert [e["scene_id"] for e in st["scenes"]] == [wired["sids"][2],
                                                     wired["sids"][0]]
    assert st["scenes"][0]["motion_prompt"] == "Helt ny prompt"
    assert st["scenes"][0]["kling_secs"] == 12
    assert st["scenes"][0]["duration"] == 12.0


def test_speech_is_recomputed_from_the_edited_prompt(wired):
    """`speech` feeds the caption script, the STT bias hint and QC's expected
    line — carrying the parent's stale value would caption a line the clip
    never says."""
    rows = _rows(_plan())
    rows[0]["motion_prompt"] = 'She says to the camera: "En helt annan replik."'
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"], "scenes": rows})
    assert wired["states"][view["re_id"]]["scenes"][0]["speech"] == (
        "En helt annan replik.")


def test_settings_are_inherited_unless_overridden(wired):
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())})
    st = wired["states"][view["re_id"]]
    assert (st["use_director"], st["auto_telegram_send"],
            st["outfit_mode"], st["background_source"]) == (
        True, True, "character", "character")

    view2 = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                 "scenes": _rows(_plan()),
                                 "use_director": False,
                                 "auto_telegram_send": False,
                                 "skip_qc": True,
                                 "outfit_mode": "scene"})
    st2 = wired["states"][view2["re_id"]]
    assert (st2["use_director"], st2["auto_telegram_send"],
            st2["skip_qc"], st2["outfit_mode"]) == (False, False, True, "scene")


def test_two_person_is_masked_off_a_direct_row(wired):
    rows = _rows(_plan())
    rows[1]["direct"] = True          # 👥 is meaningless without a per-char frame
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"], "scenes": rows})
    e = wired["states"][view["re_id"]]["scenes"][1]
    assert e["is_direct"] is True and "two_person" not in e


def test_repeated_scene_id_gets_its_own_id_and_file(wired):
    """Two entries sharing one scene_id silently merge their flags and leave a
    direct row waiting forever on its twin's clip."""
    wired["states"]["re_parent"]["scenes"][1]["scene_id"] = wired["sids"][0]
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())})
    st = wired["states"][view["re_id"]]
    ids = [e["scene_id"] for e in st["scenes"]]
    assert len(set(ids)) == 3
    assert "__dup" in ids[1]
    assert (settings.scenes_dir / f"{ids[1]}.png").exists()


def test_duplicated_scene_with_no_file_of_its_own_is_re_registered(wired):
    """A ⧉-duplicated scene (`…__dup<hex>`) has NO file at
    `scenes_dir/<sid>.png` — _apply_scene_duplicate points Job.scene_image_paths
    at the SOURCE's path instead. 215 of 758 scenes in the real store are like
    this. The child's job derives the canonical path, so inheriting the id
    verbatim would hand it a path that does not exist and fail every variant,
    per character, at generation time."""
    base = wired["sids"][0]
    dup = f"{base}__dupabc123"
    wired["states"]["re_parent"]["scenes"][1]["scene_id"] = dup
    job = wired["box"]["jobs"]["j_parent"]
    job.scene_ids = [base, dup, wired["sids"][2]]
    job.scene_image_paths = [str(settings.scenes_dir / f"{base}.png"),
                             str(settings.scenes_dir / f"{base}.png"),
                             str(settings.scenes_dir / f"{wired['sids'][2]}.png")]
    assert not (settings.scenes_dir / f"{dup}.png").exists()

    # The modal offers it rather than greying it out…
    plan = _plan()
    assert plan["scenes"][1]["missing_file"] is False

    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(plan)})
    st = wired["states"][view["re_id"]]
    new_sid = st["scenes"][1]["scene_id"]
    # …and the child gets a fresh id whose canonical file really exists,
    # minted off the BASE so duplicate chains don't grow a __dupA__dupB tail.
    assert new_sid != dup and new_sid.startswith(base + "__dup")
    assert "__dupabc123" not in new_sid
    assert (settings.scenes_dir / f"{new_sid}.png").read_bytes() == \
        (settings.scenes_dir / f"{base}.png").read_bytes()


def test_scene_file_falls_back_to_the_job_path_then_the_base_id(wired):
    base = wired["sids"][0]
    dup = f"{base}__dupdeadbe"
    job = wired["box"]["jobs"]["j_parent"]
    job.scene_ids = [dup]
    job.scene_image_paths = [str(settings.scenes_dir / f"{base}.png")]
    assert rerun_mod.scene_file(dup, job) == settings.scenes_dir / f"{base}.png"
    # Even with no job at all, the __dup chain resolves to its base image.
    assert rerun_mod.scene_file(dup, None) == settings.scenes_dir / f"{base}.png"
    # A genuinely unknown scene still resolves to nothing (→ loud refusal).
    assert rerun_mod.scene_file("sc_nope", job) is None


def test_empty_cast_and_empty_scenes_are_refused(wired):
    with pytest.raises(HTTPException) as ei:
        _rerun("re_parent", {"character_ids": [], "scenes": _rows(_plan())})
    assert ei.value.status_code == 400
    with pytest.raises(HTTPException) as ei:
        _rerun("re_parent", {"character_ids": ["ch_new_en"], "scenes": []})
    assert ei.value.status_code == 400


def test_unknown_character_is_404(wired):
    with pytest.raises(HTTPException) as ei:
        _rerun("re_parent", {"character_ids": ["ch_nope"],
                             "scenes": _rows(_plan())})
    assert ei.value.status_code == 404


def test_unknown_parent_run_is_404(wired):
    with pytest.raises(HTTPException) as ei:
        _rerun("re_nope", {"character_ids": ["ch_new_en"], "scenes": []})
    assert ei.value.status_code == 404


def test_child_is_always_an_image_run_and_is_scheduled(wired):
    """`resume_all` dispatches on `from_images`; a child cloned from a VIDEO
    run has no source_path, so a False here would send it through the video
    path on restart and KeyError."""
    wired["states"]["re_parent"]["from_images"] = False
    wired["states"]["re_parent"]["source_path"] = "/tmp/parent.mp4"
    bg = BackgroundTasks()
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())}, bg=bg)
    st = wired["states"][view["re_id"]]
    assert st["from_images"] is True
    assert "source_path" not in st
    # The run is scheduled on the IMAGE entry point (BackgroundTasks only
    # queues — FastAPI runs it after the response).
    assert [t.args for t in bg.tasks] == [
        (runner_reengineer.run_reengineer_from_images, view["re_id"])]


# --------------------------------------------------------------- 📌 direct-clip reuse


def _make_direct(wired, *, with_clip=True):
    sid = wired["sids"][0]
    e = wired["states"]["re_parent"]["scenes"][0]
    e["is_direct"] = True
    e["direct_image_path"] = str(settings.scenes_dir / f"{sid}.png")
    if with_clip:
        clip = wired["out"] / "reengineer" / "re_parent" / f"direct_clip_{sid}.mp4"
        clip.parent.mkdir(parents=True, exist_ok=True)
        clip.write_bytes(b"parent-direct-clip")
        e["shared_clip_path"] = str(clip)
    return sid


def test_direct_clip_is_reused_by_copy(wired):
    _make_direct(wired)
    assert _plan()["scenes"][0]["has_direct_clip"] is True
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())})
    e = wired["states"][view["re_id"]]["scenes"][0]
    clip = Path(e["shared_clip_path"])
    assert e["direct_clip_reused"] is True
    assert clip.read_bytes() == b"parent-direct-clip"
    # COPIED: a full animate on the parent overwrites its own clip in place,
    # which would otherwise silently change this run's final.
    assert view["re_id"] in clip.parts and "re_parent" not in clip.parts


def test_reuse_can_be_declined_per_scene(wired):
    _make_direct(wired)
    rows = _rows(_plan())
    rows[0]["reuse_direct_clip"] = False
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"], "scenes": rows})
    e = wired["states"][view["re_id"]]["scenes"][0]
    assert e["is_direct"] is True
    assert not e.get("shared_clip_path")
    assert "direct_clip_reused" not in e


def test_animate_keeps_a_reused_clip_but_rerenders_a_normal_one(wired):
    """The real `_do_animate` reset, which nulls every direct scene's clip at
    phase start — that is what would silently re-bill an inherited clip."""
    sid = _make_direct(wired)
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())})
    scenes = wired["states"][view["re_id"]]["scenes"]
    # A second direct scene that this run rendered ITSELF must still re-render.
    scenes[1]["is_direct"] = True
    scenes[1]["shared_clip_path"] = "/tmp/other.mp4"

    runner_reengineer._reset_direct_clips(scenes)

    assert scenes[0]["direct_clip_reused"] is True
    assert Path(scenes[0]["shared_clip_path"]).read_bytes() == b"parent-direct-clip"
    assert scenes[1]["shared_clip_path"] is None
    # …and only the re-rendering one is handed to _render_direct_clip.
    to_render = [e["scene_id"] for e in scenes
                 if e.get("is_direct") and not e.get("direct_clip_reused")]
    assert to_render == [wired["sids"][1]]
    assert sid not in to_render


def test_reuse_marker_falls_back_to_a_rerender_when_the_file_is_gone(wired):
    """A vanished clip must degrade to a normal re-render, never to a
    permanent waitable coverage gap that blocks the build forever."""
    _make_direct(wired)
    view = _rerun("re_parent", {"character_ids": ["ch_new_en"],
                                "scenes": _rows(_plan())})
    scenes = wired["states"][view["re_id"]]["scenes"]
    Path(scenes[0]["shared_clip_path"]).unlink()

    runner_reengineer._reset_direct_clips(scenes)

    assert scenes[0]["shared_clip_path"] is None
    assert "direct_clip_reused" not in scenes[0]


def test_redo_of_a_direct_scene_clears_the_reuse_marker(wired):
    """An explicit redo is a request for a NEW take, so the clip stops being
    inherited — otherwise a later full animate would skip re-rendering this
    run's own clip. The redo path and the animate reset share
    `_clear_direct_clip`, so this covers both."""
    entry = {"is_direct": True, "shared_clip_path": "/tmp/x.mp4",
             "direct_clip_reused": True, "direct_error": "gammalt fel"}
    runner_reengineer._clear_direct_clip(entry)
    # Nulled, not deleted: the crash-resume revival condition is
    # `is_direct and not shared_clip_path`.
    assert entry["shared_clip_path"] is None
    assert "direct_clip_reused" not in entry and "direct_error" not in entry
    src = Path(runner_reengineer.__file__).read_text(encoding="utf-8")
    assert "_clear_direct_clip(entries[i])" in src
