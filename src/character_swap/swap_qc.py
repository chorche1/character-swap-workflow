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
  changed into something else. Check prop COUNT, physical state and
  container type too — three kiwi halves must not become six arranged
  slices, a glass mug must not become a tumbler — and foreground
  furniture/surfaces: a desk/table filling SCENE's foreground must still be
  there. (This applies even when background_replaced=true — props and
  foreground furniture travel with the person.)
- WRONG FRAMING / ZOOM: RESULT must match SCENE's exact framing — same
  camera distance, crop and subject scale. FAIL if RESULT is noticeably more
  zoomed out than SCENE (the person/objects look smaller, more of the room
  is visible, new space appears at the edges) or noticeably more zoomed in,
  or if a key object sits at a clearly different position or size in the
  frame. Compare how much of the frame the person's body and the held
  objects occupy in SCENE vs RESULT. This holds EVEN when
  background_replaced=true: a replaced background changes WHAT surrounds the
  person, never the camera distance — judge zoom by the SUBJECT, not the
  room. The fraction of the frame the person's head, body and held objects
  occupy must match SCENE (e.g. if SCENE is chest-up, a RESULT showing the
  full torso or knees-up is a FAIL even though the environment is new and
  cannot be compared).
- WRONG HEADROOM / VERTICAL FRAMING: pay special attention to the space
  ABOVE the head. FAIL if RESULT has clearly MORE empty space / sky /
  scenery above the subject's head than SCENE does — i.e. the head sits
  lower in the frame and the subject is pushed down into the bottom portion,
  with dead space added at the top. The top of the head must sit at roughly
  the same height in RESULT as in SCENE. This holds EVEN when
  background_replaced=true: a replaced background changes WHAT is behind the
  person, never the camera geometry or how high the subject sits — if the
  new background added headroom/sky above the head that SCENE did not have,
  that is a FAIL.
- WRONG GAZE / GESTURE: the gaze direction and any distinct hand gesture
  must match SCENE. If SCENE's person looks down at what they are doing, a
  RESULT staring into the camera is a FAIL; a distinct gesture in SCENE
  (thumbs-up, pointing, mid-pour) replaced by generic open/resting hands is
  a FAIL. (The camera_gaze context flag below INVERTS the gaze half of this
  rule when set — gestures must always match.)
- WRONG OUTFIT: by default the person must wear the SAME clothing as the
  person in SCENE (an identity swap keeps the scene's wardrobe). FAIL if the
  clothing was clearly swapped to the CHARACTER reference's outfit or
  invented — including partial bleed like gloves, hats or jackets copied
  from the CHARACTER photo that the SCENE person does not wear. The context
  flags below INVERT this rule when set.
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
  same recognizable location/setting type and light is a PASS. EXCEPTION —
  distinctive symbols: when BACKGROUND contains a distinctive symbol (a
  flag, a logo, a lettered sign) and RESULT renders it clearly visible, its
  key identifying features must be intact — a US flag must show its blue
  star canton, not stripes alone; a logo must not be half-invented. FAIL
  (WRONG BACKGROUND SYMBOL) on a defaced or incomplete rendering of such a
  symbol.
- outfit_from_character=true: the RESULT's clothing is SUPPOSED to come from
  CHARACTER, not SCENE. Do not fail for changed clothing — instead FAIL
  (WRONG OUTFIT) if the clothing clearly does NOT match the CHARACTER
  reference's outfit (e.g. the scene person's clothes were kept).
- custom_outfit="...": the person is SUPPOSED to wear the described outfit —
  FAIL (WRONG OUTFIT) if the clothing clearly does not match the
  description; ignore both SCENE's and CHARACTER's wardrobe in that case.
- camera_gaze=true: the person in RESULT is SUPPOSED to look directly into
  the camera with a natural expression, REGARDLESS of where SCENE's person
  looks. Never fail camera gaze; instead FAIL (WRONG GAZE) if RESULT's
  person is clearly looking away from the camera. Distinct hand gestures
  must still match SCENE.
- USER INTENT (optional text block before the images): the user's own prompt
  for this job. It is AUTHORITATIVE and may explicitly request deviations
  from SCENE — different clothing, added/removed props, a changed action or
  expression. NEVER fail a deviation the user intent clearly requests; judge
  everything it does not mention by the normal rules above.

Be decisive. Borderline-acceptable images PASS — only clear failures fail.
When you fail, START the reason with the violated rule's NAME in caps
(e.g. "WRONG BACKGROUND: the original kitchen was kept") — the retry
machinery routes repair vs full re-roll on it — then give a one-sentence
corrective instruction for the image model (e.g. "Make the face match the
character reference exactly — do not retain the original person's facial
features.").
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
