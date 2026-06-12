"""Vision QC for swap variants (Swap flow + Reengineer).

After every variant generates, a cheap Claude vision call inspects the result
against the scene + character references. If the WRONG PERSON is in the image
(the original subject survived the swap, or a third face appeared) or the
image is otherwise broken (censorship blackout, deformed anatomy, burnt-in
text, obvious cutout look), the runner regenerates the slot — with the QC
verdict appended to the prompt as a corrective hint — up to
`settings.swap_qc_max_retries` times.

Philosophy: QC must never block the pipeline. No API key / SDK / timeout /
malformed response → verdict None → the variant ships as-is with
qc_status="skipped". After exhausted retries the LAST image is kept (not
failed) with qc_status="failed" + the reason, surfaced as a ⚠ chip in the UI
— a false-positive judge must never destroy a usable image.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

QC_SYSTEM = """\
You are a strict quality inspector for a character-swap image pipeline.

You receive three images:
1. SCENE — the original photo whose person is being replaced.
2. CHARACTER — the reference for the person who must now appear.
3. RESULT — the generated swap output you are inspecting.

The RESULT is supposed to show the CHARACTER's person (same face, same
perceived identity) in the SCENE's setting and pose. Judge ONLY hard
failures; minor style drift is acceptable. FAIL the result if ANY of these
hold:

- WRONG PERSON: the face in RESULT does not read as the same person as
  CHARACTER — e.g. the SCENE's original person is still there, the face is a
  blend of the two, or a third, different person appears. This is the most
  important check. Compare facial identity, not clothing or hair styling.
- WRONG PROPS / ACTION: the person in RESULT must hold and interact with the
  SAME object(s) and perform the SAME action as the person in SCENE. Look at
  the hands first: if SCENE shows the person holding specific items (food,
  a tool, a product), RESULT must show the SAME items held the SAME way — a
  different object, a missing object, an invented object, or a clearly
  different action is a FAIL. Also fail if a key prop on the table/counter
  changed into something else. (This applies even when
  background_replaced=true — props travel with the person.)
- WRONG FRAMING / ZOOM: RESULT must match SCENE's exact framing — same
  camera distance, crop and subject scale. FAIL if RESULT is noticeably more
  zoomed out than SCENE (the person/objects look smaller, more of the room
  is visible, new space appears at the edges) or noticeably more zoomed in,
  or if a key object sits at a clearly different position or size in the
  frame. Compare how much of the frame the person's body and the held
  objects occupy in SCENE vs RESULT. (background_replaced=true changes WHAT
  is behind the person, not the camera geometry — framing must still match
  SCENE.)
- MISSING/EXTRA PEOPLE: no person at all, or extra people who are in neither
  SCENE nor CHARACTER.
- BROKEN IMAGE: fully or mostly black/blank/censored output, heavy
  corruption, or the image is just the unmodified SCENE or CHARACTER.
- SEVERE ARTIFACTS: grossly deformed face or hands (extra/missing fingers
  clearly visible), duplicated limbs, garbled brand text on key products.
- OBVIOUS CUTOUT: the person is clearly pasted in — hard halo edges or
  lighting that contradicts the environment so strongly it looks like a
  collage.

Context flags you may receive:
- background_replaced=true: the RESULT's environment is SUPPOSED to differ
  from SCENE (a replacement background was requested). Do NOT fail for a
  changed background; still require the pose/props from SCENE and identity
  from CHARACTER, and lighting consistent with the NEW environment. When you
  ALSO receive a BACKGROUND image: that is the requested replacement
  environment — FAIL (WRONG BACKGROUND) if RESULT's surroundings clearly
  show SCENE's original environment instead of BACKGROUND's (the original
  location/walls/buildings were kept), or an unrelated third environment
  matching neither. RESULT does not need to be a pixel match of BACKGROUND —
  same recognizable location/setting type and light is a PASS.
- outfit_from_character=true: the RESULT's clothing is SUPPOSED to come from
  CHARACTER, not SCENE. Do not fail for changed clothing.

Be decisive. Borderline-acceptable images PASS — only clear failures fail.
When you fail, give a short concrete reason AND a one-sentence corrective
instruction for the image model (e.g. "Make the face match the character
reference exactly — do not retain the original person's facial features.").
"""

QC_TOOL: dict = {
    "name": "submit_inspection",
    "description": "Submit the QC verdict for the generated swap image.",
    "input_schema": {
        "type": "object",
        "required": ["passed", "reason", "corrective_hint"],
        "properties": {
            "passed": {"type": "boolean"},
            "reason": {
                "type": "string",
                "description": "Short reason. Empty string when passed.",
            },
            "corrective_hint": {
                "type": "string",
                "description": "One sentence for the image model on what to fix. "
                               "Empty string when passed.",
            },
        },
    },
}


@dataclass
class QCVerdict:
    passed: bool
    reason: str
    corrective_hint: str


def repair_prompt(hint: str) -> str:
    """Minimal-change repair instruction for a QC-failed image.

    The first QC retry does NOT re-roll from the scene — it feeds the failed
    image itself back through the edit engine with this prompt, so everything
    that was already right is preserved and only the flagged flaw changes
    (Hugo: "bilden ska ändras så lite som möjligt")."""
    fix = hint.strip() or "the person's face must match the identity reference exactly"
    return (
        "Image 1 is an almost-correct generated photo that needs ONE repair. "
        "Image 2 is the identity reference for the person who must appear. "
        f"Fix only this: {fix} "
        "Keep absolutely everything else in Image 1 unchanged — identical "
        "framing, crop, pose, body position, clothing, objects, background, "
        "lighting, colors and photographic style. Change as little of the "
        "image as possible."
    )


def inspect_variant(
    *,
    scene_image: Path,
    character_image: Path,
    result_image: Path,
    background_replaced: bool = False,
    background_image: Path | None = None,
    outfit_from_character: bool = False,
    job_id: str | None = None,
) -> QCVerdict | None:
    """ONE cheap vision call: does the generated swap pass? None when QC is
    unavailable (no key / SDK / API error / bad response) — callers treat
    None as 'skip QC', never as a failure.

    `background_image`: the requested replacement environment. Without it
    the judge can only IGNORE background changes — it cannot catch the
    observed 2026-06-12 failure where the ORIGINAL scene background was kept
    despite a replacement being requested (that image PASSED QC)."""
    from character_swap.config import settings
    if not settings.swap_qc_enabled or not settings.anthropic_api_key:
        return None
    try:
        from character_swap.clients import anthropic_client
        flags = (f"background_replaced={'true' if background_replaced else 'false'}, "
                 f"outfit_from_character={'true' if outfit_from_character else 'false'}")
        content = [
            {"type": "text", "text": f"Context flags: {flags}\nSCENE:"},
            anthropic_client._file_to_image_block(scene_image),
            {"type": "text", "text": "CHARACTER:"},
            anthropic_client._file_to_image_block(character_image),
        ]
        if background_replaced and background_image is not None:
            content += [
                {"type": "text",
                 "text": "BACKGROUND (the requested replacement environment):"},
                anthropic_client._file_to_image_block(background_image),
            ]
        content += [
            {"type": "text", "text": "RESULT (inspect this):"},
            anthropic_client._file_to_image_block(result_image),
        ]
        resp = anthropic_client.messages_with_tools(
            system=QC_SYSTEM,
            messages=[{"role": "user", "content": content}],
            tools=[QC_TOOL],
            tool_choice={"type": "tool", "name": "submit_inspection"},
            max_tokens=400,
            temperature=0.0,
            model=settings.swap_qc_model,
            job_id=job_id,
            phase="swap_qc",
        )
        data = anthropic_client.extract_tool_call(resp, "submit_inspection")
        if data is None or "passed" not in data:
            return None
        return QCVerdict(
            passed=bool(data["passed"]),
            reason=str(data.get("reason") or ""),
            corrective_hint=str(data.get("corrective_hint") or ""),
        )
    except Exception:
        return None
