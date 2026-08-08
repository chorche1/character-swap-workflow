"""Reengineer pipeline — primitives.

Takes an uploaded reference video and rebuilds it with a different character:
the video is split into scenes, each scene contributes one representative
frame, a Claude vision agent writes a motion+speech prompt per scene from the
original footage, and the frames become the scenes of a regular Swap job
(character swap → per-scene Kling v3 clips with NATIVE audio → trim to the
original scene durations → concat per character).

This module holds the pure pieces: per-run state I/O (a state.json per run
dir), ffmpeg scene detection + frame extraction, and the scene-analysis
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
from character_swap.video_edit import (
    DIALOGUE_RE,
    Word,
    _probe_duration,
    dialogue_matches,
    phrase_key,
)

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
    carry_start: float | None = None   # too-short opener start, folded forward
    for s in scenes:
        span = (carry_start if carry_start is not None else s[0], s[1])
        if merged and (span[1] - span[0]) < min_secs:
            merged[-1] = (merged[-1][0], span[1])
        elif not merged and (span[1] - span[0]) < min_secs and len(scenes) > 1:
            # Fold a too-short opener into what FOLLOWS: carry its start
            # forward so the next scene absorbs it. (Appending it as a
            # "provisional" scene left the fragment as its own scene
            # whenever the second scene was normal-length — the extend
            # branch above only fires for a too-short successor.)
            carry_start = span[0]
            continue
        else:
            merged.append(span)
        carry_start = None
    if carry_start is not None:
        # Every scene was a too-short fragment folding forward — emit the
        # accumulated span as ONE scene rather than dropping the video.
        merged.append((carry_start, scenes[-1][1]))

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
        # The only consumer (runner_reengineer._analyze) pairs the plans
        # POSITIONALLY against spans/frames — a partial plan would shift
        # every scene after a gap onto the NEXT scene's prompt/dialogue,
        # drop the last scene entirely, and cache the corruption in
        # plan.json forever. Refuse anything but exact 1:1 coverage; the
        # caller falls back to fallback_plans (one plan per span, always).
        missing = [i for i in range(len(frames)) if i not in by_idx]
        extra = sorted(i for i in by_idx if not 0 <= i < len(frames))
        if missing or extra:
            logger.warning(
                "reengineer analyst (%s): plan does not cover the %d scenes "
                "1:1 (missing idx %s, unknown idx %s) — discarding it, "
                "falling back to transcript-derived prompts",
                re_id, len(frames), missing, extra)
            return None
        return [by_idx[i] for i in range(len(frames))] or None
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


def translate_dialogue(lines: list[str], *, language: str = "es",
                       re_id: str | None = None) -> list[str] | None:
    """Translate each line to `language` (one GPT-4o call). Target defaults to
    Spanish — the first language this path ever had — and any code in
    `SPOKEN_LANGUAGES` is accepted (German added 2026-08-02).

    Returns a list aligned 1:1 with `lines`, or None on any failure — the
    caller then leaves the English text in place (visible + editable at the
    gate, never silently shipped as final). Empty strings pass through."""
    spec = SPOKEN_LANGUAGES.get((language or "es").strip().lower())
    if spec is None:
        logger.warning("reengineer translate (%s): unknown target language %r",
                       re_id, language)
        return None
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if not idx:
        return list(lines)
    payload = {str(j): lines[i] for j, i in enumerate(idx)}
    try:
        import json as _json

        from character_swap.call_log import record
        from character_swap.clients import openai_image
        client = openai_image._client()
        system = spec.translate_system
        with record(phase="reengineer_translate", model="gpt-4o",
                    character=spec.code, job_id=re_id):
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


# --- Per-character spoken languages (canonical registry) ----------------------
# A library character can be flagged with a spoken language: "es" (Hugo
# 2026-06-26) and "de" (Hugo 2026-08-02). EVERYTHING the localizer needs per
# language lives in ONE row here — accent clause, the "already in this
# language" marker, the inline attribution the analyst-style says-clause uses,
# and the translator's own system prompt — so adding a language is a data
# change plus its UI option, never a new branch in every helper.
#
# English is the implicit default and deliberately has NO row: it is what a
# prompt already is. Its accent clause lives in ACCENT_CLAUSE below (the
# run-level 🗣 picker still speaks in en/es terms; German is per-character
# only, Hugo 2026-08-02).
@dataclass(frozen=True)
class SpokenLanguage:
    code: str             # "es"
    name_en: str          # "Spanish" — operator-facing error text
    accent_clause: str    # standalone Kling clause (leading space, ends ".")
    accent_key: str       # lowercase word = "this clause is already present"
    marker: str           # lowercase phrase = "prompt is already in this lang"
    attribution: str      # inline: '…says to the camera <attribution>: "…"'
    translate_system: str  # GPT-4o system prompt for translate_dialogue
    # Explicit "speak ONLY this language" order (leading space, ends "."), and
    # the lowercase phrase that marks it already present. Third and last layer
    # of the language guarantee (Hugo 2026-08-02): the standalone accent clause
    # sits at the END of a long English prompt, and on its own it was not
    # enough — 8 of 10 🇩🇪 clips in re_b3170d2118 spoke ENGLISH despite carrying
    # correct German dialogue. See `enforce_language_directives`.
    speak_only_clause: str
    speak_only_key: str
    @property
    def speech_order(self) -> str:
        """Replacement for an explicit "speaking English…" order in a prompt."""
        return f"speaking {self.attribution}"

    @property
    def accent_phrase(self) -> str:
        """The attribution as a bare NOUN phrase — "standard German" — for the
        Director's labeled voice fields ("Accent: American" → "Accent: standard
        German", "Voice: male, American accent" → "…, standard German
        accent"). Derived from `attribution` so the two can never drift."""
        return re.sub(r"(?i)^in\s+", "", self.attribution)


GERMAN_SPEECH_ATTRIBUTION = "in standard German"

SPOKEN_LANGUAGES: dict[str, SpokenLanguage] = {
    "es": SpokenLanguage(
        code="es",
        name_en="Spanish",
        accent_clause=(" The person speaks fluent, natural Latin American "
                       "Spanish with a neutral Latin American Spanish accent."),
        accent_key="spanish",
        # Substring-matching the bare "latin american spanish" was too broad —
        # a dialogue line "...authentic Latin American Spanish cuisine" would
        # falsely skip translation.
        marker="neutral latin american spanish",
        attribution=SPANISH_SPEECH_ATTRIBUTION,
        speak_only_clause=(" The person speaks ONLY Spanish — every spoken "
                           "word is Spanish. No English is spoken at any "
                           "point."),
        speak_only_key="speaks only spanish",
        translate_system=(
            "You translate short spoken lines from a video script into "
            "natural, idiomatic NEUTRAL LATIN AMERICAN Spanish (the kind used "
            "in widely-distributed social content — no country-specific slang, "
            "no Spain 'vosotros'/'th' seseo). Convey meaning and tone, not a "
            "word-for-word translation. Spell numbers and units as Spanish "
            "words. If a line is ALREADY in Spanish, return it EXACTLY as "
            "given — unchanged, word for word — never a reworded version. "
            "Return STRICT JSON: the SAME keys you were given, each "
            "mapped to its Spanish translation. No extra keys, no commentary."),
    ),
    "de": SpokenLanguage(
        code="de",
        name_en="German",
        accent_clause=(" The person speaks fluent, natural standard German "
                       "(Hochdeutsch) with a neutral German accent."),
        accent_key="german",
        # "standard german" appears in both this clause and the attribution,
        # and (unlike a bare "german") never in a natural German dialogue line.
        marker="standard german",
        attribution=GERMAN_SPEECH_ATTRIBUTION,
        speak_only_clause=(" The person speaks ONLY German — every spoken "
                           "word is German. No English is spoken at any "
                           "point."),
        speak_only_key="speaks only german",
        translate_system=(
            "You translate short spoken lines from a video script into "
            "natural, idiomatic STANDARD GERMAN (Hochdeutsch) — the neutral "
            "register used in widely-distributed social content: no regional "
            "dialect (no Bavarian, Swiss or Austrian markers), no heavy slang. "
            "Address the viewer with the informal 'du'. Convey meaning and "
            "tone, not a word-for-word translation. Spell numbers and units as "
            "German words. If a line is ALREADY in German, return it EXACTLY "
            "as given — unchanged, word for word — never a reworded version. "
            "Return STRICT JSON: the SAME keys you were given, "
            "each mapped to its German translation. No extra keys, no "
            "commentary."),
    ),
}


# --- Per-language Kling speech clause (canonical home) ------------------------
# runner_reengineer aliases these as `_ACCENT_CLAUSE` / `_with_accent` for
# back-compat. Per-language accent clause + the keyword that marks "already
# covered" so the clause is never doubled. A non-English language only changes
# WHICH accent is enforced — the dialogue itself is written in that language
# upstream (analyst / fallback translation, or per-character
# `localize_motion_prompt`). The en/es rows are mirrored in app.js
# klingSuffix() (the run-level picker's two languages).
ACCENT_CLAUSE: dict[str, tuple[str, str]] = {
    "en": (" The person speaks fluent American English with a natural "
           "American accent.", "american"),
    **{code: (spec.accent_clause, spec.accent_key)
       for code, spec in SPOKEN_LANGUAGES.items()},
}


# The two non-language clauses `with_accent` appends to EVERY scene, silent
# ones included. Named so `_without_own_clauses` can subtract our own
# boilerplate before scanning a prompt for a speech directive — otherwise a
# wordless shot looks like it orders speech and the loud nets misfire.
_PRONOUNCE_CLAUSE = (" Every word is pronounced clearly, correctly and "
                     "distinctly.")
_NO_MUSIC_CLAUSE = (" No background music — natural ambient room sound only.")


def with_accent(prompt: str, language: str = "en") -> str:
    """Kling synthesizes voice AND ambience from the prompt — enforce three
    guarantees centrally, even if a scene's agent-written prompt forgot them:
    the run's spoken-language accent + clear pronunciation (Hugo 2026-06-11;
    garbled words like "baking goda" observed) and NO music bed (research
    2026-06-12: generate_audio invents background music unless told otherwise;
    there is no API switch, suppression is prompt-level). The pronunciation +
    no-music directives stay English (they are instructions, not speech). Each
    clause is skipped when the prompt already covers it. Mirrored in app.js
    klingSuffix().

    For a NON-English target the English accent order is rewritten to this
    language's own attribution FIRST (Hugo 2026-08-02: "alla videoprompts för
    de spanska och tyska ska automatiskt justeras så att de inte har american
    accent"). Every scene prompt Hugo writes at the gate says "…to the camera
    with an american accent…"; without this the finished prompt ordered an
    American accent right beside the line AND a Spanish/German accent at the
    end — two contradictory language orders, with the English one closest to
    the dialogue. The English path is untouched: there, an American accent
    phrase is exactly what belongs."""
    out = prompt
    spec = SPOKEN_LANGUAGES.get(language)
    if spec is not None:
        out = _INLINE_EN_ACCENT_RE.sub(f" {spec.attribution}", out)
        out = _EN_SPEECH_ORDER_RE.sub(spec.speech_order, out)
        # Both substitutions above can land the attribution next to one the
        # prompt already carried. `_force_language_speech` dedupes for the
        # per-character path; this is the run-level 🗣 path, whose output is
        # FINAL for an unflagged character — `localize_motion_prompt` returns
        # those unchanged, so nothing downstream would collapse it.
        out = _dedupe_attribution(out, spec)
        out = out.replace(ACCENT_CLAUSE["en"][0], "")
    clause, key = ACCENT_CLAUSE.get(language, ACCENT_CLAUSE["en"])
    if key not in out.lower():
        out = out.rstrip() + clause
    if "pronounc" not in out.lower():
        out = out.rstrip() + _PRONOUNCE_CLAUSE
    if "music" not in out.lower():
        out = out.rstrip() + _NO_MUSIC_CLAUSE
    return out


# The analyst (and the English fallback) write the speaking accent INLINE inside
# the says-clause — "...says to the camera with a clear American accent: \"…\"" —
# not as the standalone ACCENT_CLAUSE["en"] sentence. The per-character ES
# localizer must strip that inline phrase too, or Kling gets a contradictory
# accent next to the Spanish words. Allows up to two adjectives ("natural",
# "clear", "General", ...) before "American"; anchored so it never eats the
# Spanish clause's "...Latin American Spanish accent" ("American" there is
# followed by "Spanish", not "English"/"accent").
#
# The ARTICLE is optional (Hugo 2026-08-08): the Director's AUDIO fields write
# the phrase without one — "Voice: Female with American accent", "addresses the
# camera with American accent". Those used to fall through to the bare pattern
# below, which then ate the preposition's OBJECT along with it ("Female", "the
# camera"). Catching them here replaces the whole prepositional phrase, so the
# result reads "Voice: Female in standard German" and the speaker's gender —
# the very thing the 👥 agent exists to get right — survives.
_INLINE_EN_ACCENT_RE = re.compile(
    r"(?i)\s+(?:with|in)\s+(?:an?\s+)?(?:\w+\s+){0,2}?"
    r"American(?:\s+English)?\s+accent")

# An explicit ORDER to speak English — the Director's own AUDIO phrasing,
# "Deep, clear male voice speaking English with a thick Texas accent". Neither
# marker above catches it: it is not the standalone clause, and its accent is
# "Texas", not "American". Left standing NEXT TO a translated Spanish line it
# is the strongest, closest instruction in the prompt, so Kling follows it and
# the clip speaks English anyway (Hugo 2026-07-31). The optional trailing
# `with a … accent` is consumed with it so no orphan English-region accent
# survives the swap.
#
# The verb and "English" are not always adjacent (Hugo 2026-08-07): the
# Director wrote "Voice of a middle-aged man speaking clearly and
# enthusiastically in English" on re_db8e7b4e91, so the order survived, sat
# next to the appended Spanish attribution, and the clip came out Spanish with
# an ENGLISH tail ("…and it's really for Colferin", vd_295249). Up to five
# intervening word/comma tokens are allowed; `.` and `—` are NOT in the class,
# so the span can never cross a sentence boundary — and our own speak-only
# clause ("speaks ONLY German — … No English is spoken at any point") stays
# unmatched for exactly that reason.
_EN_SPEECH_ORDER_RE = re.compile(
    r"(?i)\b(?:speaks?|speaking|spoken)\s+(?:[\w,-]+\s+){0,5}?"
    r"(?:in\s+)?English\b"
    r"(?:\s+with\s+an?\s+(?:[\w-]+\s+){0,3}?accent)?")

# A BARE English accent with no "with an" / "in an" preposition — the
# Director's AUDIO block writes it as a field: "Voice: Male, American accent"
# / "Accent: American." `_INLINE_EN_ACCENT_RE` requires the preposition, so
# these went to the model untouched. On re_db8e7b4e91 scene 1 that was one of
# the two misses that let four 🇪🇸/🇩🇪 clips ship in verbatim English.
# Replaced with the target language's own accent phrase (not deleted) for the
# 2026-08-02 reason: a voice field that names NO language reads as English.
#
# The words it may absorb before "American" are restricted to genuine accent
# QUALIFIERS (Hugo 2026-08-08). A bare `\w+` ate whatever preceded the phrase:
# `Voice: Female, American accent` lost "Female" and `addresses the camera with
# American accent` became "addresses the neutral Latin American Spanish
# accent" — the gender the 👥 speaker-fix agent had just been paid to establish,
# and a camera instruction, deleted on 18 of 997 real prompts. The qualifier
# list still swallows the regional adjectives that must not survive the swap
# ("thick Texas American accent"), because an orphan English-region accent is
# the whole reason this replaces rather than deletes.
_ACCENT_QUALIFIER = (
    r"(?:American|English|British|General|Standard|Neutral|Natural|Clear|"
    r"Thick|Strong|Slight|Mild|Heavy|Light|Soft|Deep|Warm|Crisp|Smooth|"
    r"Subtle|Authentic|Regional|Southern|Northern|Midwest\w*|Texas|Texan|"
    r"Californian?|Boston|Brooklyn|East\s+Coast|West\s+Coast|New\s+York)")
_BARE_EN_ACCENT_RE = re.compile(
    rf"(?i)\b(?:{_ACCENT_QUALIFIER}\s+){{0,2}}?American(?:\s+English)?\s+accent")

# `Language: English.` as a labeled field in the same AUDIO block. Nothing
# stripped it, so re_db8e7b4e91 shipped prompts ordering English and German at
# once, with English written FIRST and closest to the line.
_EN_LANGUAGE_FIELD_RE = re.compile(
    r"(?i)\bLanguage\s*:\s*English\b")
# The negative lookahead is what keeps this off OUR OWN output: the
# replacement is "Accent: neutral Latin American Spanish", whose "…Latin
# American" would otherwise re-match and make the residual scan refuse every
# good Spanish clip.
_EN_ACCENT_FIELD_RE = re.compile(
    r"(?i)\bAccent\s*:\s*(?:\w+\s+){0,2}?American(?:\s+English)?\b"
    r"(?!\s+Spanish)")

# The inline attribution of ANOTHER registered language ("...says to the camera
# in neutral Latin American Spanish:"). Reachable when the run-level 🗣 picker
# localized the whole run to Spanish and a character is flagged for a different
# language — the dialogue gets re-translated, so the old attribution must go
# with it or the prompt orders two languages at once.
_ATTRIBUTION_RES: dict[str, re.Pattern[str]] = {
    code: re.compile(r"(?i)\s*\b(?:speaking\s+)?" + re.escape(spec.attribution))
    for code, spec in SPOKEN_LANGUAGES.items()
}

# The marker that a prompt is ALREADY a full-"es" run. Kept as the historical
# name; the canonical value lives on the registry row.
_ES_LOCALIZED_MARKER = SPOKEN_LANGUAGES["es"].marker


def _has_english_speech_directive(prompt: str) -> bool:
    """True when a prompt TELLS the model to speak English, even though it
    carries no quoted dialogue we could translate.

    Such a prompt is not a silent clip: Kling improvises speech from it, in the
    language it was told to use. For an 🇪🇸/🇩🇪-flagged character that directive
    is exactly wrong, so the language swap must happen anyway (Hugo
    2026-07-31)."""
    text = prompt or ""
    return bool(ACCENT_CLAUSE["en"][0] in text
                or _INLINE_EN_ACCENT_RE.search(text)
                or _EN_SPEECH_ORDER_RE.search(text)
                or _BARE_EN_ACCENT_RE.search(text)
                or _EN_LANGUAGE_FIELD_RE.search(text)
                or _EN_ACCENT_FIELD_RE.search(text))


def _without_own_clauses(text: str, spec: SpokenLanguage) -> str:
    """`text` with OUR OWN boilerplate removed, so the scans below cannot trip
    over words we ourselves appended — `speak_only_clause` ends with "No
    English is spoken at any point.", and `with_accent` appends the English
    accent clause ("The person speaks fluent American English…") to EVERY
    scene, silent ones included."""
    out = text or ""
    for clause in (spec.speak_only_clause, spec.accent_clause,
                   ACCENT_CLAUSE["en"][0], _PRONOUNCE_CLAUSE, _NO_MUSIC_CLAUSE):
        out = out.replace(clause, " ")
    return _ATTRIBUTION_RES[spec.code].sub(" ", out)


def _residual_english_order(text: str, spec: SpokenLanguage) -> str | None:
    """The English speech order still standing AFTER sanitization, or None.

    The sanitizer recognises SHAPES, and the Director writes free-form English
    — every shape it has not met yet is a silent English clip (five leaked on
    2026-08-06 alone). This turns the next unknown shape into a LOUD failure
    before a single credit is spent, which is Hugo's standing rule."""
    stripped = _without_own_clauses(text, spec)
    for rx in (_INLINE_EN_ACCENT_RE, _EN_SPEECH_ORDER_RE, _BARE_EN_ACCENT_RE,
               _EN_LANGUAGE_FIELD_RE, _EN_ACCENT_FIELD_RE):
        m = rx.search(stripped)
        if m:
            return m.group(0).strip()
    if ACCENT_CLAUSE["en"][0] in stripped:
        return ACCENT_CLAUSE["en"][0].strip()
    return None


# A quoted line long enough to BE dialogue (≥5 words) that the extractor could
# not read. A quoted PROP — `bottle labeled "Heinz White Vinegar"`, `comment
# "Skin"` — is shorter than that, which is what keeps a silent scene from
# tripping the loud refusal below.
_QUOTED_LINE_RE = re.compile(r'["“]([^"”]{8,})["”]')

# Deliberately wider than the extractor's own verb list — this one only has to
# recognise that the prompt is TALKING ABOUT speech, and the shapes that broke
# used "Spoken words are", "Voice:" and "Dialogue" with no verb at all. Run on
# the prompt with our own boilerplate subtracted, so the accent/pronounce
# clauses auto-appended to every scene never make a silent shot look spoken.
_SPEECH_CONTEXT_RE = re.compile(
    r"(?i)\b(?:dialogue|dialog|spoken|speech|voice|voice-?over|says?|saying|"
    r"said|speaks?|speaking|narrat\w*|utter\w*|recites?|announces|declares|"
    r"proclaims)\b")

# A prompt DENYING speech says "dialogue" too, and the gate above cannot tell
# the two apart (Hugo 2026-08-08, adversarial review). `SHOT — … No dialogue,
# ambient room tone only. A bottle labeled "Certified Organic Extra Virgin
# Olive Oil" …` is a silent B-roll shot whose only quote is a PRODUCT LABEL —
# yet it read as "orders speech, carries a line we cannot parse" and the clip
# was refused. The margin was one word: the longest non-dialogue quoted string
# in Hugo's history is 4 words, against a ≥5-word rule. Deleting the denial
# before the scan cannot weaken the guard — a prompt that also ORDERS speech
# still matches on that order.
_NO_SPEECH_RE = re.compile(
    r"(?i)\b(?:no|without|zero)\s+(?:audible\s+)?"
    r"(?:dialogue|dialog|speech|spoken\s+\w+|talking|narration|voice-?over|"
    r"words|lines)\b")


def _unparsed_dialogue_line(prompt: str, spec: SpokenLanguage) -> str | None:
    """A quoted LINE the extractor missed, or None.

    Called only once the prompt is known to ORDER speech. `dialogue_matches`
    found nothing, yet a sentence-length quote sits in a speech context — so
    the clip WILL talk, and we could neither translate what it says nor give
    video-QC anything to check it against. That combination is exactly how
    re_db8e7b4e91 shipped four verbatim-English clips carrying
    `qc_status="skipped"` (Hugo 2026-08-07: fail loudly instead).

    The ≥5-word rule is what separates a LINE from a quoted PROP — `bottle
    labeled "Heinz White Vinegar"` (3) and `comment "Skin"` (1) stay below it,
    so a short quoted prop stays below it. It is a heuristic, not a proof: a
    five-word quoted SIGN in an otherwise silent prompt would be refused. The
    longest non-dialogue quoted string in Hugo's history is 4 words, one below
    the rule — so the denial strip below is what keeps the margin honest, and
    the refusal itself is loud, names the quote, and is fixed by editing it."""
    stripped = _NO_SPEECH_RE.sub(" ", _without_own_clauses(prompt, spec))
    if not _SPEECH_CONTEXT_RE.search(stripped):
        return None
    for m in _QUOTED_LINE_RE.finditer(stripped):
        line = m.group(1).strip()
        if len(line.split()) >= 5:
            return line
    return None


def _has_foreign_speech_directive(prompt: str, language: str) -> bool:
    """True when a dialogue-less prompt still ORDERS a language that is not
    `language` — English (above) or another registered language's clause. Only
    such a prompt gets rewritten; a genuinely silent one is left untouched."""
    text = prompt or ""
    if _has_english_speech_directive(text):
        return True
    low = text.lower()
    return any(spec.marker in low
               for code, spec in SPOKEN_LANGUAGES.items() if code != language)


# Whitespace / punctuation that introduces a quoted line (": ", ", ", " — ").
# `_inject_inline_attribution` slides back over it so the language lands INSIDE
# the says-clause ("…to the camera in standard German: \"…\"") rather than
# between the colon and the quote.
_PRE_QUOTE_PUNCT_RE = re.compile(r"[\s:,\-–—]*$")

# How far back from a quoted line still counts as "this line's clause" when
# asking whether the language is already named next to it. One clause, not the
# whole prompt — see `_inject_inline_attribution`.
_ATTRIBUTION_SCOPE = 160


def _inject_inline_attribution(text: str, spec: SpokenLanguage) -> str:
    """Put "<attribution>" immediately before the FIRST quoted line.

    Hugo 2026-08-02: the standalone accent clause is appended at the very END
    of a long, otherwise-English prompt. In re_b3170d2118 that was too far from
    the dialogue for Kling to obey — 8 of 10 🇩🇪 clips spoke ENGLISH even though
    the quoted line was correct German and the clause was present. The clause
    that DOES get obeyed is the one inside the says-clause, which is exactly
    what the analyst writes for a run-level 🗣 run (`spanish_speech_clause`) and
    exactly what the per-character localizer used to DELETE without replacing.

    No-ops when the prompt carries no quoted line (nothing to attribute) or
    THAT line is already attributed (idempotent — a redo re-localizes the same
    text).

    "Already attributed" is judged on the clause introducing THIS line, not on
    the whole prompt (Hugo 2026-08-08). The old whole-prompt guard broke as
    soon as a prompt stated its line twice: `_force_language_speech` rewrites
    the inline American-accent phrase into the attribution BEFORE calling here,
    so if that phrase sat in the SECOND copy (the Director's AUDIO block), the
    attribution already existed somewhere in the text and injection at the
    first copy was skipped — leaving the sentence the model reads FIRST naming
    no language at all. That is precisely the 2026-08-02 failure it exists to
    prevent, reintroduced one layer up."""
    if not text:
        return text
    matches = dialogue_matches(text)
    if not matches:
        return text
    quote_at = matches[0].start(1) - 1        # the opening quote character
    if quote_at <= 0:
        return text
    head, tail = text[:quote_at], text[quote_at:]
    if spec.attribution.lower() in head[-_ATTRIBUTION_SCOPE:].lower():
        return text
    cut = _PRE_QUOTE_PUNCT_RE.search(head).start()
    if cut <= 0:                               # nothing but punctuation before
        return text                            # the quote — leave it alone
    return f"{head[:cut]} {spec.attribution}{head[cut:]}{tail}"


def enforce_language_directives(text: str, spec: SpokenLanguage) -> str:
    """The THREE reinforcing layers that make the clip actually speak `spec`:

    1. the inline attribution, right next to the quoted line (the one Kling
       obeys — see `_inject_inline_attribution`),
    2. the standalone accent clause (kept as a hard guarantee even when the
       inline attribution already contains the accent keyword, which would
       otherwise falsely suppress `with_accent`'s substring guard), and
    3. an explicit "speaks ONLY <language>, no English" order.

    Idempotent: re-running it on its own output changes nothing."""
    out = _inject_inline_attribution(text, spec)
    out = with_accent(out, spec.code)
    if spec.accent_clause.strip() not in out:
        out = out.rstrip() + spec.accent_clause
    if spec.speak_only_key not in out.lower():
        out = out.rstrip() + spec.speak_only_clause
    return out


def _force_language_speech(text: str, language: str) -> str:
    """Remove EVERY other language's order and guarantee `language`'s.

    One helper for both localize paths (dialogue translated / nothing to
    translate) so a prompt shape can never come out with the dialogue in the
    target language but the language order still in English. Strips the
    standalone English clause + any OTHER registered language's
    clause/attribution, REPLACES the inline American-accent phrase with this
    language's own attribution, flips an explicit "speaking English…" order in
    place, and then enforces all three directive layers.

    The American-accent phrase is REPLACED, not deleted (Hugo 2026-08-02).
    Deleting it left "…says enthusiastically to the camera: \"<German>\"" — a
    says-clause that names no language at all, inside an English prompt, with
    the German clause 200 characters further down. Kling answered that by
    speaking English."""
    spec = SPOKEN_LANGUAGES[language]
    out = text.replace(ACCENT_CLAUSE["en"][0], "")
    for code, other in SPOKEN_LANGUAGES.items():
        if code == language:
            continue
        out = out.replace(other.accent_clause, "")
        out = _ATTRIBUTION_RES[code].sub("", out)
    # LABELED voice fields first (most specific), then the prepositional inline
    # phrase, and only then the BARE accent — running bare first would eat the
    # preposition's object and leave a dangling "with a".
    out = _EN_LANGUAGE_FIELD_RE.sub(f"Language: {spec.name_en}", out)
    out = _EN_ACCENT_FIELD_RE.sub(f"Accent: {spec.accent_phrase}", out)
    out = _INLINE_EN_ACCENT_RE.sub(f" {spec.attribution}", out)
    out = _EN_SPEECH_ORDER_RE.sub(spec.speech_order, out)
    out = _BARE_EN_ACCENT_RE.sub(f"{spec.accent_phrase} accent", out)
    out = _dedupe_attribution(out, spec)
    out = enforce_language_directives(out, spec)
    if spec.marker not in out.lower():
        out = out.rstrip() + spec.accent_clause
    return out


def _dedupe_attribution(text: str, spec: SpokenLanguage) -> str:
    """Collapse an attribution repeated back-to-back.

    Replacing an English order can land the attribution right next to one the
    prompt already carried — "speaking clearly and enthusiastically in English
    in neutral Latin American Spanish" becomes "speaking in neutral Latin
    American Spanish in neutral Latin American Spanish" (re_db8e7b4e91,
    vd_295249)."""
    attr = re.escape(spec.attribution)
    return re.sub(rf"(?i)({attr})(?:\s+{attr})+", r"\1", text)

# Memoize localized prompts so SERIAL re-renders (per-clip retries / crash
# resumes / a later job reusing the same scene prompt) skip a redundant GPT-4o
# call. Keyed by (language, prompt); bounded so a long-lived server can't grow it
# without limit. NOTE: this does NOT dedupe the CONCURRENT fan-out — the M
# same-prompt takes animate via asyncio.gather and all miss the cache before any
# writes it, so they each translate once. That is a bounded, correctness-neutral
# cost (the translation is deterministic) on a cheap call.
_LOCALIZE_CACHE_MAX = 1024
_LOCALIZE_CACHE: dict[tuple[str, str], str] = {}


def _cache_localized(lang: str, prompt: str, localized: str) -> str:
    """Memoize one localized prompt, honoring the cache bound on EVERY write."""
    if len(_LOCALIZE_CACHE) >= _LOCALIZE_CACHE_MAX:
        _LOCALIZE_CACHE.clear()
    _LOCALIZE_CACHE[(lang, prompt)] = localized
    return localized


class LocalizationError(Exception):
    """Raised when a clip's dialogue MUST be translated (an 🇪🇸/🇩🇪-flagged
    character with spoken dialogue) but the translation failed. The runner fails
    the clip loudly (Hugo 2026-06-27 — "refuse loudly over silent partial")
    rather than silently shipping an English clip for a character marked with
    another spoken language."""


def _strip_quotes(text: str) -> str:
    """Drop double-quote chars from a translated phrase before it is spliced back
    BETWEEN the prompt's says-clause quotes — a stray quote would unbalance the
    clause and corrupt a later DIALOGUE_RE re-parse (incl. video_qc)."""
    return text.replace('"', '').replace('“', '').replace('”', '')


def localize_motion_prompt(prompt: str, language: str | None, *,
                           job_id: str | None = None,
                           force: bool = False) -> str:
    """Localize ONE clip's motion prompt for a per-CHARACTER spoken language
    (Hugo 2026-06-26; "es" = neutral Latin American Spanish, "de" = standard
    German since 2026-08-02 — see SPOKEN_LANGUAGES).

    For a registered language AND only when the clip carries spoken dialogue:
    translate the quoted dialogue (each says-clause phrase) IN PLACE — the
    English framing / camera / action stays English — swap any other language's
    accent clause for this one, and HARD-guarantee that accent clause. A
    purely-visual clip (no says-clause) is returned UNCHANGED — we never inject
    a speech / accent / no-music directive into a silent shot. Returns the
    prompt UNCHANGED for None/"en"/any unregistered code.

    Additive to the per-run 🗣 picker: if the prompt is already in the target
    language (it already carries that accent marker), it is returned unchanged
    so user-approved text is never re-translated.

    `force=True` overrides ONLY that marker short-circuit — for a prompt the
    USER just typed (the per-clip "Regenerate this clip" modal, which prefills
    the localized text since 2026-08-03). The marker then proves nothing about
    the DIALOGUE: Hugo can type a fresh ENGLISH line inside a prompt that still
    says "in standard German", and the short-circuit would ship that line
    verbatim — the clip speaks English, silently. Forcing costs one cheap
    translate call whose no-op case is explicit in every `translate_system`
    ("a line already in the language is returned unchanged"), so re-localizing
    an already-translated prompt does not reword it.

    Fail LOUD: if the clip HAS dialogue but translation fails, raises
    LocalizationError so the runner fails the clip (Hugo 2026-06-27) instead of
    silently shipping English for a flagged character. A clip with no dialogue,
    an already-localized prompt, or language None/"en" returns unchanged (not a
    failure — nothing to translate)."""
    lang = (language or "en").strip().lower()
    spec = SPOKEN_LANGUAGES.get(lang)
    if spec is None or not (prompt or "").strip():
        return prompt
    # Already in the target language (run-level picker localized it upstream,
    # or this exact prompt came back through a redo) — unless the caller says
    # the text is user-typed and the marker can no longer be trusted (`force`),
    # or the prompt states more than one DISTINCT line and so cannot be
    # vouched for as a whole.
    if (spec.marker in prompt.lower() and not force
            and not _states_more_than_one_line(prompt)):
        return prompt
    cached = _LOCALIZE_CACHE.get((lang, prompt))
    if cached is not None:
        return cached

    # Localize ONLY a clip that actually carries spoken dialogue — a silent /
    # visual-only clip is left completely untouched. `dialogue_matches` is the
    # SHARED extractor (says-clause → labeled line → speech verb), so every
    # prompt shape the captions can read is a shape we can translate.
    matches = dialogue_matches(prompt)
    phrases = [m.group(1).strip() for m in matches]
    if not any(phrases):
        # No quoted line to translate — but "no line" is NOT the same as
        # "silent". A prompt that still ORDERS English ("The person speaks
        # fluent American English…", "with a thick Texas accent") makes Kling
        # improvise English speech for a character flagged 🇪🇸/🇩🇪 (Hugo
        # 2026-07-31: two characters shipped English clips this way, silently,
        # because the early return skipped the accent swap too). Swap the
        # language directive even with nothing to translate; a genuinely
        # silent prompt carries no such directive and is still returned
        # untouched.
        if not _has_foreign_speech_directive(prompt, lang):
            return prompt
        # The prompt ORDERS speech. If it also carries a sentence-length quote
        # the extractor could not read, that quote IS the line (Hugo
        # 2026-08-07) — the clip will talk, we cannot translate what it says,
        # and video-QC gets no expected speech, so it renders unchecked with
        # `qc_status="skipped"`. That is exactly how re_db8e7b4e91 scene 1
        # shipped four verbatim-ENGLISH clips. Refuse instead of rendering
        # blind; the directive gate above keeps genuinely silent shots out.
        unparsed = _unparsed_dialogue_line(prompt, spec)
        if unparsed:
            logger.warning("localize_motion_prompt (%s): a quoted line is "
                           "present but unreadable by the extractor (%r); "
                           "failing the clip loudly", job_id, unparsed[:80])
            raise LocalizationError(
                f"the prompt carries a spoken line the extractor cannot read, "
                f"so it can be neither translated to {spec.name_en} nor "
                f"language-checked: “{unparsed[:80]}”")
        return _cache_localized(
            lang, prompt, _sanitized_language_prompt(prompt, lang, job_id))

    translated = _translate_each_line_once(phrases, lang, job_id)
    if translated is None:
        logger.warning("localize_motion_prompt (%s): dialogue translation "
                       "failed; failing the clip loudly", job_id)
        raise LocalizationError(
            f"could not translate the dialogue to {spec.name_en}")

    # Replace each captured dialogue span in REVERSE so earlier spans keep their
    # offsets while later ones are substituted (quotes stripped from each).
    localized = prompt
    for m, line in reversed(list(zip(matches, translated))):
        line = _strip_quotes((line or "").strip())
        if not line:
            continue
        s, e = m.span(1)
        localized = localized[:s] + line + localized[e:]

    # Strip EVERY foreign language order (standalone clause, inline American
    # accent, another language's attribution, and an explicit "speaking
    # English…" order sitting right next to the line we just translated) and
    # enforce the target one.
    localized = _sanitized_language_prompt(localized, lang, job_id)
    leaked = _surviving_source_line(localized, phrases, translated)
    if leaked:
        logger.warning("localize_motion_prompt (%s): the English source line "
                       "survived localization (%r); failing the clip loudly",
                       job_id, leaked[:80])
        raise LocalizationError(
            f"the prompt still contains the untranslated line “{leaked[:80]}” "
            f"after localization to {spec.name_en} — the clip would speak "
            f"English. Reword or remove that copy of the line and retry.")
    return _cache_localized(lang, prompt, localized)


def _states_more_than_one_line(prompt: str) -> bool:
    """True when the prompt carries two or more DIFFERENT spoken lines.

    The "already in the target language" short-circuit reads one marker phrase
    and concludes the WHOLE prompt is localized. That inference is only sound
    while a prompt says one thing. Measured on vd_2670d7 (Susanne, 🇩🇪,
    re_8e87b525b2 scene 1) it was not: the 👥 speaker-fix agent produced a
    MIXED prompt — an English line in the ACTION block and the German
    translation in the AUDIO block — so the marker was present, the
    short-circuit returned it untouched (`localized_movement_prompt` empty),
    and the clip spoke the English line. Same false inference the 2026-08-03
    retry fix found for user-typed prompts and answered with `force`; this is
    the machine-written half of it, where no caller knows to pass `force`.

    Falling through costs one cheap translate call whose no-op case is
    explicit in every `translate_system` ("a line ALREADY in this language is
    returned EXACTLY as given"), and the copy that IS already translated comes
    back unchanged."""
    lines = {phrase_key(m.group(1)) for m in dialogue_matches(prompt)}
    lines.discard("")
    return len(lines) > 1


def _translate_each_line_once(phrases: list[str], language: str,
                              job_id: str | None) -> list[str] | None:
    """`translate_dialogue`, but a line repeated in the prompt is translated
    ONCE and the single result reused for every occurrence.

    Two reasons, both about the same prompt shape — the Director stating one
    line in its ACTION block and again in its AUDIO block. Sending it twice
    bills a longer call, and (worse) GPT-4o can return two slightly different
    translations of the same sentence, so the finished prompt would order the
    model to say two different things."""
    order: dict[str, int] = {}
    unique: list[str] = []
    slot: list[int] = []
    for phrase in phrases:
        key = phrase_key(phrase)
        if key not in order:
            order[key] = len(unique)
            unique.append(phrase)
        slot.append(order[key])
    out = translate_dialogue(unique, language=language, re_id=job_id)
    if out is None:
        return None
    return [out[i] for i in slot]


# A line short enough that finding it again proves nothing — a two-word phrase
# ("Save this") can legitimately survive inside a translation or a prop label.
_MIN_LEAK_WORDS = 4


def _surviving_source_line(localized: str, phrases: list[str],
                           translated: list[str]) -> str | None:
    """The ENGLISH source line still standing in the localized prompt, or None.

    The last line of defence, and the one that does not depend on knowing the
    prompt's shape (Hugo 2026-08-08). Every earlier layer recognises SHAPES —
    which regex reads the line, which regex strips an English order — and the
    Director writes free-form English, so each new shape has silently shipped
    an English clip: 2026-06-30, 2026-07-31, 2026-08-02, 2026-08-06, and again
    on 2026-08-08 when it stated the line twice and only one copy was
    rewritten. This check asks the question those all failed to ask: after all
    the rewriting, is the English sentence still in the prompt? If it is, the
    model can still read it, and a flagged character will speak it.

    Everything we WROTE is subtracted from the text before the search, so the
    question asked is strictly "is the English line somewhere we did NOT
    rewrite?". Without that subtraction the check would fire on a translation
    that happens to contain its own source — which is the documented no-op for
    a line already in the target language (every `translate_system` orders
    "return it EXACTLY as given"), exactly what a run-level 🗣 prompt hands
    us."""
    haystack = phrase_key(localized)
    for target in translated:
        key = phrase_key(target or "")
        if key:
            haystack = haystack.replace(key, " ")
    for source in phrases:
        line = (source or "").strip()
        if len(line.split()) < _MIN_LEAK_WORDS:
            continue
        key = phrase_key(line)
        if key and key in haystack:
            return line
    return None


def _sanitized_language_prompt(text: str, language: str,
                               job_id: str | None) -> str:
    """`_force_language_speech` + a hard assertion that it actually worked.

    The sanitizer matches SHAPES and the Director writes free-form English, so
    it can only strip orders it has already met. Five unmet shapes leaked on
    2026-08-06 (`Dialogue exact:`, `Voice: Male, American accent`, `Language:
    English.`, `Accent: American.`, "speaking clearly and enthusiastically in
    English") and every one of them failed SILENTLY. Verifying the output
    turns the next unknown shape into a loud, pre-submit failure — Hugo's
    standing "refuse loudly over silent partial" rule, at zero render cost."""
    spec = SPOKEN_LANGUAGES[language]
    out = _force_language_speech(text, language)
    residual = _residual_english_order(out, spec)
    if residual:
        logger.warning("localize_motion_prompt (%s): an English speech order "
                       "survived sanitization (%r); failing the clip loudly",
                       job_id, residual[:80])
        raise LocalizationError(
            f"the prompt still orders English after localization to "
            f"{spec.name_en} — “{residual[:80]}”. Reword that phrase in the "
            f"clip's prompt and retry.")
    return out


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
