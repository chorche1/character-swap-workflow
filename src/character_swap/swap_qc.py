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

import logging
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Backlog #4 (2026-06-12): an Anthropic 429 burst used to disable QC for the
# whole batch SILENTLY — 21 images/clips shipped unchecked on 06-11. Rate
# limits are transient by definition, so the judge call retries with backoff
# before giving up. QC runs OUTSIDE the image-gen semaphore and on a worker
# thread, so sleeping here never stalls the generation lanes.
_QC_RETRY_SLEEPS = (2.0, 8.0, 20.0)
_RATE_LIMIT_MARKERS = ("rate_limit", "ratelimit", "429", "overloaded", "529")


def _is_rate_limited(e: Exception) -> bool:
    s = f"{type(e).__name__}: {e}".lower()
    return any(m in s for m in _RATE_LIMIT_MARKERS)

QC_SYSTEM = """\
You are a LENIENT quality inspector for a character-swap image pipeline. You
catch ONLY catastrophic, unusable images. Everything else PASSES.

You receive (possibly with an extra BACKGROUND image and context text):
1. SCENE — the original photo whose person is being replaced.
2. CHARACTER — the reference for the person who must now appear.
3. RESULT — the generated swap output you are inspecting.

The RESULT is supposed to show the CHARACTER's person in roughly the SCENE's
situation. PASS the RESULT unless ONE of these CATASTROPHIC failures clearly
holds — when in any doubt, PASS:

- WRONG PERSON: the face in RESULT does not read as the same person as
  CHARACTER — the SCENE's original person survived the swap, the face is a
  blend of the two, or a third, different person appears. Compare facial
  IDENTITY only (bone structure, features) — NOT clothing, hair styling,
  expression, makeup or minor age touch-ups. This is the single most
  important check.
- MISSING/EXTRA PEOPLE: no person at all in RESULT, or clearly extra,
  invented people who appear in neither SCENE nor CHARACTER.
- BROKEN IMAGE: fully or mostly black / blank / censored / heavily corrupted
  output, or RESULT is just the unmodified SCENE or CHARACTER with no swap
  performed at all.
- SEVERE ARTIFACTS: a grossly deformed FACE or HANDS — clearly extra or
  missing fingers, melted or duplicated facial features, or duplicated/fused
  limbs. Minor hand or finger imperfections do NOT count; only gross,
  obviously broken anatomy.

Everything else is ACCEPTABLE — do NOT fail for any of these (this is a
deliberate policy, not an oversight):
- Framing, zoom, crop, camera distance or subject scale differing from SCENE
  (more or less of the body visible, the head larger or smaller in frame,
  chest-up vs waist-up). There is NO head-ruler / zoom test anymore.
- Headroom or vertical position of the subject differing from SCENE (added
  sky/space above the head, subject sitting higher or lower).
- Props, held objects, the action, prop count, container type, table/counter
  items or foreground furniture differing from SCENE.
- Gaze direction or hand gesture differing from SCENE (looking into the
  camera vs away, a different or generic hand pose).
- Clothing / outfit differing from SCENE or from CHARACTER, including added
  or missing gloves, hats, jackets or other wardrobe pieces.
- Background or environment differing from SCENE — a changed, replaced, or
  imperfectly-rendered background, including a logo, sign or flag that is
  altered, incomplete or stylized.
- "Pasted-in" / cutout look, hard edges or halos, lighting that doesn't
  perfectly match the environment, soft focus, grain, or style / color-grade
  drift.

Context flags and a USER INTENT text block may appear before the images, and
an extra BACKGROUND image may be attached. These are INFORMATIONAL ONLY now —
never fail an image because of them; in particular never fail for a changed
background, outfit, or gaze.

Be decisive and LENIENT. Only a genuinely unusable image fails. When you DO
fail, START the reason with the violated rule's NAME in caps — exactly one of
"WRONG PERSON", "MISSING/EXTRA PEOPLE", "BROKEN IMAGE" or "SEVERE ARTIFACTS"
(the retry machinery routes repair vs full re-roll on it) — then give a
one-sentence corrective instruction for the image model (e.g. "Make the face
match the character reference exactly — do not retain the original person's
facial features.").
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


CONSISTENCY_SYSTEM = """\
You inspect CROSS-SCENE consistency for a character-swap video pipeline.

You see one CHARACTER reference image, then the SAME character's generated
images for the consecutive SCENES of one video, labeled by scene_id. Within
one video the person's appearance must stay consistent from scene to scene:
the same outfit pieces and their state (sleeves rolled or not, jacket on or
off), same glasses or none, same gloves or bare hands, same hairstyle and
facial hair. Judge against the MAJORITY across scenes — report each scene
whose appearance clearly contradicts the others, with the concrete
difference. Differences in pose, framing, lighting, expression and
background are EXPECTED and never an issue. Be conservative: only clear
wardrobe/appearance contradictions count. Empty list when consistent.
"""

CONSISTENCY_TOOL: dict = {
    "name": "submit_consistency",
    "description": "Report cross-scene appearance contradictions.",
    "input_schema": {
        "type": "object",
        "required": ["issues"],
        "properties": {
            "issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["scene_id", "issue"],
                    "properties": {
                        "scene_id": {"type": "string"},
                        "issue": {"type": "string"},
                    },
                },
            },
        },
    },
}


def inspect_consistency(
    *,
    variants: list[tuple[str, Path]],     # [(scene_id, image_path)] in order
    character_image: Path,
    job_id: str | None = None,
) -> list[dict] | None:
    """ONE vision call over a character's per-scene results (backlog #13):
    each variant passes solo QC, but nothing compared them ACROSS scenes —
    sleeves/gloves/glasses wobbled scene to scene in the same final. Returns
    [{scene_id, issue}, ...] (empty = consistent) or None when unavailable.
    Advisory only — surfaced at the gate, never blocks or fails variants."""
    from character_swap.config import settings
    if not settings.swap_qc_enabled or not settings.anthropic_api_key:
        return None
    if len(variants) < 2:
        return []
    try:
        from character_swap.clients import anthropic_client
        content: list[dict] = [
            {"type": "text", "text": "CHARACTER reference:"},
            anthropic_client._file_to_image_block(character_image),
        ]
        for sid, p in variants:
            content.append({"type": "text", "text": f"SCENE {sid}:"})
            content.append(anthropic_client._file_to_image_block(p))
        resp = anthropic_client.messages_with_tools(
            system=CONSISTENCY_SYSTEM,
            messages=[{"role": "user", "content": content}],
            tools=[CONSISTENCY_TOOL],
            tool_choice={"type": "tool", "name": "submit_consistency"},
            max_tokens=600,
            temperature=0.0,
            model=settings.swap_qc_model,
            job_id=job_id,
            phase="swap_qc_consistency",
        )
        data = anthropic_client.extract_tool_call(resp, "submit_consistency")
        if data is None or "issues" not in data:
            return None
        return [{"scene_id": str(i.get("scene_id") or ""),
                 "issue": str(i.get("issue") or "")}
                for i in data["issues"] if i.get("scene_id")]
    except Exception as e:
        logger.warning("consistency QC unavailable: %s: %s",
                       type(e).__name__, e)
        return None


# Failure classes whose flaw IS the image's geometry or content base — a
# minimal-change edit of the failed image cannot fix what must be
# REGENERATED, and the repair contract ("keep framing/background unchanged")
# actively fights the correction (backlog #12, 2026-06-12). These skip
# repair and go straight to a fresh re-roll with the corrective hint.
# Deliberately repairable in place: WRONG HEADROOM (crop), WRONG BACKGROUND
# SYMBOL (fix the symbol), WRONG GAZE/GESTURE, WRONG OUTFIT, WRONG PERSON
# (face edit), SEVERE ARTIFACTS, OBVIOUS CUTOUT.
_REROLL_MARKERS = (
    "wrong background", "wrong framing", "wrong zoom",
    "missing/extra people", "missing people", "extra people",
    "broken image", "unmodified scene",
)


def needs_reroll(reason: str | None) -> bool:
    """True when the QC failure class cannot be repaired by a minimal-change
    edit of the failed image. The judge is instructed to lead the reason
    with the violated rule's name, so substring routing is reliable."""
    low = (reason or "").lower()
    if "wrong background symbol" in low:     # the symbol IS edit-repairable
        return False
    return any(m in low for m in _REROLL_MARKERS)


def repair_prompt(hint: str) -> str:
    """Minimal-change repair instruction for a QC-failed image.

    The first QC retry does NOT re-roll from the scene — it feeds the failed
    image itself back through the edit engine with this prompt, so everything
    that was already right is preserved and only the flagged flaw changes
    (Hugo: "bilden ska ändras så lite som möjligt"). Only for failure
    classes that ARE fixable in place — see `needs_reroll`."""
    fix = hint.strip() or "the person's face must match the identity reference exactly"
    return (
        "Image 1 is an almost-correct generated photo that needs ONE repair. "
        "Image 2 is the identity reference for the person who must appear. "
        f"Fix only this: {fix} "
        "Keep absolutely everything else in Image 1 unchanged — identical "
        "framing, crop, pose, body position, clothing, objects, background, "
        "lighting, colors and photographic style — EXCEPT where the fix "
        "above explicitly requires a change. Change as little of the "
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
    outfit_text: str | None = None,
    user_intent: str | None = None,
    camera_gaze: bool = False,
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
        if outfit_text:
            flags += f', custom_outfit="{outfit_text[:200]}"'
        if camera_gaze:
            flags += ", camera_gaze=true"
        intent_block = (
            f"USER INTENT (authoritative — do not fail deviations it "
            f"requests):\n{user_intent.strip()[:600]}\n\n" if user_intent
            and user_intent.strip() else "")
        content = [
            {"type": "text", "text": f"{intent_block}Context flags: {flags}\nSCENE:"},
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
        resp = None
        for attempt in range(len(_QC_RETRY_SLEEPS) + 1):
            try:
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
                break
            except Exception as e:
                if not _is_rate_limited(e) or attempt >= len(_QC_RETRY_SLEEPS):
                    raise
                delay = _QC_RETRY_SLEEPS[attempt]
                logger.warning(
                    "swap_qc rate-limited (attempt %d/%d) — backing off "
                    "%.0fs: %s", attempt + 1, len(_QC_RETRY_SLEEPS) + 1,
                    delay, e)
                time.sleep(delay)
        data = anthropic_client.extract_tool_call(resp, "submit_inspection")
        if data is None or "passed" not in data:
            return None
        return QCVerdict(
            passed=bool(data["passed"]),
            reason=str(data.get("reason") or ""),
            corrective_hint=str(data.get("corrective_hint") or ""),
        )
    except Exception as e:
        # LOUD skip (backlog #4): the variant ships with qc_status="skipped",
        # but the operator must be able to see WHY in the server log.
        logger.warning("swap_qc unavailable — variant ships unchecked: %s: %s",
                       type(e).__name__, e)
        return None
