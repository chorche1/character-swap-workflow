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
- BACKGROUND — DO NOT CHANGE (default): describe the scene's background
  concretely and lock it in a standalone clause — keep room, surfaces,
  furniture, fixtures, decor, and lighting EXACTLY. Only deviate if the user
  EXPLICITLY asked to change or replace the background.
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
- OVERALL LOOK — ORDINARY UNEDITED PHONE PHOTO: every prompt MUST specify that
  the final image looks like a completely ordinary, unedited iPhone photo taken
  quickly by another person — NOT staged, composed, retouched, filtered, color
  corrected, or professionally lit. Plain, slightly dull phone-camera colors,
  neutral white balance, no warm tint. Mundane natural daylight with slightly
  uneven exposure, mild softness, subtle sensor noise, ordinary shadows, and
  small background distractions. Do not beautify, and do not perfectly center
  or symmetrize. It should look like a normal photo from someone's camera roll,
  not an advertisement or a professionally edited social-media image. Explicitly
  forbid: golden tones, cinematic contrast, dramatic shadows, rich saturation,
  glossy highlights, crisp commercial sharpness, HDR, enhanced clarity, and
  polished skin.
- INTEGRATION (so the character NEVER looks pasted in — this is the difference
  between a believable swap and an obvious cutout): relight the inserted person
  with the scene's OWN plain ambient daylight — match its direction, softness,
  and intensity, and discard the lighting baked into the character photo. Match
  the scene's plain, neutral white balance on their skin and clothing. Add
  ordinary contact shadows + ambient occlusion where they touch surfaces. Blend
  edges (no cutout halo or fringe). Match the scene's softness, sensor noise,
  and grain so the subject isn't crisper or cleaner than the background. Keep
  the lighting unremarkable and consistent with the rest of the frame —
  EVERYTHING must look like the same ordinary snapshot, not like the person was
  pasted in. Do NOT add rim / edge lights, glamour lighting, color grading, or
  any cinematic relighting.
- NO BURNT-IN TEXT: instruct removal of any captions, subtitles, progress bars,
  logos, or watermarks present in the source image.
- INLINE NEGATIVES: end each prompt with a short "Avoid:" clause listing the
  failure modes to exclude — a pasted-in / cutout / collage look, the subject
  floating with no shadow, lighting that doesn't match the scene, the subject
  sharper or cleaner than the background, professional / studio / glamour
  lighting, cinematic contrast, dramatic shadows, HDR, color grading, warm or
  golden tint, oversaturation, glossy highlights, crisp commercial sharpness,
  enhanced clarity, retouching, beautification, polished skin, shallow depth of
  field / portrait-mode blur, identity bleed from the original subject, extra or
  distorted fingers, warped facial features, changed/restyled background, altered
  prop counts, props changing state, misspelled labels, captions, subtitles,
  watermarks, cartoon/illustration look — plus anything scene-specific you can
  see.

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
        plan = SwapDirectorPlan.model_validate(payload)
    except ValidationError as e:
        logger.warning("director_swap: validation failed: %s", e)
        return None
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
    job_id: str | None = None,
) -> tuple[str, dict[str, str]] | None:
    """ONE Claude call with every scene frame → (intent, {scene_id: prompt}).
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
            "entirely with Image 3's light. CRITICAL framing rule (fold it "
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
                   str(s["prompt"]).strip() + ORGANIC_STYLE_CLAUSE
                   for s in data["scenes"]
                   if s.get("scene_id") and (s.get("prompt") or "").strip()}
        if not prompts:
            return None
        return (str(data.get("intent") or ""), prompts)
    except ProviderNotConfigured:
        return None
    except Exception:
        logger.exception("director_reengineer failed; falling back")
        return None
