"""🎞 Versions — alternative CUTS of a run's scenes.

A finished Reengineer / Swap-from-images run already ships two videos per
character: the FINAL (the assemble) and the 🔁 REPURPOSE (a mirrored copy).
A VERSION is a third kind — another video per character, built from the SAME
run but through a scene list the user edited: scenes removed, reordered,
duplicated, or added (Hugo 2026-08-10). Several named versions coexist, and
building one never touches the original final.

    "one asset pool, many cuts"

The pool is the run: every scene a version owns is registered as an ORDINARY
scene on the underlying job, so image generation, QC, the 👥 person gate, the
✎↻ per-clip retake and 📥 import all work on it unchanged. Only two things
mark it out — an `owner_version` tag on its `state["scenes"]` entry, and the
fact that no base-cut reader ever sees it.

The CUT is `state["versions"][vid]["rows"]`: an ordered list of `{row_id,
scene_id}`. Order and repetition live ONLY there. That matters: `state["scenes"]`
is addressed by LIST INDEX by a dozen endpoints and `api._renumber_scenes`
re-stamps those indices after every mutation, so a second ordering stored in
that list would be rewritten out from under the version. It also means a
version reorder never calls the scene-order endpoint at all, which is what
keeps a version edit from silently permuting the ORIGINAL reel.

Two rules the rest of the code depends on:

  * **A version-owned scene is APPENDED to `state["scenes"]`, never inserted.**
    Base entries then keep contiguous indices 0…n-1, so the original run card's
    scene numbering and every stored idx stay exactly as they were.
  * **Anything that writes the whole scene list back must read it UNFILTERED.**
    `_update` merges rather than deletes, so a caller that reads a filtered
    copy and writes it back silently deletes the other cut's scenes. Only
    readers that decide what enters a BUILD take a cut.

This module is deliberately pure — it imports no runner — so both `api` and
`runner_reengineer` can use it without an import cycle.
"""
from __future__ import annotations

import secrets

# `state["scenes"]` entries carrying this key belong to a version and are
# invisible to the base cut.
OWNER_KEY = "owner_version"


def new_version_id() -> str:
    return "v_" + secrets.token_hex(4)


def new_row_id() -> str:
    return "r_" + secrets.token_hex(4)


def all_versions(state: dict) -> dict:
    return state.get("versions") or {}


def get(state: dict, version_id: str) -> dict | None:
    return all_versions(state).get(version_id)


# ------------------------------------------------------------------ scenes

def base_scenes(state: dict) -> list[dict]:
    """The run's OWN scenes — the ones the original final is built from.

    Entries are returned as stored, indices untouched: `idx` is the raw list
    position everywhere else in the app, and a version must never renumber it.
    """
    return [e for e in (state.get("scenes") or []) if not e.get(OWNER_KEY)]


def owned_scenes(state: dict, version_id: str) -> list[dict]:
    """Scenes belonging to ONE version (its duplicates and additions)."""
    return [e for e in (state.get("scenes") or [])
            if e.get(OWNER_KEY) == version_id]


def base_cut(state: dict) -> dict:
    """`state` as the ORIGINAL deliverable sees it — no version scenes.

    A shallow copy with a filtered `scenes`, so it can be handed to
    `_collect_clips` / `_assembly_gaps` / `_char_is_uninvolved` unchanged.
    Those three then keep their exact current behaviour on a run with no
    versions, which is what makes this feature inert until one is created.
    """
    return {**state, "scenes": base_scenes(state)}


def resolve_rows(state: dict,
                 version: dict) -> tuple[list[dict], list[dict]]:
    """(scenes, dangling) — a version's rows turned into a scene list.

    `scenes` is what the version's build walks: one entry per row, in row
    order, repeats included, each stamped with its POSITION in the version so
    every "scen N" the user is shown counts within this version rather than
    within the run.

    `dangling` names rows whose scene no longer exists in the run — deleted
    from the run after the version referenced it. They are returned rather
    than skipped ON PURPOSE. Both `_collect_clips` and `_assembly_gaps` read
    "no variant slots at all" as *this scene was never this character's* and
    skip it in silence, so a vanished row would quietly shorten the version's
    video with the gate still reporting {ok} — the exact failure that shipped
    a final missing a scene in 2026-08-06. The caller refuses loudly instead.

    A row references a scene by `scene_id`, which is NOT unique in
    `state["scenes"]` (byte-identical uploaded frames collapse onto one id).
    Resolving to the FIRST entry with that id is nonetheless build-correct:
    every clip is looked up per (character, scene_id), so same-id entries
    yield the same clip, and `api._refuse_shared_direct_scene` already forbids
    the one per-entry difference that would change a build — marking one of
    them 📌 direct.
    """
    by_id: dict[str, dict] = {}
    for entry in (state.get("scenes") or []):
        by_id.setdefault(entry.get("scene_id"), entry)
    scenes: list[dict] = []
    dangling: list[dict] = []
    for position, row in enumerate(version.get("rows") or []):
        entry = by_id.get(row.get("scene_id"))
        if entry is None:
            dangling.append({"row_id": row.get("row_id"),
                             "scene_id": row.get("scene_id"),
                             "position": position})
            continue
        resolved = {**entry, "idx": position}
        # Strip the ownership tag from the COPY. This is what lets the three
        # build readers (`_collect_clips`, `_assembly_gaps`,
        # `_char_is_uninvolved`) filter `owner_version` unconditionally: given
        # the run's state they drop every version's scenes, and given a cut
        # they drop nothing, because a cut's entries no longer claim an owner.
        # The alternative — asking each caller to pass the right list — is a
        # filter that can be forgotten, and forgetting it puts a version's
        # scene into the ORIGINAL reel.
        resolved.pop(OWNER_KEY, None)
        scenes.append(resolved)
    return scenes, dangling


def version_cut(state: dict, version: dict) -> dict:
    """`state` as ONE version's build sees it. Dangling rows are dropped here —
    call `resolve_rows` directly when you need to refuse on them."""
    scenes, _dangling = resolve_rows(state, version)
    return {**state, "scenes": scenes}


def rows_for_scenes(scenes: list[dict]) -> list[dict]:
    """A fresh cut that reproduces `scenes` exactly — the starting point a new
    version is seeded with, so creating one and building it immediately gives
    the same reel as the original final."""
    return [{"row_id": new_row_id(), "scene_id": e.get("scene_id")}
            for e in scenes]
