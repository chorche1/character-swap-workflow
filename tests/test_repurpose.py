"""Repurpose: mirror-flipped final-video variants (Hugo 2026-06-27).

One button on the Swap Step-6 + Reengineer finals produces a HORIZONTALLY
mirrored copy of every final (captions stay UPRIGHT — the flip happens on the
source clips BEFORE caption burn-in), kept ALONGSIDE the originals with its own
edit settings.

Locks the four load-bearing pieces:
  1. `video_edit.hflip_video` builds the right ffmpeg command (audio copied).
  2. `run_editor_pipeline(mirror_h=True)` pre-flips EVERY clip and feeds the
     flipped paths downstream — mirroring can't silently no-op.
  3. The compile/repurpose `_CompileSlot` writes the correct JobCharacter
     fields / events / filename, and the DEFAULT (compile) path is unchanged.
  4. The Reengineer `_do_repurpose` builds `repurposed` finals with mirror_h=True
     and NEVER touches `finals` / run `status`.
"""
from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace

from character_swap import runner_compile, runner_reengineer, video_edit
from character_swap.config import settings
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VariantStatus,
    VideoStatus,
    VideoVariant,
)
from character_swap.runner_compile import (
    _COMPILE_SLOT,
    _REPURPOSE_SLOT,
    EditorResult,
)


# --- 1. hflip_video ffmpeg command ----------------------------------------------------


def _capture_hflip(monkeypatch, *, has_audio: bool):
    cmds: list[list[str]] = []
    monkeypatch.setattr(video_edit, "_run", lambda args, **k: cmds.append(args))
    monkeypatch.setattr(video_edit, "_has_audio_stream", lambda p: has_audio)
    monkeypatch.setattr(video_edit, "_probe_duration", lambda p: 1.0)
    # Isolate from the call log (don't touch calls.jsonl in tests).
    monkeypatch.setattr(video_edit, "record",
                        lambda **k: contextlib.nullcontext({}))
    return cmds


def test_hflip_video_mirrors_and_copies_audio(monkeypatch, tmp_path):
    cmds = _capture_hflip(monkeypatch, has_audio=True)
    video_edit.hflip_video(tmp_path / "in.mp4", tmp_path / "out.mp4",
                           job_id="j")
    assert len(cmds) == 1
    cmd = cmds[0]
    assert "hflip" in cmd                      # the mirror filter
    assert cmd[cmd.index("-vf") + 1] == "hflip"
    # Audio is copied bit-for-bit so transcription/alignment is unaffected.
    assert "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "copy"


def test_hflip_video_audioless_clip_drops_audio(monkeypatch, tmp_path):
    cmds = _capture_hflip(monkeypatch, has_audio=False)
    video_edit.hflip_video(tmp_path / "in.mp4", tmp_path / "out.mp4")
    cmd = cmds[0]
    assert "hflip" in cmd
    assert "-an" in cmd
    assert "-c:a" not in cmd                    # nothing to copy


# --- 2. run_editor_pipeline(mirror_h=True) pre-flips every clip -----------------------


def test_run_editor_pipeline_mirror_flips_each_clip(monkeypatch, tmp_path):
    """mirror_h=True flips each source clip ONCE up front and hands the FLIPPED
    paths to assemble_clips — so concat + (later) caption burn-in run on the
    mirrored video and the flip can never silently no-op."""
    p1 = tmp_path / "a.mp4"; p1.write_bytes(b"a")
    p2 = tmp_path / "b.mp4"; p2.write_bytes(b"b")
    edit_dir = tmp_path / "edit"; edit_dir.mkdir()

    flipped: list[tuple[str, str]] = []

    def fake_hflip(src, dst, *, job_id=None):
        flipped.append((str(src), str(dst)))
        dst.write_bytes(b"flipped")
        return dst
    monkeypatch.setattr(runner_compile.video_edit, "hflip_video", fake_hflip)

    seen_paths: list[list[str]] = []

    def fake_assemble(paths, out, **kw):
        seen_paths.append([str(p) for p in paths])
        out.write_bytes(b"concat")
        return {"clip_keeps": None}
    monkeypatch.setattr(runner_compile.video_edit, "assemble_clips",
                        fake_assemble)

    result = asyncio.run(runner_compile.run_editor_pipeline(
        [p1, p2], edit_id="ed_x", edit_dir=edit_dir,
        template="capcut-bluebox", overrides=None,
        enable_trim=False, enable_captions=False, enable_wpm_normalize=False,
        target_wpm=190, threshold_db=-24.0, min_silence_secs=0.4, pad_secs=0.1,
        voice_id=None, enable_transcribe=False, mirror_h=True))

    # One flip per input clip, into the edit dir.
    assert len(flipped) == 2
    assert [s for s, _ in flipped] == [str(p1), str(p2)]
    assert all(d.endswith("mirror-00.mp4") or d.endswith("mirror-01.mp4")
               for _, d in flipped)
    # assemble_clips saw the FLIPPED paths, not the originals.
    assert seen_paths[0] == [d for _, d in flipped]
    assert str(p1) not in seen_paths[0] and str(p2) not in seen_paths[0]
    assert result.final == edit_dir / "00-concat.mp4"


def test_run_editor_pipeline_no_mirror_uses_originals(monkeypatch, tmp_path):
    """The default (mirror_h=False) never calls hflip and passes the original
    clips straight through — the compile path is byte-for-byte unchanged."""
    p1 = tmp_path / "a.mp4"; p1.write_bytes(b"a")
    edit_dir = tmp_path / "edit"; edit_dir.mkdir()

    called = {"flip": 0}
    monkeypatch.setattr(runner_compile.video_edit, "hflip_video",
                        lambda *a, **k: called.__setitem__("flip", called["flip"] + 1))
    seen: list[list[str]] = []

    def fake_assemble(paths, out, **kw):
        seen.append([str(p) for p in paths])
        out.write_bytes(b"c")
        return {"clip_keeps": None}
    monkeypatch.setattr(runner_compile.video_edit, "assemble_clips", fake_assemble)

    asyncio.run(runner_compile.run_editor_pipeline(
        [p1], edit_id="ed_y", edit_dir=edit_dir, template="capcut-bluebox",
        overrides=None, enable_trim=False, enable_captions=False,
        enable_wpm_normalize=False, target_wpm=190, threshold_db=-24.0,
        min_silence_secs=0.4, pad_secs=0.1, voice_id=None,
        enable_transcribe=False))

    assert called["flip"] == 0
    assert seen[0] == [str(p1)]


# --- 3. _CompileSlot selects fields / events / filename -------------------------------


def _mkvideo(path, source_variant_id):
    return VideoVariant(video_id=f"v_{source_variant_id}",
                        grok_job_id=f"g_{source_variant_id}",
                        status=VideoStatus.DONE, source_variant_id=source_variant_id,
                        final_video_path=str(path))


def _mkjob_one_scene(tmp_path):
    v1 = tmp_path / "s1.mp4"; v1.write_text("clip")
    jc = JobCharacter(
        char_id="c1", name="A", source_image_path="/tmp/a.png",
        images=[GeneratedImage(variant_id="var1", path="/tmp/var1.png",
                               prompt="x", scene_id="sc1",
                               status=VariantStatus.READY)],
        approved_variant_ids=["var1"],
        videos=[_mkvideo(v1, "var1")])
    job = Job(job_id="j1", scene_id="sc1", scene_image_path="/tmp/sc1.png",
              scene_ids=["sc1"], scene_image_paths=["/tmp/sc1.png"],
              characters={"c1": jc})
    return job, jc


def _run_one_char(monkeypatch, tmp_path, job, *, slot):
    fake_store = SimpleNamespace(get_job=lambda jid: job,
                                 update_job=lambda j: None,
                                 get_character=lambda cid: None)
    monkeypatch.setattr(runner_compile, "store", lambda: fake_store)
    monkeypatch.setattr(settings, "output_dir", tmp_path, raising=False)
    events: list[tuple[str, dict]] = []

    async def fake_emit(job_id, kind, **kw):
        events.append((kind, kw))
    monkeypatch.setattr(runner_compile, "_emit", fake_emit)

    final = tmp_path / "result.mp4"; final.write_text("final")
    seen = {}

    async def fake_pipeline(paths, **kw):
        seen["mirror_h"] = kw.get("mirror_h")
        return EditorResult(final=final, voice_applied=False)
    monkeypatch.setattr(runner_compile, "run_editor_pipeline", fake_pipeline)

    asyncio.run(runner_compile._compile_one_character(
        "j1", "c1", template="capcut-bluebox", overrides=None,
        enable_trim=False, enable_captions=False, enable_wpm_normalize=False,
        target_wpm=190, threshold_db=-24.0, min_silence_secs=0.4, pad_secs=0.1,
        voice_override=None, enable_voice_swap=False, slot=slot))
    return events, seen


def test_compile_slot_writes_compile_fields(monkeypatch, tmp_path):
    job, jc = _mkjob_one_scene(tmp_path)
    events, seen = _run_one_char(monkeypatch, tmp_path, job, slot=_COMPILE_SLOT)
    assert jc.compile_status == "done"
    assert jc.compiled_video_path.endswith("/compiled/c1.mp4")
    assert jc.repurpose_status is None          # repurpose untouched
    assert jc.repurposed_video_path is None
    assert seen["mirror_h"] is False
    assert {k for k, _ in events} >= {"char.compile_started", "char.compile_done"}


def test_repurpose_slot_writes_repurpose_fields_and_mirrors(monkeypatch, tmp_path):
    job, jc = _mkjob_one_scene(tmp_path)
    events, seen = _run_one_char(monkeypatch, tmp_path, job, slot=_REPURPOSE_SLOT)
    assert jc.repurpose_status == "done"
    assert jc.repurposed_video_path.endswith("/compiled/c1__repurpose.mp4")
    assert jc.compile_status is None            # the original compile is untouched
    assert jc.compiled_video_path is None
    assert seen["mirror_h"] is True             # mirror is applied
    assert {k for k, _ in events} >= {"char.repurpose_started", "char.repurpose_done"}


# --- 4. Reengineer _do_repurpose ------------------------------------------------------


def _re_clip(vid, variant, path):
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid,
                        status=VideoStatus.DONE, source_variant_id=variant,
                        final_video_path=path)


def _re_job(tmp_path):
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    imgs = [GeneratedImage(variant_id=f"v{i}", path=f"/v{i}.png", prompt="p",
                           scene_id=f"s{i}", status="ready") for i in (1, 2)]
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/c.png",
                      status=CharStatus.APPROVED, images=imgs,
                      approved_variant_ids=["v1", "v2"],
                      videos=[_re_clip("vd1", "v1", str(c1)),
                              _re_clip("vd2", "v2", str(c2))])
    job = Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1", "s2"],
              scene_image_path="/p.png", scene_image_paths=["/p.png"] * 2,
              characters={"cA": jc}, origin="reengineer:re_t")
    return job, [c1, c2]


def _re_state():
    return {"re_id": "re_t", "job_id": "j1", "status": "done",
            "scenes": [{"idx": 0, "scene_id": "s1", "duration": 2.0,
                        "motion_prompt": "a", "speech": "", "summary": ""},
                       {"idx": 1, "scene_id": "s2", "duration": 2.0,
                        "motion_prompt": "b", "speech": "", "summary": ""}]}


def test_do_repurpose_builds_mirrored_finals_without_touching_finals(
        monkeypatch, tmp_path):
    job, clips = _re_job(tmp_path)
    run_dir = tmp_path / "run"; run_dir.mkdir()

    class _S:
        def get_job(self, jid):
            return job

        def get_character(self, cid):
            return None
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.runner_compile, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    monkeypatch.setattr(type(runner_reengineer.settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)

    captured = {}

    async def fake_pipeline(paths, **kw):
        captured["mirror_h"] = kw.get("mirror_h")
        captured["paths"] = [str(p) for p in paths]
        out = kw["edit_dir"] / "final.mp4"; out.write_bytes(b"mp4")
        return EditorResult(final=out, voice_applied=False)
    monkeypatch.setattr(runner_reengineer.runner_compile,
                        "run_editor_pipeline", fake_pipeline)

    updates: dict = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.update(kw))

    asyncio.run(runner_reengineer._do_repurpose("re_t", _re_state()))

    # The mirror flag reached the pipeline, over BOTH source clips.
    assert captured["mirror_h"] is True
    assert captured["paths"] == [str(clips[0]), str(clips[1])]
    # Result lands in `repurposed` at its own path — not `finals`.
    rp = updates["repurposed"]["cA"]
    assert rp["status"] == "done"
    assert rp["final_path"].endswith("repurpose_cA.mp4")
    assert rp["edit_id"]
    assert updates["repurposing"] is False and updates["repurposed_at"]
    # The run status / finals must be left ALONE (originals kept).
    assert "finals" not in updates
    assert "status" not in updates
    assert "finals_stale" not in updates


def test_repurpose_slot_invariants():
    """Cheap lock: the two slots stay distinct on every load-bearing field."""
    assert _COMPILE_SLOT.mirror_h is False and _REPURPOSE_SLOT.mirror_h is True
    assert _COMPILE_SLOT.status_field == "compile_status"
    assert _REPURPOSE_SLOT.status_field == "repurpose_status"
    assert _REPURPOSE_SLOT.filename.format(cid="c1") == "c1__repurpose.mp4"
    assert _COMPILE_SLOT.event_prefix != _REPURPOSE_SLOT.event_prefix
