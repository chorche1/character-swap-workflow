"""Auto-finalize: build final videos + push them to Drive automatically.

Hugo's directive (2026-07-19): "när jag trycker generate videos och alla videor
blir lyckade" the finals should build + upload to Drive with NO manual clicks.
Two entry points, one per flow — both gated on a per-run checkbox (default ON):

  • `finalize_swap_job(job_id)` — the classic Swap job flow. Called at the end
    of `runner.run_video_synthesis` (non-reengineer jobs only). When EVERY
    approved character's clips are DONE (zero failed) it runs the Step-6
    `compile_job_videos`, then pushes each finished final to Drive.

  • `push_reengineer_finals(re_id)` — the Reengineer / Swap-from-images flow.
    Called after `assemble()` finishes. That flow ALREADY auto-assembles the
    finals; the only new step is the Drive push once the run lands `done`
    (every included character built).

Everything is best-effort + LOUD on failure: a compile failure is already
surfaced per-character by `runner_compile`; a Drive-auth problem is pushed to
the phone + a job event (never a silent skip), and the compiled finals are kept
so the user can run the one-time Drive login and push manually. The chain never
raises back into the video/assemble phase — the callers wrap it defensively too.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from character_swap import drive_push, events, push, runner_compile
from character_swap.models import Job, VideoStatus
from character_swap.state import store

logger = logging.getLogger(__name__)


# Mirrors the frontend Step-6 seed (web/app.js `_compileDefault`, incl. Hugo
# 2026-07-17 voice-swap-ON) so an AUTO compile produces the same result a
# manual Step-6 click would on a fresh job. A job that WAS compiled manually
# before overrides these from its persisted `compile_settings`.
_DEFAULT_COMPILE_SETTINGS: dict = {
    "template": "capcut-bluebox",
    "overrides": None,
    "enable_trim": True,
    "enable_captions": True,
    "enable_wpm_normalize": False,
    "target_wpm": 190.0,
    "threshold_db": -24.0,
    "min_silence_secs": 0.4,
    "pad_secs": 0.1,
    "enable_gap_trim": False,
    "gap_max_secs": 0.35,
    "voice_override": None,
    "enable_voice_swap": True,
}
# The subset of persisted `compile_settings` keys that `compile_job_videos`
# accepts (char_ids / persist_settings are per-click controls, not settings).
_COMPILE_KEYS = frozenset(_DEFAULT_COMPILE_SETTINGS)


async def _emit(job_id: str | None, kind: str, **data) -> None:
    if not job_id:
        return
    payload = {"kind": kind, "job_id": job_id,
               "ts": datetime.utcnow().isoformat() + "Z"}
    payload.update(data)
    await events.publish(job_id, payload)


def _all_videos_successful(job: Job) -> bool:
    """True iff EVERY character with an approval has clips AND all of them are
    DONE (none pending / failed / errored) — Hugo's "alla videor blir lyckade"
    gate. A single failed clip returns False, so the auto chain waits for the
    user's retry instead of shipping a final that silently drops a scene."""
    approved = [jc for jc in job.characters.values()
                if (jc.approved_variant_ids or jc.approved_variant_id)]
    if not approved:
        return False
    saw_done_video = False
    for jc in approved:
        if not jc.videos:
            return False           # approved but nothing rendered yet
        for v in jc.videos:
            if v.status != VideoStatus.DONE:
                return False       # pending / failed / errored → not all good
            saw_done_video = True
    return saw_done_video


def _resolve_compile_settings(job: Job) -> dict:
    """The kwargs to drive `compile_job_videos`: the frontend defaults, then
    the job's own persisted Step-6 preset layered on top (if it was ever
    compiled manually)."""
    out = dict(_DEFAULT_COMPILE_SETTINGS)
    if job.compile_settings:
        out.update({k: v for k, v in job.compile_settings.items()
                    if k in _COMPILE_KEYS})
    return out


async def finalize_swap_job(job_id: str) -> None:
    """Auto-compile + auto-push for the classic Swap flow. No-op unless the
    job opted in, isn't reengineer-backed, and every approved character's clips
    succeeded. Safe to call more than once — it won't rebuild a job whose
    finals are already compiled, and it won't race a manual compile in flight."""
    s = store()
    job = s.get_job(job_id)
    if job is None or job.from_reengineer or not job.auto_compile_push:
        return
    if not _all_videos_successful(job):
        return
    # Don't collide with a manual compile the user kicked off, and don't rebuild
    # if we've already finalized (idempotent on a duplicate trigger).
    compilable = [jc for jc in job.characters.values()
                  if runner_compile._eligible_for_compile(jc)]
    if not compilable:
        return
    if any(jc.compile_status == "compiling" for jc in compilable):
        return
    if all(jc.compile_status == "done" for jc in compilable):
        return

    logger.info("auto-finalize %s: all clips DONE — compiling %d character(s)",
                job_id, len(compilable))
    await _emit(job_id, "job.auto_finalize_started", n=len(compilable))
    try:
        await runner_compile.compile_job_videos(
            job_id, **_resolve_compile_settings(job))
    except Exception:
        logger.exception("auto-finalize %s: compile failed", job_id)
        # compile_job_videos already marks per-character failures; nothing more
        # to push, so stop here.
        return
    await _push_job_finals(job_id)


async def _push_job_finals(job_id: str) -> None:
    """Push every DONE compiled final of a Swap job to Drive, persisting the
    receipts onto `JobCharacter.drive_pushes['final']` (so the ⬆ Drive ✓ shows
    on reload) and reporting the outcome loudly."""
    from character_swap.clients import google_drive

    s = store()
    job = s.get_job(job_id)
    if job is None:
        return
    base = drive_push.drive_safe_name(job.title or "swap")
    targets: list[tuple[str, Path, str, str | None]] = []
    for cid, jc in job.characters.items():
        path = jc.compiled_video_path
        if path and jc.compile_status == "done" and Path(path).is_file():
            prior = (jc.drive_pushes.get("final") or {}).get("file_id")
            targets.append((cid, Path(path),
                            drive_push.drive_safe_name(jc.name or cid), prior))
    if not targets:
        return

    # Pre-flight auth ONCE: if the drive.file token is missing/lost, fail loud
    # + bail (the finals are compiled + kept — the user runs the one-time ⬆
    # Drive login and pushes manually). A background task can't do the
    # interactive OAuth bootstrap the UI's 409 path does.
    try:
        await asyncio.to_thread(google_drive.ensure_folder_path,
                                [drive_push.DRIVE_ROOT_FOLDER])
    except google_drive.DriveNotAuthorized as e:
        push.notify("Drive-push hoppad över",
                    "Finalerna byggdes men Drive är inte auktoriserad — kör "
                    "engångs-inloggningen (⬆ Drive) och pusha om.",
                    priority=4, tags=["warning"])
        await _emit(job_id, "job.auto_drive_skipped", reason=str(e))
        return
    except RuntimeError:
        pass  # transient folder error — let the per-file attempts surface it

    pushed: dict[str, dict] = {}
    failed: dict[str, str] = {}
    for cid, path, char_name, prior in targets:
        filename = drive_push.drive_final_name(char_name, base, "final", job_id)
        try:
            pushed[cid] = await drive_push.push_file_core(
                path, [char_name], filename, prior_file_id=prior)
        except Exception as e:  # noqa: BLE001 — record + keep going
            failed[cid] = f"{type(e).__name__}: {e}"
            logger.warning("auto-finalize %s: drive push failed for %s: %s",
                           job_id, cid, e)

    if pushed:
        # Reload before save — the compile persisted meanwhile; write only the
        # receipts so we don't clobber concurrent state.
        fresh = s.get_job(job_id)
        if fresh is not None:
            for cid, receipt in pushed.items():
                jc = fresh.characters.get(cid)
                if jc is not None:
                    jc.drive_pushes["final"] = receipt
            s.update_job(fresh)
    await _emit(job_id, "job.auto_drive_pushed",
                pushed=list(pushed), failed=failed)
    _notify_drive_result("Slutvideor", len(pushed), len(failed))


async def push_reengineer_finals(re_id: str) -> None:
    """Auto-push a Reengineer run's finished finals to Drive. No-op unless the
    run opted in AND landed `done` (every included character built) — the
    assemble step already produced the finals; this only uploads them."""
    from character_swap import reengineer as reengineer_mod
    from character_swap.clients import google_drive

    state = reengineer_mod.load_state(re_id)
    if not state or not state.get("auto_drive_push"):
        return
    # Only when the whole run succeeded — mirrors "alla videor blir lyckade".
    if state.get("status") != "done":
        return
    finals = state.get("finals") or {}
    ready = {cid: e for cid, e in finals.items()
             if e.get("status") == "done" and e.get("final_path")
             and Path(e["final_path"]).is_file()}
    if not ready:
        return

    job_id = state.get("job_id")
    base_src = state.get("title") or state.get("name")
    if not base_src and job_id:
        linked = store().get_job(job_id)
        base_src = linked.title if linked else None
    base = drive_push.drive_safe_name(base_src or re_id)

    try:
        await asyncio.to_thread(google_drive.ensure_folder_path,
                                [drive_push.DRIVE_ROOT_FOLDER])
    except google_drive.DriveNotAuthorized as e:
        push.notify("Drive-push hoppad över",
                    "Reengineer-finalerna byggdes men Drive är inte "
                    "auktoriserad — kör engångs-inloggningen och pusha om.",
                    priority=4, tags=["warning"])
        await _emit(job_id, "job.auto_drive_skipped", reason=str(e))
        return
    except RuntimeError:
        pass

    pushed: dict[str, dict] = {}
    failed: dict[str, str] = {}
    for cid, entry in ready.items():
        char = store().get_character(cid)
        char_name = drive_push.drive_safe_name(char.name if char else cid)
        filename = drive_push.drive_final_name(char_name, base, "final", re_id)
        prior = (entry.get("drive") or {}).get("file_id")
        try:
            pushed[cid] = await drive_push.push_file_core(
                Path(entry["final_path"]), [char_name], filename,
                prior_file_id=prior)
        except Exception as e:  # noqa: BLE001 — record + keep going
            failed[cid] = f"{type(e).__name__}: {e}"
            logger.warning("auto-finalize %s: reengineer drive push failed for "
                           "%s: %s", re_id, cid, e)

    if pushed:
        # Reload before save — an assemble/repurpose/manual push may have landed
        # during the multi-minute upload; write only the receipts.
        fresh = reengineer_mod.load_state(re_id)
        if fresh is not None:
            fbucket = fresh.get("finals") or {}
            changed = False
            for cid, receipt in pushed.items():
                if fbucket.get(cid):
                    fbucket[cid]["drive"] = receipt
                    changed = True
            if changed:
                fresh["finals"] = fbucket
                reengineer_mod.save_state(fresh)
    await _emit(job_id, "job.auto_drive_pushed",
                pushed=list(pushed), failed=failed)
    _notify_drive_result("Reengineer-finaler", len(pushed), len(failed))


def _notify_drive_result(label: str, n_ok: int, n_fail: int) -> None:
    """One phone push summarizing the auto Drive upload. Silent (no-op) when
    NTFY_TOPIC isn't configured — `push.notify` handles that."""
    if n_ok and not n_fail:
        push.notify(f"{label} på Drive", f"{n_ok} uppladdade automatiskt",
                    priority=3, tags=["white_check_mark"])
    elif n_ok and n_fail:
        push.notify(f"{label} delvis på Drive",
                    f"{n_ok} uppladdade, {n_fail} misslyckades",
                    priority=4, tags=["warning"])
    elif n_fail and not n_ok:
        push.notify(f"{label}: Drive-push misslyckades",
                    f"0/{n_fail} uppladdade", priority=5,
                    tags=["rotating_light"])
