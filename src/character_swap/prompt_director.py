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


class SwapDirectorPlan(BaseModel):
    intent: str
    notes: str | None = None
    characters: list[CharacterPlan] = Field(default_factory=list)

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
- BACKGROUND — DO NOT CHANGE (default): describe the scene's background
  concretely and lock it in a standalone clause — keep room, surfaces,
  furniture, fixtures, decor, and lighting EXACTLY. Only deviate if the user
  EXPLICITLY asked to change or replace the background.
- CLOTHING: describe the character's garments precisely and keep them
  consistent across all of that character's scenes (unless the user asked to
  match the scene's original outfit instead).
- FRAMING & POSE ANCHOR: match the scene's shot type, camera angle, subject-
  to-camera distance, crop, head-room, eye-line, and hand positions EXACTLY.
  No zoom, no focal-length skew.
- LIGHTING & INTEGRATION (so the character NEVER looks pasted in — this is the
  difference between a believable swap and an obvious cutout): relight the
  inserted person with the scene's OWN light — match its direction, color
  temperature, softness, and intensity, and discard the lighting baked into the
  character photo. Match the scene's white balance / color grade on their skin
  and clothing. Add contact shadows + ambient occlusion where they touch
  surfaces and a cast shadow consistent with the key light. Blend edges (no
  cutout halo or fringe). Match the scene's depth of field, lens character, and
  grain so the subject isn't crisper than the background. Name the scene's
  actual light sources and their direction when you can see them ("warm window
  light from camera-left", "soft overhead kitchen downlight", "golden-hour
  backlight"). Make the environment ACT ON them: wrap a subtle rim / edge light
  from the background around their silhouette, and bounce nearby surface colors
  onto their skin and clothing (color spill). The lighting must look organic and
  real, and EVERYTHING must look like the same scene — not like the person was
  pasted onto the background.
- NO BURNT-IN TEXT: instruct removal of any captions, subtitles, progress bars,
  logos, or watermarks present in the source image.
- INLINE NEGATIVES: end each prompt with a short "Avoid:" clause listing the
  failure modes to exclude — a pasted-in / cutout / collage look, the subject
  floating with no shadow, lighting or color temperature that doesn't match the
  scene, the subject sharper than the background, an edge with no light wrap, a
  subject untouched by the scene's bounce light or grain, identity bleed from the
  original subject, extra or distorted fingers, warped facial features,
  changed/restyled background, altered prop counts, props changing state,
  misspelled labels, captions, subtitles, watermarks, cartoon/illustration
  look — plus anything scene-specific you can see.

PRESERVE every verbatim user constraint WORD-FOR-WORD: exact phrases ("exact
same pose"), hex codes (#FFD400), brand names (Pumpkin Oil), exact text
strings.

SWAP-WITH-MODIFICATIONS: state the modification clearly AFTER the swap +
preservation baseline so the model treats it as an intentional change.

VARIANTS: across the N variants for one (character, scene), vary ONLY subtle
lighting, expression, and micro-framing within the scene's constraints — NEVER
the identity, props, or background. Variant 0 is the safest by-the-book pass;
later variants may be marginally more interpretive.

Each prompt should be specific and vivid — roughly 120–200 words is fine given
the enforcement sections. Never generic.

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

For each scene, write ONE cinematic shot description that will animate every
approved variant for that scene. You MUST USE the `submit_movement_plan` tool.
Do not respond with prose.

PROCESS:
1. Identify the action the user wants in this scene ("he pours oil", "she
   waves").
2. Look at the approved variant images: what's visible? What naturally moves
   in that frame? What pose is the character in?
3. Choose the camera movement that fits: static / push-in / pull-out /
   tracking / hand-held. Default to static unless action demands otherwise.
4. Compose the prompt.

CRITICAL RULES:
- Preserve every user-named action / subject WORD-FOR-WORD.
- Add: subject performance cues (expression, gesture, timing), naturalistic
  motion details, lighting consistency.
- DISTINCT per scene. If scene A is "pouring oil" and scene B is "waving",
  the camera + framing must reflect each scene's needs (close-up vs medium
  shot, etc.).
- End every prompt with: "Shot on cinema camera, 24fps, shallow depth of
  field, naturalistic color, sharp focus."
- Keep each prompt under 100 words.

Use the exact scene_ids from the user message verbatim. Every scene the user
lists must appear in your output."""


# --- Public entry points ---------------------------------------------------

def direct_swap(
    *,
    user_prompt: str,
    characters: list[tuple[str, str, Path]],   # [(char_id, name, ref_path)]
    scenes: list[tuple[str, Path]],            # [(scene_id, scene_path)]
    images_per_character: int,
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
            system=SWAP_DIRECTOR_SYSTEM,
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
        return SwapDirectorPlan.model_validate(payload)
    except ValidationError as e:
        logger.warning("director_swap: validation failed: %s", e)
        return None


def direct_movement(
    *,
    scenes: list[tuple[str, Path, list[Path], str]],
    # (scene_id, scene_img, approved_variant_imgs, raw_movement_prompt)
    job_id: str | None = None,
) -> MovementDirectorPlan | None:
    """ONE Claude call. Returns per-scene movement prompts. None on failure."""
    if not scenes:
        return None

    content: list[dict] = [
        {"type": "text", "text": "USER PROMPT PER SCENE:"},
    ]
    for scene_id, scene_path, approved_paths, raw in scenes:
        text = f"SCENE {scene_id} — user wrote: '{(raw or '').strip()}'"
        content.append({"type": "text", "text": text})
        # The scene reference (starting composition).
        try:
            content.append(anthropic_client._file_to_image_block(scene_path))
        except Exception as e:
            logger.warning("director_movement: failed to encode scene %s: %s",
                           scene_id, e)
            return None
        # Each approved variant (what the video model will actually start from).
        for ap in approved_paths or []:
            content.append({
                "type": "text",
                "text": f"approved variant for scene {scene_id} (this is the actual start frame):",
            })
            try:
                content.append(anthropic_client._file_to_image_block(ap))
            except Exception as e:
                logger.warning(
                    "director_movement: failed to encode approved variant for %s: %s",
                    scene_id, e,
                )
                return None

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
