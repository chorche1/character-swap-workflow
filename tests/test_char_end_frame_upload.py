"""Per-character VERBATIM end-frame uploads (Hugo 2026-07-04).

Upload your OWN finished image as ONE character's end frame — used exactly
as-is (no swap, no QC). It WINS over the swap-into-pose result, coexists with
the shared end-pose mechanism, and only end-frame-capable models honor it.

Covers: the resolver precedence (upload > swapped > swap-now > none), the two
new endpoints (upload + clear) incl. the Reengineer-only lock relaxation +
validation, survival across a shared-pose regen, and model round-trip.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

from character_swap import api, runner
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)


def _job(*, origin: str | None = "reengineer:re_t", movement: bool = True,
         video_model: str = "kling-v3") -> Job:
    v = GeneratedImage(variant_id="va", path="/a.png", prompt="p",
                       scene_id="s1", status=VariantStatus.READY)
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/c.png",
        status=CharStatus.APPROVED, images=[v],
        approved_variant_ids=["va"],
        videos=[VideoVariant(video_id="vidA", grok_job_id="g1",
                             status=VideoStatus.DONE, source_variant_id="va",
                             final_video_path="/clip.mp4")])
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               characters={"cA": jc}, origin=origin,
               video_model=video_model,
               movement_prompt=("animate" if movement else None),
               movement_prompts=({"s1": "animate"} if movement else {}))


class _Store:
    def __init__(self, job):
        self.job = job

    def get_job(self, jid):
        return self.job if jid == "j1" else None

    def update_job(self, j):
        pass


def _png(filename: str = "finished.png", data: bytes = b"\x89PNGfinished") -> UploadFile:
    return UploadFile(io.BytesIO(data), filename=filename)


@pytest.fixture
def wired(monkeypatch, tmp_path):
    job = _job()
    monkeypatch.setattr(api, "store", lambda: _Store(job))

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(api.events, "publish", _noop)
    monkeypatch.setattr(type(api.settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)
    return job


# ----------------------------------------------------------------- data model

def test_field_defaults_empty():
    jc = JobCharacter(char_id="c", name="n", source_image_path="/c.png")
    assert jc.end_frame_uploads == {}


def test_model_roundtrip_preserves_uploads():
    job = _job()
    job.characters["cA"].end_frame_uploads = {"s1": "/out/j1/cA/endframe_upload_s1.png"}
    back = Job.model_validate(job.model_dump())
    assert back.characters["cA"].end_frame_uploads == {
        "s1": "/out/j1/cA/endframe_upload_s1.png"}


def test_sqlite_roundtrip_preserves_uploads(tmp_path):
    """The REAL persistence path (SqliteStateStore model_json), not just
    Pydantic — a server restart must not wipe the verbatim uploads."""
    from character_swap.state import SqliteStateStore
    db = tmp_path / "state.sqlite3"
    s1 = SqliteStateStore(db_path=db)
    job = _job()
    job.characters["cA"].end_frame_uploads = {
        "s1": "/out/j1/cA/endframe_upload_s1.png"}
    s1.add_job(job)
    # Throw the store away, rebuild against the same file.
    s2 = SqliteStateStore(db_path=db)
    loaded = s2.get_job("j1")
    assert loaded.characters["cA"].end_frame_uploads == {
        "s1": "/out/j1/cA/endframe_upload_s1.png"}


# ------------------------------------------------------------ upload endpoint

def test_upload_writes_verbatim_and_surfaces_url(wired):
    out = asyncio.run(api.upload_char_end_frame(
        "j1", "cA", "s1", file=_png(data=b"my finished frame")))
    jc = wired.characters["cA"]
    saved = jc.end_frame_uploads.get("s1")
    assert saved and Path(saved).exists()
    # written VERBATIM — exact bytes, no swap/re-encode
    assert Path(saved).read_bytes() == b"my finished frame"
    assert saved.endswith("endframe_upload_s1.png")
    # serialized for the frontend
    assert out["characters"]["cA"]["end_frame_upload_urls"]["s1"]


def test_upload_clears_stale_swap_error(wired):
    wired.characters["cA"].end_frame_errors = {"s1": "content policy"}
    asyncio.run(api.upload_char_end_frame("j1", "cA", "s1", file=_png()))
    assert "s1" not in wired.characters["cA"].end_frame_errors


def test_upload_does_not_touch_shared_pose_or_swapped_frame(wired):
    # A shared pose + its swapped frame coexist; a per-char upload is additive.
    wired.end_frames_by_scene = {"s1": "/pose.png"}
    wired.characters["cA"].end_frame_paths = {"s1": "/swapped.png"}
    asyncio.run(api.upload_char_end_frame("j1", "cA", "s1", file=_png()))
    assert wired.end_frames_by_scene == {"s1": "/pose.png"}
    assert wired.characters["cA"].end_frame_paths == {"s1": "/swapped.png"}


# ------------------------------------------------------------- resolver order

def test_resolve_prefers_upload_over_swapped(monkeypatch, tmp_path):
    """The verbatim upload wins over a pre-generated swapped frame, and
    _ensure_end_frame_swap is never invoked."""
    job = _job()
    up = tmp_path / "up.png"
    up.write_bytes(b"u")
    sw = tmp_path / "sw.png"
    sw.write_bytes(b"s")
    job.characters["cA"].end_frame_uploads = {"s1": str(up)}
    job.characters["cA"].end_frame_paths = {"s1": str(sw)}

    def _boom(*a, **kw):
        raise AssertionError("must not swap when a verbatim upload exists")
    monkeypatch.setattr(runner, "_ensure_end_frame_swap", _boom)
    got = asyncio.run(runner._resolve_end_image(job, job.characters["cA"], "s1"))
    assert got == up


def test_resolve_falls_back_when_upload_missing_file(tmp_path):
    """A recorded upload whose file vanished falls through to the swapped
    frame instead of returning a dead path."""
    job = _job()
    sw = tmp_path / "sw.png"
    sw.write_bytes(b"s")
    job.characters["cA"].end_frame_uploads = {"s1": str(tmp_path / "gone.png")}
    job.characters["cA"].end_frame_paths = {"s1": str(sw)}
    got = asyncio.run(runner._resolve_end_image(job, job.characters["cA"], "s1"))
    assert got == sw


def test_resolve_ignores_upload_for_non_capable_model(tmp_path):
    job = _job(video_model="grok-imagine")
    up = tmp_path / "up.png"
    up.write_bytes(b"u")
    job.characters["cA"].end_frame_uploads = {"s1": str(up)}
    got = asyncio.run(runner._resolve_end_image(job, job.characters["cA"], "s1"))
    assert got is None


# ------------------------------------------- survives a shared-pose regen

def test_upload_survives_shared_pose_regen(monkeypatch, tmp_path):
    """regen_scene_end_frames rewrites end_frame_paths but must NOT clobber a
    verbatim upload — the resolver keeps returning the upload afterwards."""
    job = _job()
    up = tmp_path / "up.png"
    up.write_bytes(b"u")
    pose = tmp_path / "pose.png"
    pose.write_bytes(b"p")
    job.end_frames_by_scene = {"s1": str(pose)}
    job.characters["cA"].end_frame_uploads = {"s1": str(up)}
    monkeypatch.setattr(runner, "store", lambda: _Store(job))

    swapped = tmp_path / "swapped.png"

    def fake_swap(j, jc, sid, p, *, force=False):
        swapped.write_bytes(b"s")
        return swapped
    monkeypatch.setattr(runner, "_ensure_end_frame_swap", fake_swap)

    async def _noop(*a, **kw):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(runner, "_persist",
                        lambda *a, **kw: job.characters["cA"])

    asyncio.run(runner.regen_scene_end_frames("j1", "s1"))
    # regen filled the swapped frame but the upload is untouched...
    assert job.characters["cA"].end_frame_uploads == {"s1": str(up)}
    # ...and still wins at resolve time.
    got = asyncio.run(runner._resolve_end_image(job, job.characters["cA"], "s1"))
    assert got == up


# --------------------------------------------------------------- clear endpoint

def test_clear_removes_upload_and_unlinks(wired):
    asyncio.run(api.upload_char_end_frame("j1", "cA", "s1", file=_png()))
    saved = Path(wired.characters["cA"].end_frame_uploads["s1"])
    assert saved.exists()
    asyncio.run(api.clear_char_end_frame("j1", "cA", "s1"))
    assert "s1" not in wired.characters["cA"].end_frame_uploads
    assert not saved.exists()


def test_clear_then_resolve_falls_back_to_swapped(wired, tmp_path):
    sw = tmp_path / "sw.png"
    sw.write_bytes(b"s")
    wired.characters["cA"].end_frame_paths = {"s1": str(sw)}
    asyncio.run(api.upload_char_end_frame("j1", "cA", "s1", file=_png()))
    asyncio.run(api.clear_char_end_frame("j1", "cA", "s1"))
    got = asyncio.run(runner._resolve_end_image(wired, wired.characters["cA"], "s1"))
    assert got == sw


# ------------------------------------------------------------- lock + validation

def test_upload_locked_for_plain_swap_job(monkeypatch):
    plain = _job(origin=None)
    monkeypatch.setattr(api, "store", lambda: _Store(plain))
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.upload_char_end_frame("j1", "cA", "s1", file=_png()))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e2:
        asyncio.run(api.clear_char_end_frame("j1", "cA", "s1"))
    assert e2.value.status_code == 409


def test_upload_unknown_char_404(wired):
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.upload_char_end_frame("j1", "nope", "s1", file=_png()))
    assert e.value.status_code == 404


def test_upload_unknown_scene_404(wired):
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.upload_char_end_frame("j1", "cA", "s9", file=_png()))
    assert e.value.status_code == 404


def test_upload_rejects_non_image(wired):
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.upload_char_end_frame(
            "j1", "cA", "s1", file=_png(filename="x.txt")))
    assert e.value.status_code == 400


def test_upload_rejects_empty(wired):
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.upload_char_end_frame(
            "j1", "cA", "s1", file=_png(data=b"")))
    assert e.value.status_code == 400


def test_unknown_job_404(monkeypatch):
    monkeypatch.setattr(api, "store", lambda: _Store(_job()))
    with pytest.raises(HTTPException) as e:
        asyncio.run(api.upload_char_end_frame("nope", "cA", "s1", file=_png()))
    assert e.value.status_code == 404


# ------------------------------------------------- scene-op consistency helpers

def test_duplicate_scene_carries_upload(wired):
    wired.characters["cA"].end_frame_uploads = {"s1": "/out/up.png"}
    new_sid = api._apply_scene_duplicate(wired, "s1")
    assert wired.characters["cA"].end_frame_uploads[new_sid] == "/out/up.png"
    # original still present
    assert wired.characters["cA"].end_frame_uploads["s1"] == "/out/up.png"


def test_repoint_scene_rekeys_upload(wired):
    """'Byt scenbild' re-points the scene id — a verbatim upload is scene-
    agnostic user content, so it must MOVE to the new id, not be dropped."""
    wired.characters["cA"].end_frame_uploads = {"s1": "/out/up.png"}
    state = {"scenes": [{"scene_id": "s1"}]}
    api._repoint_scene(wired, state, 0, "s1", "s2new", "/newscene.png")
    assert "s1" not in wired.characters["cA"].end_frame_uploads
    assert wired.characters["cA"].end_frame_uploads["s2new"] == "/out/up.png"


def test_shared_clear_scene_end_frame_preserves_uploads(wired, tmp_path):
    """Clearing the SHARED end pose must leave per-character verbatim uploads
    untouched — the two mechanisms are independent."""
    pose = tmp_path / "pose.png"
    pose.write_bytes(b"p")
    wired.end_frames_by_scene = {"s1": str(pose)}
    asyncio.run(api.upload_char_end_frame("j1", "cA", "s1", file=_png()))
    up = wired.characters["cA"].end_frame_uploads["s1"]
    asyncio.run(api.clear_scene_end_frame("j1", "s1"))
    assert wired.end_frames_by_scene == {}
    assert wired.characters["cA"].end_frame_uploads.get("s1") == up


def test_clear_keeps_file_shared_by_duplicate(wired):
    """DATA-LOSS GUARD: a duplicated scene shares the SAME upload file string;
    clearing the ORIGINAL must not unlink a file the duplicate still uses.
    Only the last referrer's clear unlinks it (delete_variant convention)."""
    asyncio.run(api.upload_char_end_frame("j1", "cA", "s1", file=_png()))
    f = Path(wired.characters["cA"].end_frame_uploads["s1"])
    assert f.exists()
    new_sid = api._apply_scene_duplicate(wired, "s1")
    assert wired.characters["cA"].end_frame_uploads[new_sid] == str(f)
    # clear the original — duplicate still references f → file survives
    asyncio.run(api.clear_char_end_frame("j1", "cA", "s1"))
    assert f.exists()
    assert wired.characters["cA"].end_frame_uploads.get(new_sid) == str(f)
    # clear the last referrer → file is finally unlinked
    asyncio.run(api.clear_char_end_frame("j1", "cA", new_sid))
    assert not f.exists()
