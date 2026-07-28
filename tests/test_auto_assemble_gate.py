"""⚡ auto-build gate (Hugo 2026-07-29): "det ska bara byggas ihop automatiskt
när alla klipp för alla karaktärer är färdigt genererade."

`_watch_video_phase` waits for every clip to go TERMINAL — which includes
FAILED. In ⚡ auto_mode that used to trigger `assemble()` anyway: the character
with the dead clip failed loudly *inside* the build, after Whisper/Remotion had
already billed for the others. The run now parks at the manual
`awaiting_assembly` gate with the reason on the card (and a louder phone push),
so the clip can be retaken before ▶ Bygg ihop.

Non-goals locked here too: a merely `dirty` scene must NOT block (Hugo
2026-06-24 removed that requirement) and a never-approved character is
`excluded`, not a blocker (Hugo 2026-06-27).
"""
from __future__ import annotations

import asyncio
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


def _job(clip_rows: dict[str, VideoVariant | None], *,
         extra_chars: dict[str, JobCharacter] | None = None) -> Job:
    """One character ("A") approved on s1 + s2, with the given clip per scene."""
    imgs = [GeneratedImage(variant_id=f"v{i}", path=f"/v{i}.png", prompt="p",
                           scene_id=f"s{i}", status="ready")
            for i in (1, 2)]
    jc = JobCharacter(char_id="cA", name="A", source_image_path="/c.png",
                      status=CharStatus.APPROVED, images=imgs,
                      approved_variant_ids=["v1", "v2"],
                      videos=[r for r in clip_rows.values() if r is not None])
    chars = {"cA": jc}
    chars.update(extra_chars or {})
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1", "s2"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"] * 2,
               characters=chars, origin="reengineer:re_t")


def _done(vid: str, variant: str, path) -> VideoVariant:
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid,
                        status=VideoStatus.DONE, source_variant_id=variant,
                        final_video_path=str(path))


def _failed(vid: str, variant: str) -> VideoVariant:
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid,
                        status=VideoStatus.FAILED, source_variant_id=variant,
                        error="content policy")


def _state(**kw) -> dict:
    state = {"re_id": "re_t", "job_id": "j1", "auto_mode": True,
             "scenes": [{"idx": 0, "scene_id": "s1", "duration": 2.0,
                         "motion_prompt": "a", "speech": "", "summary": ""},
                        {"idx": 1, "scene_id": "s2", "duration": 2.0,
                         "motion_prompt": "b", "speech": "", "summary": ""}]}
    state.update(kw)
    return state


def _wire(monkeypatch, job: Job, state: dict):
    monkeypatch.setattr(runner_reengineer, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job))
    monkeypatch.setattr(runner_reengineer, "_POLL_SECS", 0.01)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: dict(state))
    updates: list[dict] = []
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.append(kw))
    assembled: list[str] = []

    async def fake_assemble(re_id):
        assembled.append(re_id)
    monkeypatch.setattr(runner_reengineer, "assemble", fake_assemble)
    return updates, assembled


def test_auto_build_refuses_when_a_clip_failed(monkeypatch, tmp_path):
    """THE regression: one dead clip, ⚡ on → no build, gate + reason instead."""
    ok = tmp_path / "c1.mp4"; ok.write_bytes(b"x")
    job = _job({"s1": _done("vd1", "v1", ok), "s2": _failed("vd2", "v2")})
    updates, assembled = _wire(monkeypatch, job, _state())

    asyncio.run(runner_reengineer._watch_video_phase("re_t", "j1"))

    assert assembled == []                          # nothing was built
    last = updates[-1]
    assert last["status"] == "awaiting_assembly"
    assert last["auto_assemble_blocked"]            # marker for the push/UI
    # The banner names the scene + character so the fix needs no digging.
    assert "scen 2" in last["error"] and "A" in last["error"]
    assert "Bygg ihop" in last["error"]


def test_image_sourced_auto_build_refuses_when_a_clip_failed(monkeypatch,
                                                             tmp_path):
    """Hugo's actual case: the Swap tab (from_images) assembles off
    `auto_telegram_send` WITHOUT ⚡ auto_mode — that trigger needs the same
    all-clips-succeeded gate, not just the ⚡ one."""
    ok = tmp_path / "c1.mp4"; ok.write_bytes(b"x")
    job = _job({"s1": _done("vd1", "v1", ok), "s2": _failed("vd2", "v2")})
    updates, assembled = _wire(
        monkeypatch, job,
        _state(auto_mode=False, from_images=True, auto_telegram_send=True))

    asyncio.run(runner_reengineer._watch_video_phase("re_t", "j1"))

    assert assembled == []
    assert updates[-1]["status"] == "awaiting_assembly"
    assert "scen 2" in updates[-1]["error"]


def test_image_sourced_auto_build_runs_when_every_clip_is_done(monkeypatch,
                                                              tmp_path):
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    job = _job({"s1": _done("vd1", "v1", c1), "s2": _done("vd2", "v2", c2)})
    updates, assembled = _wire(
        monkeypatch, job,
        _state(auto_mode=False, from_images=True, auto_telegram_send=True))

    asyncio.run(runner_reengineer._watch_video_phase("re_t", "j1"))

    assert assembled == ["re_t"]                    # trigger still works


def test_auto_build_refuses_while_a_clip_still_renders(monkeypatch, tmp_path):
    ok = tmp_path / "c1.mp4"; ok.write_bytes(b"x")
    rendering = VideoVariant(video_id="vd2", grok_job_id="g2",
                             status=VideoStatus.PROCESSING,
                             source_variant_id="v2")
    job = _job({"s1": _done("vd1", "v1", ok), "s2": rendering})
    updates, assembled = _wire(monkeypatch, job, _state())

    # The phase loop itself never exits while a clip is non-terminal, so drive
    # the gate directly — this pins the "pending counts as a blocker" rule.
    blocked = runner_reengineer._auto_assemble_blockers("re_t", "j1")
    assert [b["label"] for b in blocked] == ["scen 2"]
    assert "renderas" in blocked[0]["reason"]
    assert updates == [] and assembled == []


def test_auto_build_runs_when_every_clip_is_done(monkeypatch, tmp_path):
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    job = _job({"s1": _done("vd1", "v1", c1), "s2": _done("vd2", "v2", c2)})
    updates, assembled = _wire(monkeypatch, job, _state())

    asyncio.run(runner_reengineer._watch_video_phase("re_t", "j1"))

    assert assembled == ["re_t"]
    assert not any(u.get("status") == "awaiting_assembly" for u in updates)


def test_dirty_scene_alone_never_blocks_the_auto_build(monkeypatch, tmp_path):
    """Hugo 2026-06-24: a stale (edited-but-not-reanimated) scene builds."""
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    job = _job({"s1": _done("vd1", "v1", c1), "s2": _done("vd2", "v2", c2)})
    state = _state()
    state["scenes"][1]["dirty"] = True
    updates, assembled = _wire(monkeypatch, job, state)

    asyncio.run(runner_reengineer._watch_video_phase("re_t", "j1"))

    assert assembled == ["re_t"]


def test_never_approved_character_never_blocks_the_auto_build(monkeypatch,
                                                              tmp_path):
    """Hugo 2026-06-27: an unused character is excluded, not a hard gap."""
    c1 = tmp_path / "c1.mp4"; c1.write_bytes(b"x")
    c2 = tmp_path / "c2.mp4"; c2.write_bytes(b"x")
    idle = JobCharacter(char_id="cB", name="B", source_image_path="/b.png",
                        status=CharStatus.QUEUED, images=[],
                        approved_variant_ids=[], videos=[])
    job = _job({"s1": _done("vd1", "v1", c1), "s2": _done("vd2", "v2", c2)},
               extra_chars={"cB": idle})
    updates, assembled = _wire(monkeypatch, job, _state())

    asyncio.run(runner_reengineer._watch_video_phase("re_t", "j1"))

    assert assembled == ["re_t"]


def test_manual_gate_is_unchanged_without_auto_mode(monkeypatch, tmp_path):
    """auto_mode=False still parks at the gate — with NO refusal banner."""
    ok = tmp_path / "c1.mp4"; ok.write_bytes(b"x")
    job = _job({"s1": _done("vd1", "v1", ok), "s2": _failed("vd2", "v2")})
    updates, assembled = _wire(monkeypatch, job, _state(auto_mode=False))

    asyncio.run(runner_reengineer._watch_video_phase("re_t", "j1"))

    assert assembled == []
    assert updates[-1]["status"] == "awaiting_assembly"
    assert "auto_assemble_blocked" not in updates[-1]
    assert "error" not in updates[-1]


def test_blocked_gate_pushes_loudly_with_the_reason(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(runner_reengineer.push, "notify",
                        lambda title, body, **kw: sent.append((title, body, kw)))
    runner_reengineer._push_status(
        {"scenes": [{}], "error": "Byggde INTE ihop automatiskt — scen 2 (A)",
         "auto_assemble_blocked": [{"label": "scen 2", "name": "A"}]},
        "awaiting_assembly")
    title, body, kw = sent[0]
    assert "Bygger inte ihop" in title
    assert "scen 2" in body
    assert kw["priority"] >= 4                      # louder than a normal gate


def test_plain_gate_push_is_unchanged(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(runner_reengineer.push, "notify",
                        lambda title, body, **kw: sent.append((title, body, kw)))
    runner_reengineer._push_status({"scenes": [{}]}, "awaiting_assembly")
    title, _body, kw = sent[0]
    assert "redo att bygga" in title and kw["priority"] == 3


def test_build_clears_a_stale_refusal_marker():
    import inspect
    src = inspect.getsource(runner_reengineer._do_assemble)
    assert 'auto_assemble_blocked=None' in src
