"""Auto-clear of the stuck 'ändrad' (dirty) flag — Hugo 2026-06-22 (2nd hit).

The footgun: per-clip ↻ and single-character "gör om scen" redos regenerate a
scene's clips with the current prompt but pass clear_dirty=False (siblings may
be stale). Redo EVERY character's clip one at a time and the scene is fully
fresh — yet the flag stayed stuck, so "▶ Bygg ihop igen" refused the build
forever even though the clips were exactly what the user wanted (re_56fc036309,
scenes 1-2). Re-animating to clear it would throw away the good clips + re-bill.

Fix: stamp `dirty_at` when a scene is flagged, then treat the scene as resolved
once EVERY expected character has an imported clip or a generated clip whose
`submitted_at` is strictly after the edit. The gate skips such scenes and the
poll + assemble endpoint POP the flag. Conservative: a genuinely stale clip
(pre-edit submit), a missing/pending clip, or a flag without a stamp NEVER
auto-clears (refuse-loudly).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from character_swap import runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
)

T0 = datetime(2026, 6, 22, 18, 0, 0)
BEFORE = T0 - timedelta(minutes=5)          # clip generated before the edit (stale)
AFTER = T0 + timedelta(minutes=5)           # clip regenerated after the edit (fresh)
DIRTY_AT = T0.isoformat() + "Z"


def _vv(vid, variant, path, submitted, *, status=VideoStatus.DONE, imported=False):
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid, status=status,
                        source_variant_id=variant,
                        final_video_path=str(path) if path else None,
                        submitted_at=submitted, imported=imported)


def _job(char_videos: dict) -> Job:
    """One scene `s1`; each char approves the image its clip animates."""
    chars = {}
    for cid, vv in char_videos.items():
        var = vv.source_variant_id
        img = GeneratedImage(variant_id=var, path=f"/{var}.png", prompt="p",
                             scene_id="s1", status="ready")
        chars[cid] = JobCharacter(char_id=cid, name=cid, source_image_path="/c.png",
                                  status=CharStatus.APPROVED, images=[img],
                                  approved_variant_ids=[var], videos=[vv])
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               characters=chars, origin="reengineer:re_t")


def _state(*, dirty_at=DIRTY_AT):
    sc = {"idx": 0, "scene_id": "s1", "duration": 2.0,
          "motion_prompt": "a", "speech": "", "summary": "", "dirty": True}
    if dirty_at is not None:
        sc["dirty_at"] = dirty_at
    return {"re_id": "re_t", "job_id": "j1", "status": "awaiting_assembly",
            "scenes": [sc]}


def test_fresh_redo_resolves_and_clears(tmp_path):
    clip = tmp_path / "c.mp4"; clip.write_bytes(b"x")
    job = _job({"cA": _vv("vd1", "v1", clip, AFTER)})       # redone AFTER the edit
    st = _state()
    # The gate no longer counts the scene as dirty...
    assert runner_reengineer._assembly_gaps(st, job)["dirty"] == []
    # ...and the flag (+ stamp) is popped.
    assert runner_reengineer.clear_resolved_dirty(st, job) == [0]
    assert "dirty" not in st["scenes"][0]
    assert "dirty_at" not in st["scenes"][0]


def test_stale_clip_stays_blocked(tmp_path):
    clip = tmp_path / "c.mp4"; clip.write_bytes(b"x")
    job = _job({"cA": _vv("vd1", "v1", clip, BEFORE)})      # predates the edit
    st = _state()
    assert runner_reengineer._assembly_gaps(st, job)["dirty"] == \
        [{"idx": 0, "label": "scen 1"}]
    assert runner_reengineer.clear_resolved_dirty(st, job) == []
    assert st["scenes"][0]["dirty"] is True


def test_legacy_flag_without_stamp_stays_blocked(tmp_path):
    """Old flags set before this fix shipped have no `dirty_at` → we can't prove
    freshness, so they must NOT auto-clear (never false-clear a stale scene)."""
    clip = tmp_path / "c.mp4"; clip.write_bytes(b"x")
    job = _job({"cA": _vv("vd1", "v1", clip, AFTER)})
    st = _state(dirty_at=None)
    assert runner_reengineer._assembly_gaps(st, job)["dirty"] == \
        [{"idx": 0, "label": "scen 1"}]
    assert runner_reengineer.clear_resolved_dirty(st, job) == []


def test_imported_clip_counts_as_fresh(tmp_path):
    """An imported clip never derives from the motion prompt, so even one that
    predates the edit resolves the scene."""
    clip = tmp_path / "c.mp4"; clip.write_bytes(b"x")
    job = _job({"cA": _vv("vd1", "v1", clip, BEFORE, imported=True)})
    st = _state()
    assert runner_reengineer.clear_resolved_dirty(st, job) == [0]


def test_pending_clip_stays_blocked(tmp_path):
    """A redo still rendering (no DONE clip yet) keeps the scene blocked."""
    job = _job({"cA": _vv("vd1", "v1", None, AFTER, status=VideoStatus.PROCESSING)})
    st = _state()
    assert runner_reengineer.clear_resolved_dirty(st, job) == []
    assert runner_reengineer._assembly_gaps(st, job)["dirty"] == \
        [{"idx": 0, "label": "scen 1"}]


def test_partial_per_char_redo_clears_only_when_all_fresh(tmp_path):
    """The exact footgun: redo each character's clip one at a time. The flag
    must stay until EVERY character's clip is fresh, then clear."""
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    # cA redone (fresh), cB still pre-edit (stale) → not resolved yet.
    half = _job({"cA": _vv("vdA", "vA", c1, AFTER),
                 "cB": _vv("vdB", "vB", c2, BEFORE)})
    st = _state()
    assert runner_reengineer.clear_resolved_dirty(st, half) == []
    assert st["scenes"][0]["dirty"] is True

    # Now cB is redone too → fully fresh → clears.
    both = _job({"cA": _vv("vdA", "vA", c1, AFTER),
                 "cB": _vv("vdB", "vB", c2, AFTER)})
    st2 = _state()
    assert runner_reengineer.clear_resolved_dirty(st2, both) == [0]


def test_unapproved_slot_blocks_resolution(tmp_path):
    """A character with image slots for the scene but NO approved image is a
    real coverage gap, not a resolvable one — keep the scene blocked."""
    clip = tmp_path / "c.mp4"; clip.write_bytes(b"x")
    job = _job({"cA": _vv("vd1", "v1", clip, AFTER)})
    # cB has a slot for s1 but nothing approved.
    img = GeneratedImage(variant_id="vB", path="/vB.png", prompt="p",
                         scene_id="s1", status="ready")
    job.characters["cB"] = JobCharacter(
        char_id="cB", name="cB", source_image_path="/c.png",
        status=CharStatus.APPROVED, images=[img],
        approved_variant_ids=[], videos=[])
    st = _state()
    assert runner_reengineer.clear_resolved_dirty(st, job) == []
    assert st["scenes"][0]["dirty"] is True
