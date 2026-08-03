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

import contextlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from character_swap import video_edit

# Shared canonical dialogue extractor (2026-06-26) — see video_edit.DIALOGUE_RE.
# This module's private `says:\s*[“"]([^”"]+)[”"]` copy truncated CTAs like
# `Comment "Skin" …` to just `Comment ` (a review caught it diverging from the
# fix applied to runner_reengineer + app.js), feeding expected_speech() a
# corrupted line → false "dialogue mismatch" QC rejections + misleading hints
# the moment VIDEO_QC is re-enabled. Sharing the regex was NOT enough: the
# extractor FUNCTION kept evolving (labeled `Dialogue: "…"` fallback f68651e,
# stage-direction stripping 70f3537) while this module's local extraction
# stood still — so expected_speech now delegates to the whole
# video_edit.extract_dialogue (2026-07-02). The alias below stays exported for
# the cross-module sync test.
_DIALOGUE_RE = video_edit.DIALOGUE_RE

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
    # WRONG LANGUAGE (Hugo 2026-08-02): the clip's audio is not in the language
    # the character is flagged with. Unlike a fuzzy "garbled a word" mismatch —
    # which Hugo deliberately runs FLAG-ONLY (VIDEO_QC_MAX_RETRIES=0) — this is
    # an unambiguous, whole-clip defect, so the runner always re-renders it
    # (`VIDEO_QC_LANGUAGE_MAX_RETRIES`) and fails the clip LOUDLY when the
    # re-renders don't take. Kept as its own flag rather than sniffing the
    # reason text.
    wrong_language: bool = False


def expected_speech(prompt: str) -> str:
    """Dialogue the clip is supposed to contain, extracted from the motion
    prompt. Delegates to the canonical `video_edit.extract_dialogue` (the
    single source of truth for every caller): says-clause(s) first, then the
    AI Director's labeled `AUDIO — Dialogue: "…"` / `Voice-over: "…"` form,
    with unspoken `(stage directions)` stripped from inside the quotes.
    "" when the prompt carries no dialogue (then the speech check is
    skipped)."""
    return video_edit.extract_dialogue(prompt or "")


# Number words → digits, so an engine that writes "ten seconds" scores the same
# as one that writes "10 seconds" (Hugo 2026-08-03). This is pure measurement
# hygiene: both transcriptions are CORRECT, and penalizing one cost real points
# in both directions — Scribe read "ten"/"zehn" where the prompt said "10"
# (English 0.983 instead of 1.000), whisper-1 read "Nummer 1" where the prompt
# said "Nummer eins" (German 0.942 instead of 1.000). Only the range that
# actually shows up in these scripts is covered; unknown words pass through.
_NUMBER_WORDS = {
    "en": ["zero", "one", "two", "three", "four", "five", "six", "seven",
           "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
           "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"],
    "de": ["null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben",
           "acht", "neun", "zehn", "elf", "zwölf", "dreizehn", "vierzehn",
           "fünfzehn", "sechzehn", "siebzehn", "achtzehn", "neunzehn",
           "zwanzig"],
    "es": ["cero", "uno", "dos", "tres", "cuatro", "cinco", "seis", "siete",
           "ocho", "nueve", "diez", "once", "doce", "trece", "catorce",
           "quince", "dieciséis", "diecisiete", "dieciocho", "diecinueve",
           "veinte"],
}
_NUMBERS = {w: str(n) for words in _NUMBER_WORDS.values()
            for n, w in enumerate(words)}
# German ordinals/inflections that appear as list markers ("Nummer eins").
_NUMBERS.update({"ein": "1", "eine": "1", "una": "1", "un": "1"})


def _norm_words(text: str) -> str:
    """Lowercase word tokens for comparison.

    Keeps NON-ASCII letters. The old `[^a-z0-9' ]` class deleted every umlaut
    and accent, which did not merely drop a character — it SPLIT the word:
    "Stärke" became the two tokens "st" + "rke", so every German and Spanish
    transcript was scored against a differently-tokenized expectation and lost
    points it never should have. `\\w` under Python 3's default unicode
    semantics keeps them whole. ß→ss is folded because the two spellings are
    the same word; accents are NOT folded, since "año" and "ano" are not.
    """
    cleaned = re.sub(r"[^\w' ]+", " ", text.lower().replace("ß", "ss"))
    return " ".join(_NUMBERS.get(w, w) for w in cleaned.split())


def speech_similarity(expected: str, heard: str) -> float:
    """Symmetric similarity of expected vs heard dialogue in [0, 1].

    Compared on WORD tokens with autojunk disabled. CHARACTER-level matching
    with difflib's default autojunk heuristic collapses to ~0 on transcripts
    >200 chars (frequent letters become "junk" and stop matching) — the exact
    root cause already fixed in `video_edit.caption_transcript_ratio`
    (2026-06-22) but missed here: a near-perfect Whisper transcript of a
    longer dialogue line scored below `video_qc_speech_threshold` and burned
    a full video regeneration on a good clip (audit 2026-07-03)."""
    a = _norm_words(expected).split()
    b = _norm_words(heard).split()
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


def check_speech(video: Path, expected: str, *, app_job_id: str | None = None
                 ) -> tuple[bool, str, float] | None:
    """Transcribe the clip and compare to the expected dialogue. Returns
    (ok, heard_text, similarity), or None when transcription is unavailable
    (no OpenAI key / API error) — callers skip the check. No language hint:
    `inspect_clip` owns that decision, since it is the caller that knows
    whether the character is 🗣 flagged."""
    got = _transcribe(video, expected, app_job_id=app_job_id)
    return None if got is None else got[:3]


def _transcribe(video: Path, expected: str, *, app_job_id: str | None = None,
                language: str | None = None,
                ) -> tuple[bool, str, float, str | None, str] | None:
    """Transcribe the clip for BOTH speech checks: (ok, heard, similarity,
    detected_language, heard_unhinted).

    For a 🗣 language-flagged character this runs TWO passes, and they are not
    interchangeable:

    * UNHINTED feeds the wrong-language check, which works by seeing the clip
      transcribe back to the ENGLISH source line. Tell the engine the audio is
      German and English audio comes back as German-ish nonsense that matches
      nothing — the check would go blind exactly when it matters.
    * HINTED feeds the similarity score, because the hint is worth real points
      on correct clips (German mean 0.571 → 0.602, and 0.41 → 1.00 on a clip
      Scribe had otherwise read as Dutch).

    Gating on the engine's own detected language instead would be cheaper and
    is tempting — but measured, neither engine is good enough for it:
    whisper-1 called 4 of 20 German clips English, Scribe called 3 of 20 Dutch
    or English. `detected` is returned for display only.

    English characters pass no hint, so they still cost exactly one call.
    """
    from character_swap.config import settings
    if not expected.strip() or not settings.openai_api_key:
        return None
    try:
        from character_swap import video_edit
        words, detected = video_edit.transcribe_detailed(video, job_id=app_job_id)
        heard_unhinted = " ".join(w.text for w in words)
        heard = heard_unhinted
        if language:
            h_words, h_detected = video_edit.transcribe_detailed(
                video, job_id=app_job_id, language=language)
            hinted = " ".join(w.text for w in h_words)
            # A hinted pass that comes back EMPTY is a failure, not a verdict —
            # keep the unhinted text rather than scoring the clip against "".
            if hinted.strip():
                heard, detected = hinted, h_detected
        sim = speech_similarity(expected, heard)
        return (sim >= settings.video_qc_speech_threshold, heard, sim, detected,
                heard_unhinted)
    except Exception:
        return None


@contextlib.contextmanager
def _sample_frames(video: Path, n: int = 4):
    """Yield up to `n` frame JPEGs sampled evenly across the clip. The frames
    live in a private vqc_* temp dir that is ALWAYS removed on exit — every
    visual-QC inspection used to leak the dir + JPEGs for the process's
    lifetime."""
    from character_swap.video_edit import _probe_duration
    dur = max(0.1, _probe_duration(video))
    tmp = Path(tempfile.mkdtemp(prefix="vqc_"))
    try:
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
        yield frames
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def check_visual(video: Path, *, app_job_id: str | None = None) -> ClipVerdict | None:
    """One Claude vision call over sampled frames. None = unavailable."""
    from character_swap.config import settings
    if not settings.anthropic_api_key:
        return None
    try:
        from character_swap.clients import anthropic_client
        with _sample_frames(video) as frames:
            if not frames:
                return None
            content: list[dict] = [
                {"type": "text",
                 "text": f"{len(frames)} frames sampled evenly from one clip, in order:"},
            ]
            # _file_to_image_block reads + base64s each frame NOW, so the
            # temp dir can be reclaimed before the (slow) vision call.
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
                 app_job_id: str | None = None,
                 expected_language: str | None = None,
                 original_speech: str | None = None) -> ClipVerdict | None:
    """Combined clip QC: language (when the character is language-flagged) +
    speech (when dialogue is expected) + visual.
    None = no check could run (treat as skip).

    `expected_language` + `original_speech` arm the wrong-language check:
    the character's 🗣 flag and the ENGLISH line the prompt carried before
    localization. A clip that transcribes back to that English line spoke the
    wrong language (Hugo 2026-08-02)."""
    from character_swap.config import settings
    if not settings.video_qc_enabled:
        return None

    ran_any = False
    expected = expected_speech(movement_prompt)
    # The hint is only passed for a language-flagged character — see
    # `_transcribe` for why that costs a second, UNHINTED pass there.
    from character_swap import reengineer as _re
    _spec = _re.SPOKEN_LANGUAGES.get((expected_language or "").lower())
    speech = _transcribe(video, expected, app_job_id=app_job_id,
                         language=_spec.code if _spec else None)
    if speech is not None:
        ran_any = True
        ok, heard, sim, detected, heard_unhinted = speech
        # LANGUAGE FIRST — it outranks the fuzzy word-similarity check. A clip
        # that says the ORIGINAL ENGLISH line instead of the translated one is
        # not a "garbled TTS" case (which Hugo runs flag-only); it is unusable,
        # and reporting it as a similarity miss buried the real cause behind a
        # 0.00 score (re_b3170d2118: 8 of 10 German clips, every one of them
        # mislabeled "dialogue mismatch").
        #
        # The signal is a MATCH AGAINST THE ENGLISH SOURCE LINE, which we still
        # have — not Whisper's own `language` field. Measured on the real clips
        # from that run, that field is unusable here: it reported "english" for
        # vd_26334d, whose audio is plainly German ("Dieser Punkt unter der
        # Sutna ist direkt mit dem Lymphsystem…"). Gating on it would have
        # re-rendered and then FAILED correct German clips — worse than the bug.
        # Matching the English line cannot go wrong the same way: a clip
        # speaking German does not transcribe into the English sentence.
        #
        # It is scored on the UNHINTED transcript (2026-08-03): the hinted pass
        # is told the audio is German, so English audio comes back as German-ish
        # nonsense that matches the English line at ~0 — the check would go
        # blind on exactly the clips it exists to catch.
        spec = _spec
        if spec is not None and (original_speech or "").strip():
            en_sim = speech_similarity(original_speech, heard_unhinted)
            if en_sim >= settings.video_qc_language_threshold and en_sim > sim:
                return ClipVerdict(
                    passed=False,
                    reason=(f"fel språk: klippet talar engelska, inte "
                            f"{spec.name_en} (matchar originalrepliken "
                            f"{en_sim:.2f} mot {sim:.2f}) — hörde "
                            f"“{heard_unhinted[:100]}”"),
                    corrective_hint=(
                        f"CRITICAL: every spoken word must be in "
                        f"{spec.name_en}. The previous take spoke ENGLISH. Say "
                        f'exactly, {spec.attribution}: "{expected}" — never in '
                        f"English."),
                    wrong_language=True,
                )
        if not ok:
            return ClipVerdict(
                passed=False,
                reason=(f"dialogue mismatch (similarity {sim:.2f}): expected "
                        f"“{expected}” but heard “{heard[:120]}”"),
                corrective_hint=(f'The person must say exactly: "{expected}" — '
                                 "pronouncing every word clearly and correctly."),
            )

    # The anatomy/vision check is skippable independently (VIDEO_QC_VISUAL=0)
    # so a run can flag ONLY dialogue mismatches without paying for a vision
    # call per clip (Hugo 2026-07-03: "bara repliken").
    if settings.video_qc_visual_enabled:
        visual = check_visual(video, app_job_id=app_job_id)
        if visual is not None:
            ran_any = True
            if not visual.passed:
                return visual

    return ClipVerdict(passed=True, reason="", corrective_hint="") if ran_any else None
