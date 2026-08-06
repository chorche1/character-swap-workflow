"""How many people are in a swap scene — and who they are.

Hugo 2026-08-06, re_a5613a883e scene 2. A two-person photo (an older man on the
left holding a shot glass, a blonde woman on the right) was swapped for nine
characters and came back nine different ways: the woman deleted entirely in
most, kept in three, and in one the new character was painted onto HER while
the man was left untouched. Every one of the nine passed QC.

The cause was a coverage hole, not a bad prompt. Multi-person detection lived
INSIDE the AI Director's per-scene call, which runs exactly once — when the run
is created. That scene was added afterwards with "+ egen", so it never got a
Director prompt, never reached the "välj person"-gate, and fell back to the
generic job prompt: "Replace THE PERSON in Image 1", singular and unanchored,
with a constraint list that forbids ADDING people but never REMOVING them. On a
two-person frame the model is free to improvise, and it did — differently for
each character. The scene in the same run that DID go through the gate
(swap_person_idx=0, "Replace SPECIFICALLY the older man grey beard on the
left … keep the other people exactly as they are") was consistent across all
nine.

So the detection moves OUT of the Director and into this module: one cheap
Sonnet vision call per scene image, callable from any path that introduces a
scene. It answers the same question in the same shape the Director's metadata
used, so the existing gate + `api._person_directive` consume it unchanged.

Contract:

- **Advisory, never blocking.** No key / SDK / API error / unusable response →
  None. The caller proceeds with generation; the hardened stock prompt ("remove
  nobody") and the person-count QC check are the other two layers, and neither
  needs this call to have succeeded. Failing a scene because a $0.01 judge call
  timed out would be worse than the bug.
- **Conservative on the multi-person verdict.** Only people who are actually
  part of the shot count. Passers-by in the far background, faces on a poster,
  a reflection or a photo on the wall are NOT people here — flagging those would
  stop the run to ask a question with no meaningful answer.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SCENE_PEOPLE_SYSTEM = """\
You look at ONE photograph and report the people in it. This photo is about to
have ONE of its people replaced by a different person, so the app needs to know
whether there is any ambiguity about WHICH person that is.

Set "multi_person" to true ONLY when MORE THAN ONE person is genuinely part of
the shot — two or more real, present people a viewer would say are IN the photo.

Do NOT count any of these as a second person (when only one real subject is
present, "multi_person" is false and "people" is empty):
- distant or blurred passers-by in the far background who are not part of the
  scene;
- a person on a poster, screen, painting, photograph, statue or product label;
- a reflection or shadow of the same person;
- a hand, arm or body part with no visible person attached.

When multi_person is true, fill "people" with ONE entry per real person, listed
LEFT TO RIGHT as they appear in the frame:
- "position": exactly one of left / right / center / background.
- "description": 2-3 words that let a person pick this one out of the frame —
  approximate age, gender and ONE clearly visible trait. For example "older man
  grey beard", "young woman red top", "blonde woman right".

The description is shown to the user so they can choose, and it is quoted back
to the image model as the person to replace. Describe what is VISIBLE and
DISTINGUISHING — never guess a name, relationship or occupation.
"""

SCENE_PEOPLE_TOOL: dict = {
    "name": "submit_people",
    "description": "Report the people visible in the scene photo.",
    "input_schema": {
        "type": "object",
        "required": ["multi_person"],
        "properties": {
            "multi_person": {
                "type": "boolean",
                "description": "True only when more than one real person is "
                               "genuinely part of the shot.",
            },
            "people": {
                "type": "array",
                "description": "Only when multi_person=true: one entry per "
                               "person, left to right.",
                "items": {
                    "type": "object",
                    "required": ["position", "description"],
                    "properties": {
                        "position": {
                            "type": "string",
                            "enum": ["left", "right", "center", "background"],
                        },
                        "description": {"type": "string"},
                    },
                },
            },
        },
    },
}


def detect_people(image: Path, *, job_id: str | None = None) -> dict | None:
    """One vision call → ``{"multi_person": bool, "people": [...]}``, or None
    when the judge is unavailable or returns something unusable.

    The returned dict is the SAME shape `prompt_director.direct_reengineer_swap`
    puts in its metadata, so `runner_reengineer`'s gate and
    `api._person_directive` read it without a translation layer.

    A "multi_person" verdict with fewer than two people is downgraded to
    single-person: the whole point of the flag is that the user has a choice to
    make, and a one-item list gives them nothing to choose between."""
    from character_swap.config import settings
    if not settings.anthropic_api_key:
        return None
    try:
        from character_swap.clients import anthropic_client
        resp = anthropic_client.messages_with_tools(
            system=SCENE_PEOPLE_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "Scene photo:"},
                anthropic_client._file_to_image_block(image),
            ]}],
            tools=[SCENE_PEOPLE_TOOL],
            tool_choice={"type": "tool", "name": "submit_people"},
            max_tokens=600,
            temperature=0.0,
            model=settings.scene_people_model,
            job_id=job_id,
            phase="scene_people",
        )
        data = anthropic_client.extract_tool_call(resp, "submit_people")
        if data is None or "multi_person" not in data:
            return None
        people = [
            {"position": str(p.get("position") or "").strip(),
             "description": str(p.get("description") or "").strip()}
            for p in (data.get("people") or [])
            if isinstance(p, dict)
        ]
        if not bool(data["multi_person"]) or len(people) < 2:
            return {"multi_person": False, "people": []}
        return {"multi_person": True, "people": people}
    except Exception as e:
        # Advisory: the caller generates anyway. LOUD in the log so an operator
        # can see the layer was down (same doctrine as swap_qc's skip).
        logger.warning("scene people detection unavailable for %s: %s: %s",
                       image, type(e).__name__, e)
        return None


def people_count(meta: dict | None) -> int:
    """People in the frame per `meta` — 1 whenever the detection is missing,
    failed, or said single-person. Callers store this on
    `Job.scene_people_counts` and QC only tightens on values > 1, so an absent
    or failed detection can never make the judge stricter."""
    if not meta or not meta.get("multi_person"):
        return 1
    return max(1, len(meta.get("people") or []))
