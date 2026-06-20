"""
AI Director — Claude/Opus agent writes tailored per-variant prompts.

Hugo's ask: "Let an LLM agent like Opus understand my intent from the user
prompt, then write Opus's own prompts for each generation. Like Higgsfield
MCP with Claude." This is the heavy "full use" version: vision + forced
tool-use + per-(character, scene, variant) tailoring.

Architecture: ONE Claude Opus call per entry point.
  - All reference images attached to the single message.
  - System prompt instructs the agent on swap/movement pipeline mechanics.
  - `tool_choice` forces the agent into a structured-output tool that
    returns the complete plan as JSON.
  - We parse → Pydantic-validate → return.

Failure mode: ANY error (no API key, SDK missing, API timeout, parse
failure, validation failure, tool not called) → return None. Callers
(`runner.py`, `runner_media.py`) fall back to `prompt_enrich` if also
enabled, else use the raw user prompt or `GENERATION_PROMPT`. Image gen
NEVER blocks on Director.

Logging: `call_log.record(phase="director_swap"|"director_movement", ...)`
captures latency, cost (`settings.claude_opus_price_usd`), ok flag, and
error string. Inspect via `state/calls.jsonl`.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from character_swap.clients import ProviderNotConfigured, anthropic_client

logger = logging.getLogger(__name__)


# --- Pydantic schemas ------------------------------------------------------

class VariantPlan(BaseModel):
    variant_index: int = Field(ge=0)
    prompt: str


class ScenePlanForChar(BaseModel):
    scene_id: str
    variants: list[VariantPlan] = Field(default_factory=list)


class CharacterPlan(BaseModel):
    char_id: str
    name: str
    scenes: list[ScenePlanForChar] = Field(default_factory=list)


def prompt_fingerprint() -> str:
    """Version key stamped on every cached Director plan. A hash of THIS
    module's source: any prompt-logic change (system prompts, light rules,
    clause builders) invalidates previously cached plans, so a regen after a
    prompt upgrade re-runs the Director instead of silently reusing prompts
    written by an older template generation. Slightly over-eager (comment
    edits also invalidate) — by design: one extra ~$0.10 Director call beats
    a stale plan resurfacing an already-fixed drift mode."""
    global _PROMPT_FINGERPRINT
    if _PROMPT_FINGERPRINT is None:
        import hashlib
        _PROMPT_FINGERPRINT = hashlib.sha256(
            Path(__file__).read_bytes()).hexdigest()[:16]
    return _PROMPT_FINGERPRINT


_PROMPT_FINGERPRINT: str | None = None


class SwapDirectorPlan(BaseModel):
    intent: str
    notes: str | None = None
    characters: list[CharacterPlan] = Field(default_factory=list)
    # Set from prompt_fingerprint() at plan-build time. None on plans cached
    # before 2026-06-12 — readers treat those as stale (re-run / fall back).
    prompt_version: str | None = None

    def lookup(self, char_id: str, scene_id: str) -> list[str]:
        """Return the per-variant prompts for (char_id, scene_id) in
        ascending `variant_index` order. Empty list if the pair is absent
        from the plan — caller falls back to enrich / raw."""
        for c in self.characters:
            if c.char_id != char_id:
                continue
            for s in c.scenes:
                if s.scene_id != scene_id:
                    continue
                ordered = sorted(s.variants, key=lambda v: v.variant_index)
                return [v.prompt for v in ordered]
        return []


class ScenePlanMovement(BaseModel):
    scene_id: str
    prompt: str


class MovementDirectorPlan(BaseModel):
    intent: str
    scenes: list[ScenePlanMovement] = Field(default_factory=list)

    def as_dict(self) -> dict[str, str]:
        return {s.scene_id: s.prompt for s in self.scenes}


# --- Tool schemas (force structured output) --------------------------------

SWAP_DIRECTOR_TOOL: dict[str, Any] = {
    "name": "submit_swap_plan",
    "description": (
        "Submit the complete per-variant prompt plan for every "
        "(character × scene) pair in a swap job. Call this EXACTLY ONCE."
    ),
    "input_schema": {
        "type": "object",
        "required": ["intent", "characters"],
        "properties": {
            "intent": {
                "type": "string",
                "description": (
                    "One sentence: swap-only | swap-with-modifications | "
                    "freeform — plus a short note on what the user wants."
                ),
            },
            "notes": {
                "type": "string",
                "description": "Optional: constraints / stylistic decisions you made.",
            },
            "characters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["char_id", "name", "scenes"],
                    "properties": {
                        "char_id": {"type": "string"},
                        "name": {"type": "string"},
                        "scenes": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["scene_id", "variants"],
                                "properties": {
                                    "scene_id": {"type": "string"},
                                    "variants": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "required": ["variant_index", "prompt"],
                                            "properties": {
                                                "variant_index": {
                                                    "type": "integer",
                                                    "minimum": 0,
                                                },
                                                "prompt": {"type": "string"},
                                            },
                                        },
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}

MOVEMENT_DIRECTOR_TOOL: dict[str, Any] = {
    "name": "submit_movement_plan",
    "description": (
        "Submit one cinematic movement prompt per scene. Call EXACTLY ONCE."
    ),
    "input_schema": {
        "type": "object",
        "required": ["intent", "scenes"],
        "properties": {
            "intent": {"type": "string"},
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["scene_id", "prompt"],
                    "properties": {
                        "scene_id": {"type": "string"},
                        "prompt": {"type": "string"},
                    },
                },
            },
        },
    },
}


# --- System prompts --------------------------------------------------------

SWAP_DIRECTOR_SYSTEM = """\
You are the AI Director for a character-swap image pipeline. You see reference
images for multiple CHARACTERS and multiple SCENES, plus the user's intent.
Your job: for every (character × scene) pair, write a tailored, highly
structured prompt for the image-edit model — one per requested variant.

YOU MUST USE THE `submit_swap_plan` TOOL. Do not respond with prose.

THE PIPELINE: For each pair, the image model receives the SCENE image as the
base composition and the CHARACTER image as the new subject. It replaces the
original person in the scene with the character, keeping the scene's framing,
props, lighting, AND background. The image models have NO separate
negative-prompt field, so any "avoid" instructions MUST be written inline in
the prompt text itself.

PROCESS:
1. Read the user's intent. Classify: swap-only | swap-with-modifications.
2. For each CHARACTER, write its visible identity in concrete words: gender
   presentation, apparent age, ethnicity, bone structure, hair color/length/
   style, facial hair, eyes, build, clothing item-by-item, accessories,
   distinctive features (glasses, tattoos, etc.).
3. For each SCENE, note in concrete words: the original subject's pose + hand
   positions + held objects; every visible prop (count, color, material,
   position, physical state); the background (room, surfaces, furniture,
   fixtures, decor); lighting direction & quality; shot type, camera angle,
   crop, head-room.
4. For each (character × scene × variant_index), compose a structured prompt.

EVERY PROMPT MUST ENFORCE THE FOLLOWING — this is what makes the output good:
- IDENTITY — FULL OVERRIDE: completely overwrite the original subject's face,
  head, hair, age, gender, ethnicity, and bone structure with the character's.
  State explicitly that ZERO demographic traits may bleed from the original
  person. Refer to the character by VISIBLE features ("the 60-year-old man
  with silver-grey hair in a navy suit"), NEVER by "the second image".
- SECONDARY PEOPLE: if a scene contains anyone besides the primary subject,
  state explicitly whether to keep or fully erase them. If erased, remove every
  trace — loose arms, hands, hair, clothing folds, and shadows.
- PROPS — PIXEL-EXACT: name the key props and lock their count, color,
  material, position, and physical state. Forbid state changes (e.g. an
  overturned cup stays overturned; a powder pile stays on the table, NOT inside
  a cup). Forbid ingredient/material mutation.
- BRANDING: keep brand labels legible and correctly spelled; forbid warping
  them into gibberish or different fonts.
{background_rule}
- CLOTHING — KEEP THE SCENE'S OUTFIT: the swapped person must wear EXACTLY the
  same outfit as the original person already in the scene. Describe the scene
  subject's garments concretely (clothing items, colors, patterns, fit,
  accessories, gloves) and lock them in place. Do NOT transfer the character's
  own clothing — take only the character's identity (face, hairstyle, skin
  tone), never their wardrobe. (Only deviate if the user EXPLICITLY asked to
  dress the character in their own clothes instead.)
- FRAMING & POSE ANCHOR: match the scene's shot type, camera angle, subject-
  to-camera distance, crop, head-room, eye-line, and hand positions EXACTLY.
  No zoom, no focal-length skew.
- SCENE LIGHT, IN A FEW WORDS: name the scene's ACTUAL light source and
  direction ("lit by the window on the left", "dim warm ceiling lamp") so the
  inserted person is relit by it. Nothing more — no style or grading language.
- STYLE & INTEGRATION — DO NOT WRITE IT: a fixed organic phone-photo style +
  integration + "Avoid:" clause is appended to every prompt automatically in
  code (ordinary unedited phone photo, scene-light relighting, contact
  shadows, no cutout look, the full negative list). NEVER restate or
  paraphrase any of it — every word you spend there is a word stolen from
  the scene-specific anchors above, and the appended clause cannot be
  paraphrased away.
- NO BURNT-IN TEXT: instruct removal of any captions, subtitles, progress bars,
  logos, or watermarks present in the source image.

PRESERVE every verbatim user constraint WORD-FOR-WORD: exact phrases ("exact
same pose"), hex codes (#FFD400), brand names (Pumpkin Oil), exact text
strings.

SWAP-WITH-MODIFICATIONS: state the modification clearly AFTER the swap +
preservation baseline so the model treats it as an intentional change.

VARIANTS: across the N variants for one (character, scene), vary ONLY subtle
lighting, expression, and micro-framing within the scene's constraints — NEVER
the identity, props, or background. Variant 0 is the safest by-the-book pass;
later variants may be marginally more interpretive.

Each prompt: at most ~120 words of pure scene-specific content (identity
override, secondary people, props, background lock, clothing, framing/pose,
scene light). Specific and vivid, never generic — the style boilerplate is
not your job.

REFERENCE IMAGE ORDER (as you will see them):
1. SCENES first, each labeled "SCENE <scene_id>".
2. CHARACTERS next, each labeled "CHARACTER <char_id> (<name>)".

Use the exact char_ids and scene_ids from the user message verbatim in the
tool call. Every char_id × scene_id pair the user lists must appear in
your output."""


MOVEMENT_DIRECTOR_SYSTEM = """\
You are the AI Director for an image-to-video pipeline. Per scene you see:
- the original SCENE image (start frame composition),
- one or more APPROVED variant images (the actual starting frames the video
  model animates from — character already swapped in),
- the user's per-scene movement description.

For each scene, write ONE motion prompt that will animate every approved
variant for that scene. You MUST USE the `submit_movement_plan` tool.
Do not respond with prose.

The start frame already IS the scene — image-to-video models follow the
official subject+movement formula, and text that re-describes the
environment, background, lighting or the person's appearance makes the
model CUT AWAY from the start frame. Describe only WHO does WHAT.

PROCESS:
1. Identify the action the user wants in this scene ("he pours oil", "she
   waves").
2. Look at the approved variant images: what's visible? What naturally moves
   in that frame? What pose is the character in?
3. Compose the prompt: subject anchor (the person + what is in their hands)
   then ONE continuous hero action with strong verbs and a clear endpoint.

CRITICAL RULES:
- Preserve every user-named action / subject WORD-FOR-WORD.
- Add: subject performance cues (expression, gesture, timing) and
  naturalistic motion details. Hands stay anchored to the objects they hold.
- NEVER describe the environment, background, lighting, or the person's
  appearance/clothing — the start frame carries all of that.
- DISTINCT per scene. If scene A is "pouring oil" and scene B is "waving",
  the performance cues must reflect each scene's needs.
- ONE camera description only, and it is always this: end every prompt with
  "Handheld phone footage with subtle micro-shake, naturalistic color." —
  never cinema cameras, dolly moves, shallow depth of field or film jargon
  (the start frames are ordinary phone photos; a cinematic clip from a
  phone-photo start frame reads as fake).
- Write numbers and abbreviations as they are PRONOUNCED ("forty-two",
  "doctor") — TTS reads digits one at a time.
- Keep each prompt under 100 words.

Use the exact scene_ids from the user message verbatim. Every scene the user
lists must appear in your output."""


# Background rule injected into SWAP_DIRECTOR_SYSTEM's {background_rule} slot.
# "scene" keeps the scene's background (pre-2026-06-21 default); "character"
# borrows the background from the character reference image (the new standard).
_SWAP_BG_RULE_SCENE = (
    "- BACKGROUND — DO NOT CHANGE: describe the scene's background concretely "
    "and lock it in a standalone clause — keep room, surfaces, furniture, "
    "fixtures, decor, and lighting EXACTLY. Only deviate if the user EXPLICITLY "
    "asked to change or replace the background.")
_SWAP_BG_RULE_CHARACTER = (
    "- BACKGROUND — USE THE CHARACTER'S OWN ENVIRONMENT: the finished photo "
    "takes place in the CHARACTER reference's own surroundings, NOT the "
    "scene's. In a standalone clause, instruct: replace the scene's room/walls/"
    "floor/furniture/backdrop with the environment visible in the character's "
    "photo (name 1-2 of its actual visible background features if you can see "
    "them), and relight the person and the kept props entirely to that "
    "environment's light. Keep the SCENE's framing, crop, camera angle, "
    "subject scale, pose, hand positions and every held/foreground prop "
    "EXACTLY — only the character photo's environment and light are borrowed, "
    "never its framing, headroom or any empty space above the head. STRICTLY "
    "FORBIDDEN: re-describing or locking the scene's original background — it "
    "is being thrown away.")


def _swap_bg_rule(background_mode: str) -> str:
    return (_SWAP_BG_RULE_CHARACTER if background_mode == "character"
            else _SWAP_BG_RULE_SCENE)


# --- Public entry points ---------------------------------------------------

def direct_swap(
    *,
    user_prompt: str,
    characters: list[tuple[str, str, Path]],   # [(char_id, name, ref_path)]
    scenes: list[tuple[str, Path]],            # [(scene_id, scene_path)]
    images_per_character: int,
    background_mode: str = "scene",
    job_id: str | None = None,
) -> SwapDirectorPlan | None:
    """ONE Claude call. Returns the structured plan or None on any failure.

    On None, callers fall back to `prompt_enrich.enrich_prompt(...)` if that
    is also enabled, else to the raw user prompt / `GENERATION_PROMPT`.
    Image generation never blocks on Director failure.
    """
    if not characters or not scenes:
        return None
    n = max(1, min(10, int(images_per_character)))
    raw_prompt = (user_prompt or "").strip() or "(no user prompt — use sensible defaults)"

    # Build the user-message content blocks. Order matters: scenes first
    # (so the agent grounds in setting), then characters (the subjects).
    content: list[dict] = [
        {"type": "text", "text": f"USER PROMPT (verbatim):\n{raw_prompt}"},
        {"type": "text", "text": "SCENES (use these scene_ids verbatim):"},
    ]
    for scene_id, scene_path in scenes:
        content.append({"type": "text", "text": f"SCENE {scene_id}:"})
        try:
            content.append(anthropic_client._file_to_image_block(scene_path))
        except Exception as e:
            logger.warning("director_swap: failed to encode scene %s: %s", scene_id, e)
            return None

    content.append({"type": "text",
                    "text": "CHARACTERS (use these char_ids verbatim):"})
    for char_id, name, char_path in characters:
        content.append({"type": "text",
                        "text": f"CHARACTER {char_id} '{name}':"})
        try:
            content.append(anthropic_client._file_to_image_block(char_path))
        except Exception as e:
            logger.warning("director_swap: failed to encode character %s: %s", char_id, e)
            return None

    content.append({
        "type": "text",
        "text": (
            f"Plan {n} variant(s) per (character × scene). Submit the complete "
            f"plan via the `submit_swap_plan` tool. Every char_id × scene_id "
            f"pair listed above MUST appear in your output."
        ),
    })

    try:
        response = anthropic_client.messages_with_tools(
            system=SWAP_DIRECTOR_SYSTEM.format(
                background_rule=_swap_bg_rule(background_mode)),
            messages=[{"role": "user", "content": content}],
            tools=[SWAP_DIRECTOR_TOOL],
            tool_choice={"type": "tool", "name": "submit_swap_plan"},
            max_tokens=8192,
            temperature=0.3,
            job_id=job_id,
            phase="director_swap",
        )
    except ProviderNotConfigured:
        # Expected when ANTHROPIC_API_KEY is missing — silent fallback.
        return None
    except Exception as e:
        logger.warning("director_swap: API call failed: %s", e)
        return None

    payload = anthropic_client.extract_tool_call(response, "submit_swap_plan")
    if not payload:
        logger.warning("director_swap: tool `submit_swap_plan` not invoked in response")
        return None
    try:
        plan = SwapDirectorPlan.model_validate(payload)
    except ValidationError as e:
        logger.warning("director_swap: validation failed: %s", e)
        return None
    # Style/integration/negatives are appended HERE, in code — never
    # delegated to the agent (backlog #33: ~250 words of boilerplate were
    # demanded inside every variant prompt, crowding out the scene-specific
    # anchors; an appended clause also cannot be paraphrased away).
    for c in plan.characters:
        for sc_plan in c.scenes:
            for v in sc_plan.variants:
                v.prompt = (v.prompt.strip() + ORGANIC_STYLE_CLAUSE
                            + SWAP_AVOID_CLAUSE)
    plan.prompt_version = prompt_fingerprint()
    return plan


def direct_movement(
    *,
    scenes: list[tuple[str, Path, list[Path], str]],
    # (scene_id, scene_img, approved_variant_imgs, raw_movement_prompt)
    job_id: str | None = None,
) -> MovementDirectorPlan | None:
    """ONE Claude call. Returns per-scene movement prompts. None on failure."""
    if not scenes:
        return None

    # Backlog #28 (2026-06-12): the payload was unbounded — the scene ref
    # plus EVERY approved variant per scene; 64% of director_movement calls
    # died with RequestTooLargeError on multi-character jobs. The prompt is
    # per-SCENE, so ONE approved variant per scene (the actual start frame)
    # carries all the signal; a global image budget keeps big runs inside
    # the API limits, shedding variant images before scene refs.
    _MAX_IMAGES = 60
    n_images = 0
    n_dropped = 0
    content: list[dict] = [
        {"type": "text", "text": "USER PROMPT PER SCENE:"},
    ]
    for scene_id, scene_path, approved_paths, raw in scenes:
        text = f"SCENE {scene_id} — user wrote: '{(raw or '').strip()}'"
        content.append({"type": "text", "text": text})
        # The scene reference (starting composition).
        try:
            content.append(anthropic_client._file_to_image_block(scene_path))
            n_images += 1
        except Exception as e:
            logger.warning("director_movement: failed to encode scene %s: %s",
                           scene_id, e)
            return None
        # ONE approved variant — what the video model will actually start
        # from. The rest are near-duplicates for a per-scene prompt.
        kept = list(approved_paths or [])[:1]
        n_dropped += max(0, len(approved_paths or []) - len(kept))
        if n_images + len(scenes) - 1 >= _MAX_IMAGES:
            n_dropped += len(kept)
            kept = []
        for ap in kept:
            content.append({
                "type": "text",
                "text": f"approved variant for scene {scene_id} (this is the actual start frame):",
            })
            try:
                content.append(anthropic_client._file_to_image_block(ap))
                n_images += 1
            except Exception as e:
                logger.warning(
                    "director_movement: failed to encode approved variant for %s: %s",
                    scene_id, e,
                )
                return None
    if n_dropped:
        logger.info("director_movement: payload capped — %d images sent, "
                    "%d approved-variant images dropped", n_images, n_dropped)

    content.append({
        "type": "text",
        "text": (
            "Submit via `submit_movement_plan` with one prompt per scene_id. "
            "Every scene_id listed above MUST appear in your output."
        ),
    })

    try:
        response = anthropic_client.messages_with_tools(
            system=MOVEMENT_DIRECTOR_SYSTEM,
            messages=[{"role": "user", "content": content}],
            tools=[MOVEMENT_DIRECTOR_TOOL],
            tool_choice={"type": "tool", "name": "submit_movement_plan"},
            max_tokens=4096,
            temperature=0.3,
            job_id=job_id,
            phase="director_movement",
        )
    except ProviderNotConfigured:
        return None
    except Exception as e:
        logger.warning("director_movement: API call failed: %s", e)
        return None

    payload = anthropic_client.extract_tool_call(response, "submit_movement_plan")
    if not payload:
        logger.warning("director_movement: tool `submit_movement_plan` not invoked")
        return None
    try:
        return MovementDirectorPlan.model_validate(payload)
    except ValidationError as e:
        logger.warning("director_movement: validation failed: %s", e)
        return None


# --- Reengineer swap Director ----------------------------------------------
#
# Opt-in (checkbox at Reengineer upload): ONE Claude call that LOOKS at every
# detected scene frame and writes a tailored, COMPACT swap prompt per SCENE
# (identity comes from the reference image at generation time, so prompts
# don't need to vary per character — the per-scene prompt is replicated into
# the standard SwapDirectorPlan so the existing `_kick_char` plumbing picks
# it up unchanged).
#
# Two hard rules learned from this project's data:
#   1. COMPACT (≤ ~120 words) — the 2026-06-10 bake-off showed GPT image
#      models score WORSE with long constraint-block prose.
#   2. Concrete anchors beat generic rules — name each key prop with its
#      position and approximate size in frame, and describe the camera
#      distance, so framing/prop drift (the observed failure modes) has
#      specific targets to hold on to.
#
# Prompts are written in STANDARD orientation (Image 1 = scene, Image 2 =
# identity reference, Image 3 = optional new environment) — the gpt2-id-swap
# dispatch mechanically flips Image 1<->2 for its reversed reference order.

REENGINEER_SWAP_DIRECTOR_SYSTEM = """\
You are a swap-prompt director for a character-swap image pipeline. You will
be shown N scene frames extracted from a reference video. For EACH scene,
write ONE compact image-editing prompt (max ~120 words) that instructs an
image model to replace the person in the scene with a different person whose
identity comes from a separate reference image.

Prompt format rules (follow exactly):
- Refer to the scene frame as "Image 1" and the identity reference as
  "Image 2". {bg_role}
- ALWAYS include, verbatim: "The face must be a clear, recognizable likeness
  of the person in Image 2 — unmistakably the same individual; this identity
  match is the single most important requirement."
- ALWAYS include this outfit directive: {outfit_directive}
- FRAMING ANCHORS — the most important part of YOUR job: name the 2-4 key
  objects/props you actually SEE, each with its position in frame and
  approximate size (e.g. "the blender with kiwi pieces sits bottom-center,
  filling about a quarter of the frame"). State the camera distance/crop you
  see (e.g. "waist-up shot, person fills the left two-thirds of the frame")
  and add: "identical camera distance and crop — do not zoom out or
  recompose; every object keeps this exact size and position."
- PROP PRECISION — when you name a prop, pin its COUNT, physical state and
  container in a few words ("three kiwi halves on the white plate", "a clear
  glass mug, half full") — a vague "kiwis" became a staged 6-slice flower
  and a glass mug became a tumbler. Include foreground furniture/surfaces
  with their frame coverage ("the wooden desk fills the bottom of frame") —
  dropping the surface the props rest on breaks set continuity.
- PERFORMANCE ANCHORS — GAZE IS FIXED BY POLICY (Hugo 2026-06-13): ALWAYS
  include, verbatim: "They look directly into the camera with a natural,
  composed expression, even if the original person was not." Never anchor
  the original gaze direction and never write any other gaze. Still anchor
  the exact hand state/gesture you see ("right hand thumbs-up beside the
  blender", "bare hands — no gloves"): unanchored, hands drift to generic
  open palms and gloves/jewelry leak in from the character reference.
- OUTFIT ANCHOR — name the scene outfit's visible pieces AND colors in a few
  words ("white open-collar shirt under a grey pinstripe suit"): unanchored,
  single scenes invent role-typical wardrobe (a doctor got blue scrubs in
  ONE scene while every other scene wore the white shirt) and the final
  flickers at that cut.
- VERTICAL FRAMING / HEADROOM — measure it from the scene frame and lock it,
  because the model otherwise pushes the subject down and fills the top with
  empty space. State where the top of the head sits in Image 1 ("nearly
  touches the top edge, almost no headroom" / "a hand's-width of space above
  the head") and where the body is cropped at the bottom, then add: "keep
  the head at this same height in the frame — add no empty space, sky or
  scenery above it."
- {light_rule}
- Do NOT write any photographic-style/grading language (no "cinematic",
  "professional", "high quality", camera/lens jargon) — a fixed organic
  phone-photo style paragraph is appended to your prompt automatically.
- Imperative, concrete, NO long lists of generic constraints, no headers.

MULTI-PERSON DETECTION (Hugo 2026-06-14) — REQUIRED for every scene:
- Set "multi_person": true ONLY if MORE THAN ONE person is clearly visible in
  the frame (so it is ambiguous which one should be swapped). A single subject
  → false (and omit "people").
- When multi_person=true, fill "people" with ONE entry per visible person:
  "position" is one of left / right / center / background; "description" is
  2-3 words (age range, gender, one visible trait — e.g. "young woman red
  top", "older man left"). METADATA ONLY — it does NOT go in the swap prompt; the
  app pauses and asks the user which person to swap. Still write the normal
  per-scene "prompt" as usual (it will be refined with the user's choice).

Return via the tool with one entry per scene, scene_ids verbatim.
"""

# Hugo's organic anti-"produced" look (same intent as the static templates'
# style paragraph): appended VERBATIM in code to every Director-written
# prompt — never delegated to the agent, so it can't be paraphrased away.
ORGANIC_STYLE_CLAUSE = (
    " Style: a completely ordinary, unedited iPhone photo — plain, slightly "
    "dull phone-camera colors, neutral white balance, flat everyday ambient "
    "light that is slightly uneven and unflattering, slightly uneven "
    "exposure, mild softness, subtle sensor noise, natural non-polished skin "
    "with visible pores. The person is lit by the scene's own ordinary light "
    "and grounded with a real contact shadow where the body and held objects "
    "meet surfaces — not brightened, not relit flatteringly, not pasted on. "
    "Not staged, not professional: no studio lighting, no soft flattering "
    "key light, no cinematic grading, no glossy highlights, no retouching, "
    "no portrait-mode blur."
)

# Reengineer gaze policy (Hugo 2026-06-13): EVERY image generated in the
# Reengineer flow has the person looking straight into the camera — same
# sentence the static templates use. The Director systems are instructed to
# include it verbatim; ensure_camera_gaze() is the code-level guarantee
# (never delegated to the agent alone).
CAMERA_GAZE_SENTENCE = (
    "They look directly into the camera with a natural, composed "
    "expression, even if the original person was not."
)


def ensure_camera_gaze(prompt: str) -> str:
    """Append CAMERA_GAZE_SENTENCE unless the prompt already demands camera
    gaze (matched on the affirmative phrase, so an old away-gaze anchor like
    'NOT at camera' does not count as compliance)."""
    p = (prompt or "").strip()
    if "directly into the camera" in p:
        return p
    return (p + " " + CAMERA_GAZE_SENTENCE) if p else CAMERA_GAZE_SENTENCE


# The full inline negative list for direct_swap plans (the image engines
# have no separate negative-prompt field). Appended in code after
# ORGANIC_STYLE_CLAUSE — backlog #33.
SWAP_AVOID_CLAUSE = (
    " Avoid: a pasted-in / cutout / collage look, the subject floating with "
    "no contact shadow, lighting that doesn't match the scene, the subject "
    "sharper or cleaner than the background, professional / studio / glamour "
    "lighting, cinematic contrast, dramatic shadows, HDR, color grading, "
    "warm or golden tint, oversaturation, glossy highlights, enhanced "
    "clarity, retouching, polished skin, portrait-mode blur, identity bleed "
    "from the original subject, extra or distorted fingers, warped facial "
    "features, changed/restyled background, altered prop counts, props "
    "changing state, misspelled labels, captions, subtitles, watermarks, "
    "cartoon/illustration look."
)

REENGINEER_SWAP_TOOL: dict[str, Any] = {
    "name": "submit_reengineer_swap_prompts",
    "description": "One compact tailored swap prompt per scene.",
    "input_schema": {
        "type": "object",
        "required": ["intent", "scenes"],
        "properties": {
            "intent": {"type": "string",
                       "description": "One line on the overall video."},
            "scenes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["scene_id", "prompt", "multi_person"],
                    "properties": {
                        "scene_id": {"type": "string"},
                        "prompt": {"type": "string"},
                        "multi_person": {
                            "type": "boolean",
                            "description": "True if MORE THAN ONE person is "
                            "visible in the scene (so it is ambiguous which one "
                            "to swap). False for a single subject."},
                        "people": {
                            "type": "array",
                            "description": "Only when multi_person=true: one "
                            "entry per visible person.",
                            "items": {
                                "type": "object",
                                "required": ["position", "description"],
                                "properties": {
                                    "position": {"type": "string",
                                                 "description": "left | right | "
                                                 "center | background"},
                                    "description": {"type": "string",
                                                    "description": "2-3 words: "
                                                    "age, gender, one trait "
                                                    "(e.g. 'young woman red top')"},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}

_REENGINEER_OUTFIT_DIRECTIVES = {
    "scene": ('"Take only the face, hairstyle, hair color and skin tone from '
              'Image 2. The person keeps the original pose, hand placement and '
              'interaction with objects, and wears exactly the outfit from '
              'Image 1."'),
    "character": ('"Take the face, hairstyle, hair color, skin tone AND '
                  'clothing from Image 2 — the person wears their own outfit '
                  'from Image 2, fitted naturally to the original pose."'),
    "custom": ('"Take only the face, hairstyle, hair color and skin tone from '
               'Image 2. The person keeps the original pose and interaction '
               'with objects, and wears: {outfit}."'),
}


def plan_from_scene_prompts(
    intent: str,
    scene_prompts: dict[str, str],
    characters: list[tuple[str, str]],          # [(char_id, name)]
) -> SwapDirectorPlan:
    """Expand per-SCENE prompts into the per-(char × scene × variant) shape
    `_kick_char` consumes — the same prompt replicated for every character
    (identity varies via the reference image, not the text)."""
    return SwapDirectorPlan(
        intent=intent,
        prompt_version=prompt_fingerprint(),
        characters=[
            CharacterPlan(
                char_id=cid, name=name,
                scenes=[
                    ScenePlanForChar(
                        scene_id=sid,
                        variants=[VariantPlan(variant_index=0, prompt=prompt)],
                    )
                    for sid, prompt in scene_prompts.items()
                ],
            )
            for cid, name in characters
        ],
    )


def direct_reengineer_swap(
    *,
    scenes: list[tuple[str, Path]],             # [(scene_id, frame_path)]
    outfit_mode: str = "scene",
    outfit_text: str | None = None,
    background_path: Path | None = None,
    background_mode: str = "scene",
    job_id: str | None = None,
) -> tuple[str, dict[str, str], dict[str, dict]] | None:
    """ONE Claude call with every scene frame → (intent, {scene_id: prompt},
    {scene_id: {multi_person, people}}).
    Returns None on ANY failure — callers fall back to the static template;
    image generation never blocks on the Director.

    `background_path`: the replacement environment (Image 3 at generation
    time). The Director SEES it and anchors the prompt's environment + light
    to IT — and is forbidden from naming the original scene's background.
    (Observed 2026-06-12: without seeing it, the Director anchored "red barn
    visible upper background" from the scene frame, directly contradicting
    the replace-surroundings directive → wrong background in the output.)"""
    if not scenes:
        return None
    outfit = _REENGINEER_OUTFIT_DIRECTIVES.get(outfit_mode or "scene")
    if outfit is None:
        return None
    if outfit_mode == "custom":
        if not (outfit_text or "").strip():
            return None
        outfit = outfit.format(outfit=outfit_text.strip())
    if background_path is not None:
        bg_role = (
            "Image 3 is the NEW ENVIRONMENT the finished photo takes place "
            "in (the REPLACEMENT BACKGROUND image you are shown) — say the "
            "surroundings are replaced with Image 3's location, name 1-2 of "
            "Image 3's actual visible features so the model targets THAT "
            "environment, and say the person plus kept objects are relit "
            "entirely with Image 3's light. If a named feature is a "
            "distinctive SYMBOL (a flag, a logo, a lettered sign), anchor "
            "its key identifying parts in a few words — e.g. 'US flag: red/"
            "white stripes AND the blue star canton top-left' — otherwise "
            "the model renders a near-miss that flickers across scenes. "
            "CRITICAL framing rule (fold it "
            "into your framing-anchor sentence, do NOT add a separate "
            "clause): only Image 3's LOOK is borrowed — the framing, crop, "
            "subject scale and subject position stay exactly as Image 1, and "
            "Image 3 is cropped behind the subject to fit Image 1's framing, "
            "so the subject must NOT shrink or move and Image 3's headroom / "
            "horizon / open sky must NOT appear above the head. STRICTLY "
            "FORBIDDEN: naming or describing ANYTHING visible only in the "
            "scene frame's original background (buildings, walls, sky, "
            "signage, flags, location) — the original environment is being "
            "thrown away, so your framing anchors must cover ONLY the "
            "person, the held/foreground props and the camera distance/crop."
        )
        light_rule = (
            "Describe Image 3's light the way it actually looks in an "
            "ORDINARY phone snapshot — flat, uneven, everyday/ambient, "
            "slightly harsh or dim, mixed/imperfect color temperature. NEVER "
            "use flattering photographic words: no 'soft', 'diffused', "
            "'even', 'flattering', 'golden', 'cinematic' or 'studio' light. "
            "The inserted person and kept props are relit by exactly that "
            "ordinary light, never by the original scene's light.")
    elif background_mode == "character":
        bg_role = (
            "There is no Image 3. The finished photo takes place in the "
            "PERSON'S OWN environment — the surroundings from the identity "
            "reference (Image 2), NOT the scene's. In your framing-anchor "
            "sentence say the surroundings are replaced with Image 2's own "
            "environment and the person plus kept objects are relit entirely "
            "with Image 2's light. You do NOT see Image 2, so do not name its "
            "specific features — refer to it generically as 'Image 2's "
            "surroundings'. Only Image 2's environment and look are borrowed: "
            "the framing, crop, subject scale and subject position stay "
            "exactly as Image 1, and Image 2's environment is cropped behind "
            "the subject to fit Image 1's framing, so the subject must NOT "
            "shrink or move and no headroom, horizon or open sky may appear "
            "above the head. STRICTLY FORBIDDEN: naming or describing ANYTHING "
            "visible only in the scene frame's original background (the "
            "original room, walls, sky, signage, flags, location) — the "
            "original environment is being thrown away, so your framing "
            "anchors must cover ONLY the person, the held/foreground props and "
            "the camera distance/crop.")
        light_rule = (
            "Describe the light as it would look in the PERSON'S OWN "
            "environment (Image 2's surroundings) in an ORDINARY phone "
            "snapshot — flat, uneven, everyday/ambient, slightly harsh or dim, "
            "mixed/imperfect color temperature. NEVER use flattering "
            "photographic words: no 'soft', 'diffused', 'even', 'flattering', "
            "'golden', 'cinematic' or 'studio' light. The inserted person and "
            "kept props are relit by exactly that ordinary light, never by the "
            "original scene's light.")
    else:
        bg_role = ("There is no Image 3 — the scene's own background is "
                   "preserved exactly.")
        light_rule = (
            "Describe the scene's actual light the way it looks in an "
            "ORDINARY phone snapshot — flat, uneven, everyday/ambient, "
            "slightly harsh or dim, mixed/imperfect color temperature. NEVER "
            "use flattering photographic words: no 'soft', 'diffused', "
            "'even', 'flattering', 'golden', 'cinematic' or 'studio' light. "
            "The inserted person is lit by exactly that ordinary light.")
    system = REENGINEER_SWAP_DIRECTOR_SYSTEM.format(
        bg_role=bg_role, outfit_directive=outfit, light_rule=light_rule)

    content: list[dict] = []
    if background_path is not None:
        content.append({"type": "text",
                        "text": "REPLACEMENT BACKGROUND (this is Image 3 at "
                                "generation time — the environment every "
                                "finished photo must take place in):"})
        try:
            content.append(anthropic_client._file_to_image_block(background_path))
        except Exception as e:
            logger.warning("director_reengineer: failed to encode background: %s", e)
            return None
    content.append(
        {"type": "text",
         "text": "SCENES (use these scene_ids verbatim in your answer):"})
    for scene_id, frame in scenes:
        content.append({"type": "text", "text": f"SCENE {scene_id}:"})
        try:
            content.append(anthropic_client._file_to_image_block(frame))
        except Exception as e:
            logger.warning("director_reengineer: failed to encode %s: %s",
                           scene_id, e)
            return None

    try:
        resp = anthropic_client.messages_with_tools(
            system=system,
            messages=[{"role": "user", "content": content}],
            tools=[REENGINEER_SWAP_TOOL],
            tool_choice={"type": "tool",
                         "name": "submit_reengineer_swap_prompts"},
            max_tokens=8192,
            job_id=job_id,
            phase="director_swap",
        )
        data = anthropic_client.extract_tool_call(
            resp, "submit_reengineer_swap_prompts")
        if not data or not data.get("scenes"):
            return None
        prompts = {str(s["scene_id"]):
                   ensure_camera_gaze(str(s["prompt"])) + ORGANIC_STYLE_CLAUSE
                   for s in data["scenes"]
                   if s.get("scene_id") and (s.get("prompt") or "").strip()}
        if not prompts:
            return None
        # Per-scene multi-person metadata (Hugo 2026-06-14): flags scenes with
        # >1 visible person so the run can pause and ask which one to swap.
        metadata: dict[str, dict] = {}
        for s in data["scenes"]:
            sid = str(s.get("scene_id") or "")
            if not sid or sid not in prompts:
                continue
            if s.get("multi_person"):
                people = [{"position": str(p.get("position") or ""),
                           "description": str(p.get("description") or "")}
                          for p in (s.get("people") or [])
                          if isinstance(p, dict)]
                if len(people) >= 2:        # only a REAL ambiguity counts
                    metadata[sid] = {"multi_person": True, "people": people}
        return (str(data.get("intent") or ""), prompts, metadata)
    except ProviderNotConfigured:
        return None
    except Exception:
        logger.exception("director_reengineer failed; falling back")
        return None


# --- Scene-level prompt REWRITE (Hugo 2026-06-13) ---------------------------
#
# "Ändra scenens bild för alla karaktärer": the user describes WHAT should
# change in plain language (Swedish or English), ONE Claude call looks at the
# scene frame + the current swap prompt and rewrites the prompt with only
# that change applied. The caller shows the result for review before any
# image is regenerated — this function never spends image-generation money.

SCENE_REWRITE_DIRECTOR_SYSTEM = """\
You are revising ONE existing swap prompt for a character-swap image
pipeline. You see the scene frame (it is "Image 1" at generation time; the
identity reference is "Image 2" and is not shown to you). You get the
CURRENT PROMPT and the user's CHANGE REQUEST (may be Swedish or English).
{bg_rule}

Rules:
- Apply the requested change fully and concretely — anchor every new or
  changed element exactly like the existing anchors do: name it with its
  position in frame and approximate size (e.g. "a clear glass of water,
  half full, bottom-center, about a tenth of the frame").
- Keep EVERYTHING the change does not touch word-for-word: the identity
  sentence, outfit directive, framing/camera anchors, prop anchors, hand
  anchors, headroom lock and light description. Remove or rewrite ONLY
  anchors the change invalidates.
- GAZE IS FIXED BY POLICY: the rewritten prompt must always include,
  verbatim: "They look directly into the camera with a natural, composed
  expression, even if the original person was not." Keep it if present, add
  it if missing, and never write any other gaze direction — even if the
  change request or the current prompt says otherwise.
- ONE compact prompt, max ~120 words, imperative and concrete; no headers,
  no lists.
- Do NOT write photographic-style/grading language (no "cinematic",
  "professional", "high quality", camera/lens jargon) — a fixed organic
  phone-photo style paragraph is appended to your prompt automatically. If
  the current prompt still contains style/quality boilerplate, drop it.
- Write the prompt in English even if the change request is Swedish.

Return the FULL rewritten prompt via the tool.
"""

# {bg_rule} variants for the rewrite system. The with-background rule is the
# distilled REENGINEER bg machinery (the "red barn" lesson: a Director blind
# to Image 3 anchors the ORIGINAL background that is being thrown away).
_REWRITE_BG_RULE = """
A REPLACEMENT BACKGROUND image is also shown (it is "Image 3" at generation
time): the finished photo's surroundings are REPLACED by Image 3's location,
and the person plus kept props are relit entirely with Image 3's ordinary
flat phone-snapshot light. Keep the current prompt's Image 3 directives
intact wherever the change doesn't touch them. STRICTLY FORBIDDEN: naming or
describing ANYTHING visible only in the scene frame's original background
(buildings, walls, sky, signage, location) — the original environment is
thrown away, so anchors must cover ONLY the person, the held/foreground
props and the camera distance/crop; only Image 1's framing, crop and subject
scale are kept, never Image 3's headroom or horizon."""

_REWRITE_NO_BG_RULE = """
There is no Image 3 — the scene's own background is preserved exactly. If
the change touches the light description, describe ordinary phone-snapshot
light (flat, uneven, everyday) — never 'soft', 'diffused', 'flattering',
'golden', 'cinematic' or 'studio'."""

# Character-background mode (Hugo 2026-06-21): the output's surroundings come
# from the PERSON'S OWN reference photo (Image 2 at generation time), not the
# scene. The rewriter never sees Image 2 (it's per-character), so it must keep
# the existing character-environment directives and never re-anchor the scene's
# original background.
_REWRITE_CHAR_BG_RULE = """
The finished photo takes place in the PERSON'S OWN environment — the
surroundings from the identity reference (Image 2 at generation time), NOT the
scene's original background. Keep the current prompt's character-environment
directives intact wherever the change doesn't touch them. STRICTLY FORBIDDEN:
re-introducing, naming or locking the scene frame's original background
(buildings, walls, sky, signage, location) — it is thrown away, so anchors
cover ONLY the person, the held/foreground props and the camera distance/crop;
only Image 1's framing, crop and subject scale are kept, never Image 2's
headroom or horizon. If the change touches the light description, describe
ordinary phone-snapshot light (flat, uneven, everyday) — never 'soft',
'diffused', 'flattering', 'golden', 'cinematic' or 'studio'."""


SCENE_REWRITE_TOOL: dict[str, Any] = {
    "name": "submit_rewritten_swap_prompt",
    "description": "The full rewritten swap prompt with the change applied.",
    "input_schema": {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
        },
    },
}


def strip_style_clauses(prompt: str) -> str:
    """Remove the code-appended style/negative clauses from a stored variant
    prompt — the Director must see (and rewrite) only the scene-specific
    part; the clauses are re-appended verbatim in code afterwards."""
    out = prompt or ""
    for clause in (SWAP_AVOID_CLAUSE, ORGANIC_STYLE_CLAUSE):
        out = out.replace(clause, "").replace(clause.strip(), "")
    return " ".join(out.split()).strip()


def direct_scene_prompt_rewrite(
    *,
    scene_id: str,
    frame_path: Path,
    current_prompt: str,
    change_request: str,
    background_path: Path | None = None,
    background_mode: str = "scene",
    job_id: str | None = None,
) -> str | None:
    """ONE Claude call: scene frame + current swap prompt + the user's
    plain-language change → the rewritten prompt (style clause re-appended).
    `background_path`: the run's replacement environment (Image 3 at
    generation time) — the Director must SEE it to keep anchoring the
    environment to IT instead of the discarded original background.
    Returns None on ANY failure — the caller surfaces that to the user, who
    can edit the prompt by hand instead. Never blocks image generation."""
    if not (change_request or "").strip():
        return None
    content: list[dict] = []
    if background_path is not None:
        content.append({"type": "text",
                        "text": "REPLACEMENT BACKGROUND (this is Image 3 at "
                                "generation time — the environment the "
                                "finished photo takes place in):"})
        try:
            content.append(anthropic_client._file_to_image_block(background_path))
        except Exception as e:
            logger.warning("director_rewrite: failed to encode background: %s", e)
            return None
    content.append(
        {"type": "text",
         "text": f"SCENE FRAME (Image 1 at generation time) — scene {scene_id}:"})
    try:
        content.append(anthropic_client._file_to_image_block(frame_path))
    except Exception as e:
        logger.warning("director_rewrite: failed to encode %s: %s",
                       scene_id, e)
        return None
    content.append({
        "type": "text",
        "text": ("CURRENT PROMPT:\n"
                 f"{strip_style_clauses(current_prompt)}\n\n"
                 "CHANGE REQUEST:\n"
                 f"{change_request.strip()}"),
    })
    if background_path is not None:
        bg_rule = _REWRITE_BG_RULE
    elif background_mode == "character":
        bg_rule = _REWRITE_CHAR_BG_RULE
    else:
        bg_rule = _REWRITE_NO_BG_RULE
    system = SCENE_REWRITE_DIRECTOR_SYSTEM.format(bg_rule=bg_rule)
    try:
        resp = anthropic_client.messages_with_tools(
            system=system,
            messages=[{"role": "user", "content": content}],
            tools=[SCENE_REWRITE_TOOL],
            tool_choice={"type": "tool",
                         "name": "submit_rewritten_swap_prompt"},
            max_tokens=2048,
            job_id=job_id,
            phase="director_rewrite",
        )
        data = anthropic_client.extract_tool_call(
            resp, "submit_rewritten_swap_prompt")
        new_prompt = (data or {}).get("prompt", "")
        if not (new_prompt or "").strip():
            return None
        return ensure_camera_gaze(str(new_prompt)) + ORGANIC_STYLE_CLAUSE
    except ProviderNotConfigured:
        return None
    except Exception:
        logger.exception("director_rewrite failed")
        return None


# --- Moderation rescue REWRITE (Hugo 2026-06-13) ----------------------------
#
# When the image engine's safety system blocks a swap even after the
# append-only softeners, ONE Claude call looks at the scene and rewords the
# prompt — same scene, same visual result, neutral phrasing. Hugo verified
# the approach empirically: a manually reworded prompt generated the exact
# scene the original prompt couldn't.

MODERATION_REWRITE_SYSTEM = """\
You are rescuing a BLOCKED image-edit prompt for a character-swap pipeline.
The image engine's safety system rejected the generation — usually a false
positive on harmless UGC/fitness content (a bare-chested creator, a hand
indicating skin, body close-ups). You see the scene frame (it is "Image 1"
at generation time; the identity reference is "Image 2" and is not shown to
you), the CURRENT PROMPT and the engine's REJECTION message.

Rewrite the prompt so the SAME scene — same composition, props, action,
outfit, framing and gaze — is produced, while avoiding wording and emphasis
that trips safety filters:
- Describe bodies and touch neutrally and matter-of-factly: prefer "torso",
  "midsection", "gently holds / points at / indicates" over charged words
  (bare, flesh, fat, pinch, squeeze, grab) when a neutral synonym keeps the
  same visual.
- Open with ONE short wholesome-context clause (e.g. "Everyday fitness-
  education content filmed at home.") — never claim anything visually false.
- Do NOT change what is visible. If the scene shows a bare torso, the
  rewrite must still produce a bare torso — change the WORDS, not the image.
- Keep ALL identity / framing / prop / headroom / outfit / gaze anchors and
  the overall structure intact; roughly the same length as the input.
- Do not add style/grading language.

Return the FULL rewritten prompt via the tool.
"""

MODERATION_REWRITE_TOOL: dict[str, Any] = {
    "name": "submit_safe_prompt",
    "description": "The full rewritten prompt, safety-filter-friendly.",
    "input_schema": {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
        },
    },
}


def direct_moderation_rewrite(
    *,
    scene_path: Path,
    current_prompt: str,
    rejection_reason: str,
    camera_gaze: bool = False,
    job_id: str | None = None,
) -> str | None:
    """ONE Claude call: scene frame + the blocked prompt + the engine's
    rejection → a reworded prompt that aims for the SAME image. Returns None
    on ANY failure (no key, tool not called, API error) — the caller falls
    through to the next rung. Code-appended style clauses are stripped
    before and re-appended after, exactly like the scene-level rewrite."""
    if not (current_prompt or "").strip():
        return None
    had_organic = ORGANIC_STYLE_CLAUSE in current_prompt
    had_avoid = SWAP_AVOID_CLAUSE in current_prompt
    content: list[dict] = [
        {"type": "text",
         "text": "SCENE FRAME (Image 1 at generation time):"},
    ]
    try:
        content.append(anthropic_client._file_to_image_block(scene_path))
    except Exception as e:
        logger.warning("moderation_rewrite: failed to encode scene: %s", e)
        return None
    content.append({
        "type": "text",
        "text": ("CURRENT PROMPT:\n"
                 f"{strip_style_clauses(current_prompt)}\n\n"
                 "ENGINE REJECTION:\n"
                 f"{(rejection_reason or '')[:400]}"),
    })
    try:
        resp = anthropic_client.messages_with_tools(
            system=MODERATION_REWRITE_SYSTEM,
            messages=[{"role": "user", "content": content}],
            tools=[MODERATION_REWRITE_TOOL],
            tool_choice={"type": "tool", "name": "submit_safe_prompt"},
            max_tokens=2048,
            job_id=job_id,
            phase="director_rewrite",
        )
        data = anthropic_client.extract_tool_call(resp, "submit_safe_prompt")
        new_prompt = (data or {}).get("prompt", "")
        if not (new_prompt or "").strip():
            return None
        out = str(new_prompt).strip()
        if camera_gaze:
            out = ensure_camera_gaze(out)
        if had_organic:
            out += ORGANIC_STYLE_CLAUSE
        if had_avoid:
            out += SWAP_AVOID_CLAUSE
        return out
    except ProviderNotConfigured:
        return None
    except Exception:
        logger.exception("moderation_rewrite failed")
        return None


def replace_scene_prompt_in_plan(plan: SwapDirectorPlan, scene_id: str,
                                 prompt: str) -> bool:
    """Overwrite the cached plan's prompt for ONE scene across every
    character (scene prompts are shared — identity varies via the reference
    image, not the text). Returns True if any entry changed."""
    changed = False
    for cp in plan.characters:
        for sp in cp.scenes:
            if sp.scene_id == scene_id:
                for vp in sp.variants:
                    vp.prompt = prompt
                changed = True
    return changed


def rekey_scene_in_plan(plan: SwapDirectorPlan, old_scene_id: str,
                        new_scene_id: str) -> bool:
    """Re-key a scene's plan entry from old_scene_id to new_scene_id across
    every character — used when a scene's REFERENCE image is replaced (the
    per-scene prompt is unchanged, only the scene identity moves). Returns
    True if any entry changed."""
    if old_scene_id == new_scene_id:
        return False
    changed = False
    for cp in plan.characters:
        for sp in cp.scenes:
            if sp.scene_id == old_scene_id:
                sp.scene_id = new_scene_id
                changed = True
    return changed
