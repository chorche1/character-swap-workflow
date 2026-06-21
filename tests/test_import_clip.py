"""Import-own-clip (Hugo 2026-06-21): replace ONE (char × scene) generated
clip with a user-uploaded video.

Locks the four load-bearing guarantees:
  1. The resolvers (`runner.pick_clip_for_variant`, reengineer `_collect_clips`,
     compile `_ordered_scene_videos`) PREFER an imported take over a generated
     one — so the final actually uses the imported clip.
  2. `runner.attach_imported_clip` replaces a take IN PLACE (same video_id) and
     marks it DONE + imported with the file copied into the char's output dir.
  3. …and CREATES a clip when the slot had none, keyed to the approved variant.
  4. A bulk re-animate (`_do_reanimate`) NEVER clobbers an imported clip, while
     a sibling character's stale clip still re-animates.
"""
from __future__ import annotations

import asyncio

from character_swap import runner, runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
)
from character_swap.runner_compile import _ordered_scene_videos
from character_swap.state import store


def _img(vid, sid):
    return GeneratedImage(variant_id=vid, path=f"/{vid}.png", prompt="p",
                          scene_id=sid, status="ready")


def _clip(vid, variant, status, path=None, imported=False):
    return VideoVariant(video_id=vid, grok_job_id="", status=status,
                        source_variant_id=variant, final_video_path=path,
                        imported=imported)


def _job(videos, *, job_id="j1", chars=None) -> Job:
    if chars is None:
        jc = JobCharacter(char_id="cA", name="A", source_image_path="/c.png",
                          status=CharStatus.APPROVED, images=[_img("v1", "s1")],
                          approved_variant_ids=["v1"], videos=videos)
        chars = {"cA": jc}
    return Job(job_id=job_id, title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               characters=chars, origin="reengineer:re_t",
               movement_prompt="x")


def _state(re_id="re_t", job_id="j1"):
    return {"re_id": re_id, "job_id": job_id, "status": "awaiting_assembly",
            "scenes": [{"idx": 0, "scene_id": "s1", "duration": 2.0,
                        "motion_prompt": "a", "speech": "", "summary": ""}]}


# ---------------------------------------------------------------- prefer-imported

def test_pick_clip_prefers_imported(tmp_path):
    gen = tmp_path / "gen.mp4"; gen.write_bytes(b"g")
    imp = tmp_path / "imp.mp4"; imp.write_bytes(b"i")
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/c.png",
                      images=[_img("v1", "s1")], approved_variant_ids=["v1"],
                      videos=[_clip("vd_gen", "v1", VideoStatus.DONE, str(gen)),
                              _clip("vd_imp", "v1", VideoStatus.DONE, str(imp),
                                    imported=True)])
    picked = runner.pick_clip_for_variant(jc, "v1")
    assert picked is not None and picked.video_id == "vd_imp"
    assert picked.imported is True


def test_collect_clips_uses_imported(tmp_path):
    gen = tmp_path / "gen.mp4"; gen.write_bytes(b"g")
    imp = tmp_path / "imp.mp4"; imp.write_bytes(b"i")
    job = _job([_clip("vd_gen", "v1", VideoStatus.DONE, str(gen)),
                _clip("vd_imp", "v1", VideoStatus.DONE, str(imp), imported=True)])
    clips, missing, _ = runner_reengineer._collect_clips(_state(), job.characters["cA"])
    assert missing == []
    assert clips == [imp]


def test_compile_orders_prefer_imported(tmp_path):
    gen = tmp_path / "gen.mp4"; gen.write_bytes(b"g")
    imp = tmp_path / "imp.mp4"; imp.write_bytes(b"i")
    job = _job([_clip("vd_gen", "v1", VideoStatus.DONE, str(gen)),
                _clip("vd_imp", "v1", VideoStatus.DONE, str(imp), imported=True)])
    paths, missing = _ordered_scene_videos(job, job.characters["cA"])
    assert missing == []
    assert paths == [imp]


# ------------------------------------------------------------- attach (real store)

def test_attach_replaces_in_place(tmp_path):
    src = tmp_path / "mine.mov"; src.write_bytes(b"hello-clip")
    job = _job([_clip("vd1", "v1", VideoStatus.DONE, "/old/video_vd1.mp4")],
               job_id="j_attach_inplace")
    store().add_job(job)

    out = asyncio.run(runner.attach_imported_clip(
        "j_attach_inplace", "cA", src, video_id="vd1"))
    assert out is not None
    assert out.video_id == "vd1"                 # replaced IN PLACE
    assert out.imported is True
    assert out.status == VideoStatus.DONE
    assert out.source_variant_id == "v1"

    fresh = store().get_job("j_attach_inplace")
    rows = [v for v in fresh.characters["cA"].videos if v.source_variant_id == "v1"]
    assert len(rows) == 1                          # no orphan/ghost row
    v = rows[0]
    assert v.imported and v.video_id == "vd1"
    from pathlib import Path
    assert Path(v.final_video_path).exists()
    assert Path(v.final_video_path).read_bytes() == b"hello-clip"
    assert "imported_" in Path(v.final_video_path).name


def test_attach_creates_when_no_clip(tmp_path):
    src = tmp_path / "mine.mp4"; src.write_bytes(b"fresh-clip")
    job = _job([], job_id="j_attach_create")       # approved image, NO clip yet
    store().add_job(job)

    out = asyncio.run(runner.attach_imported_clip(
        "j_attach_create", "cA", src, variant_id="v1"))
    assert out is not None and out.imported is True
    assert out.source_variant_id == "v1"

    fresh = store().get_job("j_attach_create")
    picked = runner.pick_clip_for_variant(fresh.characters["cA"], "v1")
    assert picked is not None and picked.imported is True
    from pathlib import Path
    assert Path(picked.final_video_path).read_bytes() == b"fresh-clip"


# --------------------------------------------------------- re-animate never clobbers

def test_reanimate_skips_imported(tmp_path, monkeypatch):
    imp = tmp_path / "imp.mp4"; imp.write_bytes(b"i")
    gen = tmp_path / "gen.mp4"; gen.write_bytes(b"g")
    cA = JobCharacter(char_id="cA", name="A", source_image_path="/a.png",
                      status=CharStatus.APPROVED, images=[_img("vA", "s1")],
                      approved_variant_ids=["vA"],
                      videos=[_clip("vd_imp", "vA", VideoStatus.DONE, str(imp),
                                    imported=True)])
    cB = JobCharacter(char_id="cB", name="B", source_image_path="/b.png",
                      status=CharStatus.APPROVED, images=[_img("vB", "s1")],
                      approved_variant_ids=["vB"],
                      videos=[_clip("vd_gen", "vB", VideoStatus.DONE, str(gen))])
    job = _job(None, job_id="j_reanim", chars={"cA": cA, "cB": cB})
    store().add_job(job)

    retried: list[tuple] = []
    generated: list[tuple] = []

    async def fake_retry(job_id, cid, video_id, *a, **k):
        retried.append((cid, video_id))

    async def fake_more(job_id, cid, n, *a, **k):
        generated.append((cid, n))

    monkeypatch.setattr(runner, "retry_one_video", fake_retry)
    monkeypatch.setattr(runner, "generate_more_videos", fake_more)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: _state(re_id="re_reanim", job_id="j_reanim"))
    monkeypatch.setattr(runner_reengineer, "_update", lambda re_id, **kw: None)
    monkeypatch.setattr(runner_reengineer, "_finalize_reanimate",
                        lambda re_id, **kw: None)

    asyncio.run(runner_reengineer._do_reanimate(
        "re_reanim", [0], char_id=None, clear_dirty=True))

    # cB's stale generated clip re-animates; cA's imported clip is untouched.
    assert retried == [("cB", "vd_gen")]
    assert generated == []
