"""En duplicerad scen kostar EN bild, inte en per kopia (Hugo 2026-08-08).

Measured on the live store, job j_94c3258684 (re_feba7996a8): scene
`sc_ee6a1f88f7` had been ⧉-duplicated four times, so the run carried five
scene entries backed by ONE image and ONE prompt — and generated five
DIFFERENT renders. Four wasted image generations per character (36 across
that run's nine), and a reel whose "same" shot visibly jumps between clips.

⧉ duplicate is documented as costing zero image credits, so the copies were
always meant to share the source's image; they only did when the source
already had a ready variant at duplication time.

The subtle half is what must NOT collapse: `images_per_character = N` renders
N slots per scene from the identical prompt and frame ON PURPOSE — they are
the alternatives the approval gate lets the user pick between. The first
version of this fix collapsed those too, which
tests/e2e/test_swap_flow.py::test_full_swap_flow_six_steps caught.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from character_swap import runner
from character_swap.models import (CharStatus, GeneratedImage, Job,
                                   JobCharacter, VariantStatus)


@pytest.fixture
def scenes(tmp_path, monkeypatch):
    """Three scene files: two byte-identical (a duplicate), one different."""
    monkeypatch.setattr(runner, "_emit", _noop_emit)
    a = tmp_path / "sc_a.png"
    a.write_bytes(b"\x89PNG-frame-A")
    dup = tmp_path / "sc_a__dup.png"      # different path, identical bytes
    dup.write_bytes(b"\x89PNG-frame-A")
    b = tmp_path / "sc_b.png"
    b.write_bytes(b"\x89PNG-frame-B")
    runner._file_fingerprint.cache_clear()
    return {"a": a, "dup": dup, "b": b}


async def _noop_emit(*a, **k):
    return None


def _job(scenes, ids_and_paths) -> Job:
    sids = [sid for sid, _ in ids_and_paths]
    paths = [str(p) for _, p in ids_and_paths]
    return Job(job_id="j_dup", scene_id=sids[0], scene_image_path=paths[0],
               scene_ids=sids, scene_image_paths=paths)


def _slot(sid, prompt, vid) -> GeneratedImage:
    return GeneratedImage(variant_id=vid, path=f"/tmp/{vid}.png", prompt=prompt,
                          scene_id=sid, status=VariantStatus.GENERATING)


def _partition(job, placeholders):
    """The exact grouping _kick_char does."""
    leaders, followers, first_of, takes = [], [], {}, {}
    for v in placeholders:
        sid = v.scene_id or ""
        take = takes.get(sid, 0)
        takes[sid] = take + 1
        lead = first_of.setdefault(
            runner._identical_generation_key(job, v, take), v)
        if lead is v:
            leaders.append(v)
        else:
            followers.append((v, lead))
    return leaders, followers


def test_duplicate_scene_renders_once(scenes):
    """THE REPORTED CASE: same image + same prompt on two scene ids."""
    job = _job(scenes, [("sc_a", scenes["a"]), ("sc_a__dup", scenes["dup"])])
    slots = [_slot("sc_a", "P", "v1"), _slot("sc_a__dup", "P", "v2")]
    leaders, followers = _partition(job, slots)
    assert [v.variant_id for v in leaders] == ["v1"]
    assert [(f.variant_id, l.variant_id) for f, l in followers] == [("v2", "v1")]


def test_identical_bytes_at_a_different_path_still_group(scenes):
    """`register_scene_duplicate` writes a SEPARATE file with identical bytes,
    so grouping on the path alone would miss it."""
    assert scenes["a"] != scenes["dup"]
    assert (hashlib.sha256(scenes["a"].read_bytes()).digest()
            == hashlib.sha256(scenes["dup"].read_bytes()).digest())
    job = _job(scenes, [("sc_a", scenes["a"]), ("sc_a__dup", scenes["dup"])])
    k1 = runner._identical_generation_key(job, _slot("sc_a", "P", "v1"), 0)
    k2 = runner._identical_generation_key(job, _slot("sc_a__dup", "P", "v2"), 0)
    assert k1 == k2


def test_multi_variant_per_scene_is_never_collapsed(scenes):
    """images_per_character = N renders N takes of ONE scene from the identical
    prompt on purpose — they are the options the gate offers. Collapsing them
    would silently turn the chooser into a single image."""
    job = _job(scenes, [("sc_a", scenes["a"])])
    slots = [_slot("sc_a", "P", f"v{i}") for i in range(4)]
    leaders, followers = _partition(job, slots)
    assert len(leaders) == 4 and followers == []


def test_duplicate_mirrors_take_for_take(scenes):
    """With N takes per scene, take i of the copy mirrors take i of the
    leader — so every copy still offers N distinct options."""
    job = _job(scenes, [("sc_a", scenes["a"]), ("sc_a__dup", scenes["dup"])])
    slots = ([_slot("sc_a", "P", f"lead{i}") for i in range(3)]
             + [_slot("sc_a__dup", "P", f"dup{i}") for i in range(3)])
    leaders, followers = _partition(job, slots)
    assert [v.variant_id for v in leaders] == ["lead0", "lead1", "lead2"]
    assert [(f.variant_id, l.variant_id) for f, l in followers] == [
        ("dup0", "lead0"), ("dup1", "lead1"), ("dup2", "lead2")]


def test_an_edited_prompt_breaks_the_group(scenes):
    """A copy whose prompt was changed (🪄 ändra bild / ✎↻) is NOT the same
    request and must render on its own."""
    job = _job(scenes, [("sc_a", scenes["a"]), ("sc_a__dup", scenes["dup"])])
    slots = [_slot("sc_a", "P", "v1"), _slot("sc_a__dup", "ANNAN PROMPT", "v2")]
    leaders, followers = _partition(job, slots)
    assert [v.variant_id for v in leaders] == ["v1", "v2"]
    assert followers == []


def test_different_scenes_never_group(scenes):
    job = _job(scenes, [("sc_a", scenes["a"]), ("sc_b", scenes["b"])])
    slots = [_slot("sc_a", "P", "v1"), _slot("sc_b", "P", "v2")]
    leaders, followers = _partition(job, slots)
    assert len(leaders) == 2 and followers == []


def test_a_missing_scene_file_never_groups(scenes, tmp_path):
    """Two unreadable paths must not hash to the same "missing" key and be
    silently treated as copies of each other."""
    job = _job(scenes, [("sc_x", tmp_path / "gone_a.png"),
                        ("sc_y", tmp_path / "gone_b.png")])
    slots = [_slot("sc_x", "P", "v1"), _slot("sc_y", "P", "v2")]
    leaders, followers = _partition(job, slots)
    assert len(leaders) == 2 and followers == []


def test_mirror_copies_the_result_and_the_qc_verdict(scenes, tmp_path):
    img = tmp_path / "rendered.png"
    img.write_bytes(b"rendered-bytes")
    lead = _slot("sc_a", "P", "v1")
    lead.path = str(img)
    lead.status = VariantStatus.READY
    lead.qc_status = "failed"
    lead.qc_reason = "SEVERE ARTIFACTS"
    lead.qc_attempts = 2
    follower = _slot("sc_a__dup", "P", "v2")

    job = _job(scenes, [("sc_a", scenes["a"]), ("sc_a__dup", scenes["dup"])])
    jc = JobCharacter(char_id="c1", name="C", source_image_path="x.png",
                      status=CharStatus.QUEUED, images=[lead, follower])
    job.characters["c1"] = jc

    stragglers = asyncio.run(
        runner._mirror_duplicate_slots(job, jc, [(follower, lead)]))

    assert stragglers == []
    assert follower.path == str(img)
    assert follower.status == VariantStatus.READY
    # The judge saw this exact image once; the copy shows the same verdict.
    assert (follower.qc_status, follower.qc_reason) == ("failed", "SEVERE ARTIFACTS")
    # …but not the leader's rejected takes, which belong to the slot that
    # actually rendered them.
    assert not follower.qc_rejects


def test_a_failed_leader_leaves_its_copies_to_render_normally(scenes):
    """A copy must never be stuck on a slot that produced no image."""
    lead = _slot("sc_a", "P", "v1")
    lead.status = VariantStatus.FAILED
    lead.error = "content policy"
    follower = _slot("sc_a__dup", "P", "v2")
    job = _job(scenes, [("sc_a", scenes["a"]), ("sc_a__dup", scenes["dup"])])
    jc = JobCharacter(char_id="c1", name="C", source_image_path="x.png",
                      status=CharStatus.QUEUED, images=[lead, follower])
    job.characters["c1"] = jc

    stragglers = asyncio.run(
        runner._mirror_duplicate_slots(job, jc, [(follower, lead)]))

    assert stragglers == [follower]
    assert follower.status == VariantStatus.GENERATING


def test_a_leader_whose_file_vanished_leaves_its_copies_to_render(scenes, tmp_path):
    lead = _slot("sc_a", "P", "v1")
    lead.path = str(tmp_path / "never_written.png")
    lead.status = VariantStatus.READY          # status says ready, file is gone
    follower = _slot("sc_a__dup", "P", "v2")
    job = _job(scenes, [("sc_a", scenes["a"]), ("sc_a__dup", scenes["dup"])])
    jc = JobCharacter(char_id="c1", name="C", source_image_path="x.png",
                      status=CharStatus.QUEUED, images=[lead, follower])
    job.characters["c1"] = jc

    assert asyncio.run(
        runner._mirror_duplicate_slots(job, jc, [(follower, lead)])) == [follower]


def test_fingerprint_is_cached_per_file_not_per_lookup(scenes):
    runner._file_fingerprint.cache_clear()
    job = _job(scenes, [("sc_a", scenes["a"])])
    v = _slot("sc_a", "P", "v1")
    for _ in range(5):
        runner._identical_generation_key(job, v, 0)
    info = runner._file_fingerprint.cache_info()
    assert info.misses == 1 and info.hits == 4, (
        "a 9-character run must hash each scene file once, not 9 times")
