"""Which deliverable a `variant` string names — one resolver, ten call sites.

A run produces more than one video per character. There is the FINAL (the
assemble), the 🔁 REPURPOSE (a mirrored copy for re-posting) and, since
2026-08-10, any number of named 🎞 VERSIONS — each an alternative CUT of the
scene list, built into its own extra video per character. All three travel
through the same Telegram/Drive plumbing.

That plumbing used to hardcode the two-value pair `("final", "repurpose")` in
ten places: five `not in (...)` validations, two
`bucket = "finals" if variant == "final" else "repurposed"` ternaries, two
"is a build running" ternaries and two filename-suffix branches. Every one of
those reads a THIRD value as "not final, therefore repurpose", so bolting a
version onto that shape fails in three separate silent ways:

  * its Telegram/Drive receipts overwrite the repurpose copy's receipts;
  * its "is a build running" guard watches the REPURPOSE build's flag, so a
    manual ➤ can stream a half-written file (or be refused for the wrong
    reason);
  * worst — `drive_push.drive_final_name` suffixes only on `"repurpose"`, so a
    version's filename comes out BYTE-IDENTICAL to the original final's, and
    `google_drive.upload_or_replace` keys on the name. The version would
    silently OVERWRITE the original final in Drive.

Hence this module. It is pure and imports nothing from the runners at module
scope, so it is safe to import from `telegram_delivery` / `drive_push` /
`api` alike.
"""
from __future__ import annotations

FINAL = "final"
REPURPOSE = "repurpose"

# A version variant is "version:<vid>" — the prefix keeps the namespace open
# without a registry lookup, and makes every version's variant string distinct
# from every other's, which is what the filename uniqueness rests on.
VERSION_PREFIX = "version:"

#: The two variants that exist independently of a run's stored state. Kept as
#: a tuple (not a set) so error messages list them in a stable order.
BASE_VARIANTS: tuple[str, ...] = (FINAL, REPURPOSE)


class UnknownVariant(ValueError):
    """Raised for a variant string no deliverable answers to."""


def version_variant(version_id: str) -> str:
    """The variant string naming a 🎞 version's deliverable."""
    return f"{VERSION_PREFIX}{version_id}"


def version_id(variant: str) -> str | None:
    """The version id inside a version variant, else None.

    None for `"final"` / `"repurpose"` / anything unrecognised — callers use
    this as the "is this a version?" test, so it must never raise.
    """
    v = str(variant or "")
    if not v.startswith(VERSION_PREFIX):
        return None
    return v[len(VERSION_PREFIX):] or None


def is_version(variant: str) -> bool:
    return version_id(variant) is not None


def known_variants(state: dict | None = None) -> list[str]:
    """Every variant this run can deliver, in a stable order.

    Without `state` only the two base variants are known — that is the correct
    answer for the Swap-job endpoints, which have no run state and no versions.
    """
    out = list(BASE_VARIANTS)
    for vid in (state or {}).get("versions") or {}:
        out.append(version_variant(vid))
    return out


def validate(variant: str, state: dict | None = None) -> str:
    """Return `variant` if this run can deliver it, else raise UnknownVariant.

    A version variant is accepted ONLY when that version actually exists in
    `state` — an unknown id must not fall through to a generic bucket, which
    is exactly the silent-misfiling this module exists to prevent.
    """
    v = str(variant or "")
    if v in BASE_VARIANTS:
        return v
    vid = version_id(v)
    if vid is not None and vid in ((state or {}).get("versions") or {}):
        return v
    raise UnknownVariant(f"Unknown variant {variant!r}")


def name_suffix(variant: str, *, label: str | None = None) -> str:
    """The filename suffix that keeps this deliverable distinct from its siblings.

    `""` for the final (its name is the baseline every other name differs
    from), `" — repurpose"` for the mirrored copy — both byte-identical to
    what shipped before this module existed, so existing Drive files keep
    their overwrite key.

    A version uses its NAME when one is supplied and falls back to its id.
    The fallback is deliberate rather than defensive: a version whose label is
    missing still gets a suffix no other deliverable can produce, so the
    overwrite-the-original failure is impossible even when a caller forgets to
    pass the label.
    """
    if variant == FINAL:
        return ""
    if variant == REPURPOSE:
        return " — repurpose"
    vid = version_id(variant)
    if vid is None:
        raise UnknownVariant(f"Unknown variant {variant!r}")
    return f" — {(label or vid).strip() or vid}"


# --------------------------------------------------------------- run state

def char_entries(state: dict, variant: str) -> dict:
    """The per-character result entries for `variant` in a Reengineer run.

    `{cid: {status, final_path, n_clips, edit_id, warning?, error?, telegram?}}`
    — the same shape for all three deliverables, which is why the callers can
    stay variant-agnostic once they resolve it through here.

    Finals and repurposes live in top-level buckets for historical reasons; a
    version's entries live under the version itself, so that deleting a
    version takes its receipts with it in one write (`_update` merges and can
    never delete a key — see `rerun.build_state`).
    """
    vid = version_id(variant)
    if vid is not None:
        return ((state.get("versions") or {}).get(vid) or {}).get("chars") or {}
    if variant == FINAL:
        return state.get("finals") or {}
    if variant == REPURPOSE:
        return state.get("repurposed") or {}
    raise UnknownVariant(f"Unknown variant {variant!r}")


def label_for(state: dict, variant: str) -> str | None:
    """The human name of a version variant (for filenames), else None."""
    vid = version_id(variant)
    if vid is None:
        return None
    return ((state.get("versions") or {}).get(vid) or {}).get("name")


def is_building(state: dict, variant: str, re_id: str) -> bool:
    """Is the build that OWNS this deliverable running right now?

    Each deliverable is written by its own builder to its own path, and each
    builder overwrites that path IN PLACE while its bucket entry still says
    "done". A manual ➤ Telegram / Drive push that streams the file mid-write
    lands torn bytes with an ok receipt — so every send path asks this first.

    It must be per-DELIVERABLE, not per-run: watching the wrong builder's flag
    both refuses sends that are perfectly safe (the 2026-08-03 "Bygget pågår"
    lie) and permits the one that is not.
    """
    # Late import: runner_reengineer imports half the package, and this module
    # is imported BY telegram_delivery / drive_push. Same pattern the api.py
    # endpoints already use for their own runner_reengineer lookups.
    from character_swap import runner_reengineer

    vid = version_id(variant)
    if vid is not None:
        version = (state.get("versions") or {}).get(vid) or {}
        return (bool(version.get("building"))
                or (re_id, vid) in runner_reengineer._BUILDING_VERSIONS)
    if variant == FINAL:
        return (state.get("status") == "assembling"
                or re_id in runner_reengineer._ASSEMBLING)
    if variant == REPURPOSE:
        return (bool(state.get("repurposing"))
                or re_id in runner_reengineer._REPURPOSING)
    raise UnknownVariant(f"Unknown variant {variant!r}")
