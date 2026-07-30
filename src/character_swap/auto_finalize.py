"""Auto-finalize: build final videos + send them to Telegram automatically.

Hugo's directive (2026-07-19): "när jag trycker generate videos och alla videor
blir lyckade" the finals should build + ship with NO manual clicks.
Two entry points, one per flow — both gated on a per-run checkbox (default ON):

  • `finalize_swap_job(job_id)` — the classic Swap job flow. Called at the end
    of `runner.run_video_synthesis` (non-reengineer jobs only). When EVERY
    approved character's clips are DONE (zero failed) it runs the Step-6
    `compile_job_videos`, then sends each final to its character channel.

  • `send_reengineer_finals(re_id)` — the Reengineer / Swap-from-images flow.
    Called after `assemble()` finishes. That flow ALREADY auto-assembles the
    finals; the only new step is Telegram delivery once the run lands `done`
    (every included character built).

Everything is best-effort + LOUD on failure: a compile failure is already
surfaced per-character by `runner_compile`; missing Telegram configuration or
delivery errors are pushed to the phone + a job event (never a silent skip),
and the compiled finals are kept for manual resend. The chain never raises back
into the video/assemble phase.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from character_swap import events, push, runner_compile, telegram_delivery
from character_swap.models import Job, VideoStatus
from character_swap.state import store

logger = logging.getLogger(__name__)

# Process-local guard for the completion hooks in ``runner``. Several clip
# tasks can notice "everything is done" almost simultaneously; only one of
# them may compile/send a given job. The check+add happens before the first
# await, so it is atomic within the server's asyncio event loop.
_FINALIZING: set[str] = set()


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
    """Auto-compile + auto-send for the classic Swap flow. No-op unless the
    job opted in, isn't reengineer-backed, and every approved character's clips
    succeeded. Safe to call more than once — it won't rebuild a job whose
    finals are already compiled, and it won't race a manual compile in flight."""
    s = store()
    job = s.get_job(job_id)
    if job is None or job.from_reengineer or not job.auto_compile_push:
        return
    if not _all_videos_successful(job):
        return
    if job_id in _FINALIZING:
        return
    _FINALIZING.add(job_id)
    try:
        # Don't collide with a manual compile the user kicked off, and don't
        # rebuild if we've already finalized (idempotent duplicate trigger).
        compilable = [jc for jc in job.characters.values()
                      if runner_compile._eligible_for_compile(jc)]
        if not compilable:
            return
        if any(jc.compile_status == "compiling" for jc in compilable):
            return
        if all(jc.compile_status == "done" for jc in compilable):
            # A previous compile may have finished while Telegram was
            # unavailable (or the channel had not been connected yet). Do not
            # rebuild the final; retry only destinations lacking a receipt.
            await _send_job_finals(job_id)
            return

        logger.info("auto-finalize %s: all clips DONE — compiling %d character(s)",
                    job_id, len(compilable))
        await _emit(job_id, "job.auto_finalize_started", n=len(compilable))
        try:
            await runner_compile.compile_job_videos(
                job_id, **_resolve_compile_settings(job))
        except Exception:
            logger.exception("auto-finalize %s: compile failed", job_id)
            # compile_job_videos already marks per-character failures; nothing
            # more to send, so stop here.
            return
        await _send_job_finals(job_id)
    finally:
        _FINALIZING.discard(job_id)


async def _send_job_finals(job_id: str) -> None:
    """Send every DONE Swap final to its character-specific Telegram channel."""
    s = store()
    job = s.get_job(job_id)
    if job is None:
        return
    base = job.title or "swap"
    targets: list[tuple[str, Path, str, str]] = []
    failed: dict[str, str] = {}
    for cid, jc in job.characters.items():
        # A duplicate completion trigger (parallel retries/resume tasks) must
        # never create duplicate Telegram messages. Manual resend endpoints
        # remain intentionally non-idempotent.
        if (jc.telegram_sends or {}).get("final", {}).get("ok"):
            continue
        path = jc.compiled_video_path
        if path and jc.compile_status == "done" and Path(path).is_file():
            asset = s.get_character(cid)
            chat_id = (asset.telegram_chat_id if asset else None) or ""
            if not chat_id:
                failed[cid] = (
                    f"Telegram-kanal saknas för {jc.name or cid}. "
                    "Ange den i karaktärsbiblioteket.")
                continue
            targets.append((cid, Path(path), jc.name or cid, chat_id))
    if not targets:
        # All destinations already have successful receipts: this was a
        # harmless duplicate completion hook, so do not emit another "sent"
        # event or phone notification.
        if not failed:
            return
        await _emit(job_id, "job.auto_telegram_sent",
                    sent=[], failed=failed)
        _notify_telegram_result("Slutvideor", 0, len(failed))
        return

    sent: dict[str, dict] = {}
    for cid, path, char_name, chat_id in targets:
        try:
            sent[cid] = await telegram_delivery.send_character_final(
                path, chat_id=chat_id, char_name=char_name, base=base,
                variant="final", run_id=job_id)
        except Exception as e:  # noqa: BLE001 — record + keep going
            failed[cid] = f"{type(e).__name__}: {e}"
            logger.warning("auto-finalize %s: Telegram send failed for %s: %s",
                           job_id, cid, e)

    if sent:
        # Reload before save — the compile persisted meanwhile; write only the
        # receipts so we don't clobber concurrent state.
        fresh = s.get_job(job_id)
        if fresh is not None:
            for cid, receipt in sent.items():
                jc = fresh.characters.get(cid)
                if jc is not None:
                    jc.telegram_sends["final"] = receipt
            s.update_job(fresh)
    await _emit(job_id, "job.auto_telegram_sent",
                sent=list(sent), failed=failed)
    _notify_telegram_result("Slutvideor", len(sent), len(failed))


async def _send_reengineer_bucket(
        re_id: str, *, variant: str, require_opt_in: bool,
        char_ids: list[str] | None = None) -> None:
    """Send one Reengineer final bucket to character Telegram channels.

    `char_ids` narrows the send to those characters: a per-character rebuild
    must not repost the finals it never touched to their channels (Hugo
    2026-07-31, when redoing one clip on a finished run started rebuilding +
    delivering automatically). None = the whole bucket, as before."""
    from character_swap import reengineer as reengineer_mod

    state = reengineer_mod.load_state(re_id)
    if not state:
        return
    if require_opt_in and not state.get(
            "auto_telegram_send", state.get("auto_drive_push", False)):
        return
    # Only when the whole run succeeded — mirrors "alla videor blir lyckade".
    if variant == "final" and state.get("status") != "done":
        return
    bucket = "finals" if variant == "final" else "repurposed"
    finals = state.get(bucket) or {}
    only = {c for c in (char_ids or []) if c}
    ready = {cid: e for cid, e in finals.items()
             if e.get("status") == "done" and e.get("final_path")
             and Path(e["final_path"]).is_file()
             and (not only or cid in only)}
    if not ready:
        return

    job_id = state.get("job_id")
    base_src = state.get("title") or state.get("name")
    if not base_src and job_id:
        linked = store().get_job(job_id)
        base_src = linked.title if linked else None
    base = base_src or re_id
    sent: dict[str, dict] = {}
    failed: dict[str, str] = {}
    for cid, entry in ready.items():
        char = store().get_character(cid)
        char_name = char.name if char else cid
        chat_id = (char.telegram_chat_id if char else None) or ""
        if not chat_id:
            failed[cid] = (
                f"Telegram-kanal saknas för {char_name}. "
                "Ange den i karaktärsbiblioteket.")
            continue
        try:
            sent[cid] = await telegram_delivery.send_character_final(
                Path(entry["final_path"]), chat_id=chat_id,
                char_name=char_name, base=base, variant=variant, run_id=re_id)
        except Exception as e:  # noqa: BLE001 — record + keep going
            failed[cid] = f"{type(e).__name__}: {e}"
            logger.warning("auto-finalize %s: reengineer Telegram send failed for "
                           "%s: %s", re_id, cid, e)

    if sent:
        # Reload before save — an assemble/repurpose/manual send may have landed
        # during the multi-minute upload; write only the receipts.
        fresh = reengineer_mod.load_state(re_id)
        if fresh is not None:
            fbucket = fresh.get(bucket) or {}
            changed = False
            for cid, receipt in sent.items():
                if fbucket.get(cid):
                    fbucket[cid]["telegram"] = receipt
                    changed = True
            if changed:
                fresh[bucket] = fbucket
                reengineer_mod.save_state(fresh)
    await _emit(job_id, "job.auto_telegram_sent",
                sent=list(sent), failed=failed)
    label = ("Reengineer-finaler" if variant == "final"
             else "Reengineer-repurpose")
    _notify_telegram_result(label, len(sent), len(failed))


async def send_reengineer_finals(re_id: str,
                                 char_ids: list[str] | None = None) -> None:
    """Auto-send original finals after a fully successful assembly. `char_ids`
    mirrors the build's own scope — only what was (re)built gets delivered."""
    await _send_reengineer_bucket(
        re_id, variant="final", require_opt_in=True, char_ids=char_ids)


async def send_reengineer_repurposed(re_id: str) -> None:
    """Send every newly built Reengineer repurpose copy."""
    await _send_reengineer_bucket(
        re_id, variant="repurpose", require_opt_in=False)


def _notify_telegram_result(label: str, n_ok: int, n_fail: int) -> None:
    """One phone push summarizing Telegram delivery. Silent (no-op) when
    NTFY_TOPIC isn't configured — `push.notify` handles that."""
    if n_ok and not n_fail:
        push.notify(f"{label} på Telegram", f"{n_ok} skickade automatiskt",
                    priority=3, tags=["white_check_mark"])
    elif n_ok and n_fail:
        push.notify(f"{label} delvis på Telegram",
                    f"{n_ok} skickade, {n_fail} misslyckades",
                    priority=4, tags=["warning"])
    elif n_fail and not n_ok:
        push.notify(f"{label}: Telegram misslyckades",
                    f"0/{n_fail} skickade", priority=5,
                    tags=["rotating_light"])
