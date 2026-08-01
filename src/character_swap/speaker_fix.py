"""Speaker-attribution fix for female characters in two-person scenes.

Hugo 2026-08-02: every movement prompt in the library was written off an
original video whose speaker was a MAN ("the man in the grey hoodie says to the
camera: …"). Swap a FEMALE character into a scene where TWO people are visible
and the video model has a choice — and it reliably picks the man, so the clip
ships with the wrong person's lips moving.

This module runs ONE Claude vision call per (female character × flagged scene),
right before the clip is submitted. It sees the scene's actual swapped image for
THAT character plus the prompt that is about to be sent, and rewrites only the
speaker attribution: who says the line (described by what is visible — position
in frame, clothing, hair) and an explicit instruction that the other person
listens silently without moving their lips. Dialogue, camera, motion, accent and
every other clause are left verbatim.

Deliberately narrow:

- **Opt-in per scene.** Only scenes the user ticked 👥 (`Job.two_person_scenes`)
  are inspected, so credits are never spent on single-person scenes.
- **Female characters only.** A male character in a flagged scene is skipped —
  the source prompt already attributes the line to a man.
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
but the character now in the shot is a WOMAN, and the image contains MORE THAN
ONE person. Left as-is, the video model makes the wrong person talk.

You are given:
1. IMAGE — the exact frame the video will be generated from.
2. PROMPT — the text about to be sent to the video model.
3. CHARACTER NAME — the name of the woman who must be the speaker.

Look at the IMAGE and identify the woman who is the character (she is the
subject of the shot; the other person or people are secondary). Then rewrite the
PROMPT with these rules:

CHANGE ONLY THESE THINGS:
- Every reference to WHO IS SPEAKING must point to her, described by what is
  actually VISIBLE in the image: her position in frame plus one or two concrete
  traits, e.g. "the woman on the left in the beige blazer". Never say "the
  character", "the woman from the reference", "person 1", or use her name — the
  video model cannot see names, only the frame.
- Replace male words that refer to the speaker (man, guy, he, his, him, male
  voice, his voice) with the correct female equivalents.
- Add ONE short sentence stating that the other person (described by what is
  visible) listens silently and does not move their lips or speak.

CHANGE NOTHING ELSE:
- Keep the quoted dialogue EXACTLY as written, character for character —
  including its language, punctuation and any words that look like typos.
- Keep every other clause verbatim: camera, framing, motion, lighting, accent
  and pronunciation instructions, background/music directives, negatives.
- Do not add creative direction, new actions, new props or new shot types.
- Do not translate anything.

If the image shows only ONE person, still make the attribution explicitly female
and skip the silent-listener sentence.

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
                               "rewritten prompt, e.g. 'the woman on the left in "
                               "the beige blazer'.",
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
                      two_person_scenes: list[str] | None) -> bool:
    """The whole gate in one place: a female character on a scene the user
    ticked 👥. Both conditions are required — ticking a scene does NOT make the
    male characters in the same run run the agent (Hugo 2026-08-02)."""
    if not is_female(gender):
        return False
    if not scene_id:
        return False
    return scene_id in set(two_person_scenes or [])


def fix_speaker_attribution(prompt: str, image: Path, *,
                            character_name: str,
                            job_id: str | None = None) -> str:
    """Rewrite `prompt` so the woman in `image` is unmistakably the speaker.

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
                 "text": (f"CHARACTER NAME: {character_name}\n\n"
                          f"PROMPT:\n{text}")},
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
