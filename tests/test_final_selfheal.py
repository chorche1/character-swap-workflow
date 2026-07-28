"""Self-healing finals (Hugo 2026-07-29, run re_f786da400c).

Two clips FAILED during the animate phase, so the auto-assemble refused those
two characters' finals — correct per "never ship a shorter final in silence".
The user then retried the clips, they succeeded, and NOTHING rebuilt the
finals: every clip present, two finals missing, and the only way back was
knowing to press "▶ Bygg ihop igen".

Now every clip completion re-checks the run: a character whose final FAILED is
rebuilt as soon as ITS last gap closes. Guarded so it never:
  • rebuilds a final that is already done (no re-billing, no re-send),
  • builds while a clip is still rendering,
  • fires before the first build has run at all,
  • clobbers the finals it didn't rebuild.
"""
from __future__ import annotations

import asyncio

from character_swap import auto_finalize, runner, runner_reengineer
from character_swap.models import (
    CharStatus, GeneratedImage, Job, JobCharacter, VideoStatus, VideoVariant)
from character_swap.runner_compile import EditorResult


def _clip(vid, variant, status, path=None):
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid, status=status,
                        source_variant_id=variant, final_video_path=path)


def _char(cid, videos):
    img = GeneratedImage(variant_id=f"v_{cid}", path=f"/{cid}.png", prompt="p",
                         scene_id="s1", status="ready")
    return JobCharacter(char_id=cid, name=cid.upper(),
                        source_image_path="/c.png", status=CharStatus.APPROVED,
                        images=[img], approved_variant_ids=[f"v_{cid}"],
                        videos=videos)


def _job(chars) -> Job:
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"],
               characters={c.char_id: c for c in chars},
               origin="reengineer:re_t")


def _state(finals: dict | None = None, status: str = "partial_success") -> dict:
    st = {"re_id": "re_t", "job_id": "j1", "status": status,
          "scenes": [{"idx": 0, "scene_id": "s1", "duration": 2.0,
                      "motion_prompt": "a", "speech": "", "summary": ""}]}
    if finals is not None:
        st["finals"] = finals
    return st


def _wire_store(monkeypatch, job, state):
    class _S:
        def get_job(self, jid):
            return job if jid == job.job_id else None

        def get_character(self, cid):
            return None
    monkeypatch.setattr(runner_reengineer, "store", lambda: _S())
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda rid: state if rid == "re_t" else None)


# --------------------------------------------------------------- partial build

def test_partial_assemble_keeps_the_finals_it_did_not_rebuild(
        monkeypatch, tmp_path):
    """`only={cB}` builds ONLY cB and merges — cA's finished final survives
    verbatim (a heal must never re-render or drop a good final)."""
    good = tmp_path / "a.mp4"; good.write_bytes(b"x")
    clip = tmp_path / "b.mp4"; clip.write_bytes(b"x")
    job = _job([_char("ca", [_clip("vd_a", "v_ca", VideoStatus.DONE, str(good))]),
                _char("cb", [_clip("vd_b", "v_cb", VideoStatus.DONE, str(clip))])])
    prior = {"ca": {"status": "done", "final_path": str(good), "n_clips": 1,
                    "edit_id": "ed_old"},
             "cb": {"status": "failed", "error": "finalen saknar 1 scen(er)"}}
    state = _state(prior)
    _wire_store(monkeypatch, job, state)

    run_dir = tmp_path / "run"; run_dir.mkdir()
    monkeypatch.setattr(runner_reengineer.reengineer, "reengineer_dir",
                        lambda rid: run_dir)
    monkeypatch.setattr(type(runner_reengineer.settings), "output_dir",
                        property(lambda self: tmp_path / "out"), raising=False)
    monkeypatch.setattr(runner_reengineer.runner_compile, "store",
                        lambda: type("S", (), {"get_job": lambda s, j: job})())
    built: list[list[str]] = []

    async def fake_pipeline(paths, **kw):
        built.append([str(p) for p in paths])
        out = kw["edit_dir"] / "final.mp4"; out.write_bytes(b"mp4")
        return EditorResult(final=out, voice_applied=False)
    monkeypatch.setattr(runner_reengineer.runner_compile,
                        "run_editor_pipeline", fake_pipeline)
    updates: dict = {}
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.update(kw))

    asyncio.run(runner_reengineer._do_assemble("re_t", state, only={"cb"}))

    assert built == [[str(clip)]]                    # only the broken char
    finals = updates["finals"]
    assert finals["ca"] == prior["ca"]               # untouched, not rebuilt
    assert finals["cb"]["status"] == "done"
    assert updates["status"] == "done"               # whole run recovered
    # A PARTIAL build says nothing about the characters it skipped.
    assert "finals_stale" not in updates


# ------------------------------------------------------------- heal candidates

def _wire_heal(monkeypatch, job, state):
    _wire_store(monkeypatch, job, state)
    seen: list = []

    async def fake_assemble(re_id, only=None):
        seen.append((re_id, only))
    monkeypatch.setattr(runner_reengineer, "assemble", fake_assemble)
    return seen


def test_heal_rebuilds_only_the_failed_char_once_its_clip_lands(
        monkeypatch, tmp_path):
    clip = tmp_path / "b.mp4"; clip.write_bytes(b"x")
    job = _job([_char("ca", [_clip("vd_a", "v_ca", VideoStatus.DONE, str(clip))]),
                _char("cb", [_clip("vd_b", "v_cb", VideoStatus.DONE, str(clip))])])
    state = _state({"ca": {"status": "done", "final_path": str(clip)},
                    "cb": {"status": "failed", "error": "saknar"}})
    seen = _wire_heal(monkeypatch, job, state)

    asyncio.run(runner_reengineer.heal_failed_finals("j1"))
    assert seen == [("re_t", {"cb"})]


def test_heal_waits_while_the_clip_is_still_rendering(monkeypatch, tmp_path):
    """The gap isn't closed yet — rebuilding now would fail all over again."""
    clip = tmp_path / "a.mp4"; clip.write_bytes(b"x")
    job = _job([_char("ca", [_clip("vd_a", "v_ca", VideoStatus.DONE, str(clip))]),
                _char("cb", [_clip("vd_b", "v_cb", VideoStatus.PROCESSING)])])
    state = _state({"ca": {"status": "done"}, "cb": {"status": "failed"}})
    seen = _wire_heal(monkeypatch, job, state)

    asyncio.run(runner_reengineer.heal_failed_finals("j1"))
    assert seen == []


def test_heal_is_a_noop_before_the_first_build(monkeypatch, tmp_path):
    """During the initial animate phase there are no finals yet — the video
    watcher owns that build; healing must not race it."""
    clip = tmp_path / "a.mp4"; clip.write_bytes(b"x")
    job = _job([_char("ca", [_clip("vd_a", "v_ca", VideoStatus.DONE, str(clip))])])
    seen = _wire_heal(monkeypatch, job, _state(None, status="animating"))

    asyncio.run(runner_reengineer.heal_failed_finals("j1"))
    assert seen == []


def test_heal_never_rebuilds_a_finished_final(monkeypatch, tmp_path):
    """Re-rendering a good final would re-bill Whisper/captions and re-send it."""
    clip = tmp_path / "a.mp4"; clip.write_bytes(b"x")
    job = _job([_char("ca", [_clip("vd_a", "v_ca", VideoStatus.DONE, str(clip))])])
    state = _state({"ca": {"status": "done", "final_path": str(clip)}},
                   status="done")
    seen = _wire_heal(monkeypatch, job, state)

    asyncio.run(runner_reengineer.heal_failed_finals("j1"))
    assert seen == []


def test_heal_skips_while_a_build_is_already_in_flight(monkeypatch, tmp_path):
    clip = tmp_path / "a.mp4"; clip.write_bytes(b"x")
    job = _job([_char("cb", [_clip("vd_b", "v_cb", VideoStatus.DONE, str(clip))])])
    state = _state({"cb": {"status": "failed"}})
    seen = _wire_heal(monkeypatch, job, state)

    runner_reengineer._ASSEMBLING.add("re_t")
    try:
        asyncio.run(runner_reengineer.heal_failed_finals("j1"))
    finally:
        runner_reengineer._ASSEMBLING.discard("re_t")
    assert seen == []


# ----------------------------------------------------------------- the hook

def test_after_clip_done_heals_reengineer_jobs_only(monkeypatch):
    called: list[str] = []

    async def fake_heal(job_id):
        called.append(job_id)
    monkeypatch.setattr(runner_reengineer, "heal_failed_finals", fake_heal)

    re_job = _job([_char("ca", [])])
    asyncio.run(runner._after_clip_done(re_job))
    assert called == ["j1"]

    plain = _job([_char("ca", [])])
    plain.origin = None                       # a normal Swap job
    asyncio.run(runner._after_clip_done(plain))
    assert called == ["j1"]                   # unchanged — Step-6 owns that flow


def test_after_clip_done_never_raises(monkeypatch):
    """A heal failure must not turn a finished clip into a failed one."""
    async def boom(job_id):
        raise RuntimeError("state write failed")
    monkeypatch.setattr(runner_reengineer, "heal_failed_finals", boom)
    asyncio.run(runner._after_clip_done(_job([_char("ca", [])])))   # no raise


# ------------------------------------------------------- Telegram send filter

def test_healed_final_send_is_limited_to_the_rebuilt_chars(monkeypatch,
                                                           tmp_path):
    """The heal rewrites ONE character's file — the others must not be
    re-delivered to Telegram."""
    a = tmp_path / "a.mp4"; a.write_bytes(b"x")
    b = tmp_path / "b.mp4"; b.write_bytes(b"x")
    state = {"re_id": "re_t", "job_id": "j1", "status": "done",
             "auto_telegram_send": True, "title": "run",
             "finals": {"ca": {"status": "done", "final_path": str(a)},
                        "cb": {"status": "done", "final_path": str(b)}}}
    from character_swap import reengineer as reengineer_mod
    monkeypatch.setattr(reengineer_mod, "load_state", lambda rid: state)
    monkeypatch.setattr(reengineer_mod, "save_state", lambda s: None)

    class _S:
        def get_character(self, cid):
            return type("C", (), {"name": cid, "telegram_chat_id": "@x"})()
    monkeypatch.setattr(auto_finalize, "store", lambda: _S())
    sent: list[str] = []

    async def fake_send(path, **kw):
        sent.append(kw["char_name"])
        return {"ok": True}
    monkeypatch.setattr(auto_finalize.telegram_delivery,
                        "send_character_final", fake_send)
    monkeypatch.setattr(auto_finalize, "_notify_telegram_result",
                        lambda *a, **k: None)

    asyncio.run(auto_finalize.send_reengineer_finals("re_t", only={"cb"}))
    assert sent == ["cb"]
