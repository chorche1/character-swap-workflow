"""Speaker-attribution fix for female characters.

Hugo 2026-08-02: every movement prompt in the library was written off an
original video whose speaker was a MAN ("the man in the grey hoodie says to the
camera: …"). Swap a FEMALE character into a scene where TWO people are visible
and the video model has a choice — and it reliably picks the man, so the clip
ships with the wrong person's lips moving.

This module runs ONE Claude vision call per (scene × gender), right before the
clips are submitted. It sees the scene's swapped image plus the prompt about to
be sent, and rewrites only the speaker attribution: who says the line (described
by what is VISIBLE) and — when a second person is in frame — an explicit
instruction that they listen silently without moving their lips. Dialogue,
camera, motion, accent and every other clause are left verbatim.

Scope widened 2026-08-03 (Hugo). The gate used to also require the user to tick
👥 "två personer" on the scene, which left the single-person case unfixed: a
female character still got "He says … while he is …" verbatim. Measured, that
survives — Veo follows the start frame and renders the woman speaking (0.95
word-similarity on a clip whose prompt said "He") — but the text is plainly
wrong and it is the model, not the prompt, doing the saving. Now EVERY female
character gets the fix; the 👥 tick is passed on as a hint about the second
person rather than as the gate.

- **One call per (scene × gender), shared.** The result is reused for every
  character of that gender in that scene, so a 9-character run costs one call
  per scene, not nine (Hugo 2026-08-03: "en agent körning per scen per språk
  per kön"). That is why the agent is told to describe the speaker by POSITION
  and GENDER and never by clothing — outfits differ per character (outfit_mode
  = "character" dresses each one in their own clothes), so a clothing-based
  description would be wrong for everyone but the character whose frame the
  agent happened to see.
- **Female characters only.** A male character is skipped — the source prompt
  already attributes the line to a man.
- **Loud failure.** If the agent is unavailable or returns something unusable,
  the CLIP FAILS with a clear message (Hugo's standing rule: never silently ship
  a partial result). The user retries or unticks the scene. This is the same
  contract the per-character language localizer runs under.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

FEMALE = "female"
MALE = "male"
GENDERS = (MALE, FEMALE)


class SpeakerFixError(RuntimeError):
    """The speaker-attribution rewrite could not be produced. Fails the clip."""


SPEAKER_FIX_SYSTEM = """\
You edit ONE video-generation prompt so the right person is unmistakably the
speaker. The prompt was originally written for a video whose speaker was a MAN,
but the character now in the shot is a WOMAN. Left as-is, the video model
describes the speaker as male — and when a second person is in frame, it makes
that person talk instead.

You are given:
1. IMAGE — a frame the video will be generated from.
2. PROMPT — the text about to be sent to the video model.
3. SCENE NOTE — whether a second person is expected in the shot.

REUSE RULE — READ THIS FIRST. Your rewrite is sent for EVERY female character
in this scene, not only the woman in this particular image. Different women,
each wearing HER OWN clothes, will be composited into the same position in the
same shot. So describe the speaker ONLY by things that stay true for all of
them: her POSITION in the frame and the fact that she is a woman ("the woman on
the right", "the woman facing the camera"). NEVER describe her clothing, hair
colour, age or any other personal trait — that would be wrong for every
character except the one in this image.

Look at the IMAGE and identify where the character stands (she is the subject
of the shot; any other person is secondary). Then rewrite the PROMPT with these
rules:

CHANGE ONLY THESE THINGS:
- Every reference to WHO IS SPEAKING must point to her by POSITION, e.g. "the
  woman on the right". Never say "the character", "the woman from the
  reference", "person 1", or use a name — the video model cannot see names,
  only the frame. When she is the ONLY person in frame, "the woman" is enough.
- Replace male words that refer to the speaker (man, guy, he, his, him, male
  voice, his voice) with the correct female equivalents.
- When a second person IS in frame, add ONE short sentence stating that the
  other person (again by POSITION only) listens silently and does not move
  their lips or speak.

CHANGE NOTHING ELSE:
- Keep the quoted dialogue EXACTLY as written, character for character —
  including its language, punctuation and any words that look like typos.
- Keep every other clause verbatim: camera, framing, motion, lighting, accent
  and pronunciation instructions, background/music directives, negatives.
- Do not add creative direction, new actions, new props or new shot types.
- Do not translate anything.

If the image shows only ONE person, still make the attribution explicitly
female and skip the silent-listener sentence entirely.

Return the complete rewritten prompt via the tool — not a diff, not commentary.
"""

SPEAKER_FIX_TOOL: dict = {
    "name": "submit_speaker_fix",
    "description": "Submit the rewritten prompt with the speaker attribution fixed.",
    "input_schema": {
        "type": "object",
        "required": ["prompt", "speaker", "changed"],
        "properties": {
            "prompt": {
                "type": "string",
                "description": "The COMPLETE rewritten prompt, ready to send to "
                               "the video model.",
            },
            "speaker": {
                "type": "string",
                "description": "How the speaking woman is described in the "
                               "rewritten prompt — POSITION only, e.g. 'the "
                               "woman on the right'.",
            },
            "changed": {
                "type": "boolean",
                "description": "False only when the prompt already attributed the "
                               "line unambiguously to her and needed no edit.",
            },
        },
    },
}


def is_female(gender: str | None) -> bool:
    """True for a character explicitly flagged female. Unset/unknown counts as
    male — the source prompts already speak about a man, so doing nothing is the
    safe default for a character whose gender was never chosen."""
    return (gender or "").strip().lower() == FEMALE


def normalize_gender(value: str | None) -> str | None:
    """Accept "male"/"female" (any case) and the UI's Swedish spellings; anything
    empty clears the flag. Unknown values raise so a typo can never look set in
    the UI while behaving as unset (Hugo: refuse loudly)."""
    v = (value or "").strip().lower()
    if v in ("", "none", "unknown"):
        return None
    if v in ("male", "man", "m", "manlig", "kille"):
        return MALE
    if v in ("female", "woman", "f", "kvinna", "kvinnlig", "tjej"):
        return FEMALE
    raise ValueError(f"Unknown gender '{value}' (male | female)")


def needs_speaker_fix(*, gender: str | None, scene_id: str | None,
                      two_person_scenes: list[str] | None = None) -> bool:
    """The whole gate in one place: a FEMALE character on any scene.

    `two_person_scenes` is accepted (and still forwarded to the agent as a
    hint) but no longer gates the run — see the module docstring. Male
    characters are never touched: the source prompts already speak about a man,
    so doing nothing is correct and free for them.
    """
    del two_person_scenes  # hint only; see is_two_person_scene()
    return bool(is_female(gender) and scene_id)


def is_two_person_scene(scene_id: str | None,
                        two_person_scenes: list[str] | None) -> bool:
    """Did the user tick 👥 for this scene? Passed to the agent so it knows to
    expect a second person; the agent also looks at the frame itself."""
    return bool(scene_id and scene_id in set(two_person_scenes or []))


def fix_speaker_attribution(prompt: str, image: Path, *,
                            character_name: str = "",
                            two_person: bool = False,
                            job_id: str | None = None) -> str:
    """Rewrite `prompt` so the woman in `image` is unmistakably the speaker.

    The result is REUSED for every female character in this scene, so the agent
    is told to describe her by position only — `character_name` is therefore no
    longer sent to the model (it is kept in the signature for callers/tests and
    ignored). `two_person` forwards the scene's 👥 tick as a hint.

    Returns the rewritten prompt (or the original when the agent reports it was
    already unambiguous). Raises `SpeakerFixError` on ANY failure — missing key,
    missing file, API error, no tool call, empty result — because shipping the
    unfixed prompt is exactly the bug this exists to prevent.
    """
    from character_swap.config import settings

    text = (prompt or "").strip()
    if not text:
        raise SpeakerFixError("empty movement prompt")
    if not settings.anthropic_api_key:
        raise SpeakerFixError(
            "ANTHROPIC_API_KEY saknas — talar-agenten kan inte köras "
            "(avmarkera 👥 för scenen för att animera utan den)")
    if not image.exists():
        raise SpeakerFixError(f"scene image missing on disk: {image}")

    try:
        from character_swap.clients import anthropic_client
        resp = anthropic_client.messages_with_tools(
            system=SPEAKER_FIX_SYSTEM,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": "IMAGE — the frame the video starts from:"},
                anthropic_client._file_to_image_block(image),
                {"type": "text",
                 "text": (
                     "SCENE NOTE: "
                     + ("a second person is expected in this shot — add the "
                        "silent-listener sentence."
                        if two_person else
                        "this shot is expected to show only the speaker.")
                     + f"\n\nPROMPT:\n{text}")},
            ]}],
            tools=[SPEAKER_FIX_TOOL],
            tool_choice={"type": "tool", "name": "submit_speaker_fix"},
            max_tokens=2000,
            temperature=0.0,
            model=settings.speaker_fix_model,
            job_id=job_id,
            phase="speaker_fix",
        )
    except Exception as e:  # noqa: BLE001 — re-raised as a loud clip failure
        raise SpeakerFixError(f"{type(e).__name__}: {e}") from e

    data = anthropic_client.extract_tool_call(resp, "submit_speaker_fix")
    if not data:
        raise SpeakerFixError("agenten svarade utan verktygsanrop")
    if not data.get("changed", True):
        logger.info("speaker fix (%s): prompt already unambiguous", job_id)
        return text
    out = str(data.get("prompt") or "").strip()
    if not out:
        raise SpeakerFixError("agenten returnerade en tom prompt")
    # A rewrite that lost most of the prompt dropped clauses it was told to keep
    # (accent, no-music, camera). Better to fail and let the user retry than to
    # animate a gutted prompt.
    if len(out) < len(text) * 0.5:
        raise SpeakerFixError(
            f"agenten kortade prompten från {len(text)} till {len(out)} tecken "
            "— texten verkar ha tappat instruktioner")
    return out
