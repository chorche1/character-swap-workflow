"""
Reel batch-edit runner — anchor-first orchestration.

Given a ReelJob with N input frames, this module:

1. Renders frame 0 (the "anchor") using the user's full prompt + only the
   anchor's own input as a reference. This output establishes the new
   clothing color, background, and overall style for the whole batch.

2. Renders frames 1..N-1 in parallel (bounded by IMAGE_CONCURRENCY), each
   with refs = [anchor_output, input_frame]. The follower prompt instructs
   the model to copy style/clothes/background from the anchor and pose/
   composition from the input frame.

Provider dispatch mirrors `runner_media.run_image_gen` but is bespoke to
the reel flow: the input/output shape (N inputs → N outputs that share
style) is different enough that reusing `MediaGeneration` would be
awkward.

All state changes flow through `store().update_reel_job()` and `events.publish`
so the frontend can render a live grid as each frame lands.
"""
from __future__ import annotations

import asyncio
import base64
import secrets
from datetime import datetime
from pathlib import Path

from character_swap import events
from character_swap.call_log import record
from character_swap.clients import google_genai, openai_image
from character_swap.config import settings
from character_swap.images import atomic_write_bytes
from character_swap.models import (
    ReelFrame,
    ReelFrameStatus,
    ReelJob,
    ReelJobStatus,
    ReelPreset,
)
from character_swap.state import store


DEFAULT_PRESET_NAME = "UGC reel (visual consistency)"
DEFAULT_PRESET_BASELINE = (
    "No text overlays anywhere in the image. "
    "Every output image must use exactly the same camera angle as the source frame it came from. "
    "Every output image must show the subject(s) in new clothing — pick one new clothing color "
    "for the anchor and reuse the SAME color across every other output image. "
    "Every output image must use a new background — pick one new background for the anchor "
    "and reuse the SAME background across every other output image. "
    "Preserve the subject's identity, pose, and facial features exactly."
)

FOLLOWER_PROMPT_SUFFIX = (
    "\n\nCONSISTENCY RULES (critical — non-negotiable):\n"
    "1. CLOTHING — Every person must wear the EXACT same clothing as in the FIRST reference image. "
    "Identical color (same hue, same saturation, same shade), identical garment style, identical "
    "fabric and detailing. Do NOT introduce new colors or different garments.\n"
    "2. BACKGROUND — The setting must be IDENTICAL to the FIRST reference image. Same location, "
    "same flag/decor, same plants and props, same wall/floor materials, same time of day, same "
    "weather. Do not invent or change ANY background element.\n"
    "3. LIGHTING — Same direction, intensity, and color temperature as the FIRST reference image.\n"
    "4. COLOR GRADING — Match the overall mood and color palette of the FIRST reference image exactly.\n"
    "5. The ONLY things you copy from the SECOND reference image are: subject pose, framing/crop, "
    "facial expression, and which props/objects are visible. Nothing else.\n"
    "The output must read as another shot from the SAME continuous video as the FIRST reference image."
)

QUALITY_SUFFIX = (
    "\n\nQUALITY REQUIREMENTS (critical):\n"
    "- Output must be photorealistic, ultra-HD, and razor-sharp.\n"
    "- Even if the reference images are low resolution, blurry, or compressed, the OUTPUT must "
    "look like professional-grade DSLR photography: crisp focus, true-to-life skin texture and "
    "pores, detailed fabric weave, natural specular highlights, accurate shadows.\n"
    "- No motion blur, no compression artifacts, no over-smoothing, no plastic skin, no AI "
    "tell-tales (fused fingers, warped text, extra limbs).\n"
    "- Color must be accurate and rich, not washed out."
)


def seed_default_preset() -> None:
    """Ensure the default 'UGC reel' preset exists. Idempotent.

    Runs at startup. If the user has already created a custom default
    (is_default=True), we don't overwrite it — we just ensure that AT LEAST
    one preset exists so the UI's picker always has something."""
    s = store()
    existing = s.list_reel_presets()
    if any(p.is_default for p in existing):
        return
    if existing:
        # User has presets but none flagged default — leave alone.
        return
    s.add_reel_preset(ReelPreset(
        preset_id="rp_" + secrets.token_hex(5),
        name=DEFAULT_PRESET_NAME,
        baseline_prompt=DEFAULT_PRESET_BASELINE,
        is_default=True,
    ))


# --- helpers ------------------------------------------------------------------------

def _frame_input_path(job: ReelJob, frame: ReelFrame) -> Path:
    return _reel_dir(job.job_id) / frame.input_filename


def _frame_output_path(job: ReelJob, frame: ReelFrame) -> Path:
    return _reel_dir(job.job_id) / (frame.output_filename or f"output_{frame.sort_index:02d}.png")


def _reel_dir(job_id: str) -> Path:
    p = settings.output_dir / "reel" / job_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def _openai_size_for(aspect: str | None) -> str:
    if not aspect:
        return settings.image_size
    return {
        "1:1":  "1024x1024",
        "9:16": "1024x1792",
        "16:9": "1792x1024",
        "4:5":  "1024x1280",
    }.get(aspect, settings.image_size)


def _generate_one(*, prompt: str, refs: list[Path], model: str,
                  aspect: str | None, job_id: str) -> bytes:
    """Dispatch one image-edit call to the chosen provider. Synchronous —
    callers wrap in `asyncio.to_thread`.

    Every reel-tab render gets the `QUALITY_SUFFIX` appended unconditionally:
    the user wants HD/photorealistic regardless of how compressed/blurry
    their reference frames are.
    """
    full_prompt = prompt + QUALITY_SUFFIX
    if model == "gpt-image":
        return openai_image.generate(
            prompt=full_prompt, reference_images=refs,
            phase="reel", character="reel",
            size=_openai_size_for(aspect), job_id=job_id,
            quality="high",
        )
    if model in {"nano-banana", "nano-banana-pro"}:
        # Pass the SLUG (not a hardcoded google model name). The client at
        # google_genai.py maps slugs → current Google model names in one
        # place so this dispatch doesn't go stale when Google renames.
        return google_genai.generate_nano_banana(
            prompt=full_prompt, reference_images=refs,
            aspect_ratio=aspect, app_job_id=job_id,
            model=model,
        )
    raise ValueError(
        f"Reel runner does not yet support image_model={model!r}. "
        f"Currently supported: gpt-image, nano-banana, nano-banana-pro."
    )


def _persist_job(job: ReelJob) -> None:
    job.updated_at = datetime.utcnow()
    store().update_reel_job(job)


VISION_PROMPT = (
    "You are preparing a concrete style sheet for an image-edit AI that needs "
    "to render more frames matching THIS image's exact look.\n\n"
    "Describe the visual style of the attached image in highly specific terms. "
    "Cover:\n"
    "1. CLOTHING — for EACH visible person, name the garment, the exact color "
    "(use both a plain-English shade name AND a hex code like #1E3A5F), the "
    "cut/fit/collar/buttons/pattern. Be specific enough that the editor AI "
    "can reproduce the same garment on different camera angles.\n"
    "2. BACKGROUND — name the location type, materials, and every visible "
    "fixed element (flag, fence, plants, furniture, walls, sky). Include "
    "dominant color names with hex codes.\n"
    "3. LIGHTING — direction (e.g. 'warm afternoon sun from camera-left'), "
    "intensity, color temperature.\n"
    "4. SKIN / HAIR — for each person, skin tone in plain-English (no hex), "
    "hair color and style.\n\n"
    "Output as 3–6 short paragraphs, plain prose, no headers, no markdown. "
    "Keep it under 200 words. Be CONCRETE — no vague words like 'casual', "
    "'natural', 'simple'."
)


INPUT_DESC_PROMPT = (
    "Describe the composition of THIS image so another AI can reproduce the "
    "same composition with different clothing/background. Output a single "
    "short paragraph (under 80 words) covering ONLY these structural facts:\n"
    "- Number of people visible and their approximate position in the frame "
    "(e.g. 'one man centered', 'man behind woman, both head-and-shoulders').\n"
    "- Framing/crop (wide shot, medium, close-up, extreme close-up, hands-only).\n"
    "- Visible props/objects in the subjects' hands or near them.\n"
    "- Camera angle (eye-level, low angle, high angle, over-the-shoulder).\n"
    "Do NOT describe clothing colors, hair, skin, or background environment — "
    "those will be set separately. Only structural composition facts."
)


DRIFT_AUDIT_PROMPT = (
    "You are auditing image consistency for a video-reskin pipeline. You see "
    "two images: ANCHOR (the target style) and CANDIDATE (a just-rendered frame).\n\n"
    "Check whether the CANDIDATE matches the ANCHOR on these specific dimensions:\n"
    "1. CLOTHING_COLOR — do all visible people wear the same color clothing as in ANCHOR?\n"
    "2. CLOTHING_TYPE — same garment type (shirt vs jacket vs t-shirt) on each person?\n"
    "3. BACKGROUND — same environment (location, fixed objects like flag/fence/plants)?\n"
    "4. PERSON_COUNT — same number of people visible (it is OK for the CANDIDATE "
    "to crop tighter, but it is NOT OK to be missing a person who is plausibly there).\n\n"
    "Reply as a strict JSON object with this exact shape:\n"
    '{"severity": "none"|"minor"|"major", '
    '"drifts": [{"field": "...", "anchor": "...", "candidate": "..."}]}\n\n'
    "Use 'none' if the candidate is consistent. 'minor' for subtle hue/shade differences. "
    "'major' for wrong color (different name), wrong garment type, missing person, "
    "or wrong background environment. List up to 4 drifts. Be strict — favor "
    "'major' over 'minor' when in doubt. Output ONLY the JSON, no prose, no markdown."
)


def _file_to_data_url(path: Path) -> str:
    with path.open("rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    suffix = path.suffix.lower().lstrip(".") or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    return f"data:image/{suffix};base64,{b64}"


def _vision_call(*, prompt: str, image_paths: list[Path], phase: str,
                  job_id: str | None = None, max_tokens: int = 500,
                  response_format: dict | None = None) -> str | None:
    """Single-roundtrip gpt-4o call with N images + a text prompt.
    Returns the response text, or None on any failure (non-fatal)."""
    try:
        client = openai_image._client()
        content: list = [{"type": "text", "text": prompt}]
        for p in image_paths:
            content.append({
                "type": "image_url",
                "image_url": {"url": _file_to_data_url(p)},
            })
        kwargs: dict = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": content}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
        if response_format:
            kwargs["response_format"] = response_format
        with record(phase=phase, model="gpt-4o", character="reel", job_id=job_id):
            response = client.chat.completions.create(**kwargs)
        return (response.choices[0].message.content or "").strip() or None
    except Exception:
        return None


def _describe_anchor(anchor_path: Path, *, job_id: str | None = None) -> str | None:
    """Ask gpt-4o to write a hard style spec from the anchor image. Returns
    the description string, or None if the call fails."""
    return _vision_call(prompt=VISION_PROMPT, image_paths=[anchor_path],
                         phase="reel_anchor_describe", job_id=job_id,
                         max_tokens=500)


def _describe_input_frame(input_path: Path, *, job_id: str | None = None) -> str | None:
    """gpt-4o vision pass on a single input frame, focused on composition
    (people count, framing, props, camera angle). The resulting text is
    injected into the follower prompt so the model is explicitly told
    which composition to preserve. Returns None on failure (non-fatal)."""
    return _vision_call(prompt=INPUT_DESC_PROMPT, image_paths=[input_path],
                         phase="reel_input_describe", job_id=job_id,
                         max_tokens=180)


def _audit_drift(anchor_path: Path, candidate_path: Path,
                  *, job_id: str | None = None) -> dict | None:
    """Compare a freshly-rendered follower against the anchor and return a
    structured drift report:
        {"severity": "none"|"minor"|"major", "drifts": [{field, anchor, candidate}, ...]}
    Returns None on any failure (caller treats as 'no audit').
    """
    raw = _vision_call(
        prompt=DRIFT_AUDIT_PROMPT,
        image_paths=[anchor_path, candidate_path],
        phase="reel_audit_drift",
        job_id=job_id,
        max_tokens=400,
        response_format={"type": "json_object"},
    )
    if not raw:
        return None
    import json as _json
    try:
        data = _json.loads(raw)
        sev = data.get("severity")
        if sev not in {"none", "minor", "major"}:
            return None
        return data
    except Exception:
        return None


async def _publish(job_id: str, kind: str, **payload) -> None:
    await events.publish(job_id, {"type": kind, **payload})


# --- orchestration ------------------------------------------------------------------

def _find_anchor(job: ReelJob) -> ReelFrame:
    return next((f for f in job.frames if f.is_anchor), job.frames[0])


def _follower_refs(job: ReelJob, frame: ReelFrame, anchor_output: Path) -> list[Path]:
    """Build the reference list passed to the image model for a follower.
    For nano-banana-pro: include the anchor PLUS every input frame in the
    reel — Gemini's batch-coherence depends on seeing the whole set. For
    other models: [anchor, this_frame_input] (the conventional 2-ref shape)."""
    if job.image_model == "nano-banana-pro":
        # Anchor first, then every input. The model sees the whole reel
        # context per call, which is what its batch coherence is tuned for.
        refs = [anchor_output]
        for f in sorted(job.frames, key=lambda x: x.sort_index):
            if not f.is_anchor:
                refs.append(_frame_input_path(job, f))
        return refs
    return [anchor_output, _frame_input_path(job, frame)]


def _follower_prompt(job: ReelJob, frame: ReelFrame | None = None) -> str:
    """Compose the prompt for a follower frame. Includes:
      • the user's full_prompt (preset baseline + their custom tweak)
      • the vision-extracted anchor description (if available)
      • this frame's input composition description (if available)
      • FOLLOWER_PROMPT_SUFFIX (the strict consistency rules)
    """
    parts = [job.full_prompt]
    if job.anchor_description:
        parts.append(
            "\n\nEXACT STYLE TO REPRODUCE — these specifics were extracted "
            "from the FIRST reference image and are non-negotiable:\n"
            + job.anchor_description
        )
    if frame is not None and frame.input_description:
        parts.append(
            "\n\nCOMPOSITION TO PRESERVE — the SECOND reference image has "
            "this structure, and the output MUST keep it intact:\n"
            + frame.input_description
            + "\nDo not add or remove people, do not change the framing or "
            "camera angle, do not invent new props."
        )
    parts.append(FOLLOWER_PROMPT_SUFFIX)
    return "".join(parts)


def _correction_prompt(job: ReelJob, frame: ReelFrame,
                        drifts: list[dict]) -> str:
    """Prompt for the second pass when the drift audit flagged something.
    The model sees [anchor, first_pass_output] as refs and is told to fix
    ONLY the listed drifts.
    """
    parts = [job.full_prompt]
    if job.anchor_description:
        parts.append(
            "\n\nTARGET STYLE (from FIRST reference image):\n"
            + job.anchor_description
        )
    if frame.input_description:
        parts.append(
            "\n\nCOMPOSITION (preserve from the SECOND reference, which is "
            "the previous-pass output):\n" + frame.input_description
        )
    bullet_drifts = "\n".join(
        f"- {d.get('field', '?')}: should be «{d.get('anchor', '?')}» "
        f"but is «{d.get('candidate', '?')}»"
        for d in drifts[:6]
    )
    parts.append(
        "\n\nCORRECTIONS REQUIRED — fix ONLY these specific drifts from the "
        "TARGET STYLE while keeping everything else from the SECOND reference:\n"
        + bullet_drifts
        + "\nThe SECOND reference is the previous render of this frame; "
        "preserve its composition and pose. Only change the listed drifts to "
        "match the FIRST reference."
    )
    return "".join(parts)


def _refine_prompt(job: ReelJob, frame: ReelFrame, correction: str) -> str:
    """Prompt for a user-driven refine pass — they typed a specific
    correction like 'shirt should be navy not green'."""
    parts = [job.full_prompt]
    if job.anchor_description:
        parts.append(
            "\n\nTARGET STYLE (from FIRST reference image):\n"
            + job.anchor_description
        )
    parts.append(
        "\n\nUSER CORRECTION — apply this targeted fix while keeping "
        "everything else from the SECOND reference (the previous render) "
        "exactly as it is:\n"
        + correction.strip()
    )
    return "".join(parts)


async def run_reel_anchor(job_id: str) -> None:
    """Render the anchor frame only. On success, sets status to
    AWAITING_ANCHOR_APPROVAL so the user can review before paying for
    followers. On failure, sets FAILED."""
    s = store()
    job = s.get_reel_job(job_id)
    if job is None:
        return
    if not job.frames:
        job.status = ReelJobStatus.FAILED
        job.error = "No frames"
        _persist_job(job)
        return

    job.status = ReelJobStatus.GENERATING_ANCHOR
    job.error = None
    _persist_job(job)
    await _publish(job_id, "job", status=str(job.status))

    # Vision pre-pass: describe the composition of each input frame so the
    # follower prompts can be explicit about preserving people/props/framing.
    # Runs in parallel since each call is independent.
    needs_desc = [f for f in job.frames if not f.input_description]
    if needs_desc:
        descs = await asyncio.gather(*(
            asyncio.to_thread(_describe_input_frame,
                               _frame_input_path(job, f), job_id=job_id)
            for f in needs_desc
        ))
        for f, d in zip(needs_desc, descs):
            if d:
                f.input_description = d
        _persist_job(job)

    anchor = _find_anchor(job)
    ok = await _run_frame(job, anchor,
                           refs=[_frame_input_path(job, anchor)],
                           prompt=job.full_prompt)
    if not ok:
        job.status = ReelJobStatus.FAILED
        job.error = anchor.error or "Anchor frame failed"
        _persist_job(job)
        await _publish(job_id, "job", status=str(job.status), error=job.error)
        return

    # Vision pass: ask gpt-4o to write a hard style spec from the anchor
    # output. Followers get this prepended to their prompt so exact colors
    # become a constraint rather than a vague reference.
    anchor_output = _frame_output_path(job, anchor)
    description = await asyncio.to_thread(_describe_anchor, anchor_output, job_id=job_id)
    if description:
        job.anchor_description = description

    job.status = ReelJobStatus.AWAITING_ANCHOR_APPROVAL
    _persist_job(job)
    await _publish(job_id, "job", status=str(job.status),
                    anchor_description=job.anchor_description)


async def run_reel_followers(job_id: str) -> None:
    """Render all non-anchor frames in parallel using the anchor as a style
    reference. Assumes the anchor's output_filename is already set."""
    s = store()
    job = s.get_reel_job(job_id)
    if job is None:
        return
    anchor = _find_anchor(job)
    if not anchor.output_filename:
        job.status = ReelJobStatus.FAILED
        job.error = "Cannot run followers — anchor has no output"
        _persist_job(job)
        await _publish(job_id, "job", status=str(job.status), error=job.error)
        return

    job.status = ReelJobStatus.GENERATING
    job.error = None
    _persist_job(job)
    await _publish(job_id, "job", status=str(job.status))

    anchor_output = _frame_output_path(job, anchor)
    followers = [f for f in job.frames if f is not anchor]

    if job.mini_approval:
        # Sequential mode: render one follower at a time, pause after each
        # by parking on AWAITING_APPROVAL. The user's "approve" or "refine"
        # call resumes the loop by re-invoking run_reel_followers.
        next_frame = next((f for f in followers
                            if not f.approved and f.status != ReelFrameStatus.DONE),
                           None)
        if next_frame is None:
            # Everyone approved → mark done.
            n_done = sum(1 for f in job.frames
                          if f.status == ReelFrameStatus.DONE)
            job.status = (ReelJobStatus.DONE if n_done == len(job.frames)
                          else ReelJobStatus.PARTIAL if n_done >= 2
                          else ReelJobStatus.FAILED)
            _persist_job(job)
            await _publish(job_id, "job", status=str(job.status))
            return
        await _run_follower_with_correction(job, next_frame, anchor_output)
        # Park: don't auto-advance to next follower until user approves.
        if next_frame.status == ReelFrameStatus.DONE:
            next_frame.status = ReelFrameStatus.AWAITING_APPROVAL
            _persist_job(job)
            await _publish(job_id, "frame", frame_id=next_frame.frame_id,
                            status=str(next_frame.status),
                            output_filename=next_frame.output_filename,
                            drift_audit=next_frame.last_drift_audit)
        return

    # Parallel mode (default): all followers run concurrently, each with
    # its own two-pass drift-correction. Concurrency is throttled to 1 for
    # Gemini preview models — they have low per-minute caps (~5-10 RPM)
    # that parallel rendering blows through. GPT Image 2 doesn't have
    # this constraint at this scale.
    concurrency = (1 if job.image_model in {"nano-banana", "nano-banana-pro"}
                   else max(1, settings.image_concurrency))
    sem = asyncio.Semaphore(concurrency)

    async def _bounded(frame: ReelFrame) -> bool:
        async with sem:
            return await _run_follower_with_correction(job, frame, anchor_output)

    results = await asyncio.gather(*(_bounded(f) for f in followers))
    n_done = sum(1 for ok in results if ok) + 1  # anchor counted
    if n_done == len(job.frames):
        job.status = ReelJobStatus.DONE
    elif n_done <= 1:
        job.status = ReelJobStatus.FAILED
    else:
        job.status = ReelJobStatus.PARTIAL
    _persist_job(job)
    await _publish(job_id, "job", status=str(job.status))


async def _run_follower_with_correction(
    job: ReelJob, frame: ReelFrame, anchor_output: Path,
) -> bool:
    """Two-pass render: first pass with model-appropriate refs, then drift
    audit, then optional corrective second pass if drift severity is 'major'.
    Returns True if final output is DONE."""
    ok = await _run_frame(
        job, frame,
        refs=_follower_refs(job, frame, anchor_output),
        prompt=_follower_prompt(job, frame),
    )
    if not ok:
        return False

    first_output = _frame_output_path(job, frame)
    audit = await asyncio.to_thread(
        _audit_drift, anchor_output, first_output, job_id=job.job_id,
    )
    if audit:
        import json as _json
        try:
            frame.last_drift_audit = _json.dumps(audit, ensure_ascii=False)
        except Exception:
            frame.last_drift_audit = None

    # Only auto-correct on MAJOR drift. Minor drift is surfaced in the UI
    # as a soft warning but doesn't trigger an extra (paid) render.
    if not audit or audit.get("severity") != "major":
        _persist_job(job)
        return True

    drifts = audit.get("drifts") or []
    if not drifts:
        _persist_job(job)
        return True

    # Corrective pass: refs = [anchor, first_output]. The first_output
    # already has the right composition; we just need to fix the drifts.
    return await _run_frame(
        job, frame,
        refs=[anchor_output, first_output],
        prompt=_correction_prompt(job, frame, drifts),
    )


async def refine_frame(job_id: str, frame_id: str, correction: str) -> None:
    """User-driven targeted correction on a single frame. Runs ONE GPT Image
    pass with refs=[anchor_output, current_output] and a prompt focused on
    the user's free-text correction."""
    s = store()
    job = s.get_reel_job(job_id)
    if job is None:
        return
    frame = next((f for f in job.frames if f.frame_id == frame_id), None)
    if frame is None or frame.is_anchor:
        # Anchor refinement uses retry_frame instead (no anchor-to-refine-against).
        return
    anchor = _find_anchor(job)
    if not anchor.output_filename:
        return
    # Use the CURRENT output as the second ref so we keep composition stable
    # and only adjust the items the user flagged. Fall back to input if the
    # output isn't on disk yet (first refine before any render).
    cur_out = _frame_output_path(job, frame) if frame.output_filename else None
    second_ref = cur_out if (cur_out and cur_out.exists()) else _frame_input_path(job, frame)
    await _run_frame(
        job, frame,
        refs=[_frame_output_path(job, anchor), second_ref],
        prompt=_refine_prompt(job, frame, correction),
    )
    # Refining a follower in mini-approval mode keeps it parked at
    # AWAITING_APPROVAL so the user can re-review.
    if job.mini_approval and frame.status == ReelFrameStatus.DONE:
        frame.status = ReelFrameStatus.AWAITING_APPROVAL
        _persist_job(job)
        await _publish(job.job_id, "frame", frame_id=frame.frame_id,
                        status=str(frame.status),
                        output_filename=frame.output_filename,
                        drift_audit=frame.last_drift_audit)


async def approve_frame(job_id: str, frame_id: str) -> None:
    """Mark a follower as user-approved in mini-approval mode and advance
    to rendering the next follower. No-op in parallel mode."""
    s = store()
    job = s.get_reel_job(job_id)
    if job is None or not job.mini_approval:
        return
    frame = next((f for f in job.frames if f.frame_id == frame_id), None)
    if frame is None or frame.is_anchor:
        return
    frame.approved = True
    if frame.status == ReelFrameStatus.AWAITING_APPROVAL:
        frame.status = ReelFrameStatus.DONE
    _persist_job(job)
    await _publish(job.job_id, "frame", frame_id=frame.frame_id,
                    status=str(frame.status))
    # Tail-call back into the follower loop to pick up the next un-approved
    # frame (or finalize the job if all approved).
    await run_reel_followers(job_id)


async def retry_frame(job_id: str, frame_id: str) -> None:
    """Re-render a single frame. Anchor uses its input only; followers use
    [anchor_output, input]. Updates job.status afterward to reflect the
    new aggregate state (done/partial/failed) but does NOT touch the other
    frames' contents."""
    s = store()
    job = s.get_reel_job(job_id)
    if job is None:
        return
    frame = next((f for f in job.frames if f.frame_id == frame_id), None)
    if frame is None:
        return
    anchor = _find_anchor(job)

    if frame.is_anchor:
        # Re-rendering the anchor invalidates any follower outputs visually.
        # We keep the existing follower files on disk so the user can compare,
        # but flag the job as awaiting approval so they review before
        # cascading new followers.
        ok = await _run_frame(job, frame,
                               refs=[_frame_input_path(job, frame)],
                               prompt=job.full_prompt)
        job.status = (ReelJobStatus.AWAITING_ANCHOR_APPROVAL if ok
                       else ReelJobStatus.FAILED)
        if not ok:
            job.error = frame.error or "Anchor frame failed"
        _persist_job(job)
        await _publish(job_id, "job", status=str(job.status), error=job.error)
        return

    if not anchor.output_filename:
        # Can't retry a follower without an anchor output to reference.
        frame.status = ReelFrameStatus.FAILED
        frame.error = "Anchor has no output — re-render anchor first"
        _persist_job(job)
        await _publish(job_id, "frame", frame_id=frame.frame_id,
                        status=str(frame.status), error=frame.error)
        return

    follower_prompt = _follower_prompt(job, frame)
    await _run_frame(job, frame,
                      refs=[_frame_output_path(job, anchor),
                            _frame_input_path(job, frame)],
                      prompt=follower_prompt)
    # Recompute aggregate status.
    n_done = sum(1 for f in job.frames if f.status == ReelFrameStatus.DONE)
    if n_done == len(job.frames):
        job.status = ReelJobStatus.DONE
    elif n_done <= 1:
        job.status = ReelJobStatus.FAILED
    else:
        job.status = ReelJobStatus.PARTIAL
    _persist_job(job)
    await _publish(job_id, "job", status=str(job.status))


async def _run_frame(job: ReelJob, frame: ReelFrame, *,
                     refs: list[Path], prompt: str) -> bool:
    """Render one frame. Updates state + publishes events. Returns True on
    success."""
    frame.status = ReelFrameStatus.GENERATING
    frame.started_at = datetime.utcnow()
    frame.error = None
    _persist_job(job)
    await _publish(job.job_id, "frame", frame_id=frame.frame_id,
                    status=str(frame.status))

    out_path = _frame_output_path(job, frame)
    try:
        data = await asyncio.to_thread(
            _generate_one,
            prompt=prompt, refs=refs, model=job.image_model,
            aspect=job.aspect_ratio, job_id=job.job_id,
        )
        atomic_write_bytes(out_path, data)
        frame.output_filename = out_path.name
        frame.status = ReelFrameStatus.DONE
        frame.completed_at = datetime.utcnow()
        _persist_job(job)
        await _publish(job.job_id, "frame", frame_id=frame.frame_id,
                        status=str(frame.status),
                        output_filename=frame.output_filename)
        return True
    except Exception as e:
        frame.status = ReelFrameStatus.FAILED
        frame.error = f"{type(e).__name__}: {e}"
        frame.completed_at = datetime.utcnow()
        _persist_job(job)
        await _publish(job.job_id, "frame", frame_id=frame.frame_id,
                        status=str(frame.status), error=frame.error)
        return False
