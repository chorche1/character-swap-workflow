"""QC for generated video clips (Swap Step 5 + Reengineer).

Two independent checks per clip, both observed failure modes in production:

1. SPEECH — Kling's native TTS sometimes garbles words ("baking goda" for
   "baking soda"). When the motion prompt contains expected dialogue
   (`The person says: "..."`), the finished clip is Whisper-transcribed and
   fuzzy-compared against it. Similarity below
   `settings.video_qc_speech_threshold` fails the clip.

2. VISUAL — impossible motion/anatomy (limbs passing through objects or the
   body, extra/duplicated limbs). N frames are sampled evenly and judged in
   ONE cheap Claude vision call. Mid-motion frames can look odd, so the judge
   is told to fail only on clear physical impossibilities.

On failure the runner resubmits the clip (with the verdict appended to the
prompt as a corrective hint) up to `settings.video_qc_max_retries` times —
video is the expensive step, so the default is a single retry. Like the image
QC: unavailability (missing keys, API errors) NEVER blocks the pipeline, and
after exhausted retries the LAST clip is kept with qc_status="failed".
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

_DIALOGUE_RE = re.compile(r'says:\s*[“"]([^”"]+)[”"]')

VISUAL_QC_SYSTEM = """\
You are a strict quality inspector for AI-generated video clips. You receive
several frames sampled evenly from ONE short clip of a person doing an
everyday action (UGC-style phone footage).

FAIL the clip ONLY on clear physical impossibilities:
- a limb passing THROUGH another body part or through a solid object
- extra, missing or duplicated limbs/hands/heads visible in a frame
- a face or body that has collapsed into smeared, non-human geometry
- objects morphing into different objects mid-clip

Ordinary motion blur, a momentarily odd hand pose, soft focus, or slightly
unnatural movement timing are NOT failures — frames are snapshots of motion
and can look awkward. Be decisive: borderline clips PASS.

When you fail, give a short concrete reason and a one-sentence corrective
instruction for the video model (e.g. "Keep both hands on the blender lid;
arms must move naturally without intersecting the body.").
"""

VISUAL_QC_TOOL: dict = {
    "name": "submit_clip_inspection",
    "description": "Submit the QC verdict for the video clip frames.",
    "input_schema": {
        "type": "object",
        "required": ["passed", "reason", "corrective_hint"],
        "properties": {
            "passed": {"type": "boolean"},
            "reason": {"type": "string"},
            "corrective_hint": {"type": "string"},
        },
    },
}


@dataclass
class ClipVerdict:
    passed: bool
    reason: str
    corrective_hint: str


def expected_speech(prompt: str) -> str:
    """Dialogue the clip is supposed to contain, extracted from the motion
    prompt's `The person says: "..."` clause(s). "" when the prompt carries
    no dialogue (then the speech check is skipped)."""
    return " ".join(m.group(1).strip() for m in _DIALOGUE_RE.finditer(prompt or ""))


def _norm_words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split())


def speech_similarity(expected: str, heard: str) -> float:
    return SequenceMatcher(None, _norm_words(expected), _norm_words(heard)).ratio()


def check_speech(video: Path, expected: str, *, app_job_id: str | None = None
                 ) -> tuple[bool, str, float] | None:
    """Whisper-transcribe the clip and compare to the expected dialogue.
    Returns (ok, heard_text, similarity), or None when transcription is
    unavailable (no OpenAI key / API error) — callers skip the check."""
    from character_swap.config import settings
    if not expected.strip() or not settings.openai_api_key:
        return None
    try:
        from character_swap import video_edit
        words = video_edit.transcribe_words(video, job_id=app_job_id)
        heard = " ".join(w.text for w in words)
        sim = speech_similarity(expected, heard)
        return sim >= settings.video_qc_speech_threshold, heard, sim
    except Exception:
        return None


def _sample_frames(video: Path, n: int = 4) -> list[Path]:
    from character_swap.video_edit import _probe_duration
    dur = max(0.1, _probe_duration(video))
    tmp = Path(tempfile.mkdtemp(prefix="vqc_"))
    frames: list[Path] = []
    for i in range(n):
        at = dur * (i + 0.5) / n
        dest = tmp / f"f{i}.jpg"
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", "-ss", f"{at:.3f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "4", str(dest)],
            capture_output=True, text=True,
        )
        if proc.returncode == 0 and dest.exists():
            frames.append(dest)
    return frames


def check_visual(video: Path, *, app_job_id: str | None = None) -> ClipVerdict | None:
    """One Claude vision call over sampled frames. None = unavailable."""
    from character_swap.config import settings
    if not settings.anthropic_api_key:
        return None
    try:
        from character_swap.clients import anthropic_client
        frames = _sample_frames(video)
        if not frames:
            return None
        content: list[dict] = [
            {"type": "text",
             "text": f"{len(frames)} frames sampled evenly from one clip, in order:"},
        ]
        for f in frames:
            content.append(anthropic_client._file_to_image_block(f))
        resp = anthropic_client.messages_with_tools(
            system=VISUAL_QC_SYSTEM,
            messages=[{"role": "user", "content": content}],
            tools=[VISUAL_QC_TOOL],
            tool_choice={"type": "tool", "name": "submit_clip_inspection"},
            max_tokens=400,
            temperature=0.0,
            model=settings.swap_qc_model,
            job_id=app_job_id,
            phase="video_qc",
        )
        data = anthropic_client.extract_tool_call(resp, "submit_clip_inspection")
        if data is None or "passed" not in data:
            return None
        return ClipVerdict(passed=bool(data["passed"]),
                           reason=str(data.get("reason") or ""),
                           corrective_hint=str(data.get("corrective_hint") or ""))
    except Exception:
        return None


def inspect_clip(video: Path, *, movement_prompt: str,
                 app_job_id: str | None = None) -> ClipVerdict | None:
    """Combined clip QC: speech (when dialogue is expected) + visual.
    None = no check could run (treat as skip)."""
    from character_swap.config import settings
    if not settings.video_qc_enabled:
        return None

    ran_any = False
    expected = expected_speech(movement_prompt)
    speech = check_speech(video, expected, app_job_id=app_job_id)
    if speech is not None:
        ran_any = True
        ok, heard, sim = speech
        if not ok:
            return ClipVerdict(
                passed=False,
                reason=(f"dialogue mismatch (similarity {sim:.2f}): expected "
                        f"“{expected}” but heard “{heard[:120]}”"),
                corrective_hint=(f'The person must say exactly: "{expected}" — '
                                 "pronouncing every word clearly and correctly."),
            )

    visual = check_visual(video, app_job_id=app_job_id)
    if visual is not None:
        ran_any = True
        if not visual.passed:
            return visual

    return ClipVerdict(passed=True, reason="", corrective_hint="") if ran_any else None
