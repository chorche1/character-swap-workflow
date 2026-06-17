"""Swap tab = Reengineer from reference IMAGES (no source video).

POST /api/reengineer/from_images builds one scene per uploaded image with the
user's manual motion+dialogue prompt and manual Kling clip length, then hands
off to the SAME swap/animate/assemble pipeline as the video flow (locked to
kling-v3). These tests cover the new entry point + the crash-resume routing;
the downstream swap/animate/assemble is the existing, separately-tested flow.
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from character_swap import api, runner_reengineer


@pytest.fixture
def wired(monkeypatch, tmp_path):
    box = {"states": {}, "scenes": {}, "jobs": {}}

    class _S:
        def get_character(self, cid):
            return object() if cid in {"cA", "cB"} else None

        def get_scene(self, sid):
            return box["scenes"].get(sid)

        def add_scene(self, scene):
            box["scenes"][scene.scene_id] = scene

        def add_job(self, job):
            box["jobs"][job.job_id] = job

        def get_job(self, jid):
            return box["jobs"].get(jid)

        def update_job(self, j):
            box["jobs"][j.job_id] = j
    monkeypatch.setattr(api, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())

    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)
    for mod in (reengineer_mod, runner_reengineer.reengineer):
        monkeypatch.setattr(mod, "load_state", load_state)
        monkeypatch.setattr(mod, "save_state", save_state)
    monkeypatch.setattr(reengineer_mod, "reengineer_dir", lambda rid: tmp_path / rid)
    monkeypatch.setattr(type(api.settings), "scenes_dir",
                        property(lambda self: tmp_path / "library"), raising=False)
    # Provider always available in tests — isolates the from_images logic from
    # which API keys happen to be set in the environment.
    monkeypatch.setattr(type(api.settings), "has_provider",
                        lambda self, p: True)
    return box


def _upload(name: str, data: bytes = b"png-bytes") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=name)


def _call(files, motion, lengths, **kw):
    bg = BackgroundTasks()
    out = asyncio.run(api.reengineer_from_images(
        bg, files=files,
        motion_prompts=json.dumps(motion), lengths=json.dumps(lengths),
        direct=kw.get("direct", "[]"),
        character_ids=kw.get("character_ids", json.dumps(["cA"])),
        image_model=kw.get("image_model", "gpt2-id-swap"),
        outfit_mode=kw.get("outfit_mode", "scene"),
        outfit_text=kw.get("outfit_text", ""),
        auto_mode=kw.get("auto_mode", False),
        use_director=kw.get("use_director", False),
        background_file=kw.get("background_file"),
        character_source_image_ids=kw.get("character_source_image_ids", ""),
    ))
    return out, bg


def test_from_images_creates_run(wired):
    out, bg = _call(
        [_upload("a.png"), _upload("b.png")],
        ['She says enthusiastically to the camera: "Try this today."', "He waves"],
        [5, 7])
    saved = wired["states"][out["re_id"]]
    assert saved["from_images"] is True
    assert "source_path" not in saved           # no video
    assert saved["video_model"] == "kling-v3"   # locked
    assert saved["status"] == "queued"
    assert saved["n_scenes"] == 2 and len(saved["scenes"]) == 2
    s0, s1 = saved["scenes"]
    assert s0["idx"] == 0 and s1["idx"] == 1
    assert s0["source"] == "image" and s0["scene_id"].startswith("sc_")
    assert s0["kling_secs"] == 5 and s1["kling_secs"] == 7   # manual length
    assert s0["motion_prompt"].startswith("She says enthusiastically")
    assert len(bg.tasks) == 1                    # the from_images runner queued


def test_blank_prompt_and_length_get_defaults(wired):
    out, _ = _call([_upload("a.png")], [""], [0])
    sc = wired["states"][out["re_id"]]["scenes"][0]
    # Hugo 2026-06-17: a blank row keeps the Kling prompt EMPTY (no preset);
    # only the length still falls back to the 5s default.
    assert sc["motion_prompt"] == ""
    assert sc["kling_secs"] == 5                 # length 0 → default 5s
    assert sc["duration"] == 5.0


def test_manual_length_flows_to_kling_duration():
    # The endpoint sets kling_secs = _clamp_kling(length); _kling_duration honors it.
    assert runner_reengineer._clamp_kling(4) == 4
    assert runner_reengineer._clamp_kling(20) == 15        # clamp ceiling
    assert runner_reengineer._clamp_kling(1) == 3          # clamp floor
    assert runner_reengineer._kling_duration({"kling_secs": 4, "duration": 99}) == 4


def test_speech_derived_from_says_clause(wired):
    out, _ = _call([_upload("a.png"), _upload("b.png")],
                   ['He says: "drink this now"', "She just smiles"], [5, 5])
    s0, s1 = wired["states"][out["re_id"]]["scenes"]
    assert s0["speech"] == "drink this now"
    assert s1["speech"] == ""                    # no says-clause


def test_no_openai_key_required(wired, monkeypatch):
    # The video flow 503s without a Whisper key; image runs must NOT.
    monkeypatch.setattr(type(api.settings), "openai_api_key",
                        property(lambda self: ""), raising=False)
    out, _ = _call([_upload("a.png")], ["wave"], [5])
    assert out["from_images"] is True


def test_validation_errors(wired):
    with pytest.raises(HTTPException) as e:            # length array mismatch
        _call([_upload("a.png"), _upload("b.png")], ["one"], [5])
    assert e.value.status_code == 400

    with pytest.raises(HTTPException) as e:            # unknown image_model
        _call([_upload("a.png")], ["x"], [5], image_model="no-such-model")
    assert e.value.status_code == 400

    with pytest.raises(HTTPException) as e:            # no characters
        _call([_upload("a.png")], ["x"], [5], character_ids=json.dumps([]))
    assert e.value.status_code == 400

    with pytest.raises(HTTPException) as e:            # unknown character
        _call([_upload("a.png")], ["x"], [5], character_ids=json.dumps(["zzz"]))
    assert e.value.status_code == 404


def test_do_create_from_images_calls_create_job_and_swap(wired, monkeypatch):
    captured = {}

    async def fake_create(re_id, state, scene_entries, job_id):
        captured.update(re_id=re_id, entries=scene_entries, job_id=job_id)
    monkeypatch.setattr(runner_reengineer, "_create_job_and_swap", fake_create)

    state = {"re_id": "re_x", "from_images": True, "status": "queued",
             "job_id": None,
             "scenes": [{"idx": 0, "scene_id": "sc_1", "kling_secs": 7,
                         "motion_prompt": "p", "duration": 7.0}]}
    wired["states"]["re_x"] = dict(state)
    asyncio.run(runner_reengineer._do_create_from_images("re_x", dict(state)))
    assert captured["job_id"].startswith("j_")
    assert captured["entries"][0]["kling_secs"] == 7
    # job_id persisted FIRST (crash-resume contract).
    assert wired["states"]["re_x"]["job_id"] == captured["job_id"]


def test_analyze_guard_routes_image_runs(wired, monkeypatch):
    routed = {}

    async def fake_create(re_id, state):
        routed["re_id"] = re_id
    monkeypatch.setattr(runner_reengineer, "_do_create_from_images", fake_create)
    # No source_path — would KeyError if the guard didn't reroute.
    state = {"re_id": "re_y", "from_images": True, "status": "queued"}
    asyncio.run(runner_reengineer._do_analyze_and_swap("re_y", state))
    assert routed["re_id"] == "re_y"


def test_frontend_wiring_present():
    """Guard the Swap-tab → from_images wiring against accidental refactors."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    app_js = (root / "web" / "app.js").read_text(encoding="utf-8")
    assert "submitSwapFromImages" in app_js
    assert "'/api/reengineer/from_images'" in app_js
    index = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert "submitSwapFromImages()" in index
    # The run-card is shared between Swap + Reengineer, filtered by from_images.
    assert "x.from_images" in index


def test_swap_engine_picker_reasserts_identity_first():
    """Hugo 2026-06-17 bug: the Swap-tab <select x-model="swapFromImages.
    imageModel"> renders before its x-for options exist, so the browser
    auto-selected the first option ('GPT Image' = scene-first gpt-image) and
    wrote it back over the gpt2-id-swap default — the user saw the Swap flow
    silently run scene-first. loadGenModels must re-assert it (bounce through
    '' then $nextTick to gpt2-id-swap), exactly like the Reengineer picker."""
    from pathlib import Path
    app_js = (Path(__file__).resolve().parent.parent / "web" / "app.js"
              ).read_text(encoding="utf-8")
    # The state default is identity-first…
    assert "imageModel: 'gpt2-id-swap'" in app_js
    # …AND it is re-asserted after the models load (the <select> fix).
    assert "this.swapFromImages.imageModel = ''" in app_js
    assert "this.swapFromImages.imageModel = swTarget" in app_js
    assert "_avail('gpt2-id-swap') ? 'gpt2-id-swap'" in app_js
    # …AND the pick is sticky like the Reengineer one.
    assert "this.$watch('swapFromImages.imageModel'" in app_js


def test_resume_routes_from_images(wired, monkeypatch):
    spawned = []

    def fake_spawn(coro, name):
        spawned.append(coro.cr_code.co_name)
        coro.close()
    monkeypatch.setattr(runner_reengineer, "_spawn", fake_spawn)
    monkeypatch.setattr(runner_reengineer.reengineer, "list_states",
                        lambda: [{"re_id": "re_z", "status": "queued",
                                  "from_images": True}])
    asyncio.run(runner_reengineer.resume_all())
    assert spawned == ["run_reengineer_from_images"]
