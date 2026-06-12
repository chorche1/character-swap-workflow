"""Clip-review gate (Hugo 2026-06-12): without ⚡ fully automatic, the run
STOPS after the video phase at status `awaiting_assembly` — every Kling clip
is reviewable/redoable per scene and the ⚙ Editor settings are adjustable —
and only the explicit ▶ Bygg ihop click runs the final build. auto_mode=True
keeps the old auto-assemble. The gate is a user state: crash-resume leaves
it alone, and the edit/redo endpoints accept it.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from character_swap import runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
)


def _job_with_done_video() -> Job:
    v = GeneratedImage(variant_id="va", path="/a.png", prompt="p",
                       scene_id="s1", status="ready")
    jc = JobCharacter(
        char_id="cA", name="A", source_image_path="/c.png",
        status=CharStatus.APPROVED, images=[v],
        approved_variant_ids=["va"],
        videos=[VideoVariant(video_id="vd1", grok_job_id="g1",
                             status=VideoStatus.DONE, source_variant_id="va",
                             final_video_path="/clip.mp4")])
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               characters={"cA": jc}, origin="reengineer:re_t")


def _wire_watch(monkeypatch, *, auto_mode: bool):
    from types import SimpleNamespace
    job = _job_with_done_video()
    monkeypatch.setattr(runner_reengineer, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job))
    monkeypatch.setattr(runner_reengineer, "_POLL_SECS", 0.01)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: {"re_id": re_id, "job_id": "j1",
                                       "auto_mode": auto_mode})
    updates: list[dict] = []
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.append(kw))
    assembled: list[str] = []

    async def fake_assemble(re_id):
        assembled.append(re_id)
    monkeypatch.setattr(runner_reengineer, "assemble", fake_assemble)
    return updates, assembled


def test_video_phase_gates_without_auto_mode(monkeypatch):
    updates, assembled = _wire_watch(monkeypatch, auto_mode=False)
    asyncio.run(runner_reengineer._watch_video_phase("re_t", "j1"))
    assert assembled == []                          # NO auto-build
    assert updates and updates[-1]["status"] == "awaiting_assembly"


def test_video_phase_auto_assembles_with_auto_mode(monkeypatch):
    updates, assembled = _wire_watch(monkeypatch, auto_mode=True)
    asyncio.run(runner_reengineer._watch_video_phase("re_t", "j1"))
    assert assembled == ["re_t"]
    assert not any(u.get("status") == "awaiting_assembly" for u in updates)


def test_resume_leaves_clip_review_gate_alone(monkeypatch):
    monkeypatch.setattr(runner_reengineer.reengineer, "list_states",
                        lambda: [{"re_id": "re_g",
                                  "status": "awaiting_assembly"}])
    spawned: list[str] = []
    monkeypatch.setattr(runner_reengineer, "_spawn",
                        lambda coro, name: (spawned.append(name),
                                            coro.close()))
    asyncio.run(runner_reengineer.resume_all())
    assert spawned == []                            # user gate — untouched


def test_gate_state_is_editable_and_redoable():
    assert "awaiting_assembly" in runner_reengineer._EDITABLE_RUN_STATES
    # The redo endpoints' explicit status sets accept the gate.
    import inspect
    from character_swap import api
    src = inspect.getsource(api)
    assert src.count('statuses={"awaiting_assembly", "done", '
                     '"partial_success", "failed"}') >= 2


def test_clip_review_gate_ui_wired():
    root = Path(__file__).resolve().parents[1]
    html = (root / "web" / "index.html").read_text(encoding="utf-8")
    assert "r.status === 'awaiting_assembly'" in html
    assert "▶ Bygg ihop</button>" in html
    js = (root / "web" / "app.js").read_text(encoding="utf-8")
    assert "'awaiting_assembly'" in js.split("reEditable(r)")[1][:300]
    assert "re-clips-" in js                        # gate milestone chime
