"""Auto-finalize (Hugo 2026-07-19): when the video phase finishes with EVERY
approved character's clips successful, automatically compile the Step-6 finals
AND push them to Drive — no manual clicks. A per-run checkbox (default ON)
gates it. Two flows:

  • classic Swap  → auto_finalize.finalize_swap_job (compile + push)
  • Reengineer    → auto_finalize.push_reengineer_finals (push only; assemble
                    already builds the finals)

The gate is strict ("alla videor blir lyckade"): a single failed/pending clip
holds the chain so a manual retry decides, never shipping a final that dropped
a scene. Drive-auth loss is loud (phone push) + non-fatal (finals are kept).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import auto_finalize, drive_push
from character_swap.clients import google_drive
from character_swap.models import (Job, JobCharacter, VideoStatus, VideoVariant)


def _run(coro):
    return asyncio.run(coro)


async def _anoop(*a, **k):
    return None


def _vid(vid: str, status: str, path: str | None) -> VideoVariant:
    return VideoVariant(video_id=vid, grok_job_id="g-" + vid,
                        source_variant_id="var1",
                        status=VideoStatus(status), final_video_path=path)


def _char(cid: str, name: str, *, clips, path: str | None,
          approved: bool = True, compile_status=None,
          compiled_path=None) -> JobCharacter:
    """clips = list of (video_id, status) tuples; `path` = each clip's file."""
    jc = JobCharacter(char_id=cid, name=name, source_image_path="x.png")
    if approved:
        jc.approved_variant_ids = ["var1"]
    jc.videos = [_vid(v, st, path) for (v, st) in clips]
    jc.compile_status = compile_status
    jc.compiled_video_path = compiled_path
    return jc


def _job(tmp_path, *, chars, from_reengineer=False, auto=True,
         title="Honey run") -> Job:
    return Job(job_id="j1", title=title, scene_id="s1",
               scene_ids=["s1"], scene_image_path="scene.png",
               origin="reengineer:re1" if from_reengineer else None,
               auto_compile_push=auto,
               characters={jc.char_id: jc for jc in chars})


class _FakeStore:
    def __init__(self, job=None, chars=None):
        self.job = job
        self.updated = []
        self._chars = chars or {}

    def get_job(self, job_id):
        return self.job if self.job and self.job.job_id == job_id else None

    def update_job(self, job):
        self.updated.append(job)

    def get_character(self, char_id):
        return self._chars.get(char_id)


def _patch_drive(monkeypatch):
    """Records folders + every upload; used by both Swap + Reengineer paths."""
    calls = {"uploads": [], "folders": []}

    def fake_ensure(names):
        calls["folders"].append(list(names))
        return "leaf"

    def fake_upload(source, *, drive_filename, folder_id, file_id=None):
        calls["uploads"].append({"file": Path(source),
                                 "filename": drive_filename,
                                 "prior_file_id": file_id})
        return {"id": "d-" + drive_filename, "name": drive_filename,
                "webViewLink": "https://drive/" + drive_filename}

    monkeypatch.setattr(google_drive, "ensure_folder_path", fake_ensure)
    monkeypatch.setattr(google_drive, "upload_or_replace", fake_upload)
    return calls


# ------------------------------------------------------ the success gate

def test_all_videos_successful_true_when_all_done(tmp_path):
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path="/a.mp4"),
        _char("c2", "Ravi", clips=[("v2", "done")], path="/b.mp4"),
    ])
    assert auto_finalize._all_videos_successful(job) is True


def test_all_videos_successful_false_on_one_failed(tmp_path):
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path="/a.mp4"),
        _char("c2", "Ravi", clips=[("v2", "failed")], path="/b.mp4"),
    ])
    assert auto_finalize._all_videos_successful(job) is False


def test_all_videos_successful_false_on_pending(tmp_path):
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done"), ("v1b", "pending")],
              path="/a.mp4"),
    ])
    assert auto_finalize._all_videos_successful(job) is False


def test_all_videos_successful_false_when_no_approvals(tmp_path):
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path="/a.mp4",
              approved=False),
    ])
    assert auto_finalize._all_videos_successful(job) is False


def test_all_videos_successful_false_when_approved_no_videos(tmp_path):
    jc = JobCharacter(char_id="c1", name="Chang", source_image_path="x.png")
    jc.approved_variant_ids = ["var1"]        # approved but nothing rendered
    job = _job(tmp_path, chars=[jc])
    assert auto_finalize._all_videos_successful(job) is False


# ------------------------------------------- finalize_swap_job gating

def _spy_compile_and_push(monkeypatch):
    seen = {"compiled": None, "pushed": None}

    async def fake_compile(job_id, **kw):
        seen["compiled"] = (job_id, kw)

    async def fake_push(job_id):
        seen["pushed"] = job_id

    monkeypatch.setattr(auto_finalize.runner_compile, "compile_job_videos",
                        fake_compile)
    monkeypatch.setattr(auto_finalize, "_push_job_finals", fake_push)
    monkeypatch.setattr(auto_finalize, "_emit", _anoop)
    return seen


def test_finalize_swap_job_compiles_and_pushes_on_all_success(monkeypatch, tmp_path):
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path=str(tmp_path / "a.mp4")),
    ])
    (tmp_path / "a.mp4").write_bytes(b"v")
    monkeypatch.setattr(auto_finalize, "store", lambda: _FakeStore(job))
    seen = _spy_compile_and_push(monkeypatch)

    _run(auto_finalize.finalize_swap_job("j1"))

    assert seen["compiled"] is not None
    jid, kw = seen["compiled"]
    assert jid == "j1"
    # Uses the frontend defaults (voice swap ON) for a never-compiled job.
    assert kw["enable_voice_swap"] is True
    assert kw["template"] == "capcut-bluebox"
    assert seen["pushed"] == "j1"


def test_finalize_swap_job_noop_when_flag_off(monkeypatch, tmp_path):
    job = _job(tmp_path, auto=False, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path=str(tmp_path / "a.mp4")),
    ])
    (tmp_path / "a.mp4").write_bytes(b"v")
    monkeypatch.setattr(auto_finalize, "store", lambda: _FakeStore(job))
    seen = _spy_compile_and_push(monkeypatch)

    _run(auto_finalize.finalize_swap_job("j1"))
    assert seen["compiled"] is None and seen["pushed"] is None


def test_finalize_swap_job_noop_for_reengineer_job(monkeypatch, tmp_path):
    job = _job(tmp_path, from_reengineer=True, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path=str(tmp_path / "a.mp4")),
    ])
    (tmp_path / "a.mp4").write_bytes(b"v")
    assert job.from_reengineer is True
    monkeypatch.setattr(auto_finalize, "store", lambda: _FakeStore(job))
    seen = _spy_compile_and_push(monkeypatch)

    _run(auto_finalize.finalize_swap_job(job.job_id))
    assert seen["compiled"] is None and seen["pushed"] is None


def test_finalize_swap_job_noop_when_a_clip_failed(monkeypatch, tmp_path):
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path=str(tmp_path / "a.mp4")),
        _char("c2", "Ravi", clips=[("v2", "failed")], path=str(tmp_path / "b.mp4")),
    ])
    (tmp_path / "a.mp4").write_bytes(b"v")
    monkeypatch.setattr(auto_finalize, "store", lambda: _FakeStore(job))
    seen = _spy_compile_and_push(monkeypatch)

    _run(auto_finalize.finalize_swap_job("j1"))
    assert seen["compiled"] is None and seen["pushed"] is None


def test_finalize_swap_job_idempotent_when_already_compiled(monkeypatch, tmp_path):
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path=str(tmp_path / "a.mp4"),
              compile_status="done", compiled_path=str(tmp_path / "a.mp4")),
    ])
    (tmp_path / "a.mp4").write_bytes(b"v")
    monkeypatch.setattr(auto_finalize, "store", lambda: _FakeStore(job))
    seen = _spy_compile_and_push(monkeypatch)

    _run(auto_finalize.finalize_swap_job("j1"))
    assert seen["compiled"] is None       # not rebuilt on a duplicate trigger


def test_finalize_swap_job_skips_when_compile_in_flight(monkeypatch, tmp_path):
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path=str(tmp_path / "a.mp4"),
              compile_status="compiling"),
    ])
    (tmp_path / "a.mp4").write_bytes(b"v")
    monkeypatch.setattr(auto_finalize, "store", lambda: _FakeStore(job))
    seen = _spy_compile_and_push(monkeypatch)

    _run(auto_finalize.finalize_swap_job("j1"))
    assert seen["compiled"] is None       # don't race a manual compile


# ------------------------------------------- _push_job_finals

def test_push_job_finals_pushes_and_persists(monkeypatch, tmp_path):
    f1 = tmp_path / "c1.mp4"; f1.write_bytes(b"a")
    f2 = tmp_path / "c2.mp4"; f2.write_bytes(b"b")
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path=str(f1),
              compile_status="done", compiled_path=str(f1)),
        _char("c2", "Ravi", clips=[("v2", "done")], path=str(f2),
              compile_status="done", compiled_path=str(f2)),
    ])
    store = _FakeStore(job)
    monkeypatch.setattr(auto_finalize, "store", lambda: store)
    monkeypatch.setattr(auto_finalize, "_emit", _anoop)
    monkeypatch.setattr(auto_finalize.push, "notify", lambda *a, **k: None)
    calls = _patch_drive(monkeypatch)

    _run(auto_finalize._push_job_finals("j1"))

    names = sorted(u["filename"] for u in calls["uploads"])
    assert names == ["Chang — Honey run [j1].mp4", "Ravi — Honey run [j1].mp4"]
    assert job.characters["c1"].drive_pushes["final"]["file_id"]
    assert job.characters["c2"].drive_pushes["final"]["file_id"]
    assert ["Character Swap", "Chang"] in calls["folders"]
    assert store.updated                      # receipts persisted


def test_push_job_finals_loud_and_bails_when_unauthorized(monkeypatch, tmp_path):
    f1 = tmp_path / "c1.mp4"; f1.write_bytes(b"a")
    job = _job(tmp_path, chars=[
        _char("c1", "Chang", clips=[("v1", "done")], path=str(f1),
              compile_status="done", compiled_path=str(f1)),
    ])
    store = _FakeStore(job)
    monkeypatch.setattr(auto_finalize, "store", lambda: store)
    monkeypatch.setattr(auto_finalize, "_emit", _anoop)

    def raise_unauth(names):
        raise google_drive.DriveNotAuthorized("authorize first")

    monkeypatch.setattr(google_drive, "ensure_folder_path", raise_unauth)
    monkeypatch.setattr(google_drive, "upload_or_replace",
                        lambda *a, **k: pytest.fail("must not upload"))
    notes = []
    monkeypatch.setattr(auto_finalize.push, "notify",
                        lambda *a, **k: notes.append((a, k)))

    _run(auto_finalize._push_job_finals("j1"))

    assert notes                              # loud phone push
    assert "final" not in job.characters["c1"].drive_pushes  # no false receipt
    assert not store.updated


# ------------------------------------------- push_reengineer_finals

def _reengineer_state(tmp_path, *, status="done", auto=True):
    f1 = tmp_path / "final_c1.mp4"; f1.write_bytes(b"a")
    return {"re_id": "re1", "title": "Reengineer 6 bilder", "status": status,
            "auto_drive_push": auto,
            "finals": {"c1": {"status": "done", "final_path": str(f1)}}}


def test_push_reengineer_finals_pushes_and_persists(monkeypatch, tmp_path):
    from character_swap import reengineer as reengineer_mod
    state = _reengineer_state(tmp_path)
    saved = []
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda re_id: state if re_id == "re1" else None)
    monkeypatch.setattr(reengineer_mod, "save_state",
                        lambda st: saved.append(st))

    class _Char:
        name = "Chang"

    monkeypatch.setattr(auto_finalize, "store",
                        lambda: _FakeStore(chars={"c1": _Char()}))
    monkeypatch.setattr(auto_finalize, "_emit", _anoop)
    monkeypatch.setattr(auto_finalize.push, "notify", lambda *a, **k: None)
    calls = _patch_drive(monkeypatch)

    _run(auto_finalize.push_reengineer_finals("re1"))

    assert calls["uploads"][0]["filename"] == "Chang — Reengineer 6 bilder [re1].mp4"
    assert calls["folders"] == [["Character Swap"], ["Character Swap", "Chang"]]
    assert state["finals"]["c1"]["drive"]["file_id"]
    assert saved                              # persisted the receipt


def test_push_reengineer_finals_noop_when_flag_off(monkeypatch, tmp_path):
    from character_swap import reengineer as reengineer_mod
    state = _reengineer_state(tmp_path, auto=False)
    monkeypatch.setattr(reengineer_mod, "load_state", lambda re_id: state)
    monkeypatch.setattr(reengineer_mod, "save_state",
                        lambda st: pytest.fail("must not save"))
    monkeypatch.setattr(google_drive, "upload_or_replace",
                        lambda *a, **k: pytest.fail("must not upload"))
    _run(auto_finalize.push_reengineer_finals("re1"))


def test_push_reengineer_finals_noop_when_partial(monkeypatch, tmp_path):
    from character_swap import reengineer as reengineer_mod
    state = _reengineer_state(tmp_path, status="partial_success")
    monkeypatch.setattr(reengineer_mod, "load_state", lambda re_id: state)
    monkeypatch.setattr(google_drive, "upload_or_replace",
                        lambda *a, **k: pytest.fail("must not upload"))
    _run(auto_finalize.push_reengineer_finals("re1"))


def test_default_compile_settings_match_frontend_seed():
    # The auto compile must reproduce a manual Step-6 click on a fresh job:
    # voice swap ON, bluebox template, WPM normalize OFF (web/app.js seed).
    d = auto_finalize._DEFAULT_COMPILE_SETTINGS
    assert d["enable_voice_swap"] is True
    assert d["template"] == "capcut-bluebox"
    assert d["enable_captions"] is True
    assert d["enable_wpm_normalize"] is False
