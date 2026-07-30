"""Auto-build re-entry after a clip lands POST-gate (Hugo 2026-07-31).

`_watch_video_phase` is a one-shot watcher: when it found a dead clip it
parked the run at `awaiting_assembly` and returned. Retaking the clip with ↻
therefore filled the gap but started nothing — a run that had ticked "Bygg
ihop + skicka till Telegram automatiskt" still sat there waiting for a manual
click, which is exactly what the checkbox promises you don't have to do.

`runner.retry_one_video` (and every other post-gate clip path) already calls
`_maybe_auto_finalize_job`; it just dropped reengineer-backed jobs on the
floor. It now re-enters `maybe_auto_assemble_after_clip`, which re-runs the
SAME gate the watcher would have.

Locked here:
  - opt-in is preserved: a run that kept its manual clip-review gate is never
    built behind the user's back;
  - the build only fires when the LAST gap closes, not on the first retry;
  - a finished run rebuilds ONLY the character whose clip changed (Hugo chose
    rebuild-and-resend, so the scope decides what gets reposted);
  - "reanimating" keeps its documented finish-without-assembling behavior.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from character_swap import runner, runner_reengineer
from character_swap.models import (
    CharStatus,
    GeneratedImage,
    Job,
    JobCharacter,
    VideoStatus,
    VideoVariant,
)


def _char(cid: str, clips: list[VideoVariant]) -> JobCharacter:
    imgs = [GeneratedImage(variant_id=f"{cid}v{i}", path=f"/{cid}v{i}.png",
                           prompt="p", scene_id=f"s{i}", status="ready")
            for i in (1, 2)]
    return JobCharacter(char_id=cid, name=cid.upper(),
                        source_image_path="/c.png", status=CharStatus.APPROVED,
                        images=imgs, approved_variant_ids=[f"{cid}v1", f"{cid}v2"],
                        videos=clips)


def _done(vid: str, variant: str, path) -> VideoVariant:
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid,
                        status=VideoStatus.DONE, source_variant_id=variant,
                        final_video_path=str(path))


def _failed(vid: str, variant: str) -> VideoVariant:
    return VideoVariant(video_id=vid, grok_job_id="g_" + vid,
                        status=VideoStatus.FAILED, source_variant_id=variant,
                        error="content policy")


def _job(chars: dict[str, JobCharacter]) -> Job:
    return Job(job_id="j1", title="t", scene_id="s1", scene_ids=["s1", "s2"],
               scene_image_path="/p.png", scene_image_paths=["/p.png"] * 2,
               characters=chars, origin="reengineer:re_t")


def _state(**kw) -> dict:
    state = {"re_id": "re_t", "job_id": "j1", "status": "awaiting_assembly",
             "from_images": True, "auto_telegram_send": True,
             "scenes": [{"idx": 0, "scene_id": "s1", "duration": 2.0,
                         "motion_prompt": "a", "speech": "", "summary": ""},
                        {"idx": 1, "scene_id": "s2", "duration": 2.0,
                         "motion_prompt": "b", "speech": "", "summary": ""}]}
    state.update(kw)
    return state


def _wire(monkeypatch, job: Job, state: dict):
    store = SimpleNamespace(get_job=lambda jid: job)
    monkeypatch.setattr(runner_reengineer, "store", lambda: store)
    monkeypatch.setattr(runner_reengineer.reengineer, "load_state",
                        lambda re_id: dict(state))
    updates: list[dict] = []
    monkeypatch.setattr(runner_reengineer, "_update",
                        lambda re_id, **kw: updates.append(kw))
    built: list[list[str] | None] = []

    async def fake_assemble(re_id, *, char_ids=None):
        built.append(char_ids)
    monkeypatch.setattr(runner_reengineer, "assemble", fake_assemble)
    return updates, built


def _two_char_job(tmp_path, *, b_clip2):
    """A + B, both approved on s1+s2. A is complete; B's second clip varies."""
    ok = tmp_path / "c.mp4"
    ok.write_bytes(b"x")
    return _job({
        "cA": _char("cA", [_done("a1", "cAv1", ok), _done("a2", "cAv2", ok)]),
        "cB": _char("cB", [_done("b1", "cBv1", ok), b_clip2]),
    })


def test_retry_closing_last_gap_builds_every_character(monkeypatch, tmp_path):
    """THE regression: the ↻ retry landed the missing clip → build now."""
    ok = tmp_path / "c.mp4"
    job = _two_char_job(tmp_path, b_clip2=_done("b2", "cBv2", ok))
    updates, built = _wire(monkeypatch, job, _state())

    asyncio.run(runner_reengineer.maybe_auto_assemble_after_clip(job, "cB"))

    assert built == [["cA", "cB"]]      # nothing had a final yet → build both
    assert updates == []                # gate untouched; assemble owns status


def test_no_build_while_another_clip_is_still_missing(monkeypatch, tmp_path):
    """First of several retries lands → still a gap → stay parked."""
    job = _two_char_job(tmp_path, b_clip2=_failed("b2", "cBv2"))
    updates, built = _wire(monkeypatch, job, _state())

    asyncio.run(runner_reengineer.maybe_auto_assemble_after_clip(job, "cA"))

    assert built == []
    # The banner is refreshed so it names what's ACTUALLY left, not the
    # original list from when the watcher gave up.
    assert updates[-1]["status"] == "awaiting_assembly"
    assert updates[-1]["auto_assemble_blocked"]
    assert "scen 2" in updates[-1]["error"]


def test_manual_gate_run_is_never_built_behind_the_users_back(monkeypatch,
                                                              tmp_path):
    """No ⚡ and no auto-Telegram → the clip-review gate still means something."""
    ok = tmp_path / "c.mp4"
    job = _two_char_job(tmp_path, b_clip2=_done("b2", "cBv2", ok))
    _, built = _wire(monkeypatch, job,
                     _state(from_images=True, auto_telegram_send=False))

    asyncio.run(runner_reengineer.maybe_auto_assemble_after_clip(job, "cB"))

    assert built == []


def test_finished_run_rebuilds_only_the_changed_character(monkeypatch,
                                                          tmp_path):
    """Hugo's choice: redoing one clip on a done run rebuilds + reposts — but
    only that character, so the other finals aren't re-billed or re-sent."""
    ok = tmp_path / "c.mp4"
    fa = tmp_path / "final_a.mp4"; fa.write_bytes(b"a")
    fb = tmp_path / "final_b.mp4"; fb.write_bytes(b"b")
    job = _two_char_job(tmp_path, b_clip2=_done("b2", "cBv2", ok))
    _, built = _wire(monkeypatch, job, _state(
        status="done",
        finals={"cA": {"status": "done", "final_path": str(fa)},
                "cB": {"status": "done", "final_path": str(fb)}}))

    asyncio.run(runner_reengineer.maybe_auto_assemble_after_clip(job, "cB"))

    assert built == [["cB"]]


def test_finished_run_also_rebuilds_a_character_whose_final_is_gone(
        monkeypatch, tmp_path):
    """A missing/failed final is a gap too — rebuild it alongside the change."""
    ok = tmp_path / "c.mp4"
    fb = tmp_path / "final_b.mp4"; fb.write_bytes(b"b")
    job = _two_char_job(tmp_path, b_clip2=_done("b2", "cBv2", ok))
    _, built = _wire(monkeypatch, job, _state(
        status="done",
        finals={"cA": {"status": "failed", "error": "boom"},
                "cB": {"status": "done", "final_path": str(fb)}}))

    asyncio.run(runner_reengineer.maybe_auto_assemble_after_clip(job, "cB"))

    assert built == [["cA", "cB"]]


def test_reanimating_run_keeps_finishing_without_assembling(monkeypatch,
                                                            tmp_path):
    """▶ Animera om ändrade is specified to NOT auto-assemble; unchanged."""
    ok = tmp_path / "c.mp4"
    job = _two_char_job(tmp_path, b_clip2=_done("b2", "cBv2", ok))
    _, built = _wire(monkeypatch, job, _state(status="reanimating"))

    asyncio.run(runner_reengineer.maybe_auto_assemble_after_clip(job, "cB"))

    assert built == []


def test_plain_swap_job_still_uses_the_swap_finalizer(monkeypatch, tmp_path):
    """The reengineer branch must not swallow classic-Swap jobs."""
    ok = tmp_path / "c.mp4"
    job = _job({"cA": _char("cA", [_done("a1", "cAv1", ok)])})
    job.origin = None
    monkeypatch.setattr(runner, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job))
    called: list[str] = []

    async def fake_finalize(job_id):
        called.append(job_id)
    monkeypatch.setattr("character_swap.auto_finalize.finalize_swap_job",
                        fake_finalize)

    asyncio.run(runner._maybe_auto_finalize_job("j1", "cA"))

    assert called == ["j1"]


def test_reengineer_job_routes_to_the_assemble_gate(monkeypatch, tmp_path):
    """…and reengineer jobs reach the new gate instead of returning early."""
    ok = tmp_path / "c.mp4"
    job = _two_char_job(tmp_path, b_clip2=_done("b2", "cBv2", ok))
    monkeypatch.setattr(runner, "store",
                        lambda: SimpleNamespace(get_job=lambda jid: job))
    seen: list[tuple] = []

    async def fake_gate(j, char_id=None):
        seen.append((j.job_id, char_id))
    monkeypatch.setattr(runner_reengineer, "maybe_auto_assemble_after_clip",
                        fake_gate)

    asyncio.run(runner._maybe_auto_finalize_job("j1", "cB"))

    assert seen == [("j1", "cB")]
