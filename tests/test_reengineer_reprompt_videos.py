"""One-click per-scene video re-prompt for the Swap/Reengineer flow
(`POST /api/reengineer/{re_id}/scenes/{idx}/reprompt_videos`, Hugo 2026-06-23).

Sets the scene's motion prompt (synced onto the backing job), marks it dirty +
finals stale, then redoes every non-imported, not-in-flight clip of the scene
for ALL characters. **Lock-free + concurrent** (Hugo 2026-06-24): it fans the
clips out via the SAME per-clip `retry_one_video` path as the `✎↻ prompt` button
(carrying the new prompt as a per-clip override) instead of the run-locked
`reanimate` engine — so a scene can be regenerated WHILE another scene of the
same run is still rendering.

Hugo 2026-07-29: the fan-out is ONE background task that gathers every clip.
It used to add one background task PER clip — and Starlette awaits background
tasks SEQUENTIALLY, so a 7-character scene regenerated one clip at a time
(~6 min each ≈ 40 min instead of ~6) while every not-yet-started character kept
showing yesterday's clip, making the button look like it did nothing. The
targeted clips are also marked PENDING before the response so the run card
immediately shows the "🎬 renderar…" badge on every affected character.

These tests use a hermetic state store + a real Job (chars/images/clips) and
assert: the prompt is written + synced, the scene goes dirty + finals stale,
one retry_one_video runs per eligible clip carrying the new prompt, the clips
run CONCURRENTLY, a failing clip never abandons its siblings, the queue marker
is set up-front (and a clip that never reached a submit fails loudly instead of
spinning forever), the endpoint is NOT blocked while the run is animating,
clipless approved chars fall back to generate_more_videos, imported/in-flight
clips are skipped, and the guards fire — empty prompt → 400, direct scene →
409, missing job → 409, no eligible clips → 409, forbidden status → 409,
out-of-range idx → 404.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from character_swap import api, runner, runner_reengineer
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VariantStatus,
    VideoStatus, VideoVariant,
)


@pytest.fixture
def wired(monkeypatch):
    box: dict = {"states": {}, "synced": [], "job": None}
    from character_swap import reengineer as reengineer_mod

    def load_state(re_id):
        s = box["states"].get(re_id)
        return dict(s) if s else None

    def save_state(s):
        box["states"][s["re_id"]] = dict(s)

    monkeypatch.setattr(reengineer_mod, "load_state", load_state)
    monkeypatch.setattr(reengineer_mod, "save_state", save_state)
    monkeypatch.setattr(runner_reengineer, "_ANIMATING", set())

    def fake_sync(job, state, idxs):
        box["synced"].append((id(job), list(idxs),
                              [state["scenes"][i]["motion_prompt"] for i in idxs]))
    monkeypatch.setattr(runner_reengineer, "_sync_movement_from_state", fake_sync)

    class _S:
        def get_job(self, jid):
            return box["job"] if box["job"] and box["job"].job_id == jid else None

        def get_scene(self, sid):          # _reengineer_view builds scene URLs
            return None

        def update_job(self, job):         # runner._persist (queue marker)
            box["persists"].append(job.job_id)
    box["persists"] = []
    for mod in (api, runner, runner_reengineer):
        monkeypatch.setattr(mod, "store", lambda: _S())
    return box


def _char(cid, *, clip_status=VideoStatus.DONE, imported=False, with_clip=True):
    img = GeneratedImage(variant_id=f"v_{cid}", path="/tmp/a.png",
                         prompt="(uploaded)", scene_id="s1",
                         status=VariantStatus.READY)
    videos = []
    if with_clip:
        videos.append(VideoVariant(
            video_id=f"vd_{cid}", grok_job_id="g", status=clip_status,
            source_variant_id=f"v_{cid}", imported=imported,
            final_video_path="/tmp/v.mp4"))
    return JobCharacter(
        char_id=cid, name=cid.upper(), source_image_path="/tmp/a.png",
        status=CharStatus.DONE, images=[img],
        approved_variant_ids=[f"v_{cid}"], approved_variant_id=f"v_{cid}",
        videos=videos)


def _job(chars):
    return Job(job_id="j1", title="t", scene_id="s1",
               scene_image_path="/tmp/s1.png", scene_ids=["s1"],
               scene_image_paths=["/tmp/s1.png"],
               movement_prompts={"s1": "old prompt"},
               characters={c.char_id: c for c in chars})


def _state(*, is_direct=False, status="done"):
    return {"re_id": "re_t", "status": status, "job_id": "j1",
            "finals": {"cA": {"path": "/tmp/final.mp4"}},
            "scenes": [{"idx": 0, "scene_id": "s1", "start": 0.0, "end": 5.0,
                        "duration": 5.0, "motion_prompt": "old prompt",
                        "speech": "", "summary": "one",
                        **({"is_direct": True} if is_direct else {})}]}


def _fanout(bg):
    """(retry, generate, prompt) off the SINGLE scheduled fan-out task.

    Asserting there is exactly one task is the regression lock: one task per
    clip made Starlette run the clips sequentially (Hugo 2026-07-29)."""
    tasks = [t for t in bg.tasks
             if t.args and t.args[0] is runner_reengineer.reprompt_scene_clips]
    assert len(bg.tasks) == len(tasks) <= 1, (
        "the scene's clips must fan out in ONE gathering task, not one "
        f"background task per clip (got {len(bg.tasks)})")
    if not tasks:
        return [], [], None
    _fn, job_id, retry, generate, prompt = tasks[0].args
    assert job_id == "j1"
    return retry, generate, prompt


def _retries(bg):
    """{char_id: prompt} for every clip redone in place."""
    retry, _generate, prompt = _fanout(bg)
    return {cid: prompt for cid, _video_id in retry}


def _generates(bg):
    """{char_id: (source_variant_id, prompt_override)} per clipless char."""
    _retry, generate, prompt = _fanout(bg)
    return {cid: (varid, prompt) for cid, varid in generate}


def test_reprompt_fans_out_per_clip_with_new_prompt(wired):
    wired["states"]["re_t"] = _state()
    wired["job"] = _job([_char("cA"), _char("cB")])
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_reprompt_scene_videos(
        "re_t", 0, bg, api.ReRepromptVideosBody(prompt="  NEW motion  ")))

    st = wired["states"]["re_t"]
    entry = st["scenes"][0]
    assert entry["motion_prompt"] == "NEW motion"       # stripped + stored
    assert entry.get("dirty") and entry.get("dirty_at")  # dirty + stamped
    assert st.get("finals_stale")
    assert wired["synced"] and wired["synced"][0][2] == ["NEW motion"]
    # One retry_one_video per character, each carrying the new prompt verbatim.
    assert _retries(bg) == {"cA": "NEW motion", "cB": "NEW motion"}
    assert _generates(bg) == {}


def test_clips_are_marked_rendering_before_the_response(wired):
    """THE VISIBILITY FIX (Hugo 2026-07-29): every targeted clip flips to
    PENDING (→ "🎬 renderar…") and its char to ANIMATING before the endpoint
    returns. Before this the response still carried each character's OLD
    finished clip, so a 7-char re-prompt looked like nothing happened."""
    wired["states"]["re_t"] = _state()
    wired["job"] = _job([_char("cA"), _char("cB")])
    asyncio.run(api.reengineer_reprompt_scene_videos(
        "re_t", 0, BackgroundTasks(), api.ReRepromptVideosBody(prompt="P")))

    for cid in ("cA", "cB"):
        jc = wired["job"].characters[cid]
        assert jc.videos[0].status == VideoStatus.PENDING
        assert jc.status == CharStatus.ANIMATING
    assert wired["persists"], "the queue marker must be persisted"


def test_fanout_runs_clips_concurrently(wired, monkeypatch):
    """THE SPEED FIX: the clips render in PARALLEL. One background task per
    clip made Starlette await them one-by-one (~6 min each ≈ 40 min for 7
    characters); every other animation path gathers."""
    inflight, peak, order = [], [], []

    async def fake_retry(job_id, cid, video_id, prompt=None):
        inflight.append(cid)
        peak.append(len(inflight))
        await asyncio.sleep(0)          # yield: a sequential runner drains here
        order.append(cid)
        inflight.remove(cid)
    monkeypatch.setattr(runner, "retry_one_video", fake_retry)

    wired["job"] = _job([_char("cA"), _char("cB"), _char("cC")])
    asyncio.run(runner_reengineer.reprompt_scene_clips(
        "j1", [("cA", "vd_cA"), ("cB", "vd_cB"), ("cC", "vd_cC")], [], "P"))

    assert max(peak) == 3, f"clips ran sequentially (peak in-flight {max(peak)})"
    assert sorted(order) == ["cA", "cB", "cC"]


def test_one_failing_clip_never_abandons_its_siblings(wired, monkeypatch):
    done = []

    async def fake_retry(job_id, cid, video_id, prompt=None):
        if cid == "cA":
            raise RuntimeError("fal exploded")
        done.append(cid)
    monkeypatch.setattr(runner, "retry_one_video", fake_retry)

    wired["job"] = _job([_char("cA"), _char("cB"), _char("cC")])
    asyncio.run(runner_reengineer.reprompt_scene_clips(
        "j1", [("cA", "vd_cA"), ("cB", "vd_cB"), ("cC", "vd_cC")], [], "P"))
    assert sorted(done) == ["cB", "cC"]


def test_clip_that_never_reached_a_submit_fails_loudly(wired, monkeypatch):
    """A guard inside retry_one_video (approval withdrawn, char gone) used to
    leave the queue marker spinning forever. It must fail loudly instead."""
    async def noop(job_id, cid, video_id, prompt=None):
        return None
    monkeypatch.setattr(runner, "retry_one_video", noop)

    wired["states"]["re_t"] = _state()
    wired["job"] = _job([_char("cA")])
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_reprompt_scene_videos(
        "re_t", 0, bg, api.ReRepromptVideosBody(prompt="P")))
    clip = wired["job"].characters["cA"].videos[0]
    assert clip.status == VideoStatus.PENDING          # queued by the endpoint
    clip.grok_job_id = ""                             # never submitted

    asyncio.run(runner_reengineer.reprompt_scene_clips(
        "j1", [("cA", "vd_cA")], [], "P"))
    assert clip.status == VideoStatus.FAILED
    assert "ta om det" in (clip.error or "")


def test_not_blocked_while_run_is_animating(wired, monkeypatch):
    # THE FIX: a scene can be reprompted while another scene of the run renders.
    wired["states"]["re_t"] = _state()
    wired["job"] = _job([_char("cA"), _char("cB")])
    monkeypatch.setattr(runner_reengineer, "_ANIMATING", {"re_t"})
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_reprompt_scene_videos(
        "re_t", 0, bg, api.ReRepromptVideosBody(prompt="P")))
    assert _retries(bg) == {"cA": "P", "cB": "P"}        # scheduled, not 409


def test_clipless_char_generates_with_override(wired):
    wired["states"]["re_t"] = _state()
    wired["job"] = _job([_char("cA"), _char("cB", with_clip=False)])
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_reprompt_scene_videos(
        "re_t", 0, bg, api.ReRepromptVideosBody(prompt="P")))
    assert _retries(bg) == {"cA": "P"}                   # existing clip → retry
    assert _generates(bg) == {"cB": ("v_cB", "P")}       # no clip → generate


def test_imported_and_in_flight_clips_skipped(wired):
    # cA imported (authoritative), cB in flight (PROCESSING) → neither redone.
    wired["states"]["re_t"] = _state()
    wired["job"] = _job([
        _char("cA", imported=True),
        _char("cB", clip_status=VideoStatus.PROCESSING),
        _char("cC"),
    ])
    bg = BackgroundTasks()
    asyncio.run(api.reengineer_reprompt_scene_videos(
        "re_t", 0, bg, api.ReRepromptVideosBody(prompt="P")))
    assert _retries(bg) == {"cC": "P"}
    assert _generates(bg) == {}


def test_no_eligible_clips_409(wired):
    # Every char's clip is imported → nothing to regenerate.
    wired["states"]["re_t"] = _state()
    wired["job"] = _job([_char("cA", imported=True), _char("cB", imported=True)])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, BackgroundTasks(), api.ReRepromptVideosBody(prompt="P")))
    assert ei.value.status_code == 409


def test_empty_prompt_rejected(wired):
    wired["states"]["re_t"] = _state()
    wired["job"] = _job([_char("cA")])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, BackgroundTasks(), api.ReRepromptVideosBody(prompt="   ")))
    assert ei.value.status_code == 400


def test_direct_scene_rejected(wired):
    wired["states"]["re_t"] = _state(is_direct=True)
    wired["job"] = _job([_char("cA")])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, BackgroundTasks(), api.ReRepromptVideosBody(prompt="x")))
    assert ei.value.status_code == 409


def test_missing_job_refuses_loudly(wired):
    wired["states"]["re_t"] = _state()
    wired["job"] = None
    bg = BackgroundTasks()
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, bg, api.ReRepromptVideosBody(prompt="new")))
    assert ei.value.status_code == 409
    # State untouched, nothing scheduled.
    assert wired["states"]["re_t"]["scenes"][0]["motion_prompt"] == "old prompt"
    assert not wired["states"]["re_t"]["scenes"][0].get("dirty")
    assert bg.tasks == []


def test_forbidden_status_rejected(wired):
    wired["states"]["re_t"] = _state(status="awaiting_approval")
    wired["job"] = _job([_char("cA")])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 0, BackgroundTasks(), api.ReRepromptVideosBody(prompt="x")))
    assert ei.value.status_code == 409


def test_out_of_range_idx_404(wired):
    wired["states"]["re_t"] = _state()
    wired["job"] = _job([_char("cA")])
    with pytest.raises(HTTPException) as ei:
        asyncio.run(api.reengineer_reprompt_scene_videos(
            "re_t", 5, BackgroundTasks(), api.ReRepromptVideosBody(prompt="x")))
    assert ei.value.status_code == 404
