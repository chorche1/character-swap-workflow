"""Saved sequences — save finished scenes + clips, reuse them for free later.

A sequence snapshots a hand-picked subset of a finished run: the scene frame,
its prompt/length, and per character the APPROVED image + the FINISHED clip.
Pasting it into a future run materializes those as ordinary approved-image +
done-clip rows, so nothing regenerates. A character the sequence does NOT cover
generates as usual and goes through the approval gate (Hugo 2026-07-29).

These lock the money-critical parts: what gets saved, what a paste costs
(nothing), and that no path silently re-renders a reused clip.
"""
from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from character_swap import api, runner, runner_reengineer, sequences
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


# --------------------------------------------------------------------------- helpers

def _write(p: Path, data: bytes = b"x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


@pytest.fixture
def finished_run(tmp_path, monkeypatch):
    """A 2-scene run, 2 characters, everything approved + rendered."""
    scenes_dir = tmp_path / "library"
    scenes_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(type(api.settings), "scenes_dir",
                        property(lambda self: scenes_dir), raising=False)
    monkeypatch.setattr(type(api.settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)

    job = Job(job_id="j_seq", title="t", scene_id="sc_a",
              scene_image_path=str(scenes_dir / "sc_a.png"),
              scene_ids=["sc_a", "sc_b"],
              scene_image_paths=[str(scenes_dir / "sc_a.png"),
                                 str(scenes_dir / "sc_b.png")],
              origin="reengineer:re_1")
    for sid in ("sc_a", "sc_b"):
        _write(scenes_dir / f"{sid}.png", f"frame-{sid}".encode())

    for cid, name in (("cA", "Alice"), ("cB", "Bob")):
        jc = JobCharacter(char_id=cid, name=name,
                          source_image_path=str(tmp_path / f"{cid}.png"),
                          status=CharStatus.DONE)
        for sid in ("sc_a", "sc_b"):
            vid = f"v_{cid}_{sid}"
            img = _write(tmp_path / "out" / "j_seq" / cid / f"variant_{vid}.png",
                         f"img-{cid}-{sid}".encode())
            clip = _write(tmp_path / "out" / "j_seq" / cid / f"video_{vid}.mp4",
                          f"clip-{cid}-{sid}".encode())
            jc.images.append(GeneratedImage(variant_id=vid, path=str(img),
                                            prompt=f"swap {sid}", scene_id=sid,
                                            status=VariantStatus.READY))
            jc.approved_variant_ids.append(vid)
            jc.videos.append(VideoVariant(
                video_id=f"vd_{cid}_{sid}", grok_job_id="", scene_id=None,
                status=VideoStatus.DONE, source_variant_id=vid,
                final_video_path=str(clip)))
        job.characters[cid] = jc

    state = {
        "re_id": "re_1", "job_id": "j_seq", "status": "done", "from_images": True,
        "scenes": [
            {"idx": 0, "scene_id": "sc_a", "duration": 5.0, "kling_secs": 6,
             "motion_prompt": 'She says: "Hej."', "speech": "Hej.",
             "summary": "scen ett", "source": "image"},
            {"idx": 1, "scene_id": "sc_b", "duration": 4.0, "kling_secs": 5,
             "motion_prompt": "He waves", "speech": "", "summary": "scen två",
             "source": "image"},
        ],
    }
    return {"state": state, "job": job, "tmp": tmp_path, "scenes_dir": scenes_dir}


# --------------------------------------------------------------------------- saving

def test_save_snapshots_only_the_picked_scenes(finished_run):
    seq, notes = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [1], "Min outro")

    assert seq.name == "Min outro"
    assert [sc.origin_scene_id for sc in seq.scenes] == ["sc_b"]
    assert notes == []
    sc = seq.scenes[0]
    # Prompt + length ride along so the pasted row shows what was rendered.
    assert sc.motion_prompt == "He waves"
    assert sc.kling_secs == 5
    assert sorted(sc.clips) == ["cA", "cB"]
    assert seq.chars == {"cA": "Alice", "cB": "Bob"}
    # Every referenced file exists in the sequence's OWN directory.
    assert Path(sc.frame_path).exists()
    for pair in sc.clips.values():
        assert Path(pair.image_path).exists()
        assert Path(pair.clip_path).exists()
        assert sequences.sequence_dir(seq.seq_id) in Path(pair.clip_path).parents


def test_saved_order_follows_the_run_not_the_click_order(finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [1, 0], "Bägge")
    assert [sc.origin_scene_id for sc in seq.scenes] == ["sc_a", "sc_b"]
    assert [sc.order for sc in seq.scenes] == [0, 1]


def test_save_refuses_a_scene_with_no_finished_clip(finished_run):
    # Nobody has a clip for scene 2 → saving it would produce a sequence that
    # contributes nothing. Refuse loudly and name the scene.
    for jc in finished_run["job"].characters.values():
        jc.videos = [v for v in jc.videos if not v.source_variant_id.endswith("sc_b")]
    with pytest.raises(sequences.SequenceError) as e:
        sequences.save_from_run(finished_run["state"], finished_run["job"],
                                [1], "Trasig")
    assert "scen 2" in str(e.value).lower()


def test_save_refuses_a_half_written_sequence_leaves_no_files(finished_run):
    for jc in finished_run["job"].characters.values():
        jc.videos = [v for v in jc.videos if not v.source_variant_id.endswith("sc_b")]
    before = set(sequences.sequences_root().glob("*")) if sequences.sequences_root().exists() else set()
    with pytest.raises(sequences.SequenceError):
        sequences.save_from_run(finished_run["state"], finished_run["job"],
                                [0, 1], "Halv")
    after = set(sequences.sequences_root().glob("*"))
    assert after == before          # scene 1 succeeded but the dir was rolled back


def test_save_skips_a_character_without_a_clip_and_says_so(finished_run):
    finished_run["job"].characters["cB"].videos = []
    seq, notes = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Bara Alice")
    assert sorted(seq.scenes[0].clips) == ["cA"]
    assert any("Bob" in n for n in notes)


def test_save_requires_a_name_and_a_selection(finished_run):
    with pytest.raises(sequences.SequenceError):
        sequences.save_from_run(finished_run["state"], finished_run["job"], [], "x")
    with pytest.raises(sequences.SequenceError):
        sequences.save_from_run(finished_run["state"], finished_run["job"], [0], "  ")


def test_sequence_files_survive_deletion_of_the_source_run(finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Överlevare")
    clip = Path(seq.scenes[0].clips["cA"].clip_path)
    # The source run's whole output dir goes away (job delete / run delete).
    import shutil
    shutil.rmtree(finished_run["tmp"] / "out" / "j_seq")
    assert clip.exists() and clip.read_bytes() == b"clip-cA-sc_a"


def test_list_load_rename_delete_roundtrip(finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Först")
    assert [s.seq_id for s in sequences.list_all()] == [seq.seq_id]
    assert sequences.load(seq.seq_id).name == "Först"
    assert sequences.rename(seq.seq_id, "Sedan").name == "Sedan"
    assert sequences.load(seq.seq_id).name == "Sedan"
    assert sequences.delete(seq.seq_id) is True
    assert sequences.load(seq.seq_id) is None
    assert sequences.delete(seq.seq_id) is False


def test_direct_scene_saves_its_shared_clip(finished_run):
    shared = _write(finished_run["tmp"] / "direct.mp4", b"shared")
    img = _write(finished_run["scenes_dir"] / "sc_d.png", b"direct-frame")
    finished_run["state"]["scenes"].append({
        "idx": 2, "scene_id": "sc_d", "duration": 3.0, "motion_prompt": "logo",
        "summary": "produkt", "is_direct": True,
        "direct_image_path": str(img), "shared_clip_path": str(shared)})
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [2], "Outro")
    sc = seq.scenes[0]
    assert sc.is_direct and sc.clips == {}
    assert Path(sc.direct_clip_path).read_bytes() == b"shared"
    # A direct scene needs no per-character clip — it covers everyone.
    assert sc.covers("someone-brand-new") is True


def test_direct_scene_without_a_rendered_clip_is_refused(finished_run):
    _write(finished_run["scenes_dir"] / "sc_d.png", b"direct-frame")
    finished_run["state"]["scenes"].append({
        "idx": 2, "scene_id": "sc_d", "duration": 3.0, "motion_prompt": "logo",
        "summary": "produkt", "is_direct": True,
        "direct_image_path": str(finished_run["scenes_dir"] / "sc_d.png")})
    with pytest.raises(sequences.SequenceError) as e:
        sequences.save_from_run(finished_run["state"], finished_run["job"],
                                [2], "Outro")
    assert "delat klipp" in str(e.value)


# --------------------------------------------------------------------------- reuse

def _reuse_payload(seq, sc, cid):
    return sequences.reuse_entry(seq, sc, cid)


def test_kick_char_materializes_reused_slots_without_generating(
        finished_run, monkeypatch):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")
    sc = seq.scenes[0]

    job = Job(job_id="j_new", title="t", scene_id="sc_a",
              scene_image_path="x", scene_ids=["sc_a", "sc_new"],
              scene_image_paths=["x", "y"],
              reused_clips={"sc_a": {"cA": _reuse_payload(seq, sc, "cA")}})
    jc = JobCharacter(char_id="cA", name="Alice", source_image_path="s.png")
    job.characters["cA"] = jc

    generated: list[str] = []

    async def _fake_gen(_job, _jc, v, _sem):
        generated.append(v.scene_id)
    monkeypatch.setattr(runner, "_generate_one_variant", _fake_gen)
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_emit", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(runner, "store", lambda: type(
        "S", (), {"update_job": staticmethod(lambda j: None)})())

    asyncio.run(runner._kick_char(job, jc, 1, asyncio.Semaphore(2)))

    # The reused scene generated NOTHING; the other scene generated normally.
    assert generated == ["sc_new"]
    reused_img = next(im for im in jc.images if im.scene_id == "sc_a")
    assert reused_img.status == VariantStatus.READY
    assert reused_img.variant_id in jc.approved_variant_ids   # pre-approved
    assert Path(reused_img.path).read_bytes() == b"img-cA-sc_a"
    clip = runner.pick_clip_for_variant(jc, reused_img.variant_id)
    assert clip is not None and clip.imported
    assert clip.reused_from_sequence == f"{seq.seq_id}:{sc.key}"
    assert Path(clip.final_video_path).read_bytes() == b"clip-cA-sc_a"
    # Staged into the NEW job's own dir, not left pointing at the sequence.
    assert "j_new" in clip.final_video_path


def test_missing_sequence_files_fall_back_to_generating(finished_run, monkeypatch):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")
    payload = _reuse_payload(seq, seq.scenes[0], "cA")
    Path(payload["clip_path"]).unlink()          # sequence deleted mid-flight

    job = Job(job_id="j_new", title="t", scene_id="sc_a", scene_image_path="x",
              scene_ids=["sc_a"], scene_image_paths=["x"],
              reused_clips={"sc_a": {"cA": payload}})
    jc = JobCharacter(char_id="cA", name="Alice", source_image_path="s.png")
    job.characters["cA"] = jc

    generated: list[str] = []

    async def _fake_gen(_job, _jc, v, _sem):
        generated.append(v.scene_id)
    monkeypatch.setattr(runner, "_generate_one_variant", _fake_gen)
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_emit", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(runner, "store", lambda: type(
        "S", (), {"update_job": staticmethod(lambda j: None)})())

    asyncio.run(runner._kick_char(job, jc, 1, asyncio.Semaphore(2)))
    # Never a silent hole in the final: the slot generates instead.
    assert generated == ["sc_a"]


def test_animate_character_never_re_renders_a_reused_clip(monkeypatch):
    job = Job(job_id="j1", title="t", scene_id="sc_a", scene_image_path="x",
              scene_ids=["sc_a", "sc_b"], scene_image_paths=["x", "y"],
              movement_prompts={"sc_a": "p", "sc_b": "p"})
    jc = JobCharacter(char_id="cA", name="Alice", source_image_path="s.png")
    jc.images = [
        GeneratedImage(variant_id="v1", path="a.png", prompt="", scene_id="sc_a",
                       status=VariantStatus.READY),
        GeneratedImage(variant_id="v2", path="b.png", prompt="", scene_id="sc_b",
                       status=VariantStatus.READY),
    ]
    jc.approved_variant_ids = ["v1", "v2"]
    jc.videos = [VideoVariant(video_id="vd1", grok_job_id="",
                              status=VideoStatus.DONE, source_variant_id="v1",
                              final_video_path="c.mp4", imported=True,
                              reused_from_sequence="seq_x:sc00")]
    job.characters["cA"] = jc

    submitted: list[str] = []

    async def _fake_animate(_job, _jc, v, *_a):
        submitted.append(v.source_variant_id)
    monkeypatch.setattr(runner, "_animate_one_video", _fake_animate)
    monkeypatch.setattr(runner, "_persist", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_resolve_end_image",
                        lambda *a, **k: asyncio.sleep(0, result=None))

    asyncio.run(runner._animate_character(job, jc, 1, lambda sid: "prompt"))
    assert submitted == ["v2"]              # only the NON-reused scene rendered
    assert len(jc.videos) == 2              # no extra take appended for v1


def test_all_reused_character_settles_instead_of_hanging(monkeypatch):
    job = Job(job_id="j1", title="t", scene_id="sc_a", scene_image_path="x",
              scene_ids=["sc_a"], scene_image_paths=["x"],
              movement_prompts={"sc_a": "p"})
    jc = JobCharacter(char_id="cA", name="Alice", source_image_path="s.png")
    jc.images = [GeneratedImage(variant_id="v1", path="a.png", prompt="",
                                scene_id="sc_a", status=VariantStatus.READY)]
    jc.approved_variant_ids = ["v1"]
    jc.videos = [VideoVariant(video_id="vd1", grok_job_id="",
                              status=VideoStatus.DONE, source_variant_id="v1",
                              final_video_path="c.mp4", imported=True,
                              reused_from_sequence="seq_x:sc00")]
    job.characters["cA"] = jc
    seen: list = []
    monkeypatch.setattr(runner, "_persist",
                        lambda j, c, **k: seen.append(k.get("status")))
    asyncio.run(runner._animate_character(job, jc, 1, lambda sid: "p"))
    # ANIMATING → DONE, never left stuck mid-phase with nothing to wait for.
    assert seen[-1] == CharStatus.DONE


def test_collect_clips_picks_up_the_reused_clip(tmp_path):
    clip = _write(tmp_path / "reused.mp4", b"c")
    jc = JobCharacter(char_id="cA", name="Alice", source_image_path="s.png")
    jc.images = [GeneratedImage(variant_id="v1", path="a.png", prompt="",
                                scene_id="sc_a", status=VariantStatus.READY)]
    jc.approved_variant_ids = ["v1"]
    jc.videos = [VideoVariant(video_id="vd1", grok_job_id="",
                              status=VideoStatus.DONE, source_variant_id="v1",
                              final_video_path=str(clip), imported=True,
                              reused_from_sequence="seq_x:sc00")]
    state = {"scenes": [{"idx": 0, "scene_id": "sc_a", "motion_prompt": "p",
                         "speech": "Hej."}]}
    clips, dialogues, missing, waitable = runner_reengineer._collect_clips(state, jc)
    assert clips == [clip] and not missing and not waitable
    # No localized override on the reused clip → the scene's own line is used
    # as the caption script hint, exactly like a freshly rendered clip.
    assert dialogues == ["Hej."]


def test_pasted_direct_scene_is_not_re_rendered_by_do_animate(
        tmp_path, monkeypatch):
    """`_do_animate` clears every direct scene's shared clip so a re-animate
    re-renders it — a PASTED direct scene must keep its saved clip."""
    scenes = [
        {"idx": 0, "scene_id": "sc_x", "is_direct": True,
         "shared_clip_path": "/kept.mp4", "reused_direct": True,
         "motion_prompt": "p"},
        {"idx": 1, "scene_id": "sc_y", "is_direct": True,
         "shared_clip_path": "/old.mp4", "motion_prompt": "p"},
    ]
    job = Job(job_id="j1", title="t", scene_id="sc_x", scene_image_path="x",
              scene_ids=["sc_x", "sc_y"], scene_image_paths=["x", "y"],
              direct_scene_ids=["sc_x", "sc_y"])
    job.characters["cA"] = JobCharacter(char_id="cA", name="A",
                                        source_image_path="s.png")
    state = {"re_id": "re_1", "job_id": "j1", "scenes": scenes}
    saved: dict = {}

    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: state)
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda rid, **ch: saved.update(ch) or dict(state, **ch))
    monkeypatch.setattr(runner_reengineer, "store", lambda: type(
        "S", (), {"get_job": staticmethod(lambda jid: job),
                  "update_job": staticmethod(lambda j: None)})())
    monkeypatch.setattr(runner_reengineer.events, "publish",
                        lambda *a, **k: asyncio.sleep(0))
    rendered: list[str] = []

    async def _fake_direct(_re_id, sid):
        rendered.append(sid)
    monkeypatch.setattr(runner_reengineer, "_render_direct_clip", _fake_direct)
    monkeypatch.setattr(runner_reengineer, "_watch_video_phase",
                        lambda *a, **k: asyncio.sleep(0))

    asyncio.run(runner_reengineer._do_animate("re_1", state))

    assert rendered == ["sc_y"]                      # only the normal direct scene
    assert scenes[0]["shared_clip_path"] == "/kept.mp4"
    assert scenes[1]["shared_clip_path"] is None


def test_resolve_reused_clips_skips_a_deleted_sequence(finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Borta")
    entries = [{"scene_id": "sc_a",
                "reused_from": {"seq_id": seq.seq_id, "scene_key": "sc00"}}]
    assert runner_reengineer._resolve_reused_clips(entries, ["cA"])["sc_a"]
    sequences.delete(seq.seq_id)
    # Gone → no reuse map → the scene generates normally instead of failing.
    assert runner_reengineer._resolve_reused_clips(entries, ["cA"]) == {}


# --------------------------------------------------------------------------- API

@pytest.fixture
def wired(monkeypatch, tmp_path, finished_run):
    """The from_images endpoint wired against fakes (mirrors
    test_reengineer_from_images) but sharing `finished_run`'s scene library."""
    box = {"states": {}, "scenes": {}, "jobs": {}}

    class _S:
        def get_character(self, cid):
            return object() if cid in {"cA", "cB", "cC"} else None

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
    monkeypatch.setattr(reengineer_mod, "reengineer_dir",
                        lambda rid: tmp_path / "runs" / rid)
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
        seq_rows=kw.get("seq_rows", "[]"),
        file_rows=kw.get("file_rows", "[]"),
        end_frame_files=kw.get("end_frame_files", []),
        end_frame_idx=kw.get("end_frame_idx", "[]"),
        character_ids=kw.get("character_ids", json.dumps(["cA"])),
        image_model="gpt2-id-swap", outfit_mode="scene", outfit_text="",
        auto_mode=False, use_director=False, background_file=None,
        background_source="character", character_source_image_ids="",
    ))
    return out, bg


def test_paste_row_interleaves_with_uploads(wired, finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")
    # Row 0 = pasted, row 1 = an uploaded image.
    out, _bg = _call(
        [_upload("new.png", b"brand-new")],
        ["", "he waves"], [5, 7],
        seq_rows=json.dumps([{"seq_id": seq.seq_id, "scene_key": "sc00"}, None]),
        file_rows=json.dumps([1]))

    s0, s1 = wired["states"][out["re_id"]]["scenes"]
    assert s0["source"] == "sequence"
    assert s0["reused_from"]["seq_id"] == seq.seq_id
    # The saved prompt/length win — the clip is already rendered.
    assert s0["motion_prompt"] == 'She says: "Hej."'
    assert s0["kling_secs"] == 6
    assert s1["motion_prompt"] == "he waves" and s1["source"] == "image"
    assert [s0["idx"], s1["idx"]] == [0, 1]
    assert out["n_scenes"] == 2


def test_paste_only_run_needs_no_uploads(wired, finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0, 1], "Allt")
    out, _bg = _call(
        [], ["", ""], [5, 5],
        seq_rows=json.dumps([{"seq_id": seq.seq_id, "scene_key": "sc00"},
                             {"seq_id": seq.seq_id, "scene_key": "sc01"}]),
        file_rows=json.dumps([]))
    assert out["n_scenes"] == 2
    assert all(e["source"] == "sequence"
               for e in wired["states"][out["re_id"]]["scenes"])


def test_paste_builds_reused_clips_only_for_run_characters(wired, finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")
    entries = [{"scene_id": "sc_a",
                "reused_from": {"seq_id": seq.seq_id, "scene_key": "sc00"}}]
    # cC was never in the sequence → it must generate + be approved.
    reused = runner_reengineer._resolve_reused_clips(entries, ["cA", "cC"])
    assert sorted(reused["sc_a"]) == ["cA"]


def test_duplicate_pasted_frame_gets_its_own_scene_id(wired, finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")
    out, _bg = _call(
        [], ["", ""], [5, 5],
        seq_rows=json.dumps([{"seq_id": seq.seq_id, "scene_key": "sc00"},
                             {"seq_id": seq.seq_id, "scene_key": "sc00"}]),
        file_rows=json.dumps([]))
    s0, s1 = wired["states"][out["re_id"]]["scenes"]
    # Same frame twice must NOT collapse into one scene (the second row's clip
    # would be silently lost).
    assert s0["scene_id"] != s1["scene_id"]
    assert s1["scene_id"].startswith(s0["scene_id"] + "__dup")


def test_end_frame_on_a_pasted_row_is_refused(wired, finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")
    with pytest.raises(HTTPException) as e:
        _call([_upload("a.png")], ["", ""], [5, 5],
              seq_rows=json.dumps([None, {"seq_id": seq.seq_id,
                                          "scene_key": "sc00"}]),
              file_rows=json.dumps([0]),
              end_frame_files=[_upload("end.png")],
              end_frame_idx=json.dumps([1]))
    assert e.value.status_code == 400
    assert "sparad sekvens" in e.value.detail


def test_unknown_sequence_fails_the_whole_request(wired):
    with pytest.raises(HTTPException) as e:
        _call([], [""], [5],
              seq_rows=json.dumps([{"seq_id": "seq_nope", "scene_key": "sc00"}]),
              file_rows=json.dumps([]))
    assert e.value.status_code == 404


def test_file_rows_must_cover_exactly_the_upload_rows(wired, finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")
    with pytest.raises(HTTPException) as e:
        _call([_upload("a.png")], ["", ""], [5, 5],
              seq_rows=json.dumps([{"seq_id": seq.seq_id, "scene_key": "sc00"},
                                   None]),
              file_rows=json.dumps([0]))       # row 0 is the PASTED row
    assert e.value.status_code == 400


def test_arrays_are_indexed_over_rows_not_files(wired, finished_run):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")
    with pytest.raises(HTTPException) as e:
        _call([_upload("a.png")], ["only-one"], [5],   # 1 entry, but 2 rows
              seq_rows=json.dumps([{"seq_id": seq.seq_id, "scene_key": "sc00"},
                                   None]),
              file_rows=json.dumps([1]))
    assert e.value.status_code == 400


def test_without_seq_rows_behaves_exactly_as_before(wired):
    # Back-compat: no seq_rows / file_rows → files map 1:1 onto rows.
    out, _bg = _call([_upload("a.png"), _upload("b.png")],
                     ["one", "two"], [5, 6])
    entries = wired["states"][out["re_id"]]["scenes"]
    assert [e["motion_prompt"] for e in entries] == ["one", "two"]
    assert all("reused_from" not in e for e in entries)


def test_pasted_direct_scene_carries_its_shared_clip(wired, finished_run):
    shared = _write(finished_run["tmp"] / "direct.mp4", b"shared")
    _write(finished_run["scenes_dir"] / "sc_d.png", b"direct-frame")
    finished_run["state"]["scenes"].append({
        "idx": 2, "scene_id": "sc_d", "duration": 3.0, "motion_prompt": "logo",
        "summary": "produkt", "is_direct": True,
        "direct_image_path": str(finished_run["scenes_dir"] / "sc_d.png"),
        "shared_clip_path": str(shared)})
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [2], "Outro")

    out, _bg = _call([], [""], [3],
                     seq_rows=json.dumps([{"seq_id": seq.seq_id,
                                           "scene_key": "sc00"}]),
                     file_rows=json.dumps([]))
    e = wired["states"][out["re_id"]]["scenes"][0]
    assert e["is_direct"] and e["reused_direct"]
    # Staged into the RUN's own dir so deleting the sequence can't break it.
    assert Path(e["shared_clip_path"]).read_bytes() == b"shared"
    assert "runs" in e["shared_clip_path"]


def test_sequence_crud_endpoints(finished_run, monkeypatch):
    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda rid: finished_run["state"] if rid == "re_1" else None)
    monkeypatch.setattr(api, "store", lambda: type(
        "S", (), {"get_job": staticmethod(lambda jid: finished_run["job"])})())

    out = asyncio.run(api.sequence_save(api.SequenceSaveBody(
        re_id="re_1", scene_idxs=[0], name="Via API")))
    seq_id = out["sequence"]["seq_id"]
    assert out["sequence"]["n_scenes"] == 1
    assert out["sequence"]["scenes"][0]["char_ids"] == ["cA", "cB"]
    assert out["sequence"]["scenes"][0]["frame_url"]

    assert any(s["seq_id"] == seq_id for s in asyncio.run(api.sequences_list()))
    renamed = asyncio.run(api.sequence_rename(
        seq_id, api.SequenceRenameBody(name="Nytt namn")))
    assert renamed["name"] == "Nytt namn"
    assert asyncio.run(api.sequence_delete(seq_id)) == {"ok": True}
    with pytest.raises(HTTPException):
        asyncio.run(api.sequence_delete(seq_id))


def test_sequence_save_endpoint_refuses_loudly(finished_run, monkeypatch):
    from character_swap import reengineer as reengineer_mod
    for jc in finished_run["job"].characters.values():
        jc.videos = []
    monkeypatch.setattr(reengineer_mod, "load_state", lambda rid: finished_run["state"])
    monkeypatch.setattr(api, "store", lambda: type(
        "S", (), {"get_job": staticmethod(lambda jid: finished_run["job"])})())
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.sequence_save(api.SequenceSaveBody(
            re_id="re_1", scene_idxs=[0], name="Tom")))
    assert e.value.status_code == 409


def test_edit_mode_paste_attaches_clips_and_queues_only_new_chars(
        wired, finished_run, monkeypatch, tmp_path):
    """The edit-mode paste: covered characters get their finished image + clip
    attached on the spot; an uncovered one is queued for generation (→ gate)."""
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")

    job = Job(job_id="j_live", title="t", scene_id="sc_old",
              scene_image_path="x", scene_ids=["sc_old"],
              scene_image_paths=["x"], origin="reengineer:re_live")
    for cid, name in (("cA", "Alice"), ("cC", "Carol")):
        job.characters[cid] = JobCharacter(char_id=cid, name=name,
                                           source_image_path="s.png")
    wired["jobs"]["j_live"] = job
    wired["states"]["re_live"] = {
        "re_id": "re_live", "job_id": "j_live", "status": "awaiting_assembly",
        "scenes": [{"idx": 0, "scene_id": "sc_old", "motion_prompt": "p",
                    "duration": 5.0, "summary": "gammal"}]}

    queued: list[tuple] = []
    monkeypatch.setattr(api, "_run_async", lambda fn, *a, **k: queued.append((fn, a)))

    bg = BackgroundTasks()
    view = asyncio.run(api.reengineer_paste_sequence(
        "re_live", api.SequencePasteBody(seq_id=seq.seq_id, scene_keys=["sc00"]),
        bg))

    entries = view["scenes"]
    assert len(entries) == 2 and entries[1]["source"] == "sequence"
    new_sid = entries[1]["scene_id"]
    assert new_sid in job.scene_ids            # the job knows the scene
    assert sorted(job.reused_clips[new_sid]) == ["cA"]

    # Alice: image + clip attached, pre-approved, nothing to generate.
    alice = job.characters["cA"]
    img = next(im for im in alice.images if im.scene_id == new_sid)
    assert img.status == VariantStatus.READY
    assert img.variant_id in alice.approved_variant_ids
    clip = runner.pick_clip_for_variant(alice, img.variant_id)
    assert clip.reused_from_sequence == f"{seq.seq_id}:sc00"

    # Carol: nothing attached — she must generate and pass the approval gate.
    assert not [im for im in job.characters["cC"].images if im.scene_id == new_sid]
    bg_calls = [t for t in bg.tasks]
    assert bg_calls, "generation for the uncovered character must be scheduled"


def test_edit_mode_paste_of_a_fully_covered_scene_queues_nothing(
        wired, finished_run, monkeypatch):
    seq, _ = sequences.save_from_run(
        finished_run["state"], finished_run["job"], [0], "Intro")
    job = Job(job_id="j_live", title="t", scene_id="sc_old",
              scene_image_path="x", scene_ids=["sc_old"],
              scene_image_paths=["x"], origin="reengineer:re_live")
    for cid, name in (("cA", "Alice"), ("cB", "Bob")):
        job.characters[cid] = JobCharacter(char_id=cid, name=name,
                                           source_image_path="s.png")
    wired["jobs"]["j_live"] = job
    wired["states"]["re_live"] = {
        "re_id": "re_live", "job_id": "j_live", "status": "done",
        "finals": {"cA": {"status": "done", "final_path": "/f.mp4"}},
        "scenes": [{"idx": 0, "scene_id": "sc_old", "motion_prompt": "p",
                    "duration": 5.0}]}
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_paste_sequence(
        "re_live", api.SequencePasteBody(seq_id=seq.seq_id), bg))
    # Both characters covered → no image generation at all.
    assert bg.tasks == []
    # The existing finals no longer include the new scene → flagged for rebuild.
    assert wired["states"]["re_live"]["finals_stale"] is True


def test_edit_mode_paste_rejects_an_unknown_sequence(wired):
    wired["jobs"]["j_live"] = Job(job_id="j_live", title="t", scene_id="s",
                                  scene_image_path="x", scene_ids=["s"],
                                  scene_image_paths=["x"])
    wired["states"]["re_live"] = {"re_id": "re_live", "job_id": "j_live",
                                  "status": "done", "scenes": [{"idx": 0,
                                                                "scene_id": "s"}]}
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.reengineer_paste_sequence(
            "re_live", api.SequencePasteBody(seq_id="seq_nope"),
            BackgroundTasks()))
    assert e.value.status_code == 404


def test_redo_refuses_a_fully_reused_scene(monkeypatch):
    job = Job(job_id="j1", title="t", scene_id="sc_a", scene_image_path="x",
              scene_ids=["sc_a"], scene_image_paths=["x"])
    jc = JobCharacter(char_id="cA", name="Alice", source_image_path="s.png")
    jc.images = [GeneratedImage(variant_id="v1", path="a.png", prompt="",
                                scene_id="sc_a", status=VariantStatus.READY)]
    jc.approved_variant_ids = ["v1"]
    jc.videos = [VideoVariant(video_id="vd1", grok_job_id="",
                              status=VideoStatus.DONE, source_variant_id="v1",
                              final_video_path="c.mp4", imported=True,
                              reused_from_sequence="seq_x:sc00")]
    job.characters["cA"] = jc
    monkeypatch.setattr(api, "store", lambda: type(
        "S", (), {"get_job": staticmethod(lambda jid: job)})())
    state = {"job_id": "j1", "scenes": [{"idx": 0, "scene_id": "sc_a"}]}
    with pytest.raises(HTTPException) as e:
        api._refuse_fully_reused_scene(state, 0)
    assert e.value.status_code == 409
    assert "sparad sekvens" in e.value.detail


def test_redo_still_allowed_when_a_normal_clip_exists(monkeypatch):
    job = Job(job_id="j1", title="t", scene_id="sc_a", scene_image_path="x",
              scene_ids=["sc_a"], scene_image_paths=["x"])
    reused = JobCharacter(char_id="cA", name="Alice", source_image_path="s.png")
    reused.images = [GeneratedImage(variant_id="v1", path="a.png", prompt="",
                                    scene_id="sc_a", status=VariantStatus.READY)]
    reused.approved_variant_ids = ["v1"]
    reused.videos = [VideoVariant(video_id="vd1", grok_job_id="",
                                  status=VideoStatus.DONE, source_variant_id="v1",
                                  final_video_path="c.mp4", imported=True,
                                  reused_from_sequence="seq_x:sc00")]
    fresh = JobCharacter(char_id="cB", name="Bob", source_image_path="s.png")
    fresh.images = [GeneratedImage(variant_id="v2", path="b.png", prompt="",
                                   scene_id="sc_a", status=VariantStatus.READY)]
    fresh.approved_variant_ids = ["v2"]
    fresh.videos = [VideoVariant(video_id="vd2", grok_job_id="",
                                 status=VideoStatus.DONE, source_variant_id="v2",
                                 final_video_path="d.mp4")]
    job.characters = {"cA": reused, "cB": fresh}
    monkeypatch.setattr(api, "store", lambda: type(
        "S", (), {"get_job": staticmethod(lambda jid: job)})())
    state = {"job_id": "j1", "scenes": [{"idx": 0, "scene_id": "sc_a"}]}
    api._refuse_fully_reused_scene(state, 0)      # Bob's clip is redoable → OK


def test_reused_clip_is_surfaced_in_the_job_view(monkeypatch):
    job = Job(job_id="j1", title="t", scene_id="sc_a", scene_image_path="x",
              scene_ids=["sc_a"], scene_image_paths=["x"])
    jc = JobCharacter(char_id="cA", name="Alice", source_image_path="s.png")
    jc.images = [GeneratedImage(variant_id="v1", path="a.png", prompt="p",
                                scene_id="sc_a", status=VariantStatus.READY)]
    jc.approved_variant_ids = ["v1"]
    jc.videos = [VideoVariant(video_id="vd1", grok_job_id="",
                              status=VideoStatus.DONE, source_variant_id="v1",
                              final_video_path="c.mp4", imported=True,
                              reused_from_sequence="seq_x:sc00")]
    job.characters["cA"] = jc
    view = api._job_to_dict(job)
    assert (view["characters"]["cA"]["videos"][0]["reused_from_sequence"]
            == "seq_x:sc00")
