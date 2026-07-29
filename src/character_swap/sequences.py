"""Saved sequences — reuse finished scenes + clips in a future run.

Every Swap run pays for two expensive things per (character × scene): the swap
IMAGE and the video CLIP. When Hugo wants his characters to say/do the SAME
thing again in a later video, there was no way to reuse what was already
rendered — he had to re-upload the scene images and pay for both steps again.

A **saved sequence** is a named, hand-picked subset of a finished run's scenes.
For each saved scene it snapshots the scene frame, its motion prompt / length /
model, and — per character — the APPROVED swap image plus the FINISHED clip.
Pasting that sequence into a future run materializes those files as ordinary
approved-image + done-clip rows, so the run reuses them at zero cost. A
character that wasn't in the sequence simply generates as usual and lands in
the normal approval gate.

Storage per sequence under `output/sequences/<seq_id>/`:
    sequence.json                 the record below
    scenes/<key>.png              copy of the scene frame
    images/<key>__<char_id>.png   the approved swap image
    clips/<key>__<char_id>.mp4    the finished clip
    clips/<key>__direct.mp4       the shared clip of a 📌 "ingen swap" scene

Files are HARD-LINKED from the source run when possible (same filesystem →
zero extra disk) and copied otherwise. Either way the sequence owns its own
directory entries, so deleting the source run or job never breaks it.

Orchestration (pasting into a run) lives in runner_reengineer.py + runner.py;
this module is the pure store.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field

from character_swap.config import settings
from character_swap.models import Job

logger = logging.getLogger(__name__)

# Wallet/disk guard — a sequence is a hand-picked subset, not a whole library.
MAX_SEQUENCE_SCENES = 50
MAX_NAME_LEN = 120


class SequenceClip(BaseModel):
    """One character's finished pair for a saved scene."""
    char_id: str
    name: str                       # snapshot; the library entry may be renamed
    image_path: str                 # the APPROVED swap image
    clip_path: str                  # the FINISHED clip
    prompt: str = ""                # the swap prompt that produced the image
    localized_movement_prompt: str | None = None   # 🇪🇸 etc — keeps captions honest


class SequenceScene(BaseModel):
    key: str                        # unique within the sequence
    order: int
    origin_scene_id: str
    summary: str = ""
    frame_path: str = ""
    motion_prompt: str = ""
    speech: str = ""
    duration: float = 5.0
    kling_secs: int | None = None
    video_model: str | None = None
    # 📌 "ingen swap": ONE shared clip for every character, no per-char swap.
    is_direct: bool = False
    direct_clip_path: str | None = None
    clips: dict[str, SequenceClip] = Field(default_factory=dict)   # char_id → pair

    def covers(self, char_id: str) -> bool:
        """True when this scene needs NO generation for `char_id` — either it's
        a direct scene (one shared clip serves everyone) or the character has a
        stored image+clip pair."""
        return self.is_direct or char_id in self.clips


class SavedSequence(BaseModel):
    seq_id: str
    name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source_re_id: str | None = None
    source_job_id: str | None = None
    # Union of every character that has at least one stored clip. Snapshot of
    # the names at save time (the library entry may be renamed later).
    chars: dict[str, str] = Field(default_factory=dict)      # char_id → name
    scenes: list[SequenceScene] = Field(default_factory=list)

    def scene(self, key: str) -> SequenceScene | None:
        return next((sc for sc in self.scenes if sc.key == key), None)


class SequenceError(Exception):
    """Refuse loudly: the requested save/paste cannot be honored as asked."""


# --------------------------------------------------------------------------- files

def sequences_root() -> Path:
    return settings.output_dir / "sequences"


def sequence_dir(seq_id: str) -> Path:
    """Pure path — never creates. (A creating `load()` would resurrect a
    deleted sequence as an empty directory.)"""
    return sequences_root() / seq_id


def _ensure_dir(seq_id: str) -> Path:
    p = sequence_dir(seq_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def record_path(seq_id: str) -> Path:
    return sequence_dir(seq_id) / "sequence.json"


def link_or_copy(src: Path | str, dst: Path | str) -> Path:
    """Hard-link `src` to `dst`, falling back to a real copy.

    A hard link costs no extra bytes but gives the destination its own
    directory entry, so the file survives deletion of the source run/job — the
    whole point of a saved sequence. Falls back to copy2 across filesystems
    (EXDEV) or wherever links aren't permitted.
    """
    src_p, dst_p = Path(src), Path(dst)
    dst_p.parent.mkdir(parents=True, exist_ok=True)
    if dst_p.exists():
        dst_p.unlink()
    try:
        os.link(src_p, dst_p)
    except OSError:
        shutil.copy2(src_p, dst_p)
    return dst_p


# --------------------------------------------------------------------------- state

def load(seq_id: str) -> SavedSequence | None:
    p = record_path(seq_id)
    if not p.exists():
        return None
    try:
        return SavedSequence.model_validate_json(p.read_text(encoding="utf-8"))
    except Exception:                       # noqa: BLE001 — a broken record is not fatal
        logger.exception("sequences: unreadable record %s", seq_id)
        return None


def save(seq: SavedSequence) -> None:
    p = record_path(seq.seq_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(seq.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(p)


def list_all() -> list[SavedSequence]:
    """Newest first (by directory mtime, like reengineer.list_states)."""
    root = sequences_root()
    if not root.exists():
        return []
    out: list[SavedSequence] = []
    for sub in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not sub.is_dir():
            continue
        seq = load(sub.name)
        if seq:
            out.append(seq)
    return out


def delete(seq_id: str) -> bool:
    """Remove the sequence and its files. Runs that already pasted it are
    unaffected — the paste hard-links/copies into the job's own output dir."""
    d = sequences_root() / seq_id
    if not d.exists():
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def rename(seq_id: str, name: str) -> SavedSequence | None:
    seq = load(seq_id)
    if seq is None:
        return None
    seq.name = _clean_name(name) or seq.name
    save(seq)
    return seq


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())[:MAX_NAME_LEN]


# --------------------------------------------------------------------------- saving

def _scene_frame_path(scene_id: str) -> Path:
    """Where the run's scene frame lives. Mirrors the derivation in
    `runner_reengineer._create_job_and_swap` (scenes_dir/<scene_id>.png)."""
    return settings.scenes_dir / f"{scene_id}.png"


def save_from_run(state: dict, job: Job | None, scene_idxs: list[int],
                  name: str) -> tuple[SavedSequence, list[str]]:
    """Snapshot the chosen scenes of a finished run into a new sequence.

    `scene_idxs` are LIST INDICES into `state["scenes"]` (the same addressing
    every edit-mode endpoint uses — scene_id is not unique). The saved order
    follows the run's scene order, not the order the indices were given in.

    Refuses LOUDLY (SequenceError) when a chosen scene has no finished clip at
    all — saving it would produce a sequence that silently contributes nothing
    to a future run. Characters that merely lack their own clip in an otherwise
    fine scene are skipped and reported in the returned notes.
    """
    entries = state.get("scenes") or []
    if not scene_idxs:
        raise SequenceError("Bocka i minst en scen att spara.")
    wanted = sorted({int(i) for i in scene_idxs})
    for i in wanted:
        if i < 0 or i >= len(entries):
            raise SequenceError(f"Scen {i + 1} finns inte i körningen.")
    if len(wanted) > MAX_SEQUENCE_SCENES:
        raise SequenceError(f"För många scener (max {MAX_SEQUENCE_SCENES}).")

    clean = _clean_name(name)
    if not clean:
        raise SequenceError("Ge sekvensen ett namn.")

    seq_id = "seq_" + secrets.token_hex(5)
    out_dir = _ensure_dir(seq_id)
    seq = SavedSequence(
        seq_id=seq_id, name=clean,
        source_re_id=state.get("re_id"),
        source_job_id=state.get("job_id"),
    )
    notes: list[str] = []

    try:
        for order, idx in enumerate(wanted):
            entry = entries[idx]
            sc = _snapshot_scene(out_dir, entry, idx, order, job, notes)
            seq.scenes.append(sc)
            for cid, pair in sc.clips.items():
                seq.chars.setdefault(cid, pair.name)
    except Exception:
        # Never leave a half-written sequence behind — the library would list
        # a record whose files are missing.
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    save(seq)
    return seq, notes


def _snapshot_scene(out_dir: Path, entry: dict, idx: int, order: int,
                    job: Job | None, notes: list[str]) -> SequenceScene:
    from character_swap import runner, runner_reengineer

    scene_id = entry.get("scene_id") or ""
    label = f"scen {idx + 1}"
    key = f"sc{order:02d}"
    sc = SequenceScene(
        key=key, order=order, origin_scene_id=scene_id,
        summary=str(entry.get("summary") or label)[:80],
        motion_prompt=str(entry.get("motion_prompt") or ""),
        speech=str(entry.get("speech") or ""),
        duration=float(entry.get("duration") or 5.0),
        kling_secs=(int(entry["kling_secs"])
                    if entry.get("kling_secs") else None),
        video_model=entry.get("video_model") or None,
        is_direct=bool(entry.get("is_direct")),
    )

    # The scene frame. Direct scenes point at their own registered image; swap
    # scenes use the library path the job derives. (Never build a Path from an
    # empty string — Path("") is Path("."), which always "exists".)
    direct_img = entry.get("direct_image_path") if sc.is_direct else None
    src_frame = Path(direct_img) if direct_img else _scene_frame_path(scene_id)
    if not src_frame.exists():
        raise SequenceError(
            f"{label.capitalize()}: scenbilden finns inte kvar på disk — "
            "kan inte sparas.")
    sc.frame_path = str(link_or_copy(src_frame, out_dir / "scenes" / f"{key}.png"))

    if sc.is_direct:
        shared = entry.get("shared_clip_path")
        if not (shared and Path(shared).exists()):
            raise SequenceError(
                f"{label.capitalize()} (📌 ingen swap) har inget färdigt "
                "delat klipp — rendera klart scenen först.")
        sc.direct_clip_path = str(link_or_copy(
            shared, out_dir / "clips" / f"{key}__direct.mp4"))
        return sc

    if job is None:
        raise SequenceError(
            f"{label.capitalize()}: körningens jobb saknas — kan inte hämta klippen.")

    for cid, jc in (job.characters or {}).items():
        avid = runner_reengineer._approved_variant_for(jc, scene_id)
        if avid is None:
            notes.append(f"{label}: {jc.name} hoppades över (ingen godkänd bild)")
            continue
        image = next((im for im in jc.images if im.variant_id == avid), None)
        clip = runner.pick_clip_for_variant(jc, avid)
        if image is None or not Path(image.path).exists():
            notes.append(f"{label}: {jc.name} hoppades över (bildfilen saknas)")
            continue
        if clip is None or not clip.final_video_path:
            notes.append(f"{label}: {jc.name} hoppades över (inget färdigt klipp)")
            continue
        ext = Path(clip.final_video_path).suffix.lower() or ".mp4"
        sc.clips[cid] = SequenceClip(
            char_id=cid,
            name=jc.name,
            image_path=str(link_or_copy(
                image.path, out_dir / "images" / f"{key}__{cid}.png")),
            clip_path=str(link_or_copy(
                clip.final_video_path, out_dir / "clips" / f"{key}__{cid}{ext}")),
            prompt=image.prompt or "",
            localized_movement_prompt=clip.localized_movement_prompt,
        )

    if not sc.clips:
        raise SequenceError(
            f"{label.capitalize()} har inga färdiga klipp att spara — "
            "godkänn en bild och rendera klippet först.")
    return sc


# --------------------------------------------------------------------------- pasting

def reuse_entry(seq: SavedSequence, sc: SequenceScene,
                char_id: str) -> dict | None:
    """The `Job.reused_clips[scene_id][char_id]` payload for one pasted slot,
    or None when this character has nothing stored for the scene (→ it
    generates normally and goes through the approval gate)."""
    pair = sc.clips.get(char_id)
    if pair is None:
        return None
    if not (Path(pair.image_path).exists() and Path(pair.clip_path).exists()):
        logger.warning("sequences %s: scene %s missing files for %s",
                       seq.seq_id, sc.key, char_id)
        return None
    return {
        "seq_id": seq.seq_id,
        "scene_key": sc.key,
        "image_path": pair.image_path,
        "clip_path": pair.clip_path,
        "prompt": pair.prompt,
        "localized_movement_prompt": pair.localized_movement_prompt,
    }


def reused_map_for_scene(seq: SavedSequence, sc: SequenceScene,
                         char_ids: list[str]) -> dict[str, dict]:
    """`char_id → reuse payload` for every character of the NEW run that the
    sequence actually covers. Characters missing from the map generate."""
    out: dict[str, dict] = {}
    for cid in char_ids:
        payload = reuse_entry(seq, sc, cid)
        if payload is not None:
            out[cid] = payload
    return out


def scene_entry_from_saved(seq: SavedSequence, sc: SequenceScene,
                           idx: int) -> dict:
    """Build the run-state scene entry for a pasted scene.

    Mirrors the shape `POST /api/reengineer/from_images` builds, plus
    `reused_from` (provenance, used to rebuild Job.reused_clips on resume) and
    — for a 📌 direct scene — a pre-filled `shared_clip_path` guarded by
    `reused_direct` so `_do_animate` never re-renders it.
    """
    duration = round(float(sc.duration or 5.0), 3)
    entry: dict = {
        "idx": idx,
        "scene_id": "",                       # caller registers + fills this
        "start": 0.0,
        "end": duration,
        "duration": duration,
        "kling_secs": sc.kling_secs,
        "motion_prompt": sc.motion_prompt,
        "speech": sc.speech,
        "summary": sc.summary or f"Scen {idx + 1}",
        "source": "sequence",
        "reused_from": {"seq_id": seq.seq_id, "scene_key": sc.key,
                        "name": seq.name},
    }
    if sc.video_model:
        entry["video_model"] = sc.video_model
    if sc.is_direct:
        entry["is_direct"] = True
        entry["reused_direct"] = True
        entry["shared_clip_path"] = sc.direct_clip_path
    return entry
