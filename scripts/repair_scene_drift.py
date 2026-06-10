"""One-time repair for jobs damaged by the SQLite schema-drift bug (2026-06-10).

Before the model_json migration, db.py dropped Job.scene_ids /
scene_image_paths / video_audio / origin and GeneratedImage.scene_id on every
save→load round-trip, so multi-scene jobs collapsed to one scene after a
server restart. This script rebuilds the lost structure from surviving
sources, ONLY filling fields that are currently empty:

  1. Reengineer jobs — output/reengineer/<re_id>/state.json holds job_id +
     the ordered scene list. scene_ids/scene_image_paths/origin/video_audio
     are restored from it; per character, variants map to scenes by position
     (images_per_character=1 for reengineer jobs).
  2. Jobs with per-scene movement_prompts — the movement_prompts dict's keys
     survived (own column); its insertion order is the original scene order.
  3. Explicit --map job_id=sceneA,sceneB args — for jobs where neither
     source exists (e.g. a manual 2-scene job that never reached Step 4);
     variants map to the given scenes by position per character.

Run AFTER deploying the model_json code and WITH THE SERVER STOPPED:
    uv run python scripts/repair_scene_drift.py [--dry-run] [--map j_x=sc_a,sc_b ...]

Always `cp` the DB first; this script refuses to run without --i-have-a-backup.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from character_swap.config import settings
from character_swap.state import store


def _fill_scene_structure(job, scene_ids: list[str], *, label: str,
                          dry: bool) -> bool:
    """Fill missing scene_ids/scene_image_paths + variant scene_id (by
    position) on `job`. Returns True if anything would change."""
    changed = False
    if not (job.scene_ids or []):
        paths = [str(settings.scenes_dir / f"{sid}.png") for sid in scene_ids]
        # Scene files are content-addressed pngs; tolerate other extensions.
        for i, (sid, p) in enumerate(zip(scene_ids, list(paths))):
            if not Path(p).exists():
                hits = list(settings.scenes_dir.glob(f"{sid}.*"))
                if hits:
                    paths[i] = str(hits[0])
        job.scene_ids = list(scene_ids)
        job.scene_image_paths = paths
        job.scene_id = scene_ids[0]
        job.scene_image_path = paths[0]
        changed = True
    for jc in job.characters.values():
        n = min(len(jc.images), len(scene_ids))
        for i in range(n):
            if jc.images[i].scene_id is None:
                jc.images[i].scene_id = scene_ids[i]
                changed = True
    if changed:
        print(f"  {'DRY ' if dry else ''}repair {job.job_id} [{label}]: "
              f"{len(scene_ids)} scenes -> {scene_ids}")
    return changed


def main() -> None:
    args = sys.argv[1:]
    if "--i-have-a-backup" not in args:
        sys.exit("Refusing to run: back up state.sqlite3 first, then pass --i-have-a-backup")
    dry = "--dry-run" in args
    explicit: dict[str, list[str]] = {}
    for a in args:
        if a.startswith("--map"):
            continue
        if "=" in a and a.split("=")[0].startswith("j_"):
            jid, scenes = a.split("=", 1)
            explicit[jid] = [s for s in scenes.split(",") if s]

    s = store()
    repaired = 0

    # --- 1. Reengineer jobs from their run state ---------------------------
    re_root = settings.output_dir / "reengineer"
    if re_root.exists():
        for state_file in sorted(re_root.glob("*/state.json")):
            try:
                st = json.loads(state_file.read_text())
            except json.JSONDecodeError:
                continue
            jid = st.get("job_id")
            scenes = [e["scene_id"] for e in (st.get("scenes") or [])]
            if not jid or not scenes:
                continue
            job = s.get_job(jid)
            if job is None:
                continue
            changed = _fill_scene_structure(job, scenes,
                                            label=f"reengineer {st['re_id']}", dry=dry)
            if job.origin is None:
                job.origin = f"reengineer:{st['re_id']}"
                changed = True
            if job.video_audio is None:
                job.video_audio = True
                changed = True
            if changed and not dry:
                s.update_job(job)
                repaired += 1

    # --- 2. Jobs whose movement_prompts keys survived -----------------------
    for job in s.list_jobs():
        if (job.scene_ids or []) or job.job_id in explicit:
            continue
        sids = [k for k in (job.movement_prompts or {}).keys()]
        if len(sids) > 1:
            if _fill_scene_structure(job, sids, label="movement_prompts", dry=dry):
                if not dry:
                    s.update_job(job)
                    repaired += 1

    # --- 3. Explicit mappings ------------------------------------------------
    for jid, sids in explicit.items():
        job = s.get_job(jid)
        if job is None:
            print(f"  SKIP {jid}: not found")
            continue
        if _fill_scene_structure(job, sids, label="explicit --map", dry=dry):
            if not dry:
                s.update_job(job)
                repaired += 1

    print(f"{'DRY RUN — would repair' if dry else 'Repaired'} {repaired} job(s)")


if __name__ == "__main__":
    main()
