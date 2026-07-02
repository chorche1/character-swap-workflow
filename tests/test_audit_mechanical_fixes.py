"""Regression tests for the last 6 mechanical fixes from the 2026-07-01 audit.

1. GET /api/elevenlabs/voices ran a sync, tenacity-retried HTTP call directly
   on the event loop (~90s+ worst case freeze) → now asyncio.to_thread.
2. DELETE /api/characters/{char_id} unlinked the content-addressed image file
   even when ANOTHER character shared it (upload dedupe reuses the file) →
   now guarded by the same still-referenced scan delete_character_image uses.
3. delete_variant + compact_job unlinked a variant's file without checking
   that a duplicated-scene CLONE shares the same path on disk
   (_apply_scene_duplicate: path=v.path) → now refcounted across all
   variants in the job (all characters).
4. _apply_scene_delete stripped a character's LAST approval without demoting
   APPROVED → AWAITING_APPROVAL, letting movement start with nothing to
   animate → now mirrors the approve toggle's empty-list demotion.
5. Resolve-export endpoints deflated full final videos into an in-memory zip
   synchronously ON the event loop → now built to a temp file in a worker
   thread and streamed via FileResponse (temp deleted post-response, stale
   leftovers pruned).
6. runner._kick_char wiped jc.images but left jc.approved_variant_ids —
   stale ids later spawned phantom "approved variant missing on disk" ERROR
   clips → the (now all-dangling) list is cleared with the wipe.

Hermetic: endpoints are called directly with stub stores; events.publish is a
no-op (pattern: tests/test_scene_tools.py, tests/test_approve_during_generation.py).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse

from character_swap import api, runner
from character_swap.clients import ProviderNotConfigured
from character_swap.config import settings
from character_swap.models import (
    CharacterAsset,
    CharacterImage,
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
)


@pytest.fixture(autouse=True)
def _patch_events(monkeypatch):
    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(api.events, "publish", _noop)


def _run(coro):
    return asyncio.run(coro)


# --- 1. /api/elevenlabs/voices off the event loop -----------------------------------


def test_elevenlabs_voices_runs_off_the_event_loop(monkeypatch):
    """The sync tenacity-retried client call must be offloaded via
    asyncio.to_thread — inline it froze the whole server for ~90s+."""
    from character_swap.clients import elevenlabs

    seen: dict = {}

    def fake_list_voices():
        seen["thread"] = threading.current_thread()
        return [{"voice_id": "v1", "name": "Nova"}]

    monkeypatch.setattr(elevenlabs, "list_voices", fake_list_voices)

    async def _call():
        return threading.current_thread(), await api.elevenlabs_voices()

    loop_thread, out = _run(_call())
    assert out == [{"voice_id": "v1", "name": "Nova"}]
    assert seen["thread"] is not loop_thread  # worker thread, not the loop


def test_elevenlabs_voices_error_mapping_survives_offload(monkeypatch):
    """ProviderNotConfigured raised inside the worker thread still maps to
    the 503 the frontend expects."""
    from character_swap.clients import elevenlabs

    def boom():
        raise ProviderNotConfigured("ELEVENLABS_API_KEY not set")

    monkeypatch.setattr(elevenlabs, "list_voices", boom)
    with pytest.raises(HTTPException) as ei:
        _run(api.elevenlabs_voices())
    assert ei.value.status_code == 503


# --- 2. delete_character must not unlink a file another character shares ------------


class _CharState:
    def __init__(self, characters: dict) -> None:
        self.characters = characters
        self.projects: dict = {}


class _FakeCharStore:
    def __init__(self, characters: dict) -> None:
        self.state = _CharState(characters)

    def remove_character(self, char_id: str):
        return self.state.characters.pop(char_id, None)

    def update_project(self, project) -> None:  # pragma: no cover - not hit
        pass


def _char(char_id: str, filename: str, image_id: str) -> CharacterAsset:
    return CharacterAsset(
        char_id=char_id, filename=filename, name=char_id,
        images=[CharacterImage(image_id=image_id, filename=filename)],
        primary_image_id=image_id,
    )


def test_delete_character_keeps_file_shared_with_another_character(monkeypatch):
    """Content-addressed dedupe means two characters can point at the SAME
    hash-named file — deleting one must not destroy the other's reference
    photo (mirrors delete_character_image's still_referenced guard)."""
    shared = settings.characters_dir / "im_auditshared.png"
    shared.write_bytes(b"img")
    store = _FakeCharStore({
        "cA": _char("cA", shared.name, "iA"),
        "cB": _char("cB", shared.name, "iB"),
    })
    monkeypatch.setattr(api, "store", lambda: store)

    assert _run(api.delete_character("cA")) == {"ok": True}

    assert shared.exists()  # cB still references the dedup'd file
    shared.unlink()


def test_delete_character_still_unlinks_unshared_file(monkeypatch):
    """The guard must not over-suppress: a file only the deleted character
    referenced is removed from disk as before."""
    solo = settings.characters_dir / "im_auditsolo.png"
    solo.write_bytes(b"img")
    other = settings.characters_dir / "im_auditother.png"
    other.write_bytes(b"img2")
    store = _FakeCharStore({
        "cA": _char("cA", solo.name, "iA"),
        "cB": _char("cB", other.name, "iB"),
    })
    monkeypatch.setattr(api, "store", lambda: store)

    assert _run(api.delete_character("cA")) == {"ok": True}

    assert not solo.exists()
    assert other.exists()
    other.unlink()


# --- 3. delete_variant / compact_job refcount duplicated-scene clones ---------------


class _FakeJobStore:
    def __init__(self, job: Job) -> None:
        self._job = job

    def get_job(self, job_id: str):
        return self._job if job_id == self._job.job_id else None

    def update_job(self, job: Job) -> None:
        self._job = job


def _img(vid: str, scene_id: str, path: str, **kw) -> GeneratedImage:
    return GeneratedImage(variant_id=vid, path=path, prompt="p",
                          scene_id=scene_id, status=VariantStatus.READY, **kw)


def _dup_scene_job(tmp_path) -> tuple[Job, Path]:
    """A job where scene s1 was duplicated: the clone points at the SAME file
    on disk (_apply_scene_duplicate: path=v.path) and the CLONE is approved."""
    shared = tmp_path / "variant_shared.png"
    shared.write_bytes(b"img")
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/a.png",
        status=CharStatus.APPROVED,
        images=[
            _img("v1", "s1", str(shared)),
            _img("v1c", "s1__dupabc", str(shared), parent_variant_id="v1"),
        ],
        approved_variant_ids=["v1c"], approved_variant_id="v1c",
    )
    job = Job(
        job_id="j_dup", title="t",
        scene_id="s1", scene_image_path="/p1.png",
        scene_ids=["s1", "s1__dupabc"], scene_image_paths=["/p1.png", "/p1.png"],
        characters={"cA": jc},
    )
    return job, shared


def test_delete_variant_keeps_file_shared_with_clone(monkeypatch, tmp_path):
    """Deleting the source variant of a duplicated scene must NOT unlink the
    file the approved clone still points at."""
    job, shared = _dup_scene_job(tmp_path)
    monkeypatch.setattr(api, "store", lambda: _FakeJobStore(job))

    result = _run(api.delete_variant("j_dup", "cA", "v1"))

    assert shared.exists()  # the approved clone's image survives
    c = result["characters"]["cA"]
    assert [im["variant_id"] for im in c["images"]] == ["v1c"]
    assert c["approved_variant_ids"] == ["v1c"]


def test_delete_variant_still_unlinks_unshared_file(monkeypatch, tmp_path):
    """No over-suppression: a variant whose file nothing else references is
    removed from disk exactly as before."""
    job, shared = _dup_scene_job(tmp_path)
    solo = tmp_path / "variant_solo.png"
    solo.write_bytes(b"img")
    job.characters["cA"].images.append(_img("v2", "s1", str(solo)))
    monkeypatch.setattr(api, "store", lambda: _FakeJobStore(job))

    _run(api.delete_variant("j_dup", "cA", "v2"))

    assert not solo.exists()
    assert shared.exists()


def test_compact_job_keeps_files_referenced_by_kept_variants(monkeypatch, tmp_path):
    """compact_job strips non-approved variants; a stripped variant's file
    must survive when a KEPT variant (same char clone OR another character)
    still points at it. Unshared non-approved files are still freed."""
    job, shared = _dup_scene_job(tmp_path)  # cA: v1 (not approved) + v1c (approved), same file
    cross = tmp_path / "variant_cross.png"
    cross.write_bytes(b"img")
    solo = tmp_path / "variant_solo.png"
    solo.write_bytes(b"imgimg")
    # cA also has an unapproved variant sharing a file with cB's APPROVED one,
    # plus an unapproved file nothing else references.
    job.characters["cA"].images += [
        _img("v2", "s1", str(cross)),
        _img("v3", "s1", str(solo)),
    ]
    job.characters["cB"] = JobCharacter(
        char_id="cB", name="B", source_image_path="/b.png",
        status=CharStatus.APPROVED,
        images=[_img("vB1", "s1", str(cross))],
        approved_variant_ids=["vB1"], approved_variant_id="vB1",
    )
    store = _FakeJobStore(job)
    monkeypatch.setattr(api, "store", lambda: store)

    out = _run(api.compact_job("j_dup"))

    assert out["ok"] is True
    assert shared.exists()             # kept clone v1c needs it
    assert cross.exists()              # cB's approved vB1 needs it
    assert not solo.exists()           # genuinely unreferenced → freed
    assert out["bytes_freed"] == 6     # only solo's bytes counted
    kept = {v.variant_id for v in store.get_job("j_dup").characters["cA"].images}
    assert kept == {"v1c"}


# --- 4. _apply_scene_delete demotes APPROVED when the last approval goes ------------


def test_scene_delete_demotes_char_whose_only_approval_lived_there(monkeypatch):
    """Deleting the scene holding a character's ONLY approval must drop the
    char APPROVED → AWAITING_APPROVAL (like delete_variant / the approve
    toggle) — otherwise set_movement's status gate passes and the job locks
    at Step 4 with nothing to animate."""
    a = JobCharacter(
        char_id="cA", name="A", source_image_path="/a.png",
        status=CharStatus.APPROVED,
        images=[_img("vA1", "s1", "/xA1.png"), _img("vA2", "s2", "/xA2.png")],
        approved_variant_ids=["vA2"], approved_variant_id="vA2",  # ONLY s2 approved
    )
    b = JobCharacter(
        char_id="cB", name="B", source_image_path="/b.png",
        status=CharStatus.APPROVED,
        images=[_img("vB1", "s1", "/xB1.png"), _img("vB2", "s2", "/xB2.png")],
        approved_variant_ids=["vB1", "vB2"], approved_variant_id="vB1",
    )
    job = Job(
        job_id="j_del", title="t",
        scene_id="s1", scene_image_path="/p1.png",
        scene_ids=["s1", "s2"], scene_image_paths=["/p1.png", "/p2.png"],
        characters={"cA": a, "cB": b},
    )
    store = _FakeJobStore(job)
    monkeypatch.setattr(api, "store", lambda: store)

    _run(api.delete_scene("j_del", "s2"))

    stored = store.get_job("j_del")
    ca, cb = stored.characters["cA"], stored.characters["cB"]
    assert ca.approved_variant_ids == [] and ca.approved_variant_id is None
    assert ca.status == CharStatus.AWAITING_APPROVAL      # demoted
    assert cb.approved_variant_ids == ["vB1"]
    assert cb.status == CharStatus.APPROVED               # approval survives


# --- 5. Resolve export: temp file in a worker thread, FileResponse ------------------


def test_editor_export_resolve_builds_off_loop_and_streams_a_file(monkeypatch):
    edit_id = "ed_auditzip"
    edit_dir = settings.output_dir / "editor" / edit_id
    edit_dir.mkdir(parents=True, exist_ok=True)
    (edit_dir / "04-final.mp4").write_bytes(b"\x00" * 64)
    (edit_dir / "words.json").write_text(
        '[{"text": "hej", "start": 0.0, "end": 0.4}]', encoding="utf-8")

    from character_swap import exporter
    seen: dict = {}
    real_build = exporter.build_export_zip

    def spy(**kw):
        seen["thread"] = threading.current_thread()
        return real_build(**kw)

    monkeypatch.setattr(exporter, "build_export_zip", spy)

    # A stale zip from an interrupted download is pruned on the next call.
    export_dir = settings.output_dir / "cache" / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    stale = export_dir / "old-orphan.zip"
    stale.write_bytes(b"junk")
    os.utime(stale, (time.time() - 7200,) * 2)

    async def _call():
        return threading.current_thread(), await api.editor_export_resolve(edit_id)

    loop_thread, resp = _run(_call())

    # Deflate ran in a worker thread — never on the event loop.
    assert seen["thread"] is not loop_thread
    # Download UX identical: a zip attachment named <edit_id>-resolve.zip.
    assert isinstance(resp, FileResponse)
    assert resp.media_type == "application/zip"
    assert (f'filename="{edit_id}-resolve.zip"'
            in resp.headers["content-disposition"])
    zip_path = Path(resp.path)
    assert zip_path.parent == export_dir
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert f"{edit_id}/video-final.mp4" in names
    assert f"{edit_id}/captions.srt" in names
    assert not stale.exists()  # pruned
    # The temp zip is deleted once the response has been sent.
    assert resp.background is not None
    _run(resp.background())
    assert not zip_path.exists()


def test_job_char_export_resolve_uses_the_same_tempfile_path(monkeypatch):
    """The Step-6 per-character variant of the endpoint shares the helper —
    same FileResponse shape, same attachment name."""
    final = settings.output_dir / "j_exp" / "compiled" / "cA.mp4"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_bytes(b"\x00" * 32)
    jc = JobCharacter(
        char_id="cA", name="Alva", source_image_path="/a.png",
        status=CharStatus.DONE,
        compile_status="done", compiled_video_path=str(final),
    )
    job = Job(
        job_id="j_exp", title="t",
        scene_id="s1", scene_image_path="/p1.png",
        scene_ids=["s1"], scene_image_paths=["/p1.png"],
        characters={"cA": jc},
    )
    monkeypatch.setattr(api, "store", lambda: _FakeJobStore(job))

    resp = _run(api.job_char_export_resolve("j_exp", "cA"))

    assert isinstance(resp, FileResponse)
    assert 'filename="j_exp-Alva-resolve.zip"' in resp.headers["content-disposition"]
    zip_path = Path(resp.path)
    assert zip_path.exists()
    _run(resp.background())
    assert not zip_path.exists()


# --- 6. _kick_char clears the (now dangling) multi-approve list ---------------------


class _RunnerStore:
    """Granular-persist-capable fake (same surface as the SQLite store) — see
    tests/test_approve_during_generation.py."""

    def update_job(self, job):
        pass

    def update_job_character(self, job, jc):
        pass

    def update_variant(self, job, jc, v):
        pass


def test_kick_char_clears_stale_approved_variant_ids(monkeypatch, tmp_path):
    """Regenerate wipes jc.images, so EVERY old approval id is dangling
    (fresh variants get new random ids). The list used to survive the wipe:
    re-approving then seeded a stale+fresh mix and _animate_character spawned
    a phantom ERROR clip per stale id ("approved variant missing on disk")."""
    old = GeneratedImage(variant_id="v_old", path=str(tmp_path / "old.png"),
                         prompt="p", scene_id="s1", status=VariantStatus.READY)
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/char.png",
                      status=CharStatus.APPROVED, images=[old],
                      approved_variant_ids=["v_old"], approved_variant_id="v_old")
    # skip_qc: the approval bookkeeping is under test, not the QC loop.
    job = Job(job_id="j_kick", title="t", scene_id="s1",
              scene_image_path="/scene.png", scene_ids=["s1"],
              scene_image_paths=["/scene.png"], characters={"cA": jc},
              skip_qc=True)
    monkeypatch.setattr(runner, "store", lambda: _RunnerStore())

    def _fake_generate(**kw):
        # The real generate_variant creates the per-(job, char) output dir.
        dest = Path(kw["dest"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"img")

    monkeypatch.setattr(runner.pipeline, "generate_variant", _fake_generate)

    async def _noop(*a, **k):
        return None
    monkeypatch.setattr(runner, "_emit", _noop)
    monkeypatch.setattr(runner, "_scene_path_for_variant",
                        lambda j, v: Path("/scene.png"))

    asyncio.run(runner._kick_char(job, jc, 1, asyncio.Semaphore(1)))

    # No dangling approvals survive the wipe...
    assert jc.approved_variant_ids == []
    assert jc.approved_variant_id is None
    # ...the regenerated variant is fresh...
    live = {v.variant_id for v in jc.images}
    assert "v_old" not in live and len(live) == 1
    assert all(v.status == VariantStatus.READY for v in jc.images)
    # ...and with zero approvals the completion persist lands the char at
    # AWAITING_APPROVAL (fresh gate), never a phantom APPROVED. This is the
    # complement of the approve-race fix (see
    # test_approve_during_generation): LIVE approvals restore APPROVED;
    # dangling ones must not.
    assert jc.status == CharStatus.AWAITING_APPROVAL
