"""Labeled-dialogue extraction (Hugo 2026-06-30).

A `use_director` Reengineer run (re_87e851d21f) wrote every scene's spoken line
inside the AI Director's structured `AUDIO — … Dialogue: "…"` block, with NO
`says` verb. `extract_dialogue` only recognised the analyst's `… says …: "…"`
idiom, so it returned "" for ALL four scenes. That silently disengaged BOTH
caption safety nets (per-clip alignment AND the even-timed script fallback both
gate on a known line) → one character (Chang) whose Veo/Kling voice Whisper
couldn't read shipped captions on only the first clip (11 words / 3.2 s of a
~22 s reel) while the others happened to transcribe fully.

`extract_dialogue` must now recover the labeled form too — WITHOUT mistaking a
quoted PROP (`bottle labeled "Heinz White Vinegar"`) or a `Voice:` descriptor
for speech, and WITHOUT regressing the says-clause path.
"""
from __future__ import annotations

from character_swap import video_edit
from character_swap.runner_reengineer import _spoken_text


# The actual scene-0 motion prompt from re_87e851d21f (trimmed but verbatim in
# structure): a full SHOT/SUBJECT/.../AUDIO/STYLE block with quoted props in
# VISUAL DETAILS and the spoken line only in the AUDIO block's `Dialogue:`.
SCENE0_PROMPT = (
    "No subtitles. No music.\n\n"
    "SHOT — Close-up, intimate framing of the man's face and the woman's neck.\n"
    "VISUAL DETAILS — Clear plastic bottle labeled \"Heinz White Vinegar\" "
    "tilted to pour; visible droplets running down the woman's neck.\n"
    "ACTION AND CAMERA MOTION — Man pours vinegar while looking into the camera "
    "and speaks enthusiastically; camera static.\n"
    "AUDIO — Voice: male, enthusiastic, American accent; Dialogue: \"Pour "
    "vinegar on your skin tags and just watch what happens\"; primary sounds "
    "include liquid pouring; no music.\n"
    "STYLE — Hyper-realistic; 4K; no text overlays, subtitles, or captions."
)


def test_extracts_labeled_dialogue_from_audio_block():
    out = video_edit.extract_dialogue(SCENE0_PROMPT)
    assert out == "Pour vinegar on your skin tags and just watch what happens"


def test_does_not_capture_quoted_props_or_voice_descriptor():
    out = video_edit.extract_dialogue(SCENE0_PROMPT)
    # The brand on the bottle + the `Voice:` line are NOT spoken.
    assert "Heinz" not in out and "Vinegar" not in out
    assert "American accent" not in out and "Voice" not in out


def test_labeled_cta_with_inner_single_quotes_survives():
    # Scene 3's CTA — inner single quotes must not truncate the line.
    prompt = (
        "AUDIO — Voice: enthusiastic male; Language: English; Dialogue: "
        "\"Comment 'Skin' and I'll send you the one thing I give my clients\"; "
        "Ambient: quiet outdoor sounds."
    )
    out = video_edit.extract_dialogue(prompt)
    assert out == "Comment 'Skin' and I'll send you the one thing I give my clients"


def test_voiceover_and_spoken_line_labels():
    assert video_edit.extract_dialogue(
        'Voice-over: "Stop scrolling and listen."') == "Stop scrolling and listen."
    assert video_edit.extract_dialogue(
        'Spoken line: "Do this every morning."') == "Do this every morning."


def test_multiple_labeled_lines_join():
    prompt = ('Dialogue: "First line here." ... later ... '
              'Dialogue: "Second line here."')
    assert video_edit.extract_dialogue(prompt) == \
        "First line here. Second line here."


def test_says_clause_takes_precedence_when_present():
    # The labeled branch only runs when the canonical says-clause matched
    # nothing, so a says-clause is never double-counted. (Real analyst prompts
    # carry ONLY a says-clause; real Director prompts carry ONLY a label.)
    prompt = 'While pouring, he says to the camera: "Watch what happens next."'
    assert video_edit.extract_dialogue(prompt) == "Watch what happens next."


def test_says_clause_path_unregressed():
    prompt = 'He says: "Never refrigerate your apples." He says nothing else.'
    assert video_edit.extract_dialogue(prompt) == "Never refrigerate your apples."


def test_labeled_line_strips_stage_directions():
    prompt = 'Dialogue: "This is raw honey (while he points at the jar)."'
    out = video_edit.extract_dialogue(prompt)
    assert out == "This is raw honey."
    assert "(" not in out and "points" not in out


def test_silent_scene_returns_empty():
    # No quoted spoken line → "" (so the clip is treated as silent, not given
    # a phantom caption).
    assert video_edit.extract_dialogue(
        "AUDIO — ambient outdoor sounds only; no dialogue. STYLE — realistic."
    ) == ""
    assert video_edit.extract_dialogue("Dialogue: none.") == ""


def test_reengineer_spoken_text_gets_labeled_line():
    # The reengineer caption path (per-clip alignment + script hint) routes
    # through _spoken_text → extract_dialogue, so it must recover the label too.
    assert _spoken_text({"motion_prompt": SCENE0_PROMPT}) == \
        "Pour vinegar on your skin tags and just watch what happens"
