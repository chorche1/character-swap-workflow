"""Reengineer pipeline — primitives.

Takes an uploaded reference video and rebuilds it with a different character:
the video is split into scenes, each scene contributes one representative
frame, a Claude vision agent writes a motion+speech prompt per scene from the
original footage, and the frames become the scenes of a regular Swap job
(character swap → per-scene Kling v3 clips with NATIVE audio → trim to the
original scene durations → concat per character).

This module holds the pure pieces: per-run state I/O (same pattern as
broll.py), ffmpeg scene detection + frame extraction, and the scene-analysis
agent. Orchestration lives in runner_reengineer.py.

Storage per run under `output/reengineer/<re_id>/`:
    - source.<ext>          original video upload
    - scenes/scene-NN.png   representative frame per detected scene
    - words.json            Whisper word-level transcript of the source
    - plan.json             agent output: per-scene motion+speech prompts
    - final_<char_id>.mp4   reassembled video per character
    - state.json            full run status for polling
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from character_swap.config import settings
from character_swap.video_edit import Word, _probe_duration

# Scene-detection knobs. UGC reference videos cut between SIMILAR-looking
# shots (same person, same room — only the grip/pose/prop changes), which
# scores low on ffmpeg's scene metric. 0.30 missed most of those cuts (Hugo,
# 2026-06-10: "every cut where anything changed must become a scene"), so the
# default is 0.12; the per-run sensitivity option maps normal/high/max →
# 0.20/0.12/0.06. Scenes are then normalized: fragments under MIN_SCENE_SECS
# merge into a neighbor (0.8s — fast UGC cuts are real scenes; Kling clips
# get trimmed back to the original length at assembly, so short is fine).
SCENE_THRESHOLD = 0.12
SENSITIVITY_THRESHOLDS = {"normal": 0.20, "high": 0.12, "max": 0.06}
MIN_SCENE_SECS = 0.8
MAX_SCENE_SECS = 15.0      # fal Kling v3 upper bound
MAX_SCENES = 20            # wallet guard — shortest neighbors merge beyond this


# --------------------------------------------------------------------------- state

def reengineer_dir(re_id: str) -> Path:
    p = settings.output_dir / "reengineer" / re_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path(re_id: str) -> Path:
    return reengineer_dir(re_id) / "state.json"


def load_state(re_id: str) -> dict | None:
    p = state_path(re_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_state(state: dict) -> None:
    p = state_path(state["re_id"])
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(p)


def list_states() -> list[dict]:
    root = settings.output_dir / "reengineer"
    if not root.exists():
        return []
    out: list[dict] = []
    for sub in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if sub.is_dir():
            s = load_state(sub.name)
            if s:
                out.append(s)
    return out


# --------------------------------------------------------------------------- scene detection

def _ffmpeg_scene_changes(video: Path, threshold: float) -> list[float]:
    """Timestamps (secs) where ffmpeg's scene score exceeds `threshold`."""
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(video),
         "-vf", f"select='gt(scene,{threshold})',showinfo",
         "-f", "null", "-"],
        capture_output=True, text=True,
    )
    times: list[float] = []
    for m in re.finditer(r"pts_time:(\d+(?:\.\d+)?)", proc.stderr):
        times.append(float(m.group(1)))
    return sorted(set(times))


def detect_scenes(
    video: Path,
    *,
    threshold: float = SCENE_THRESHOLD,
    min_secs: float = MIN_SCENE_SECS,
    max_secs: float = MAX_SCENE_SECS,
    max_scenes: int = MAX_SCENES,
) -> list[tuple[float, float]]:
    """Split `video` into clip-sized scenes. Returns [(start, end), ...].

    1. Hard cuts from ffmpeg's scene score.
    2. Merge fragments shorter than `min_secs` into the previous scene.
    3. Subdivide anything longer than `max_secs` evenly (continuous single-
       shot UGC videos become N equal chunks).
    4. If still above `max_scenes`, re-split the whole duration evenly into
       `max_scenes` chunks (wallet guard).
    """
    total = _probe_duration(video)
    if total <= 0:
        raise ValueError(f"could not probe duration of {video}")

    cuts = [t for t in _ffmpeg_scene_changes(video, threshold) if 0.1 < t < total - 0.1]
    bounds = [0.0, *cuts, total]
    scenes = [(a, b) for a, b in zip(bounds, bounds[1:]) if b - a > 0.01]

    # Merge too-short fragments into their predecessor (or successor for the first).
    merged: list[tuple[float, float]] = []
    for s in scenes:
        if merged and (s[1] - s[0]) < min_secs:
            merged[-1] = (merged[-1][0], s[1])
        elif not merged and (s[1] - s[0]) < min_secs and len(scenes) > 1:
            # fold a too-short opener into what follows by skipping the cut
            continue_start = s[0]
            merged.append((continue_start, s[1]))  # provisional; next merge extends
        else:
            merged.append(s)

    # Subdivide long scenes evenly into <= max_secs chunks.
    sized: list[tuple[float, float]] = []
    for a, b in merged:
        span = b - a
        n = max(1, int(span // max_secs) + (1 if span % max_secs > 0.01 else 0))
        step = span / n
        for i in range(n):
            sized.append((a + i * step, a + (i + 1) * step))

    # Wallet guard: above max_scenes, repeatedly merge the SHORTEST scene into
    # its shorter neighbor. Unlike an even re-split this PRESERVES the real cut
    # boundaries of the scenes that survive.
    while len(sized) > max_scenes:
        i = min(range(len(sized)), key=lambda k: sized[k][1] - sized[k][0])
        if i == 0:
            sized[0] = (sized[0][0], sized[1][1]); del sized[1]
        elif i == len(sized) - 1:
            sized[-2] = (sized[-2][0], sized[-1][1]); del sized[-1]
        else:
            left_span = sized[i - 1][1] - sized[i - 1][0]
            right_span = sized[i + 1][1] - sized[i + 1][0]
            if left_span <= right_span:
                sized[i - 1] = (sized[i - 1][0], sized[i][1]); del sized[i]
            else:
                sized[i] = (sized[i][0], sized[i + 1][1]); del sized[i + 1]
    return sized


def extract_frame(video: Path, at_secs: float, dest: Path) -> Path:
    """Extract one frame at `at_secs` as PNG."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-y", "-ss", f"{max(0.0, at_secs):.3f}",
         "-i", str(video), "-frames:v", "1", str(dest)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0 or not dest.exists():
        raise RuntimeError(f"frame extraction failed at {at_secs:.2f}s: {proc.stderr[-300:]}")
    return dest


# --------------------------------------------------------------------------- scene analysis agent

@dataclass
class ScenePlan:
    idx: int
    motion_prompt: str     # camera + action direction for the video model
    speech: str            # what the person says in this scene ("" = silent)
    summary: str           # one-line description (UI display)


REENGINEER_ANALYST_SYSTEM = """\
You are a video reverse-engineer for a character-swap pipeline. You see, for
each SCENE of a reference UGC-style video: the scene's representative frame
and the words spoken during that scene (from Whisper). The original person in
every frame will be REPLACED by a different person, and each frame will then
be animated into a clip by an image-to-video model (Kling v3) that also
generates NATIVE AUDIO — including the person's voice when the prompt contains
dialogue.

For EVERY scene, write:
1. motion_prompt — an imperative direction for the video model describing
   what happens in the original clip: the subject's action and gesture, any
   object interaction, and the camera behavior (static / hand-held wobble /
   slow push-in). Stay true to the ORIGINAL footage — same action, same
   energy. The clip must look like ordinary hand-held UGC phone footage, NOT
   cinematic. Do not describe the person's appearance (they are being
   replaced); refer to them as "the person". If the scene has dialogue,
   include it VERBATIM as: The person says: "<dialogue>" — with natural
   lip-sync and a casual, conversational delivery in fluent American
   English with a natural American accent.
2. speech — the dialogue line alone (empty string if the scene has no speech).
3. summary — one short line describing the scene for a UI list.

Rules:
- Use the spoken words EXACTLY as transcribed; do not paraphrase dialogue.
- Keep motion_prompt under 120 words.
- Never add scene elements that are not visible in the frame.
- The voice should match the demographic of the REPLACEMENT character, so
  describe the voice generically ("a natural middle-aged male voice" style
  hints belong to the pipeline, not you) — just mark the dialogue.
"""

REENGINEER_ANALYST_TOOL: dict = {
    "name": "submit_scene_plan",
    "description": "Submit the per-scene reengineering plan.",
    "input_schema": {
        "type": "object",
        "required": ["scenes"],
        "properties": {
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["idx", "motion_prompt", "speech", "summary"],
                    "properties": {
                        "idx": {"type": "integer"},
                        "motion_prompt": {"type": "string"},
                        "speech": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                },
            },
        },
    },
}


def words_in_span(words: list[Word], start: float, end: float) -> str:
    return " ".join(w.text for w in words if (w.start + w.end) / 2 >= start
                    and (w.start + w.end) / 2 < end).strip()


def analyze_scenes(
    *,
    frames: list[Path],
    spans: list[tuple[float, float]],
    words: list[Word],
    re_id: str,
) -> list[ScenePlan] | None:
    """ONE Claude vision call: per-scene motion+speech plan. None on any
    failure — the caller falls back to transcript-derived prompts."""
    try:
        from character_swap.clients import anthropic_client
        content: list[dict] = [
            {"type": "text",
             "text": f"Reference video, {len(frames)} scenes. For each scene you "
                     "get the representative frame and the transcript span."},
        ]
        for i, (frame, (a, b)) in enumerate(zip(frames, spans)):
            spoken = words_in_span(words, a, b)
            content.append({"type": "text",
                            "text": f"SCENE {i} [{a:.1f}s – {b:.1f}s] spoken: "
                                    f"{spoken or '(silent)'}"})
            content.append(anthropic_client._file_to_image_block(frame))
        resp = anthropic_client.messages_with_tools(
            system=REENGINEER_ANALYST_SYSTEM,
            messages=[{"role": "user", "content": content}],
            tools=[REENGINEER_ANALYST_TOOL],
            tool_choice={"type": "tool", "name": "submit_scene_plan"},
            max_tokens=8192,
            temperature=0.3,
            job_id=re_id,
            phase="reengineer_analyze",
        )
        data = anthropic_client.extract_tool_call(resp, "submit_scene_plan")
        if not data:
            return None
        plans = [ScenePlan(idx=int(s["idx"]), motion_prompt=s["motion_prompt"],
                           speech=s.get("speech", ""), summary=s.get("summary", ""))
                 for s in data["scenes"]]
        by_idx = {p.idx: p for p in plans}
        return [by_idx[i] for i in range(len(frames)) if i in by_idx] or None
    except Exception:
        return None


def fallback_plans(spans: list[tuple[float, float]], words: list[Word]) -> list[ScenePlan]:
    """Agent-less fallback: generic hand-held direction + verbatim dialogue."""
    out: list[ScenePlan] = []
    for i, (a, b) in enumerate(spans):
        spoken = words_in_span(words, a, b)
        speech = (f' The person says: "{spoken}" with natural lip-sync and a casual, '
                  'conversational delivery in fluent American English with a natural '
                  'American accent.') if spoken else ""
        out.append(ScenePlan(
            idx=i,
            motion_prompt=("Ordinary hand-held UGC phone footage: the person continues the "
                           "action visible in the frame with natural small movements and "
                           "gestures, looking at the camera. Static framing with slight "
                           "hand-held wobble. Not cinematic." + speech),
            speech=spoken,
            summary=spoken[:80] or f"Scene {i + 1}",
        ))
    return out
