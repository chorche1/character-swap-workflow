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

from character_swap import (
    deliverables,
    events,
    push,
    runner_compile,
    telegram_delivery,
)
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
    # Asymmetric pad (Hugo 2026-08-10): before speech resumes / after it stops.
    "pad_secs": 0.1,
    "pad_end_secs": 0.15,
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


def _approved_ids(jc) -> list[str]:
    """The character's approved image ids (multi-approve list, falling back to
    the legacy single field)."""
    ids = list(jc.approved_variant_ids or [])
    if not ids and jc.approved_variant_id:
        ids = [jc.approved_variant_id]
    return ids


def _uncovered_approvals(jc) -> list[str]:
    """Approved images that did NOT get a usable clip.

    Hugo 2026-08-06: "ingen swap-körning ska skickas till telegram automatiskt
    om inte alla approved bilder fick en video som inte blev nekad."

    Checking the VIDEO ROWS alone cannot answer that — it only sees the clips
    that exist. An approved image with NO row at all is invisible to it, and
    that state is reachable in production: in j_86b8c588a8 (run re_c4f2bb9d0a,
    2026-07-30) Connor had 5 approved images and only 4 clip rows, and the
    Step-6 compile treats a scene it cannot fill as a WARNING, not a failure —
    so a 4-scene final was built, marked `done`, and auto-delivered.

    So ask the question the final actually depends on, per approved image:
    is there a DONE take with its file on disk? `pick_clip_for_variant` is the
    same picker the compile uses, so this gate and the build cannot disagree.
    A refused clip (content policy) leaves a FAILED row, a lost/never-created
    slot leaves none — both come back uncovered.
    """
    from character_swap import runner          # lazy: runner imports us back
    return [vid for vid in _approved_ids(jc)
            if runner.pick_clip_for_variant(jc, vid) is None]


def _all_videos_successful(job: Job) -> bool:
    """True iff EVERY character with an approval has clips, all of them are
    DONE (none pending / failed / errored), AND every approved image is
    covered by one of them — Hugo's "alla videor blir lyckade" gate. A single
    failed clip or a single uncovered approval returns False, so the auto
    chain waits for the user's retry instead of shipping a final that silently
    drops a scene."""
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
            if v.wrong_language:
                # DONE, on disk, and QC heard it speak the wrong language
                # (Hugo 2026-08-09). "Alla videor blir lyckade" cannot mean a
                # clip that says the English source line inside a 🇪🇸/🇩🇪 reel,
                # and nothing here read qc_* before today — a flagged clip was
                # compiled and Telegram-delivered exactly like a clean one.
                # A GARBLED take still passes: it stays shippable with its ⚠
                # chip by Hugo's own 2026-07-03 flag-only setting.
                return False
            saw_done_video = True
        if _uncovered_approvals(jc):
            return False           # an approved image with no clip of its own
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
            # Second lock on the same invariant, at the last moment before the
            # bytes leave: `compile_status == "done"` is NOT proof the final is
            # complete — Step-6 downgrades a scene it cannot fill to a warning
            # and still marks the character done. Re-ask here so a clip that
            # died (or a row that vanished) between the gate and the upload
            # cannot ship a short reel to a character's channel. The manual ➤
            # button stays open: overriding this is the user's call, not ours.
            gaps = _uncovered_approvals(jc)
            if gaps:
                failed[cid] = (
                    f"{jc.name or cid}: {len(gaps)} godkänd(a) bild(er) fick "
                    "inget klipp — finalen är ofullständig och skickades INTE "
                    "automatiskt. Ta om klippet och bygg ihop igen.")
                logger.warning("auto-finalize %s: refusing to send %s — %d "
                               "approved image(s) without a clip: %s",
                               job_id, cid, len(gaps), ", ".join(gaps))
                continue
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
            with telegram_delivery.sending(job_id, cid, "final"):
                sent[cid] = await telegram_delivery.send_character_final(
                    path, chat_id=chat_id, char_name=char_name, base=base,
                    variant="final", run_id=job_id)
        except telegram_delivery.AlreadySending:
            # A manual ➤ click is uploading this exact video right now —
            # skip it rather than post it twice. Not a failure: the manual
            # send writes its own receipt.
            logger.info("auto-finalize %s: %s already being sent manually "
                        "— skipping", job_id, cid)
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
    # One resolver decides which bucket this variant reads — the old binary
    # `"finals" if variant == "final" else "repurposed"` filed a 🎞 version's
    # receipts into the repurpose copy's bucket, silently.
    finals = deliverables.char_entries(state, variant)
    name_label = deliverables.label_for(state, variant)
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
            with telegram_delivery.sending(re_id, cid, variant):
                sent[cid] = await telegram_delivery.send_character_final(
                    Path(entry["final_path"]), chat_id=chat_id,
                    char_name=char_name, base=base, variant=variant,
                    run_id=re_id, label=name_label)
        except telegram_delivery.AlreadySending:
            # Manual ➤ click owns this upload right now — skip, don't
            # duplicate. It writes its own receipt.
            logger.info("auto-finalize %s: %s (%s) already being sent "
                        "manually — skipping", re_id, cid, variant)
        except Exception as e:  # noqa: BLE001 — record + keep going
            failed[cid] = f"{type(e).__name__}: {e}"
            logger.warning("auto-finalize %s: reengineer Telegram send failed for "
                           "%s: %s", re_id, cid, e)

    if sent:
        # Reload before save — an assemble/repurpose/manual send may have landed
        # during the multi-minute upload; write only the receipts.
        fresh = reengineer_mod.load_state(re_id)
        if fresh is not None:
            # char_entries returns the STORED dict, so this writes through for
            # all three deliverables — including a version's nested
            # state["versions"][vid]["chars"].
            fbucket = deliverables.char_entries(fresh, variant)
            changed = False
            for cid, receipt in sent.items():
                if fbucket.get(cid):
                    fbucket[cid]["telegram"] = receipt
                    changed = True
            if changed:
                reengineer_mod.save_state(fresh)
    await _emit(job_id, "job.auto_telegram_sent",
                sent=list(sent), failed=failed)
    if variant == deliverables.FINAL:
        label = "Reengineer-finaler"
    elif variant == deliverables.REPURPOSE:
        label = "Reengineer-repurpose"
    else:
        label = f"Version {name_label or deliverables.version_id(variant)}"
    _notify_telegram_result(label, len(sent), len(failed))


async def send_reengineer_finals(re_id: str,
                                 char_ids: list[str] | None = None) -> None:
    """Auto-send original finals after a fully successful assembly. `char_ids`
    mirrors the build's own scope — only what was (re)built gets delivered."""
    await _send_reengineer_bucket(
        re_id, variant="final", require_opt_in=True, char_ids=char_ids)


async def send_reengineer_repurposed(
        re_id: str, *, char_ids: list[str] | None = None) -> None:
    """Send every newly built Reengineer repurpose copy.

    `char_ids` narrows the send to the characters the repurpose actually
    rebuilt (Hugo 2026-08-10 — the modal's character picker). Load-bearing: a
    filtered repurpose MERGES its results into the bucket, so without this the
    untouched copies of every other character would be re-posted to their
    channels on each partial run."""
    await _send_reengineer_bucket(
        re_id, variant="repurpose", require_opt_in=False, char_ids=char_ids)


async def send_reengineer_version(re_id: str, version_id: str) -> None:
    """Send every newly built video of ONE 🎞 version.

    `require_opt_in=False` for the same reason as the repurpose: the run-level
    "skicka automatiskt" governs the ORIGINAL finals, chosen at upload. A
    version carries its own ✓, checked by `runner_versions.build` before this
    is ever called.
    """
    await _send_reengineer_bucket(
        re_id, variant=deliverables.version_variant(version_id),
        require_opt_in=False)


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
