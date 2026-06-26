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
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

from character_swap.config import settings
from character_swap.video_edit import DIALOGUE_RE, Word, _probe_duration

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


# Tombstones for hard-deleted runs (backlog #25, 2026-06-12): in-process
# watchers/tasks hold the state dict in memory — after DELETE rmtree'd the
# run dir, their next save_state() RESURRECTED a ghost state.json. A
# deleted re_id refuses all further writes. Process-lifetime only — after a
# restart the dir is gone and nothing references the run.
_DELETED_RUNS: set[str] = set()


def mark_deleted(re_id: str) -> None:
    _DELETED_RUNS.add(re_id)


def is_deleted(re_id: str) -> bool:
    return re_id in _DELETED_RUNS


def save_state(state: dict) -> None:
    re_id = state.get("re_id")
    if re_id in _DELETED_RUNS:
        logger.info("reengineer %s: refusing state write — run was deleted",
                    re_id)
        return
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
    # -an: the scene-score filter only reads video frames — decoding the
    # audio track too was pure wasted CPU on every analysis pass.
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-an", "-i", str(video),
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
each SCENE of a reference UGC-style video: a chronological FRAME SEQUENCE
sampled from the actual clip at ~2.5 frames/second (each frame labeled with
its timestamp into the scene) and the words spoken during that scene (from
Whisper). READ EACH SEQUENCE AS A VIDEO, not as separate photos: compare
consecutive frames to infer the motion's direction, speed and order, and
look for STATE CHANGES between frames (an object moved, a container
emptier, residue/foam appeared, a hand changed grip) — an action can start
and finish between two samples, and it still must be described. The
original person will be REPLACED by a different person, and the frame
closest to the scene's midpoint will then be animated into a clip by an
image-to-video model (Kling v3) that also generates NATIVE AUDIO —
including the person's voice when the prompt contains dialogue.

For EVERY scene, write:
1. motion_prompt — simple and concrete (Hugo's directive 2026-06-13): no
   boilerplate, but never at the cost of the two things that matter — the
   subject performs the scene's ACTUAL action correctly, and he speaks
   WHILE doing it. Build it as:
   a) THE PHYSICAL ACTION — when the scene has one, describe it precisely
      enough to reproduce it: strong concrete verbs, the object(s) the
      hands touch, the direction, in the order the timestamps show, with
      an endpoint ("He pours kiwi pieces into the bowl, then holds it
      toward the camera."). Play the frame sequence as a VIDEO: compare
      consecutive frames for state changes (an object moved, a container
      emptier, residue/foam appeared) — an action that happened between
      two samples must still be described, never reduced to a static pose.
      Keep hands anchored to objects. Skip filler gestures ("gestures
      expressively", "hands opening and closing") — only motion that is
      actually visible and matters. Pure talking scenes need no action
      clause.
   b) DIALOGUE, BOUND to the action so the speech happens DURING the
      movement, with the delivery folded into the attribution:
      While pouring, he says enthusiastically to the camera with an
      American accent: "<dialogue>"
      — or for pure talking scenes simply: He says enthusiastically to
      the camera with an American accent: "<dialogue>". Pick the tone word
      from the actual delivery (enthusiastically / calmly /
      matter-of-factly / warmly / urgently…) and use He/She to match the
      subject. The person must never freeze or pause the action to talk —
      speech and motion run simultaneously.
   NEVER describe the environment, background, location, lighting, camera,
   framing or the person's appearance — the start image carries all of
   that, and (officially documented) text that deviates from the image
   causes camera cuts. No "handheld", no "micro-shake", no "static
   framing", no "UGC". Aim for ~10-30 words before the dialogue quote —
   whatever correct motion needs, nothing more.
2. speech — the dialogue line alone (empty string if the scene has no speech).
3. summary — one short line describing the scene for a UI list.

Rules:
- Use the spoken words EXACTLY as transcribed; do not paraphrase dialogue.
  Exception: write digits, units and abbreviations as spoken words
  ("forty-two" not "42", "doctor" not "Dr.") — the voice engine reads
  digit characters one by one. lowercase dialogue except proper nouns and
  true acronyms.
- Never add scene elements that are not visible in the frames; movement
  must be physically plausible from the frame nearest the scene midpoint
  (that exact frame is what gets animated).
- The voice should match the demographic of the REPLACEMENT character, so
  describe the voice generically ("a natural middle-aged male voice" style
  hints belong to the pipeline, not you) — just mark the dialogue.
"""

# Appended to REENGINEER_ANALYST_SYSTEM when a run requests a non-English
# spoken language (Hugo 2026-06-20: the Spanish Reengineer mode). Kling
# synthesizes the voice from the prompt, so the ONLY thing that changes is the
# quoted dialogue + its attribution clause — every stage direction stays
# English. This block OVERRIDES the base prompt's "use the spoken words EXACTLY
# / American accent" rules for the dialogue, nothing else.
REENGINEER_SPANISH_DIRECTIVE = """

OUTPUT LANGUAGE OVERRIDE — SPANISH DIALOGUE (highest priority):
- Write ALL spoken DIALOGUE (only the text inside the quotes) in natural,
  idiomatic, NEUTRAL LATIN AMERICAN Spanish. TRANSLATE the transcribed words
  into Spanish — convey the same meaning and tone, do NOT translate word-for-
  word, and do NOT keep the original English words. This OVERRIDES the
  "use the spoken words EXACTLY as transcribed" rule (that rule still means:
  do not change the MEANING — just render it in Spanish).
- In the attribution clause, say the dialogue is spoken in Spanish instead of
  naming an English accent — e.g. 'While pouring, he says enthusiastically to
  the camera in neutral Latin American Spanish: "<diálogo>"'. NEVER write
  "American accent" or "American English" for a Spanish run.
- The digit/units rule applies in Spanish: spell numbers and units as Spanish
  words ("cuarenta", not "40" or "forty").
- EVERYTHING ELSE stays in ENGLISH: the physical-action description and all
  stage directions remain English exactly as instructed above. ONLY the quoted
  dialogue + its in-Spanish attribution are Spanish.
"""

# Attribution wording for a Spanish run's dialogue clause (fallback path +
# added-scene prefill). Mirrors what the analyst is told to write so
# _with_accent / _DIALOGUE_RE treat both paths identically.
SPANISH_SPEECH_ATTRIBUTION = "in neutral Latin American Spanish"

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
    motion_frames: list[list[tuple[Path, float]]] | None = None,
    language: str = "en",
) -> list[ScenePlan] | None:
    """ONE Claude vision call: per-scene motion+speech plan. None on any
    failure — the caller falls back to transcript-derived prompts.

    `motion_frames` (one chronological [(frame, offset_secs), ...] sequence
    per scene, ~2.5 fps) lets the analyst read each scene like a low-fps
    VIDEO — a single frame collapsed dynamic actions ("pours baking soda
    over the kiwis") into static poses ("holds kiwis"), and sparse fixed
    samples left multi-second gaps where a quick action could hide (Hugo
    2026-06-12). Falls back to the single representative frame per scene
    when omitted."""
    try:
        from character_swap.clients import anthropic_client
        content: list[dict] = [
            {"type": "text",
             "text": f"Reference video, {len(frames)} scenes. Each scene is "
                     "shown as a chronological frame sequence sampled from "
                     "the actual clip (timestamps are seconds into the "
                     "scene) — read it as a low-fps VIDEO."},
        ]
        for i, (frame, (a, b)) in enumerate(zip(frames, spans)):
            spoken = words_in_span(words, a, b)
            content.append({"type": "text",
                            "text": f"SCENE {i} [{a:.1f}s – {b:.1f}s] spoken: "
                                    f"{spoken or '(silent)'}"})
            seq = (motion_frames[i]
                   if motion_frames and i < len(motion_frames)
                   and motion_frames[i] else [(frame, None)])
            for fp, off in seq:
                if off is not None and len(seq) > 1:
                    content.append({"type": "text", "text": f"t=+{off:.1f}s:"})
                content.append(anthropic_client._file_to_image_block(fp))
        system = REENGINEER_ANALYST_SYSTEM
        if language == "es":
            system += REENGINEER_SPANISH_DIRECTIVE
        resp = anthropic_client.messages_with_tools(
            system=system,
            messages=[{"role": "user", "content": content}],
            tools=[REENGINEER_ANALYST_TOOL],
            tool_choice={"type": "tool", "name": "submit_scene_plan"},
            max_tokens=8192,
            temperature=0.3,
            job_id=re_id,
            phase="reengineer_analyze",
            timeout=settings.reengineer_analyst_timeout_secs,
        )
        data = anthropic_client.extract_tool_call(resp, "submit_scene_plan")
        if not data:
            logger.warning("reengineer analyst (%s): submit_scene_plan tool "
                           "not invoked in response", re_id)
            return None
        plans = [ScenePlan(idx=int(s["idx"]), motion_prompt=s["motion_prompt"],
                           speech=s.get("speech", ""), summary=s.get("summary", ""))
                 for s in data["scenes"]]
        by_idx = {p.idx: p for p in plans}
        return [by_idx[i] for i in range(len(frames)) if i in by_idx] or None
    except Exception as e:
        # Callers fall back to generic prompts — but the operator must be
        # able to see WHY the analyst died (backlog #23).
        logger.warning("reengineer analyst (%s) failed: %s: %s",
                       re_id, type(e).__name__, e)
        return None


def snap_spans_to_word_gaps(
    spans: list[tuple[float, float]],
    words: list[Word],
    *,
    max_shift: float = 0.6,
    min_gap: float = 0.12,
    min_span: float = 0.3,
) -> list[tuple[float, float]]:
    """Move interior span boundaries off mid-word onto the nearest
    inter-word gap (backlog #31, 2026-06-12). ffmpeg's scene detector cuts
    on VISUALS — a phrase crossing a visual cut got split mid-word, baking
    an orphan fragment into one Kling clip and the rest into the next.

    A boundary inside a word moves to the nearest gap midpoint within
    `max_shift`, falling back to that word's end. Boundaries already in
    silence stay put. Output spans remain contiguous and cover the same
    total range; degenerate spans (< `min_span`) merge into their
    neighbor."""
    if len(spans) < 2 or not words:
        return spans
    # Only contiguous span chains (detect_scenes' contract) can have their
    # shared boundaries moved — anything else passes through untouched.
    if any(abs(spans[i][1] - spans[i + 1][0]) > 1e-6
           for i in range(len(spans) - 1)):
        return spans
    gaps = [(w1.end + w2.start) / 2.0
            for w1, w2 in zip(words, words[1:])
            if (w2.start - w1.end) >= min_gap]
    start0, end_n = spans[0][0], spans[-1][1]
    moved: list[float] = []
    for _, b in spans[:-1]:
        word = next((w for w in words if w.start < b < w.end), None)
        if word is None:
            moved.append(b)
            continue
        near = [g for g in gaps if abs(g - b) <= max_shift]
        moved.append(min(near, key=lambda g: abs(g - b)) if near
                     else word.end + 0.02)
    cleaned: list[float] = []
    prev = start0
    for b in sorted(moved):
        b = min(b, end_n - min_span)
        if b < prev + min_span:
            continue                      # degenerate — merge into neighbor
        cleaned.append(b)
        prev = b
    edges = [start0, *cleaned, end_n]
    return list(zip(edges, edges[1:]))


def fallback_plans(spans: list[tuple[float, float]], words: list[Word]) -> list[ScenePlan]:
    """Agent-less fallback: minimal direction + verbatim dialogue (simple
    Kling style, Hugo 2026-06-13 — gender-neutral since no frames are
    seen)."""
    out: list[ScenePlan] = []
    for i, (a, b) in enumerate(spans):
        spoken = words_in_span(words, a, b)
        speech = (' The person says to the camera with an American accent: '
                  f'"{spoken}"') if spoken else ""
        out.append(ScenePlan(
            idx=i,
            motion_prompt=("The person continues the action visible in the "
                           "image naturally." + speech),
            speech=spoken,
            summary=spoken[:80] or f"Scene {i + 1}",
        ))
    return out


def translate_dialogue(lines: list[str], *,
                       re_id: str | None = None) -> list[str] | None:
    """Translate each line to neutral Latin American Spanish (one GPT-4o call).
    Returns a list aligned 1:1 with `lines`, or None on any failure — the
    caller then leaves the English text in place (visible + editable at the
    gate, never silently shipped as final). Empty strings pass through."""
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if not idx:
        return list(lines)
    payload = {str(j): lines[i] for j, i in enumerate(idx)}
    try:
        import json as _json

        from character_swap.call_log import record
        from character_swap.clients import openai_image
        client = openai_image._client()
        system = (
            "You translate short spoken lines from a video script into "
            "natural, idiomatic NEUTRAL LATIN AMERICAN Spanish (the kind used "
            "in widely-distributed social content — no country-specific slang, "
            "no Spain 'vosotros'/'th' seseo). Convey meaning and tone, not a "
            "word-for-word translation. Spell numbers and units as Spanish "
            "words. Return STRICT JSON: the SAME keys you were given, each "
            "mapped to its Spanish translation. No extra keys, no commentary.")
        with record(phase="reengineer_translate", model="gpt-4o",
                    character="es", job_id=re_id):
            resp = client.chat.completions.create(
                model="gpt-4o",
                messages=[{"role": "system", "content": system},
                          {"role": "user",
                           "content": _json.dumps(payload, ensure_ascii=False)}],
                response_format={"type": "json_object"},
                temperature=0.3,
                max_tokens=1200,
            )
        raw = (resp.choices[0].message.content or "").strip()
        data = _json.loads(raw)
    except Exception as e:
        logger.warning("reengineer translate (%s) failed: %s: %s",
                       re_id, type(e).__name__, e)
        return None
    out = list(lines)
    for j, i in enumerate(idx):
        val = data.get(str(j))
        if isinstance(val, str) and val.strip():
            out[i] = val.strip()
        else:
            return None          # incomplete → don't half-translate
    return out


def spanish_speech_clause(spoken_es: str) -> str:
    """Spanish-run dialogue attribution (fallback + added-scene prefill);
    mirrors what the analyst is told to write."""
    return (f' The person says to the camera {SPANISH_SPEECH_ATTRIBUTION}: '
            f'"{spoken_es}"')


# --- Per-language Kling speech clause (canonical home) ------------------------
# runner_reengineer aliases these as `_ACCENT_CLAUSE` / `_with_accent` for
# back-compat. Per-language accent clause + the keyword that marks "already
# covered" so the clause is never doubled. Spanish (Hugo 2026-06-20) only
# changes WHICH accent is enforced — the dialogue itself is written in Spanish
# upstream (analyst / fallback translation, or per-character
# `localize_motion_prompt`). Mirrored in app.js klingSuffix().
ACCENT_CLAUSE: dict[str, tuple[str, str]] = {
    "en": (" The person speaks fluent American English with a natural "
           "American accent.", "american"),
    "es": (" The person speaks fluent, natural Latin American Spanish with a "
           "neutral Latin American Spanish accent.", "spanish"),
}


def with_accent(prompt: str, language: str = "en") -> str:
    """Kling synthesizes voice AND ambience from the prompt — enforce three
    guarantees centrally, even if a scene's agent-written prompt forgot them:
    the run's spoken-language accent + clear pronunciation (Hugo 2026-06-11;
    garbled words like "baking goda" observed) and NO music bed (research
    2026-06-12: generate_audio invents background music unless told otherwise;
    there is no API switch, suppression is prompt-level). The pronunciation +
    no-music directives stay English (they are instructions, not speech). Each
    clause is skipped when the prompt already covers it. Mirrored in app.js
    klingSuffix()."""
    out = prompt
    clause, key = ACCENT_CLAUSE.get(language, ACCENT_CLAUSE["en"])
    if key not in out.lower():
        out = out.rstrip() + clause
    if "pronounc" not in out.lower():
        out = (out.rstrip() + " Every word is pronounced clearly, correctly "
               "and distinctly.")
    if "music" not in out.lower():
        out = (out.rstrip() + " No background music — natural ambient room "
               "sound only.")
    return out


# The analyst (and the English fallback) write the speaking accent INLINE inside
# the says-clause — "...says to the camera with an American accent: \"…\"" — not
# as the standalone ACCENT_CLAUSE["en"] sentence. The per-character ES localizer
# must strip that inline phrase too, or Kling gets a contradictory accent signal
# wrapping the Spanish words. Anchored on `an?` (NOT "Latin American") so it never
# touches the Spanish clause's "neutral Latin American Spanish".
_INLINE_EN_ACCENT_RE = re.compile(
    r"(?i)\s+(?:with|in)\s+an?\s+(?:natural\s+)?American(?:\s+English)?\s+accent")

# Memoize localized prompts so SERIAL re-renders (per-clip retries / crash
# resumes / a later job reusing the same scene prompt) skip a redundant GPT-4o
# call. Keyed by (language, prompt); process-life. NOTE: this does NOT dedupe the
# CONCURRENT fan-out — the M same-prompt takes animate via asyncio.gather and all
# miss the cache before any writes it, so they each translate once. That is a
# bounded, correctness-neutral cost (the translation is deterministic) on a cheap
# call; reliability is unaffected.
_LOCALIZE_CACHE: dict[tuple[str, str], str] = {}


def localize_motion_prompt(prompt: str, language: str | None, *,
                           job_id: str | None = None) -> str:
    """Localize ONE clip's motion prompt for a per-CHARACTER spoken language
    (Hugo 2026-06-26). Currently only "es" (neutral Latin American Spanish)
    differs from the default.

    For "es": translate the quoted dialogue (each says-clause phrase) to Spanish
    IN PLACE — the English framing / camera / action stays English — and swap
    any English accent clause for the Spanish one (`with_accent` adds the ES
    accent + clear-pronunciation + no-music guarantees). Returns the prompt
    UNCHANGED for None/"en".

    Additive to the per-run 🗣 picker: if the prompt is already Spanish (a full
    "es" run localized it upstream — its dialogue + accent are written in
    Spanish), it is returned unchanged so the user-approved Spanish text is
    never re-translated.

    Fail-soft: on translation failure the prompt is left FULLY English (the ES
    accent clause is NOT added, so a Spanish-accent instruction never wraps
    English words) and a warning is logged — never silently shipped as broken
    half-Spanish."""
    lang = (language or "en").strip().lower()
    if lang != "es" or not (prompt or "").strip():
        return prompt
    # Already a full-"es" run. Both the run-level shapes — the analyst's inline
    # "in neutral Latin American Spanish" attribution AND the standalone ES
    # accent clause — contain "Latin American Spanish", whereas a real "es" run
    # never re-adds the standalone sentence (with_accent skips it once the inline
    # text already says "spanish"). Matching that phrase (not the standalone
    # sentence) is what reliably detects the already-localized prompt without
    # tripping on a coincidental "Spanish-style" scene description.
    if "latin american spanish" in prompt.lower():
        return prompt
    cached = _LOCALIZE_CACHE.get((lang, prompt))
    if cached is not None:
        return cached

    matches = list(DIALOGUE_RE.finditer(prompt))
    phrases = [m.group(1).strip() for m in matches]
    es_prompt = prompt
    if any(phrases):
        translated = translate_dialogue(phrases, re_id=job_id)
        if translated is None:
            logger.warning("localize_motion_prompt (%s): dialogue translation "
                           "failed; leaving English", job_id)
            return prompt          # fail-soft: coherent English, no ES accent
        # Replace each captured dialogue span in REVERSE so earlier spans keep
        # their offsets while later ones are substituted.
        for m, es in reversed(list(zip(matches, translated))):
            es = (es or "").strip()
            if not es:
                continue
            s, e = m.span(1)
            es_prompt = es_prompt[:s] + es + es_prompt[e:]
    # Strip EVERY English-accent marker — the standalone sentence AND the
    # analyst's inline "with an American accent" attribution — then let
    # with_accent add the single Spanish accent clause (+ pronunciation /
    # no-music). Otherwise Kling sees a contradictory accent next to Spanish.
    es_prompt = es_prompt.replace(ACCENT_CLAUSE["en"][0], "")
    es_prompt = _INLINE_EN_ACCENT_RE.sub("", es_prompt)
    es_prompt = with_accent(es_prompt, "es")
    _LOCALIZE_CACHE[(lang, prompt)] = es_prompt
    return es_prompt


def spanishize_plans(plans: list[ScenePlan], *,
                     re_id: str | None = None) -> list[ScenePlan]:
    """Rewrite agent-less fallback plans into Spanish: translate each scene's
    dialogue and rebuild its motion prompt with the Spanish attribution.
    Translation failure → plans returned unchanged (English shows at the gate
    for the user to fix; never silently finalized)."""
    translated = translate_dialogue([p.speech or "" for p in plans], re_id=re_id)
    if translated is None:
        return plans
    out: list[ScenePlan] = []
    for p, es in zip(plans, translated):
        es = (es or "").strip()
        clause = spanish_speech_clause(es) if es else ""
        out.append(ScenePlan(
            idx=p.idx,
            motion_prompt=("The person continues the action visible in the "
                           "image naturally." + clause),
            speech=es,
            summary=(es[:80] or p.summary),
        ))
    return out
