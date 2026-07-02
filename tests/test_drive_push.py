"""Push-to-Drive (2026-07-02, Hugo's directive).

One-click upload of finished videos: Swap Step-6 + Reengineer finals land in
"Character Swap/<character name>/", Editor finals/repurposed reels in
"Character Swap/Editor/". Re-push OVERWRITES the same Drive file. Failures are
LOUD (409 when write auth is missing, 502 with the reason on upload errors).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from character_swap import api
from character_swap.clients import google_drive
from character_swap.models import Job, JobCharacter


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------ google_drive folder logic

class _FakeFiles:
    """Minimal files() surface: list/create/update recorded per call."""

    def __init__(self, listings):
        # listings: sequence of {"files": [...]} responses for list()
        self._listings = list(listings)
        self.created: list[dict] = []
        self.updated: list[dict] = []

    def list(self, **kw):
        resp = self._listings.pop(0) if self._listings else {"files": []}
        return _Exec(resp)

    def create(self, **kw):
        self.created.append(kw)
        return _Exec({"id": f"created-{len(self.created)}",
                      "name": (kw.get("body") or {}).get("name"),
                      "webViewLink": "https://drive/created"})

    def update(self, **kw):
        self.updated.append(kw)
        return _Exec({"id": kw.get("fileId"), "name": "updated",
                      "webViewLink": "https://drive/updated"})


class _Exec:
    def __init__(self, resp):
        self._resp = resp

    def execute(self):
        return self._resp


class _FakeSvc:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def test_ensure_folder_path_creates_missing_nested(monkeypatch):
    files = _FakeFiles([
        {"files": [{"id": "root-1", "name": "Character Swap"}]},  # root exists
        {"files": []},                                            # char missing
    ])
    monkeypatch.setattr(google_drive, "_write_service", lambda **kw: _FakeSvc(files))
    leaf = google_drive.ensure_folder_path(["Character Swap", "Chang"])
    assert leaf == "created-1"
    body = files.created[0]["body"]
    assert body["name"] == "Chang"
    assert body["parents"] == ["root-1"]
    assert body["mimeType"] == "application/vnd.google-apps.folder"


def test_ensure_folder_path_raises_when_unauthorized(monkeypatch):
    monkeypatch.setattr(google_drive, "_write_service", lambda **kw: None)
    with pytest.raises(google_drive.DriveNotAuthorized):
        google_drive.ensure_folder_path(["Character Swap"])


def test_upload_or_replace_overwrites_same_name(monkeypatch, tmp_path):
    src = tmp_path / "final.mp4"
    src.write_bytes(b"vid")
    files = _FakeFiles([{"files": [{"id": "existing-9"}]}])
    monkeypatch.setattr(google_drive, "_write_service", lambda **kw: _FakeSvc(files))
    res = google_drive.upload_or_replace(
        src, drive_filename="reel.mp4", folder_id="f1")
    assert res["id"] == "existing-9"          # updated in place
    assert files.updated and files.updated[0]["fileId"] == "existing-9"
    assert not files.created


def test_upload_or_replace_creates_when_absent(monkeypatch, tmp_path):
    src = tmp_path / "final.mp4"
    src.write_bytes(b"vid")
    files = _FakeFiles([{"files": []}])
    monkeypatch.setattr(google_drive, "_write_service", lambda **kw: _FakeSvc(files))
    res = google_drive.upload_or_replace(
        src, drive_filename="reel.mp4", folder_id="f1")
    assert res["id"] == "created-1"
    body = files.created[0]["body"]
    assert body == {"name": "reel.mp4", "parents": ["f1"]}


# ------------------------------------------------------- swap push endpoint

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


def _compiled_job(tmp_path) -> Job:
    final = tmp_path / "c1.mp4"
    final.write_bytes(b"vid")
    jc = JobCharacter(char_id="c1", name="Chang",
                      source_image_path="x.png",
                      compiled_video_path=str(final), compile_status="done")
    return Job(job_id="j1", title="Honey run", scene_id="s1",
               scene_ids=["s1"], scene_image_path="scene.png",
               characters={"c1": jc})


def _patch_drive(monkeypatch):
    calls = {}

    def fake_ensure(names):
        calls["folders"] = names
        return "leaf-id"

    def fake_upload(source, *, drive_filename, folder_id, file_id=None):
        calls["file"] = Path(source)
        calls["filename"] = drive_filename
        calls["folder_id"] = folder_id
        calls["prior_file_id"] = file_id
        return {"id": "d1", "name": drive_filename,
                "webViewLink": "https://drive/d1"}

    monkeypatch.setattr(google_drive, "ensure_folder_path", fake_ensure)
    monkeypatch.setattr(google_drive, "upload_or_replace", fake_upload)
    return calls


def test_job_char_drive_push_persists_receipt(monkeypatch, tmp_path):
    job = _compiled_job(tmp_path)
    store = _FakeStore(job)
    monkeypatch.setattr(api, "store", lambda: store)
    calls = _patch_drive(monkeypatch)

    receipt = _run(api.job_char_drive_push(
        "j1", "c1", api.DrivePushBody(variant="final")))

    assert calls["folders"] == ["Character Swap", "Chang"]
    assert calls["filename"] == "Honey run — Chang [j1].mp4"
    assert receipt["url"] == "https://drive/d1"
    assert job.characters["c1"].drive_pushes["final"]["file_id"] == "d1"
    assert store.updated                       # persisted


def test_job_char_drive_push_409_without_compiled_final(monkeypatch, tmp_path):
    job = _compiled_job(tmp_path)
    job.characters["c1"].compile_status = "compiling"
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.job_char_drive_push("j1", "c1", api.DrivePushBody()))
    assert ei.value.status_code == 409


def test_job_char_drive_push_409_when_unauthorized(monkeypatch, tmp_path):
    job = _compiled_job(tmp_path)
    monkeypatch.setattr(api, "store", lambda: _FakeStore(job))

    def raise_unauth(names):
        raise google_drive.DriveNotAuthorized("authorize first")

    monkeypatch.setattr(google_drive, "ensure_folder_path", raise_unauth)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.job_char_drive_push("j1", "c1", api.DrivePushBody()))
    assert ei.value.status_code == 409
    assert "authorize" in str(ei.value.detail)


# ------------------------------------------------- reengineer push endpoint

def test_reengineer_drive_push_uses_char_folder(monkeypatch, tmp_path):
    from character_swap import reengineer as reengineer_mod
    final = tmp_path / "final_c1.mp4"
    final.write_bytes(b"vid")
    state = {"re_id": "re1", "title": "Reengineer 6 bilder",
             "finals": {"c1": {"status": "done", "final_path": str(final)}}}
    saved = []
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda re_id: state if re_id == "re1" else None)
    monkeypatch.setattr(reengineer_mod, "save_state",
                        lambda st: saved.append(st))

    class _Char:
        name = "Chang"

    monkeypatch.setattr(api, "store",
                        lambda: _FakeStore(chars={"c1": _Char()}))
    calls = _patch_drive(monkeypatch)

    receipt = _run(api.reengineer_char_drive_push(
        "re1", "c1", api.DrivePushBody(variant="final")))

    assert calls["folders"] == ["Character Swap", "Chang"]
    assert calls["filename"] == "Reengineer 6 bilder — Chang [re1].mp4"
    assert state["finals"]["c1"]["drive"]["url"] == receipt["url"]
    assert saved                                # state persisted


# ------------------------------------------------------ editor default path

def test_editor_drive_export_defaults_to_shared_editor_folder(monkeypatch, tmp_path):
    from character_swap.config import settings
    edit_dir = settings.output_dir / "editor" / "e1"
    edit_dir.mkdir(parents=True, exist_ok=True)
    (edit_dir / "04-final.mp4").write_bytes(b"vid")
    calls = _patch_drive(monkeypatch)

    out = _run(api.editor_drive_export(
        "e1", api.DriveExportBody(filename="my-reel")))

    assert calls["folders"] == ["Character Swap", "Editor"]
    assert calls["filename"] == "my-reel.mp4"
    assert out["ok"] and out["drive_id"] == "d1"


def test_upload_or_replace_prefers_prior_file_id(monkeypatch, tmp_path):
    # A receipt's file_id survives renames: the SAME Drive file is updated
    # (and renamed) — no by-name search, no duplicate under the new name.
    src = tmp_path / "final.mp4"
    src.write_bytes(b"vid")
    files = _FakeFiles([])
    monkeypatch.setattr(google_drive, "_write_service",
                        lambda **kw: _FakeSvc(files))
    res = google_drive.upload_or_replace(
        src, drive_filename="renamed.mp4", folder_id="f1", file_id="old-3")
    assert res["id"] == "old-3"
    assert files.updated[0]["fileId"] == "old-3"
    assert files.updated[0]["body"] == {"name": "renamed.mp4"}
    assert not files.created


def test_drive_push_uploads_snapshot_not_live_path(monkeypatch, tmp_path):
    """The push source is a stable path a recompile/rebuild overwrites IN
    PLACE while the upload streams for minutes — pushing the live path landed
    a torn mix of old/new bytes on Drive with an ok:True receipt. The upload
    must read a local snapshot pinned at push-click time."""
    src = tmp_path / "final.mp4"
    src.write_bytes(b"ORIGINAL")
    seen = {}

    def fake_upload(source, *, drive_filename, folder_id, file_id=None):
        # A recompile's shutil.copyfile lands mid-upload…
        src.write_bytes(b"REWRITTEN-BY-RECOMPILE")
        seen["source"] = Path(source)
        seen["bytes"] = Path(source).read_bytes()
        return {"id": "d1", "name": drive_filename,
                "webViewLink": "https://drive/d1"}

    monkeypatch.setattr(google_drive, "ensure_folder_path", lambda names: "f1")
    monkeypatch.setattr(google_drive, "upload_or_replace", fake_upload)

    receipt = _run(api._drive_push_file(src, ["Chang"], "x.mp4"))

    assert receipt["ok"]
    assert seen["source"] != src               # snapshot, not the live path
    assert seen["bytes"] == b"ORIGINAL"        # …but the pushed bytes are coherent
    assert not seen["source"].exists()         # snapshot cleaned up after


def test_drive_push_snapshot_cleaned_up_on_upload_failure(monkeypatch, tmp_path):
    src = tmp_path / "final.mp4"
    src.write_bytes(b"vid")
    seen = {}

    def fake_upload(source, *, drive_filename, folder_id, file_id=None):
        seen["source"] = Path(source)
        raise RuntimeError("boom")

    monkeypatch.setattr(google_drive, "ensure_folder_path", lambda names: "f1")
    monkeypatch.setattr(google_drive, "upload_or_replace", fake_upload)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api._drive_push_file(src, ["Chang"], "x.mp4"))
    assert ei.value.status_code == 502
    assert not seen["source"].exists()         # no temp-file leak on failure


def test_reengineer_drive_push_409_while_assemble_in_flight(monkeypatch, tmp_path):
    """_do_assemble overwrites final_<cid>.mp4 in place while the bucket entry
    stays "done" — pushing during the rebuild must refuse, not stream a file
    that's being rewritten under the upload."""
    from character_swap import reengineer as reengineer_mod
    final = tmp_path / "final_c1.mp4"
    final.write_bytes(b"vid")
    state = {"re_id": "re1", "title": "T", "status": "assembling",
             "finals": {"c1": {"status": "done", "final_path": str(final)}}}
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda re_id: dict(state))
    monkeypatch.setattr(reengineer_mod, "save_state", lambda st: None)
    calls = _patch_drive(monkeypatch)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.reengineer_char_drive_push(
            "re1", "c1", api.DrivePushBody(variant="final")))
    assert ei.value.status_code == 409
    assert calls == {}                          # upload never started


def test_reengineer_drive_push_409_while_repurpose_in_flight(monkeypatch, tmp_path):
    from character_swap import reengineer as reengineer_mod, runner_reengineer
    final = tmp_path / "repurpose_c1.mp4"
    final.write_bytes(b"vid")
    # Cover the in-process set too (the state flag may lag the runner).
    state = {"re_id": "re1", "title": "T", "status": "done",
             "repurposed": {"c1": {"status": "done",
                                   "final_path": str(final)}}}
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda re_id: dict(state))
    monkeypatch.setattr(reengineer_mod, "save_state", lambda st: None)
    monkeypatch.setattr(runner_reengineer, "_REPURPOSING", {"re1"})
    calls = _patch_drive(monkeypatch)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _run(api.reengineer_char_drive_push(
            "re1", "c1", api.DrivePushBody(variant="repurpose")))
    assert ei.value.status_code == 409
    assert calls == {}


def test_reengineer_drive_push_never_saves_stale_snapshot_when_entry_dropped(
        monkeypatch, tmp_path):
    """A rebuild finishing during the multi-minute upload can DROP this char
    from the fresh bucket (excluded after its approvals were withdrawn). The
    fallback used to save the PRE-upload snapshot — silently rolling back
    every state write from the window (other chars' fresh finals, flags,
    prompt edits). Now: never write state; return the receipt with a note."""
    from character_swap import reengineer as reengineer_mod
    final = tmp_path / "final_c1.mp4"
    final.write_bytes(b"vid")
    stale = {"re_id": "re1", "title": "T", "status": "done",
             "finals": {"c1": {"status": "done", "final_path": str(final)},
                        "c2": {"status": "done", "final_path": "/old-c2.mp4"}}}
    fresh = {"re_id": "re1", "title": "T", "status": "done",
             "finals": {"c2": {"status": "done", "final_path": "/new-c2.mp4",
                               "edit_id": "NEW"}}}
    loads = iter([stale, fresh])
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda re_id: next(loads))
    saved = []
    monkeypatch.setattr(reengineer_mod, "save_state",
                        lambda st: saved.append(st))

    class _Char:
        name = "Chang"

    monkeypatch.setattr(api, "store",
                        lambda: _FakeStore(chars={"c1": _Char()}))
    _patch_drive(monkeypatch)

    receipt = _run(api.reengineer_char_drive_push(
        "re1", "c1", api.DrivePushBody(variant="final")))

    assert receipt["ok"] is True
    assert receipt["persisted"] is False
    assert "sparades inte" in receipt["note"]
    assert saved == []          # the stale pre-upload snapshot is NEVER written


def test_reengineer_drive_push_never_saves_stale_snapshot_when_run_deleted(
        monkeypatch, tmp_path):
    from character_swap import reengineer as reengineer_mod
    final = tmp_path / "final_c1.mp4"
    final.write_bytes(b"vid")
    stale = {"re_id": "re1", "title": "T", "status": "done",
             "finals": {"c1": {"status": "done", "final_path": str(final)}}}
    loads = iter([stale, None])                 # DELETE landed mid-upload
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda re_id: next(loads))
    saved = []
    monkeypatch.setattr(reengineer_mod, "save_state",
                        lambda st: saved.append(st))

    class _Char:
        name = "Chang"

    monkeypatch.setattr(api, "store",
                        lambda: _FakeStore(chars={"c1": _Char()}))
    _patch_drive(monkeypatch)

    receipt = _run(api.reengineer_char_drive_push(
        "re1", "c1", api.DrivePushBody(variant="final")))

    assert receipt["ok"] is True and receipt["persisted"] is False
    assert saved == []                          # deleted run NOT resurrected


def test_reengineer_drive_push_falls_back_to_job_title(monkeypatch, tmp_path):
    # Production run states have NO title/name keys — the display name lives
    # on the linked swap job. The filename must use it, not the opaque re_id.
    from character_swap import reengineer as reengineer_mod
    final = tmp_path / "final_c1.mp4"
    final.write_bytes(b"vid")
    state = {"re_id": "re1", "job_id": "j9",
             "finals": {"c1": {"status": "done", "final_path": str(final)}}}
    monkeypatch.setattr(reengineer_mod, "load_state",
                        lambda re_id: dict(state) if re_id == "re1" else None)
    monkeypatch.setattr(reengineer_mod, "save_state", lambda st: None)

    class _Char:
        name = "Chang"

    linked = _compiled_job(tmp_path)
    linked.job_id = "j9"
    linked.title = "Reengineer 6 bilder"
    fake = _FakeStore(linked, chars={"c1": _Char()})
    monkeypatch.setattr(api, "store", lambda: fake)
    calls = _patch_drive(monkeypatch)

    _run(api.reengineer_char_drive_push(
        "re1", "c1", api.DrivePushBody(variant="final")))
    assert calls["filename"] == "Reengineer 6 bilder — Chang [re1].mp4"
