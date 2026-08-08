"""Kör om en befintlig körning med ETT NYTT CAST (Hugo 2026-08-08).

The ask: "gör så jag kan köra om gamla swap körningar men med nya karaktärer.
De spanska och tyska ska såklart fortfarande ha deras regler för rätt modell."

Shape (Hugo's choice): a re-run is a BRAND NEW run. The parent is never
written to — not its state, not its job, not one byte on disk it owns. The
child inherits the parent's scenes (image, motion prompt, clip length, 📌
direct / 👥 two-person flags, per-scene model override, 🎯 end pose) and its
run settings, and the user may edit all of it in the modal before starting.

WHY A SERVER-SIDE CLONE INSTEAD OF RE-POSTING TO /from_images
    That endpoint only accepts uploaded `files`, so a browser-side "re-run"
    would have to refetch every frame and POST the bytes back — which
    re-registers new scene ids and forecloses reusing the parent's rendered
    direct clip. The scene library is content-addressed and never deleted, so
    the child can simply point at the same `sc_…` ids the parent used.

THE LANGUAGE RULE IS NOT COPIED — IT IS RE-RESOLVED, WHICH IS THE POINT
    A 🇪🇸/🇩🇪 character's clips are forced onto `SPOKEN_LANGUAGE_VIDEO_MODEL`
    by `runner._eff_video_model_for_variant`, whose FIRST line is an
    unconditional early return on the live `CharacterAsset.language` lookup —
    ahead of the per-clip override, the per-scene override and the job
    default. So an inherited `video_model` / per-scene `video_model` cannot
    defeat the redirect for the new cast, and an English character in a run
    cloned from a Spanish one is not dragged onto Veo either. What WOULD
    defeat it is copying per-character rows: `VideoVariant.language_model_
    redirect` and `.fallback_model` outrank the live lookup by design (fal
    request_ids are endpoint-scoped, so a resumed poll must hit the endpoint
    the clip was actually submitted to). This module therefore copies NO
    per-character state whatsoever — no variants, no clips, no approvals, no
    Director plan. `preflight_video_models` checks the redirect TARGET's key
    up front so an all-🗣 cast fails at the click, not inside a worker thread.

WHAT IS DELIBERATELY *NOT* INHERITED, AND WHY
  · `job_id` / `finals` / `repurposed` / `*_stale` / `completed_at` and every
    other runtime key — a copied `job_id` makes `_do_create_from_images`
    RE-ATTACH to the parent's job and rewrite it, which is the one thing this
    feature promises not to do. Copied finals would show the parent's Telegram
    receipts as this run's deliveries.
  · The Director plan (`Job.director_prompts_json`) — it is keyed by char_id,
    so `plan.lookup(new_char_id, …)` returns [] and every slot silently falls
    back to the generic template. The child re-runs the Director itself when
    `use_director` is on (~$0.10), which is the only way to get prompts
    written for the cast that is actually in it.
  · The multi-person choice (`swap_person_idx` and friends) — the instruction
    is baked PER CHARACTER with that character's gender clause
    (`api._bake_person_choice`), so the parent's bake is wrong for a new cast
    in exactly the way the gate exists to prevent (re_a5613a883e). The child
    re-detects (~$0.01 per swap scene) and re-asks. `awaiting_person` in
    particular is a HARD assembly gap, so carrying it would deadlock the
    child's build.
  · Per-character end frames (`JobCharacter.end_frame_uploads` /
    `end_frame_paths`) — they belong to characters that are not in this run.
    The SHARED 🎯 pose is inherited; each new character swaps into it as
    usual.

FILES ARE COPIED, NEVER REFERENCED ACROSS RUNS
    `DELETE /api/reengineer/{re_id}` rmtree's the whole run dir, and no
    refcount anywhere is cross-run. A child pointing into the parent's dir
    would lose its end poses silently (a missing pose is never an error —
    `_resolve_end_image` just returns None) and its reused direct clip
    loudly-but-late (a waitable assembly gap that never resolves). So every
    inherited file that lives under the parent's run dir is copied into the
    child's. Scene images are the one safe reference: content-addressed, and
    nothing in the codebase ever unlinks `input/scenes/`.
"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from character_swap import reengineer as reengineer_mod
from character_swap import runner_media, runner_reengineer
from character_swap.config import settings
from character_swap.models import Job
from character_swap.state import store

# Same ceiling as the upload form (api._MAX_SEQUENCE_IMAGES) — a re-run must
# not be a way around it.
MAX_SCENES = 50

# Run-state keys that describe the CONFIGURATION of a run and are inherited
# verbatim unless the caller overrides them. Everything else is either
# identity (re_id/timestamps), runtime (status/job_id/finals/…) or
# per-scene, and is rebuilt from scratch below.
_INHERITED_SETTINGS = (
    "image_model", "video_model", "outfit_mode", "outfit_text",
    "background_source", "use_director", "skip_qc", "auto_mode",
    "auto_telegram_send", "language",
)

# The settings the modal may override. `language` is intentionally absent:
# it is the legacy RUN-level language (set on 1 of 121 real runs, to "en"),
# superseded by the per-character 🗣 flag, which follows the new cast on its
# own. Inheriting it verbatim preserves the parent's behaviour; exposing it
# would invite mixing a run-level "es" with German-flagged characters.
_OVERRIDABLE = tuple(k for k in _INHERITED_SETTINGS if k != "language") + (
    "character_source_image_ids",)


class RerunError(ValueError):
    """Loud refusal with a user-facing Swedish message (→ HTTP 400).

    Hugo's standing rule: refuse loudly over shipping a silent partial. Every
    raise here happens BEFORE any credit is spent.
    """


# --------------------------------------------------------------------------- reading the parent


def canonical_scene_file(scene_id: str) -> Path:
    """The path the child's job will resolve for this scene.

    Delegates to `runner_reengineer.scene_path`, the SAME resolver
    `_create_job_and_swap` uses — the two must never disagree, or the modal
    offers a scene the job then cannot read.
    """
    return runner_reengineer.scene_path(scene_id)


def scene_file(scene_id: str, parent_job: Job | None = None) -> Path | None:
    """The scene's actual image bytes, wherever they live, or None.

    A scene id does not imply a file of that name: a ⧉-duplicated scene
    (`…__dup<hex>`) gets a SceneAsset pointing at the SOURCE's file and never
    one of its own, and an uploaded .webp keeps its extension. The resolver
    handles both. The two fallbacks below cover a scene with no usable
    SceneAsset at all — rare, but it must not silently produce a path the
    child's job cannot read.
    """
    p = canonical_scene_file(scene_id)
    if p.exists():
        return p
    if parent_job is not None:
        ids = list(parent_job.scene_ids or [])
        paths = list(parent_job.scene_image_paths or [])
        if scene_id in ids:
            i = ids.index(scene_id)
            if i < len(paths) and paths[i] and Path(paths[i]).exists():
                return Path(paths[i])
    # Last resort: a __dup chain is by definition the same image as the id it
    # was minted from.
    base = scene_id.split("__dup")[0]
    if base != scene_id:
        q = canonical_scene_file(base)
        if q.exists():
            return q
    return None


def end_frame_source(entry: dict, parent_job: Job | None) -> Path | None:
    """The scene's CURRENT shared 🎯 end pose on disk, or None.

    `Job.end_frames_by_scene` is the live source of truth: the post-gate
    set / regen / clear endpoints write only there, while the scene entry's
    `end_frame_path` is written once at upload and never updated. So when the
    parent's job row exists it is trusted EXCLUSIVELY — otherwise a pose the
    user cleared after the gate would be resurrected from the stale key. The
    entry is the fallback only for a run whose job is gone.
    """
    sid = entry.get("scene_id") or ""
    if parent_job is not None:
        raw = (parent_job.end_frames_by_scene or {}).get(sid)
        return Path(raw) if raw and Path(raw).exists() else None
    raw = entry.get("end_frame_path")
    return Path(raw) if raw and Path(raw).exists() else None


def direct_clip_source(entry: dict) -> Path | None:
    """The parent's rendered shared clip for a 📌 direct scene, if it exists."""
    if not entry.get("is_direct"):
        return None
    raw = entry.get("shared_clip_path")
    return Path(raw) if raw and Path(raw).exists() else None


def plan_for(state: dict, parent_job: Job | None) -> dict:
    """Everything the re-run modal needs to prefill itself.

    A dedicated payload rather than making the frontend mine the run object:
    two of the three per-scene facts it needs (`has_end_frame`,
    `has_direct_clip`) do not live on the scene entry at all.
    """
    scenes: list[dict] = []
    for idx, e in enumerate(state.get("scenes") or []):
        sid = e.get("scene_id") or ""
        f = scene_file(sid, parent_job)
        scenes.append({
            "idx": idx,
            "scene_id": sid,
            "summary": e.get("summary") or f"Scen {idx + 1}",
            # Effective seconds, resolved exactly as the parent's clips were
            # (`kling_secs` override → model-aware auto). Never the card's
            # displayed klingDuration(), which adds its own +2 margin and
            # would inflate every clip by ~2 s per re-run generation.
            "secs": runner_reengineer._scene_duration(e, state),
            "motion_prompt": e.get("motion_prompt") or "",
            "direct": bool(e.get("is_direct")),
            "two_person": bool(e.get("two_person")),
            "video_model": e.get("video_model") or "",
            "has_end_frame": end_frame_source(e, parent_job) is not None,
            "has_direct_clip": direct_clip_source(e) is not None,
            "missing_file": f is None,
        })
    settings_out = {k: state.get(k) for k in _INHERITED_SETTINGS}
    settings_out["has_background_file"] = bool(
        state.get("background_path")
        and Path(state["background_path"]).exists())
    return {
        "re_id": state.get("re_id"),
        "source_name": state.get("source_name") or state.get("re_id"),
        "from_images": bool(state.get("from_images")),
        "character_ids": list(state.get("character_ids") or []),
        "settings": settings_out,
        "scenes": scenes,
    }


# --------------------------------------------------------------------------- preflight


def preflight_video_models(character_ids: list[str], *, run_default: str,
                           scene_models: list[str]) -> None:
    """Refuse a re-run whose clips could not possibly render, before it starts.

    Covers the model a clip will ACTUALLY be submitted on, which for a
    🗣-flagged character is the language redirect target — not the run's
    `video_model`. `POST /movement` validates only the picked models and the
    Reengineer animate endpoint validates nothing, so an all-Spanish cast on a
    machine without the fal key used to sail through every gate and die inside
    a worker thread an hour later.
    """
    from character_swap import runner

    wanted: set[str] = set()
    for cid in character_ids:
        forced = runner._language_video_model(cid)
        if forced:
            wanted.add(forced)
    # Scenes still render on their own model for every NON-flagged character;
    # only skip that when every character in the run is flagged.
    if any(runner._language_video_model(cid) is None for cid in character_ids):
        for slug in scene_models:
            wanted.add(slug or run_default or "kling-v3")
    for slug in sorted(wanted):
        info = runner_media.VIDEO_MODELS.get(slug)
        if info is None:
            raise RerunError(f"Okänd videomodell '{slug}'")
        if not settings.has_provider(info["provider"]):
            raise RerunError(
                f"{info['label']} saknar API-nyckel — lägg till den i .env. "
                "(Spanska och tyska karaktärer renderas alltid på den "
                "modellen, oavsett vilken modell körningen använder.)")


# --------------------------------------------------------------------------- building the child


def _copy_into(src: Path, dest: Path) -> str:
    """Real copy (never a hardlink — several paths overwrite in place, which
    would mutate the parent's already-shipped file)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    shutil.copyfile(src, tmp)
    tmp.replace(dest)
    return str(dest)


def build_scene_entries(*, parent: dict, parent_job: Job | None,
                        requested: list[dict], run_dir: Path) -> list[dict]:
    """Turn the modal's per-scene rows into the child's `state["scenes"]`.

    `requested` rows are keyed by the parent's list INDEX, not scene_id:
    scene_id is not unique inside `state.scenes` (byte-identical frames
    collapse, and `⧉ duplicate` mints `…__dup…` ids), so an id-keyed row could
    bind to the wrong entry.
    """
    parent_scenes = parent.get("scenes") or []
    if not requested:
        raise RerunError("Välj minst en scen")
    if len(requested) > MAX_SCENES:
        raise RerunError(f"För många scener (max {MAX_SCENES})")

    out: list[dict] = []
    seen_ids: set[str] = set()
    for pos, row in enumerate(requested):
        idx = row.get("idx")
        if not isinstance(idx, int) or not (0 <= idx < len(parent_scenes)):
            raise RerunError(f"Scenrad {pos + 1} pekar på en scen som inte finns")
        src = parent_scenes[idx]
        sid = src.get("scene_id") or ""
        src_file = scene_file(sid, parent_job)
        if src_file is None:
            raise RerunError(
                f"Scen {idx + 1} ({src.get('summary') or sid}): bildfilen "
                "saknas i scenbiblioteket — går inte att köra om.")

        # Re-register under a FRESH id backed by its own file when either:
        #   · the id repeats inside THIS run — flags would silently merge
        #     (direct/two_person are last-wins dicts, end frames first-wins)
        #     and a direct row would wait forever on a clip keyed to its twin;
        #   · the resolver could not reach the bytes and a fallback found them
        #     elsewhere — inheriting that id would hand the child's job a path
        #     it cannot read, failing every variant per character at
        #     generation time. (A ⧉-duplicated or .webp scene does NOT land
        #     here: `scene_path` resolves both, so the id rides along and no
        #     extra file is written.)
        # Mint off the BASE id so duplicate chains don't grow a
        # `__dupA__dupB__dupC` tail.
        if sid in seen_ids or src_file != canonical_scene_file(sid):
            sid, src_file = runner_reengineer.register_scene_duplicate(
                sid.split("__dup")[0], src_file.read_bytes(),
                src.get("summary") or f"Scen {idx + 1} (dubblett)")
        seen_ids.add(sid)

        direct = bool(row.get("direct", src.get("is_direct")))
        secs = row.get("secs")
        try:
            secs_i = int(round(float(secs)))
        except (TypeError, ValueError):
            secs_i = runner_reengineer._scene_duration(src, parent)
        prompt = str(row.get("motion_prompt") or "").strip()

        entry: dict = {
            "idx": pos,
            "scene_id": sid,
            "start": 0.0,
            "end": float(secs_i),
            "duration": float(secs_i),
            "kling_secs": secs_i,
            "motion_prompt": prompt,
            "speech": "",
            "summary": src.get("summary") or f"Scen {idx + 1}",
            # Always "image": the child has no source video even when its
            # parent had one, and `_reengineer_view`/edit mode key the
            # "+ egen" badge off this.
            "source": "image",
        }
        # Per-scene model override rides along; the language redirect still
        # outranks it per character (see the module docstring).
        model_ov = row.get("video_model", src.get("video_model")) or ""
        if model_ov:
            entry["video_model"] = model_ov

        if direct:
            entry["is_direct"] = True
            # from_images direct rows point at the shared scene library, which
            # is safe to reference. An added-scene direct image lives under the
            # PARENT's run dir — copy it, or deleting the parent breaks this
            # run's ability to ever re-render the clip.
            raw_img = src.get("direct_image_path") or ""
            img = Path(raw_img) if raw_img else None
            if img is not None and img.exists() and _under(img, parent):
                entry["direct_image_path"] = _copy_into(
                    img, run_dir / "direct" / f"img_{pos:02d}{img.suffix}")
            elif img is not None and img.exists():
                entry["direct_image_path"] = str(img)
            else:
                entry["direct_image_path"] = str(src_file)

            # ♻️ Reuse the parent's already-rendered shared clip (Hugo's
            # choice): character-independent by construction, so it is the
            # same clip either way — and one video credit cheaper. COPIED, not
            # referenced: a full animate on the PARENT overwrites its own
            # `direct_clip_<sid>.mp4` in place, which would silently change
            # this run's final.
            clip = direct_clip_source(src)
            if clip is not None and bool(row.get("reuse_direct_clip", True)):
                entry["shared_clip_path"] = _copy_into(
                    clip, run_dir / f"direct_clip_{sid}.mp4")
                # Read by _do_animate, which otherwise nulls every direct
                # scene's clip and re-renders it.
                entry["direct_clip_reused"] = True
        else:
            # 👥 is meaningless on a direct scene (one shared clip, no
            # per-character frame) — the same mask the upload path applies.
            if bool(row.get("two_person", src.get("two_person"))):
                entry["two_person"] = True
            # 🎯 shared end pose. Copied into this run's dir; the child's
            # characters swap into it during their own swap phase.
            if bool(row.get("keep_end_frame", True)):
                pose = end_frame_source(src, parent_job)
                if pose is not None:
                    entry["end_frame_path"] = _copy_into(
                        pose, run_dir / "end_frames" / f"img_{pos:02d}{pose.suffix}")

        entry["speech"] = runner_reengineer._spoken_text(entry)
        out.append(entry)
    return out


def _under(path: Path, parent_state: dict) -> bool:
    """True when `path` lives inside the PARENT run's own directory (and is
    therefore destroyed by deleting that run)."""
    re_id = parent_state.get("re_id")
    if not re_id:
        return False
    root = settings.output_dir / "reengineer" / re_id
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def build_state(*, parent: dict, parent_job: Job | None, new_re_id: str,
                character_ids: list[str], requested_scenes: list[dict],
                overrides: dict | None = None) -> dict:
    """The child run's complete state dict, with its files already in place.

    Returns a FULL dict for `reengineer.save_state` — never a partial for
    `runner_reengineer._update`, which reloads from disk first and would drop
    every key it was not handed (the same trap that silently kills
    `analyst_fallback`).
    """
    if not character_ids:
        raise RerunError("Välj minst en karaktär")
    overrides = {k: v for k, v in (overrides or {}).items()
                 if k in _OVERRIDABLE and v is not None}

    run_dir = reengineer_mod.reengineer_dir(new_re_id)
    scenes = build_scene_entries(parent=parent, parent_job=parent_job,
                                 requested=requested_scenes, run_dir=run_dir)

    cfg = {k: parent.get(k) for k in _INHERITED_SETTINGS}
    cfg.update(overrides)

    # A replacement background is reference #3 for every swap and
    # `_create_job_and_swap` HARD-RAISES when it is missing — so it must be
    # this run's own file, not the parent's.
    background_path = None
    raw_bg = parent.get("background_path")
    if raw_bg and Path(raw_bg).exists():
        bg = Path(raw_bg)
        background_path = _copy_into(bg, run_dir / f"background{bg.suffix}")

    now = datetime.utcnow().isoformat() + "Z"
    state = {
        "re_id": new_re_id,
        "created_at": now,
        "updated_at": now,
        "status": "queued",
        "error": None,
        # ALWAYS an image-sourced run, even when cloned from a video run:
        # there is no source video to re-analyze, and `resume_all` dispatches
        # on this flag — a False here would send the child through the video
        # path on restart and KeyError on `source_path`. (Consequence: a
        # re-run of a ♻️ Reengineer run appears on the Swap tab.)
        "from_images": True,
        "rerun_of": parent.get("re_id"),
        "source_name": f"↻ {parent.get('source_name') or parent.get('re_id')}",
        "character_ids": list(character_ids),
        "image_model": cfg.get("image_model") or "gpt2-id-swap",
        "video_model": cfg.get("video_model") or "kling-v3",
        "auto_mode": bool(cfg.get("auto_mode")),
        "outfit_mode": cfg.get("outfit_mode") or "character",
        "outfit_text": (cfg.get("outfit_text") or "").strip(),
        "use_director": bool(cfg.get("use_director")) and bool(
            settings.anthropic_api_key),
        "skip_qc": bool(cfg.get("skip_qc")),
        "auto_telegram_send": bool(cfg.get("auto_telegram_send")),
        "background_path": background_path,
        "background_source": cfg.get("background_source") or "character",
        # NEVER the parent's map: it is keyed by the OLD char_ids, so it is
        # dead weight at best and pins an unintended reference image at worst.
        "character_source_image_ids": dict(
            overrides.get("character_source_image_ids") or {}),
        "scenes": scenes,
        "n_scenes": len(scenes),
        "job_id": None,
        "finals": {},
    }
    if parent.get("language"):
        state["language"] = parent["language"]
    return state


def validate_characters(character_ids: list[str]) -> None:
    """Every character must still be in the library — `_language_video_model`
    returns None for a missing one, so a stale id would silently cost a
    🇪🇸/🇩🇪 character its redirect instead of failing."""
    s = store()
    for cid in character_ids:
        if s.get_character(cid) is None:
            raise RerunError(f"Karaktären finns inte längre: {cid}")
