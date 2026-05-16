"""
Prompt enrichment via two-stage intent analysis.

Hugo's request: don't just blindly expand the prompt — first have an LLM
*understand* the user's actual intent, then *craft* the optimal prompt for
the downstream image/video model.

Implementation: a single GPT-4o call with structured JSON output. Chain-of-
thought happens inside the JSON: the model writes its understanding of
intent + constraints + requested changes FIRST, then composes the final
prompt informed by that analysis. This is cheaper and faster than two
separate API calls while giving us most of the quality gains.

The full intent analysis (intent / constraints / changes) is recorded in
`state/calls.jsonl` via call_log so users can inspect exactly what GPT-4o
thought the prompt meant. Only `final_prompt` is forwarded downstream.

Failure mode: on any error (parse fail, empty final_prompt, API timeout)
we return the ORIGINAL prompt so generation isn't blocked.
"""
from __future__ import annotations

import json
from typing import Any

from character_swap.call_log import record
from character_swap.clients import openai_image


# Per-kind system prompts. Each one asks for STRUCTURED JSON with:
#   - intent: 1-sentence summary of what the user wants
#   - constraints / requirements / motion type (varies by kind)
#   - final_prompt: the optimized prompt the downstream model receives
#
# Why JSON: it forces the model to do the analysis as a separate step
# from prompt composition, which lifts quality. It also gives us
# inspectable reasoning logged to calls.jsonl.

_SYSTEM_PROMPTS: dict[str, str] = {
    "image": (
        "You are a senior prompt engineer for image-generation AI models. "
        "Your job is to read a user's image request, understand their "
        "intent, then craft the optimal prompt for the downstream model.\n\n"
        "PROCESS:\n"
        "1. Identify INTENT — what subject + scene + mood does the user want?\n"
        "2. List explicit user requirements (named subjects, specific colors / hex codes, brand names, exact text).\n"
        "3. Infer implicit context (style, lighting, framing) the user assumes but didn't state.\n"
        "4. Compose the FINAL PROMPT.\n\n"
        "Respond with strict JSON ONLY (no preamble, no markdown):\n"
        "{\n"
        '  "intent": "1-sentence summary",\n'
        '  "explicit_requirements": ["..."],\n'
        '  "implicit_context": "1-sentence inference about style/mood",\n'
        '  "final_prompt": "the optimized prompt the model will receive"\n'
        "}\n\n"
        "Rules for final_prompt:\n"
        "- Preserve every explicit_requirement EXACTLY (named subjects, colors, hex codes, brand names).\n"
        "- Add: lighting direction + quality, composition, mood, lens / photographic style cues.\n"
        "- Do NOT invent subjects, props, or scenes the user didn't imply.\n"
        "- Keep under 120 words. Be specific and vivid, not generic."
    ),
    "video": (
        "You are a senior prompt engineer for image-to-video AI models. "
        "The user gives a short motion description; the reference image "
        "is the starting frame. Translate this into a cinematic shot "
        "direction.\n\n"
        "PROCESS:\n"
        "1. Identify INTENT — what motion or change happens between start and end of the clip?\n"
        "2. Identify the subject's action(s) the user explicitly named.\n"
        "3. Choose the appropriate camera movement (static, tracking, push-in, pull-out, hand-held, etc).\n"
        "4. Compose the FINAL PROMPT as a shot direction.\n\n"
        "Respond with strict JSON ONLY (no preamble, no markdown):\n"
        "{\n"
        '  "intent": "1-sentence summary of the motion",\n'
        '  "subject_action": "what the subject visibly does",\n'
        '  "camera_movement": "static | tracking | push-in | pull-out | hand-held | other",\n'
        '  "final_prompt": "the optimized cinematic shot description"\n'
        "}\n\n"
        "Rules for final_prompt:\n"
        "- Preserve user-named actions / subjects EXACTLY.\n"
        "- Add: camera movement, pacing, naturalistic motion details, "
        "performance cues (expression, gesture, timing), lighting consistency.\n"
        "- End with a brief quality tag: 'shot on cinema camera, 24fps, "
        "shallow depth of field, naturalistic color, sharp focus'.\n"
        "- Keep under 100 words."
    ),
    "swap": (
        "You are a prompt engineer for an image-edit model that takes TWO "
        "reference images: a SCENE (ref #1) and a CHARACTER (ref #2). The "
        "output is the character inserted into the scene, preserving the "
        "scene's composition exactly.\n\n"
        "PROCESS:\n"
        "1. Identify INTENT — is this a pure swap (preserve scene exactly), a "
        "swap-with-modifications, or a more creative freeform edit?\n"
        "2. Extract verbatim constraints — list every 'exact same X' / "
        "'identical to' / 'do not change Y' / 'same as in the first picture' "
        "phrase the user wrote.\n"
        "3. Extract modifications — what the user wants to change (clothing, "
        "expression, lighting tweaks).\n"
        "4. Compose the FINAL PROMPT.\n\n"
        "Respond with strict JSON ONLY (no preamble, no markdown):\n"
        "{\n"
        '  "intent": "swap-only | swap-with-modifications | freeform",\n'
        '  "verbatim_constraints": ["exact same pose", "exact same position", ...],\n'
        '  "modifications": ["make him smile", "blue suit instead of red", ...],\n'
        '  "final_prompt": "the optimized prompt the image model receives"\n'
        "}\n\n"
        "CRITICAL rules for final_prompt:\n"
        "- Include every phrase in verbatim_constraints WORD-FOR-WORD. "
        "Never replace 'exact same' with 'similar', 'matching', or any softer synonym.\n"
        "- Append modifications clearly after the constraints.\n"
        "- Add ONLY: integrated lighting / color-match / sharpness hints.\n"
        "- Do NOT add new scene elements, new backgrounds, new camera angles, "
        "or any composition changes.\n"
        "- Keep under 100 words."
    ),
    "reel": (
        "You are a prompt engineer for batch-consistent reel editing. The "
        "user wants the SAME visual modifications applied uniformly across "
        "every frame of a video reel.\n\n"
        "PROCESS:\n"
        "1. Identify INTENT — what's the overall transformation (clothing change, "
        "background swap, object replacement, etc)?\n"
        "2. Extract specific user requirements — hex color codes, brand names, "
        "exact text strings, object identities. These must NOT drift.\n"
        "3. Decompose the user's wish into discrete uniform changes.\n"
        "4. Compose the FINAL PROMPT as a numbered constraint list.\n\n"
        "Respond with strict JSON ONLY (no preamble, no markdown):\n"
        "{\n"
        '  "intent": "1-sentence summary of the batch transformation",\n'
        '  "specific_requirements": ["#FFD400 yellow shirt", "Pumpkin Oil label", ...],\n'
        '  "uniform_changes": ["change 1", "change 2", ...],\n'
        '  "final_prompt": "numbered list of non-negotiable constraints"\n'
        "}\n\n"
        "Rules for final_prompt:\n"
        "- Each change is a numbered item phrased as a non-negotiable constraint "
        "(e.g. '1. Replace X with Y. Y must appear in every frame.').\n"
        "- Preserve every specific_requirement (hex codes, brand names, exact text) EXACTLY.\n"
        "- Don't add changes the user didn't imply.\n"
        "- Keep under 150 words."
    ),
}


def _call_gpt4o(prompt: str, system_prompt: str, *, job_id: str | None,
                kind: str) -> dict[str, Any] | None:
    """One GPT-4o call in JSON mode. Returns parsed dict or None on any failure."""
    try:
        client = openai_image._client()
        with record(phase="prompt_intent", model="gpt-4o",
                    character=kind, job_id=job_id):
            resp = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": prompt},
                ],
                response_format={"type": "json_object"},
                # Lower temperature than freeform enrichment — we want
                # consistent analysis, not creative variation.
                temperature=0.3,
                max_tokens=900,
            )
    except Exception:
        return None

    raw = (resp.choices[0].message.content or "").strip() if resp.choices else ""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def enrich_prompt(
    prompt: str | None,
    kind: str,
    *,
    job_id: str | None = None,
) -> str:
    """Run a structured intent-analysis pass on `prompt`, return the
    `final_prompt` field from the analysis, or the original on any failure.

    Logged under `phase="prompt_intent"` in calls.jsonl. The full JSON
    response (intent / constraints / changes / final_prompt) is captured
    via `call_log.record` so users can audit the analysis.

    Falls back to the input prompt on: empty input, unknown kind (defaults
    to 'image'), API error, JSON parse fail, missing final_prompt field,
    or a final_prompt shorter than ~half the input length (likely a parse
    artifact).
    """
    if not prompt or not prompt.strip():
        return prompt or ""
    sys_prompt = _SYSTEM_PROMPTS.get(kind) or _SYSTEM_PROMPTS["image"]
    analysis = _call_gpt4o(prompt, sys_prompt, job_id=job_id, kind=kind)
    if not analysis or not isinstance(analysis, dict):
        return prompt
    final = analysis.get("final_prompt")
    if not isinstance(final, str):
        return prompt
    final = final.strip()
    if final.startswith('"') and final.endswith('"'):
        final = final[1:-1].strip()
    if not final or len(final) < max(20, len(prompt) // 2):
        # Sanity guard — refuse outputs much shorter than input
        # (suggests the model truncated or misunderstood).
        return prompt
    return final


def analyze_prompt(
    prompt: str | None,
    kind: str,
    *,
    job_id: str | None = None,
) -> dict[str, Any] | None:
    """Return the FULL intent analysis as a dict (intent / constraints /
    changes / final_prompt). Useful when callers want to inspect or
    surface the reasoning in the UI. Returns None on any failure."""
    if not prompt or not prompt.strip():
        return None
    sys_prompt = _SYSTEM_PROMPTS.get(kind) or _SYSTEM_PROMPTS["image"]
    return _call_gpt4o(prompt, sys_prompt, job_id=job_id, kind=kind)
