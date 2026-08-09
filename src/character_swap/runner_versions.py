"""Building a 🎞 version — a third deliverable per character, from an edited cut.

This is `runner_reengineer._do_repurpose` with one thing changed: the scene
list. Everything else is copied from it deliberately, because that function
encodes the contract an ADDITIONAL deliverable has to keep:

  * it never touches the run's `status`, `finals` or `finals_stale` — the
    original reel is exactly as it was before and after;
  * it writes its own files under its own names, its own bucket, its own
    in-flight flag;
  * a character with no finished clips FAILS LOUDLY on its own card instead of
    quietly shipping a shorter video;
  * the Telegram auto-send runs AFTER the in-flight guard is released, never
    inside it (Hugo 2026-08-03 — sending inside the guard made every manual ➤
    answer "Bygget pågår" for the whole delivery).

`_do_assemble` is NOT the model to copy: it owns the run status and rewrites
`finals`, which is the one thing a version must never do.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from pathlib import Path

from character_swap import (
    reengineer,
    runner_compile,
    runner_reengineer,
    versions,
)
from character_swap.config import settings
from character_swap.models import Job, JobCharacter

_log = logging.getLogger(__name__)


class VersionBuildRefused(RuntimeError):
    """The cut cannot produce complete videos right now, and we say why.

    Raised BEFORE anything is built or billed, so the caller can turn it into
    a 409 naming the scene to fix — never a half-built reel.
    """

    def __init__(self, message: str, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


def settings_for(version: dict) -> dict:
    """Editor settings for this version's build, over ASSEMBLE_DEFAULTS."""
    cfg = dict(runner_reengineer.ASSEMBLE_DEFAULTS)
    stored = version.get("settings") or {}
    cfg.update({k: v for k, v in stored.items()
                if k in runner_reengineer.ASSEMBLE_DEFAULTS})
    return cfg


def mirrored(version: dict) -> bool:
    """Is this version mirror-flipped like a 🔁 repurpose?

    The modal's ✓ ships PRE-TICKED (Hugo 2026-08-10), so a missing value must
    read as True — an older client that never sent the key must not silently
    produce an unmirrored copy.
    """
    stored = (version.get("settings") or {}).get("mirror")
    return True if stored is None else bool(stored)


def auto_telegram_send(version: dict) -> bool:
    """Should the finished videos ship to Telegram on their own?

    Same rule and same reason as `runner_reengineer._repurpose_auto_send`: the
    ✓ is pre-ticked, so a MISSING stored value resolves to True. Anything else
    lets a stale cached client quietly stop delivering.
    """
    stored = (version.get("settings") or {}).get("auto_telegram_send")
    return True if stored is None else bool(stored)


def _write_version(re_id: str, version_id: str, **fields) -> dict | None:
    """Merge `fields` into ONE version and persist.

    `_update` reloads-then-merges and can never delete a key, so the whole
    `versions` container goes back every time (the `rerun.build_state`
    doctrine). Reloading first is what keeps a build from clobbering an edit —
    or another version's build — that landed during the minutes this one ran.
    """
    state = reengineer.load_state(re_id)
    if not state:
        return None
    all_versions = dict(state.get("versions") or {})
    version = dict(all_versions.get(version_id) or {})
    if not version:
        return None
    version.update(fields)
    all_versions[version_id] = version
    runner_reengineer._update(re_id, versions=all_versions)
    return version


def is_stale(state: dict, job: Job, version: dict) -> bool:
    """Has a clip this version uses been re-rendered since it was built?

    Hugo's rule (2026-08-10): retaking a clip in a scene the original AND a
    version share must mark the version "ändrad" with a ▶ Bygg om button —
    never rebuild or re-deliver it behind his back.

    DERIVED on read rather than written by a hook, and that is the point:
    six different paths can replace a clip (↻ retry, ✎↻, 📥 import, scene
    redo, timeout salvage, post-restart resume), and a flag written by five of
    them is a flag that lies on the sixth. Comparing `submitted_at` against
    `built_at` cannot be forgotten. `submitted_at` (not `completed_at`) is the
    marker for the same reason `_scene_resolved_since_edit` uses it: a retry
    mints a fresh VideoVariant whose submit time is necessarily after the
    build, while an in-flight pre-build take that merely FINISHES late is not
    a change to anything.
    """
    built_at = runner_reengineer._parse_dirty_at(version.get("built_at"))
    if built_at is None:
        return False            # never built — unbuilt is not stale
    cut = versions.version_cut(state, version)
    for jc in (job.characters or {}).values():
        if runner_reengineer._char_is_uninvolved(cut, jc):
            continue
        for entry in cut["scenes"]:
            if entry.get("is_direct"):
                continue
            variant_id = runner_reengineer._approved_variant_for(
                jc, entry.get("scene_id"))
            if variant_id is None:
                continue
            take = runner_reengineer.runner.pick_clip_for_variant(
                jc, variant_id)
            submitted = getattr(take, "submitted_at", None)
            if submitted is not None and submitted > built_at:
                return True
    return False


def preflight(state: dict, job: Job, version: dict) -> list[dict]:
    """Refuse loudly if this cut can't build complete videos. Returns the
    non-blocking `dirty` notes.

    Mirrors the assemble endpoint's contract exactly (Hugo 2026-06-17 /
    2026-06-24): `hard` and `pending` refuse — a failed clip, an unapproved
    image, a clip still rendering — while a merely `dirty` scene (edited but
    not re-animated) is reported and built anyway.
    """
    scenes, dangling = versions.resolve_rows(state, version)
    if dangling:
        # A row whose scene was deleted from the run. Both build readers treat
        # "no slots" as "never this character's" and skip it in SILENCE, so
        # left alone this shortens the video with the gate saying {ok}.
        where = ", ".join(str(d["position"] + 1) for d in dangling)
        raise VersionBuildRefused(
            f"Version {version.get('name') or version.get('id')}: "
            f"scenen på plats {where} finns inte kvar i körningen — "
            "ta bort raden eller lägg tillbaka scenen.",
            {"dangling": dangling})
    if not scenes:
        raise VersionBuildRefused(
            "Versionen har inga scener — lägg till minst en.", {})
    gaps = runner_reengineer._assembly_gaps({**state, "scenes": scenes}, job)
    blocking = (gaps.get("hard") or []) + (gaps.get("pending") or [])
    if blocking:
        named = "; ".join(
            f"{g.get('name')}: {g.get('label')} ({g.get('reason')})"
            for g in blocking[:6])
        raise VersionBuildRefused(
            f"Versionen kan inte byggas än — {named}", gaps)
    return gaps.get("dirty") or []


async def build(re_id: str, version_id: str) -> None:
    """Build one version's video for every involved character."""
    state = reengineer.load_state(re_id)
    if not state or not state.get("job_id"):
        return
    version = versions.get(state, version_id)
    if version is None:
        return
    key = (re_id, version_id)
    if key in runner_reengineer._BUILDING_VERSIONS:
        _log.info("reengineer %s: version %s already building — skipping",
                  re_id, version_id)
        return
    runner_reengineer._BUILDING_VERSIONS.add(key)
    built = False
    try:
        await _do_build(re_id, state, version)
        built = True
    except Exception as e:
        _log.exception("reengineer %s version %s failed", re_id, version_id)
        # Loud, on the version's OWN card — never the run-level error box, and
        # never the run status. A version that dies outside the per-character
        # gather used to leave only a stopped spinner ("inget händer").
        _write_version(re_id, version_id, building=False,
                       error=f"bygget misslyckades: {type(e).__name__}: {e}")
    finally:
        runner_reengineer._BUILDING_VERSIONS.discard(key)

    if not built or not auto_telegram_send(version):
        if built:
            _log.info("reengineer %s: version %s Telegram auto-send off — "
                      "built only", re_id, version_id)
        return
    # AFTER the guard is released, never inside it: the uploads take minutes
    # and holding the flag makes every manual ➤ refuse with a false "bygget
    # pågår" (Hugo 2026-08-03).
    try:
        from character_swap import auto_finalize
        await auto_finalize.send_reengineer_version(re_id, version_id)
    except Exception:
        _log.exception("reengineer %s version %s Telegram send failed",
                       re_id, version_id)


async def _do_build(re_id: str, state: dict, version: dict) -> None:
    job = runner_reengineer.store().get_job(state["job_id"])
    if job is None:
        raise RuntimeError("underlying job disappeared")
    version_id = version["id"]
    run_dir = reengineer.reengineer_dir(re_id)
    cfg = settings_for(version)
    cut = versions.version_cut(state, version)
    _write_version(re_id, version_id, building=True, error=None)
    results: dict[str, dict] = {}

    async def _one_character(cid: str, jc: JobCharacter) -> None:
        try:
            # SAME bounded coverage wait as _do_assemble/_do_repurpose, so a
            # clip that lands a second late is waited for rather than dropped.
            deadline = (asyncio.get_event_loop().time()
                        + runner_reengineer._ASSEMBLE_COVERAGE_WAIT_SECS)
            jc_now = jc
            while True:
                clips, dialogues, missing, waitable = \
                    runner_reengineer._collect_clips(cut, jc_now)
                if (not missing or not waitable
                        or asyncio.get_event_loop().time() > deadline):
                    break
                await asyncio.sleep(
                    runner_reengineer._ASSEMBLE_COVERAGE_POLL_SECS)
                fresh_job = runner_reengineer.store().get_job(state["job_id"])
                if fresh_job is not None and cid in fresh_job.characters:
                    jc_now = fresh_job.characters[cid]
            if not clips:
                results[cid] = {"status": "failed",
                                "error": "no finished clips"}
                return
            if missing:
                results[cid] = {
                    "status": "failed",
                    "error": ("versionen saknar " + str(len(missing))
                              + " scen(er): " + ", ".join(missing)
                              + " — ta om scenen och försök igen"),
                    "n_clips": len(clips)}
                return

            edit_id = "ed_" + secrets.token_hex(5)
            edit_dir = settings.output_dir / "editor" / edit_id
            edit_dir.mkdir(parents=True, exist_ok=True)
            lib_char = runner_reengineer.store().get_character(cid)
            voice_id = runner_compile._resolve_compile_voice(
                cfg["voice_override"], lib_char, cfg["enable_voice_swap"])
            # The 🗣 caption-language hint: a version re-transcribes its own
            # clips from scratch, so leaving it off puts German audio under
            # Dutch captions on exactly the copy that gets posted.
            caption_language = getattr(lib_char, "language", None) or None
            warnings: list[str] = []

            async def _warn(message: str) -> None:
                _log.warning("reengineer %s %s (version %s): %s",
                             re_id, cid, version_id, message)
                warnings.append(message)

            script_hint = " ".join(d for d in dialogues if d.strip()) or None
            result = await runner_compile.run_editor_pipeline(
                clips,
                edit_id=edit_id, edit_dir=edit_dir,
                template=cfg["template"], overrides=cfg["overrides"],
                enable_trim=cfg["enable_trim"],
                enable_captions=cfg["enable_captions"],
                enable_wpm_normalize=cfg["enable_wpm_normalize"],
                target_wpm=cfg["target_wpm"],
                threshold_db=cfg["threshold_db"],
                min_silence_secs=cfg["min_silence_secs"],
                pad_secs=cfg["pad_secs"],
                enable_gap_trim=cfg["enable_gap_trim"],
                gap_max_secs=cfg["gap_max_secs"],
                voice_id=voice_id,
                playback_speed=cfg["playback_speed"],
                mirror_h=mirrored(version),
                warn=_warn,
                script_hint=script_hint,
                clip_dialogues=dialogues,
                language=caption_language,
            )
            final = run_dir / f"version_{version_id}_{cid}.mp4"
            await asyncio.to_thread(shutil.copyfile, result.final, final)
            results[cid] = {"status": "done", "final_path": str(final),
                            "n_clips": len(clips), "edit_id": edit_id}
            if warnings:
                results[cid]["warning"] = "; ".join(warnings)
        except Exception as e:
            results[cid] = {"status": "failed",
                            "error": f"{type(e).__name__}: {e}"}

    # "Uninvolved" is a property of THIS CUT, not of the run: a character with
    # no approved image on any scene the version actually uses contributes
    # nothing, and running it would produce a spurious "no finished clips"
    # card (the re_3bedfe62d3 regression). Evaluated against `cut` — whose
    # entries carry no owner tag, so the check sees exactly this version's
    # scenes.
    await asyncio.gather(*[
        _one_character(cid, jc) for cid, jc in job.characters.items()
        if not runner_reengineer._char_is_uninvolved(cut, jc)])

    # Merge rather than replace, so a character built alone keeps everyone
    # else's Telegram/Drive receipts (the _do_assemble char_ids rule).
    fresh = _write_version(re_id, version_id) or version
    chars = dict(fresh.get("chars") or {})
    chars.update(results)
    _write_version(re_id, version_id, chars=chars, building=False,
                   built_at=runner_reengineer._now())
    # NOTE: the Telegram auto-send lives in build(), after the guard is
    # released — never here.
