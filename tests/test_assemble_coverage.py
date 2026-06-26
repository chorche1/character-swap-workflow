"""Assemble coverage guard (Hugo 2026-06-12, re_57266cfec0).

The video watcher fired ~3s before scene 5's clip finished QC — its row
wasn't DONE yet, the clip collection silently skipped it, and the final
shipped with 5/6 scenes (n_clips=5 in calls.jsonl; the 25.5s final matches
the 5-clip sum). Two defenses:
  1. _do_assemble WAITS (bounded) while an approved scene's clip is
     plausibly still finishing (row in flight or not yet written).
  2. Anything still missing after the wait becomes a LOUD warning on the
     final's card — never a silent drop.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
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
from character_swap.runner_compile import EditorResult


def _clip_row(vid, variant, status, path=None):
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid, status=status,
                        source_variant_id=variant, final_video_path=path)


def _job(videos) -> Job:
    imgs = [GeneratedImage(variant_id=f"v{i}", path=f"/v{i}.png", prompt="p",
                           scene_id=f"s{i}", status="ready")
            for i in (1, 2)]
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/c.png",
                      status=CharStatus.APPROVED, images=imgs,
                      approved_variant_ids=["v1", "v2"], videos=videos)
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1", "s2"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"] * 2,
               characters={"cA": jc}, origin="reengineer:re_t")


def _state() -> dict:
    return {"re_id": "re_t", "job_id": "j1", "status": "assembling",
            "scenes": [
                {"idx": 0, "scene_id": "s1", "duration": 2.0,
                 "motion_prompt": "a", "speech": "", "summary": ""},
                {"idx": 1, "scene_id": "s2", "duration": 2.0,
                 "motion_prompt": "b", "speech": "", "summary": ""}]}


def test_collect_clips_reports_missing_and_waitable(tmp_path):
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    job = _job([_clip_row("vd1", "v1", VideoStatus.DONE, str(c1)),
                _clip_row("vd2", "v2", VideoStatus.PROCESSING)])
    clips, _dialogues, missing, waitable = runner_reengineer._collect_clips(
        _state(), job.characters["cA"])
    assert clips == [c1]
    assert missing == ["scen 2"]
    assert waitable is True                  # clip in flight → poll again

    # FAILED-only clip: missing but NOT waitable (no point polling).
    job2 = _job([_clip_row("vd1", "v1", VideoStatus.DONE, str(c1)),
                 _clip_row("vd2", "v2", VideoStatus.FAILED)])
    _, _, missing2, waitable2 = runner_reengineer._collect_clips(
        _state(), job2.characters["cA"])
    assert missing2 == ["scen 2"] and waitable2 is False

    # No row at all yet (the observed race): waitable.
    job3 = _job([_clip_row("vd1", "v1", VideoStatus.DONE, str(c1))])
    _, _, missing3, waitable3 = runner_reengineer._collect_clips(
        _state(), job3.characters["cA"])
    assert missing3 == ["scen 2"] and waitable3 is True


def _wire(monkeypatch, tmp_path, job, *, later_job=None):
    run_dir = tmp_path / "run"; run_dir.mkdir()
    jobs = [job] if later_job is None else [job, later_job]

    class _S:
        def get_job(self, jid):
            return jobs[-1] if len(jobs) == 1 else jobs.pop(0)

        def get_character(self, cid):
            return None
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.runner_compile, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    monkeypatch.setattr(type(runner_reengineer.settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)
    monkeypatch.setattr(runner_reengineer, "_ASSEMBLE_COVERAGE_WAIT_SECS", 0.3)
    monkeypatch.setattr(runner_reengineer, "_ASSEMBLE_COVERAGE_POLL_SECS", 0.05)
    calls: list[dict] = []

    async def fake_pipeline(paths, **kw):
        calls.append({"paths": [str(p) for p in paths]})
        out = kw["edit_dir"] / "final.mp4"; out.write_bytes(b"mp4")
        return EditorResult(final=out, voice_applied=False)
    monkeypatch.setattr(runner_reengineer.runner_compile,
                        "run_editor_pipeline", fake_pipeline)
    updates: dict = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.update(kw))
    return updates, calls


def test_assemble_waits_for_lagging_clip(monkeypatch, tmp_path):
    """The observed race: a clip row flips DONE seconds after the watcher
    fires. Assembly must pick it up instead of building 5/6."""
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    early = _job([_clip_row("vd1", "v1", VideoStatus.DONE, str(c1)),
                  _clip_row("vd2", "v2", VideoStatus.PROCESSING)])
    late = _job([_clip_row("vd1", "v1", VideoStatus.DONE, str(c1)),
                 _clip_row("vd2", "v2", VideoStatus.DONE, str(c2))])
    updates, calls = _wire(monkeypatch, tmp_path, early, later_job=late)

    asyncio.run(runner_reengineer._do_assemble("re_t", _state()))
    assert calls[0]["paths"] == [str(c1), str(c2)]      # BOTH clips
    assert updates["finals"]["cA"].get("warning") is None or \
        "saknar" not in (updates["finals"]["cA"].get("warning") or "")


def test_assemble_fails_loudly_when_scene_truly_missing(monkeypatch, tmp_path):
    """Hugo 2026-06-17: an incomplete final is NEVER built. A scene the
    character should have whose clip FAILED fails the WHOLE character loudly
    (with the missing scene named) instead of silently concatenating a
    shorter video."""
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    job = _job([_clip_row("vd1", "v1", VideoStatus.DONE, str(c1)),
                _clip_row("vd2", "v2", VideoStatus.FAILED)])
    updates, calls = _wire(monkeypatch, tmp_path, job)

    asyncio.run(runner_reengineer._do_assemble("re_t", _state()))
    assert calls == []                                  # no short final built
    fin = updates["finals"]["cA"]
    assert fin["status"] == "failed"
    assert "saknar 1 scen(er): scen 2" in fin["error"]  # loud, never silent


def test_assembly_gaps_categorizes_dirty_hard_pending(tmp_path):
    """_assembly_gaps mirrors _collect_clips' inclusion rules and sorts every
    incompleteness into dirty / hard / pending so the rebuild endpoint can
    refuse with an actionable message."""
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")

    # Clean: both clips DONE + on disk, no dirty scene → no gaps.
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    clean = _job([_clip_row("vd1", "v1", VideoStatus.DONE, str(c1)),
                  _clip_row("vd2", "v2", VideoStatus.DONE, str(c2))])
    g = runner_reengineer._assembly_gaps(_state(), clean)
    assert g == {"dirty": [], "hard": [], "pending": [], "excluded": []}

    # Scene 0 edited but not re-animated → dirty (even though its clip is DONE).
    st = _state(); st["scenes"][0]["dirty"] = True
    g = runner_reengineer._assembly_gaps(st, clean)
    assert g["dirty"] == [{"idx": 0, "label": "scen 1"}]
    assert g["hard"] == [] and g["pending"] == []

    # FAILED clip → hard; in-flight clip → pending.
    mixed = _job([_clip_row("vd1", "v1", VideoStatus.FAILED),
                  _clip_row("vd2", "v2", VideoStatus.PROCESSING)])
    g = runner_reengineer._assembly_gaps(_state(), mixed)
    assert [x["label"] for x in g["hard"]] == ["scen 1"]
    assert g["hard"][0]["char_id"] == "cA"
    assert [x["label"] for x in g["pending"]] == ["scen 2"]


# ---------------------------------------------------------------------------
# Never-approved character no longer blocks "Bygg ihop" (Hugo 2026-06-27).
# Silas had 9 ready images but approved NONE of them and animated NONE — the
# gate counted all 9 as "ingen godkänd bild" hard gaps and refused the whole
# build (re_3bedfe62d3). Such a character is not part of the reel: skip it.

def _uninvolved_char(cid="cSilas", name="Silas"):
    """A character with images for every scene but ZERO approvals + ZERO clips
    — added to the run, never used."""
    imgs = [GeneratedImage(variant_id=f"u{i}", path=f"/u{i}.png", prompt="p",
                           scene_id=f"s{i}", status="ready")
            for i in (1, 2)]
    return JobCharacter(char_id=cid, name=name, source_image_path="/s.png",
                        status=CharStatus.AWAITING_APPROVAL, images=imgs,
                        approved_variant_ids=[], videos=[])


def _job_with(*chars: JobCharacter) -> Job:
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1", "s2"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"] * 2,
               characters={c.char_id: c for c in chars},
               origin="reengineer:re_t")


def test_uninvolved_char_is_excluded_not_blocked(tmp_path):
    """A character never approved on ANY swap scene is reported in `excluded`
    (a soft note) instead of producing hard gaps that block the rebuild."""
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    done = _job([_clip_row("vd1", "v1", VideoStatus.DONE, str(c1)),
                 _clip_row("vd2", "v2", VideoStatus.DONE, str(c2))])
    job = _job_with(done.characters["cA"], _uninvolved_char())

    assert runner_reengineer._char_is_uninvolved(_state(), job.characters["cSilas"])
    assert not runner_reengineer._char_is_uninvolved(_state(), job.characters["cA"])

    g = runner_reengineer._assembly_gaps(_state(), job)
    assert g["hard"] == [] and g["pending"] == [] and g["dirty"] == []
    assert g["excluded"] == [{"char_id": "cSilas", "name": "Silas"}]


def test_partial_char_still_blocks_loudly(tmp_path):
    """Refuse-loudly preserved: a character with at least one approval but a
    withdrawn approval on another scene is a TRUE gap, NOT excluded."""
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    imgs = [GeneratedImage(variant_id="v1", path="/v1.png", prompt="p",
                           scene_id="s1", status="ready"),
            GeneratedImage(variant_id="v2", path="/v2.png", prompt="p",
                           scene_id="s2", status="ready")]
    # Approved on s1 only (s2's approval withdrawn by a scene-image regen).
    partial = JobCharacter(char_id="cP", name="P", source_image_path="/c.png",
                           status=CharStatus.APPROVED, images=imgs,
                           approved_variant_ids=["v1"],
                           videos=[_clip_row("vd1", "v1", VideoStatus.DONE, str(c1))])
    job = _job_with(partial)
    assert not runner_reengineer._char_is_uninvolved(_state(), partial)
    g = runner_reengineer._assembly_gaps(_state(), job)
    assert g["excluded"] == []
    assert [(x["char_id"], x["label"], x["reason"]) for x in g["hard"]] == [
        ("cP", "scen 2", "ingen godkänd bild")]


def test_pure_direct_char_never_uninvolved():
    """A pure-direct run has no per-character variants → zero approvals by
    construction. The character is NOT uninvolved — its shared direct clips
    are its build."""
    direct_state = {"re_id": "re_t", "job_id": "j1", "status": "assembling",
                    "scenes": [{"idx": 0, "scene_id": "s1", "is_direct": True,
                                "motion_prompt": "a", "speech": "", "summary": ""}]}
    jc = JobCharacter(char_id="cD", name="D", source_image_path="/c.png",
                      status=CharStatus.APPROVED, images=[],
                      approved_variant_ids=[], videos=[])
    assert not runner_reengineer._char_is_uninvolved(direct_state, jc)


def test_do_assemble_skips_uninvolved_char(monkeypatch, tmp_path):
    """The build itself skips the never-approved character: only the real
    character gets a final, the run lands `done` (not partial_success)."""
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    done = _job([_clip_row("vd1", "v1", VideoStatus.DONE, str(c1)),
                 _clip_row("vd2", "v2", VideoStatus.DONE, str(c2))])
    job = _job_with(done.characters["cA"], _uninvolved_char())
    updates, calls = _wire(monkeypatch, tmp_path, job)

    asyncio.run(runner_reengineer._do_assemble("re_t", _state()))
    assert set(updates["finals"].keys()) == {"cA"}      # Silas absent, not failed
    assert updates["finals"]["cA"]["status"] == "done"
    assert updates["status"] == "done"                  # not partial_success
    assert len(calls) == 1                              # one build, for cA only
