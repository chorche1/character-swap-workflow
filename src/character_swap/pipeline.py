"""
Pipeline primitives — pure functions that wrap the API clients.

The new flow is:
    generate_image(scene, character)  -> Path     # GPT Image 2 (image-to-image)
    submit_video(image, prompt)       -> job_id   # Grok Imagine
    wait_for_video(job_id, dest)      -> Path     # poll + download mp4

Orchestration (parallelism, approval, retries) lives in `runner.py`. Real-time
fan-out to the web UI lives in `events.py`. This module does no I/O beyond
what the clients require.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from character_swap import content_policy
from character_swap.clients import google_genai, grok, openai_image
from character_swap.config import settings
from character_swap.images import atomic_write_bytes

_log = logging.getLogger("pipeline")

# Last-resort model for the swap when the chosen model keeps refusing a prompt
# on content-policy grounds even after per-model prompt softening. Nano Banana
# Pro is a different moderation backend (Google) than GPT Image (OpenAI) /
# Grok (xAI), so a provider-specific false positive often clears — and it uses
# the scene+character references, so swap quality stays high.
_NSFW_FALLBACK_MODEL = "nano-banana-pro"

# Verbatim user-specified prompt. Do not paraphrase.
GENERATION_PROMPT = (
    "Replace the person in the first image with the person from the second "
    "image. Keep everything else about the first image identical.\n\n"
    "IDENTITY — FULL OVERRIDE: Completely replace the face, head, hair, skin, "
    "and body of the original person with the person from the second image. "
    "Overwrite their age, gender, ethnicity, and bone structure entirely — "
    "zero traits from the original person may bleed through. The new person is "
    "the sole subject.\n\n"
    "POSE & FRAMING — MATCH EXACTLY: Same pose, body position, hand placement, "
    "and held objects as the original. Match the shot type, camera angle, "
    "subject-to-camera distance, crop, and head-room of the first image "
    "exactly. No zoom, no focal-length change.\n\n"
    "PROPS & LAYOUT — PIXEL-EXACT: Preserve every object exactly as in the "
    "first image — same count, color, material, position, and physical state. "
    "Do not move, add, remove, or alter any prop. Keep all brand labels "
    "legible and unchanged; never warp them into misspelled or gibberish "
    "text.\n\n"
    "BACKGROUND — DO NOT CHANGE: Keep the first image's background exactly — "
    "same room, surfaces, furniture, fixtures, decor, and lighting. Do not "
    "restyle, swap, or blur it.\n\n"
    "CLEANUP: Remove any captions, subtitles, progress bars, logos, or "
    "watermarks burned into the source. If a secondary person must be removed, "
    "erase every trace — arms, hands, hair, clothing, and shadows.\n\n"
    "LIGHTING & INTEGRATION — THE PERSON MUST LOOK NATURALLY PRESENT, NOT "
    "PASTED IN: Relight the inserted person with the scene's OWN light sources "
    "— match the light direction, color temperature, softness, and intensity "
    "of the scene, and discard the lighting baked into the character photo. "
    "Match the scene's white balance and color grade across their skin and "
    "clothing. Add realistic contact shadows and ambient occlusion where the "
    "person meets surfaces (hands on a table, feet on the floor, body against "
    "the background) and a cast shadow consistent with the scene's key-light "
    "direction. Blend the edges naturally — no hard cutout outline, halo, or "
    "fringe. Match the scene's depth of field, focus falloff, lens character, "
    "and grain so the person is not sharper or cleaner than their surroundings. "
    "The result must read as one photograph taken in one place, never a "
    "collage.\n\n"
    "TECHNICAL: 9:16 aspect ratio. Photorealistic, natural skin texture, "
    "sharp focus on the subject, no blurry background. Zero burnt-in text or "
    "watermarks.\n\n"
    "AVOID: a pasted-in / cutout / sticker / collage look, the subject "
    "floating with no shadow, lighting that doesn't match the scene, "
    "color-temperature mismatch between subject and background, the subject "
    "rendered sharper than the background, hard cutout edges or halos, "
    "identity bleed from the original person, extra or distorted fingers, "
    "warped faces, changed or restyled background, altered prop counts, props "
    "changing state, misspelled labels, captions, subtitles, watermarks, "
    "cartoon or illustration look."
)


def generate_image(
    *,
    scene_image: Path,
    character_image: Path,
    character_name: str,
    dest: Path,
    job_id: str | None = None,
    prompt: str | None = None,
    extra_reference_image: Path | None = None,
) -> Path:
    """
    Image-to-image generation using GPT Image (default).
    Scene is reference #1, character is reference #2 — matches the verbatim prompt.
    If `extra_reference_image` is provided, it lands as reference #3.
    Writes the PNG bytes atomically to `dest` and returns it.

    `prompt` overrides `GENERATION_PROMPT` if provided (Swap Step 2 lets the
    user edit it; this is how that custom string reaches the API).
    """
    refs: list[Path] = [scene_image, character_image]
    if extra_reference_image is not None:
        refs.append(extra_reference_image)
    image_bytes = openai_image.generate(
        prompt=prompt or GENERATION_PROMPT,
        reference_images=refs,
        phase="generate",
        character=character_name,
        job_id=job_id,
    )
    atomic_write_bytes(dest, image_bytes)
    return dest


def generate_variant(
    *,
    model: str,
    scene_image: Path,
    character_image: Path,
    character_name: str,
    prompt: str,
    dest: Path,
    job_id: str | None = None,
    extra_reference_image: Path | None = None,
) -> Path:
    """Swap-variant generation with a content-policy fallback.

    Three-stage NSFW recovery (the first two live inside each client):
      1. the chosen model with the prompt as-is,
      2. the chosen model retried with a minimally softened prompt
         (`content_policy`), and — added here —
      3. if it STILL refuses, re-run on Nano Banana Pro (a different
         moderation backend) when Gemini is configured and we're not already
         on it. Provider-specific false positives (e.g. "shirtless person")
         usually clear, and NBP uses the same references so the swap quality
         holds. The output may look slightly different from sibling variants
         made by the original model — logged so it's traceable.

    Non-content errors propagate unchanged.
    """
    try:
        return _dispatch_variant(
            model=model, scene_image=scene_image, character_image=character_image,
            character_name=character_name, prompt=prompt, dest=dest, job_id=job_id,
            extra_reference_image=extra_reference_image,
        )
    except Exception as e:
        if (content_policy.is_content_rejection(e)
                and model != _NSFW_FALLBACK_MODEL
                and settings.has_provider("gemini")):
            _log.warning(
                "content rejection on '%s' after softening; falling back to '%s' "
                "for this variant (job=%s). Output style may differ from siblings.",
                model, _NSFW_FALLBACK_MODEL, job_id,
            )
            return _dispatch_variant(
                model=_NSFW_FALLBACK_MODEL, scene_image=scene_image,
                character_image=character_image, character_name=character_name,
                prompt=prompt, dest=dest, job_id=job_id,
                extra_reference_image=extra_reference_image,
            )
        raise


def _dispatch_variant(
    *,
    model: str,
    scene_image: Path,
    character_image: Path,
    character_name: str,
    prompt: str,
    dest: Path,
    job_id: str | None = None,
    extra_reference_image: Path | None = None,
) -> Path:
    """
    Dispatch a swap-variant generation to the right model. Used by runner.py
    so it doesn't need to know provider details.

    - gpt-image:        scene + character (+ optional extra) as refs
                        (image-to-image; weak at strict spatial preservation
                        — model often drifts to a generic photo of the character)
    - grok-image:       text-only (Grok image API doesn't take refs today);
                        `extra_reference_image` is ignored.
    - nano-banana:      Gemini 2.5 Flash Image — multi-ref edit, faster
    - nano-banana-pro:  Gemini Pro Image — multi-ref edit with strongest
                        scene-preservation. Recommended for swap-into-scene
                        when GPT Image 2 drifts to generic photos.

    When `extra_reference_image` is supplied, models that accept references
    receive it as ref #3 (after scene, character) — useful for "match this
    background" or "use this outfit/prop" hints.
    """
    if model == "gpt-image":
        return generate_image(
            scene_image=scene_image,
            character_image=character_image,
            character_name=character_name,
            dest=dest,
            job_id=job_id,
            prompt=prompt,
            extra_reference_image=extra_reference_image,
        )
    if model == "grok-image":
        data = grok.generate_image(
            prompt=prompt,
            character=character_name,
            app_job_id=job_id,
        )
        atomic_write_bytes(dest, data)
        return dest
    if model in ("nano-banana", "nano-banana-pro"):
        # Pass the slug through — google_genai client maps to the current
        # Google model name internally (nano-banana → gemini-2.5-flash-image,
        # nano-banana-pro → nano-banana-pro-preview).
        refs: list[Path] = [scene_image, character_image]
        if extra_reference_image is not None:
            refs.append(extra_reference_image)
        data = google_genai.generate_nano_banana(
            prompt=prompt,
            reference_images=refs,
            app_job_id=job_id,
            model=model,
        )
        atomic_write_bytes(dest, data)
        return dest
    raise ValueError(f"Unknown image model for swap variant: {model}")


def edit_image(
    *,
    source_image: Path,
    custom_prompt: str,
    character_name: str,
    dest: Path,
    job_id: str | None = None,
) -> Path:
    """
    Refine an existing variant with a user-supplied prompt.
    Single reference image (the variant being edited) + custom prompt.
    """
    image_bytes = openai_image.generate(
        prompt=custom_prompt,
        reference_images=[source_image],
        phase="edit",
        character=character_name,
        job_id=job_id,
    )
    atomic_write_bytes(dest, image_bytes)
    return dest


def submit_video(
    *,
    image: Path,
    movement_prompt: str,
    character_name: str,
    job_id: str | None = None,
    model: str = "grok-imagine",
    aspect_ratio: str | None = None,
    duration_secs: int | None = None,
    end_image: Path | None = None,
) -> str:
    """Submit a video job to the chosen provider. Returns the provider's job/task id.

    `model` defaults to grok-imagine for back-compat with older callers that
    don't yet pass it. The Step-4 UI lets users pick any of: grok-imagine,
    veo, veo-3-fast, kling*, runway*, luma-ray2, pika-2, hailuo*, sora-2,
    wan*, seedance, higgsfield-*.

    `aspect_ratio` overrides `settings.video_aspect_ratio` for this call.
    Used when a job needs a non-default aspect (1:1 for Instagram, 16:9 for
    YouTube). None falls back to the global default.

    `duration_secs` overrides `settings.video_duration_secs` for this call
    (set by the Step-4 picker per job). Each per-provider submit function
    additionally clamps to its own accepted bucket — Grok to [5,15],
    Kling to {5, 10}, etc.
    """
    effective_ar = aspect_ratio or settings.video_aspect_ratio
    effective_dur = duration_secs if duration_secs else settings.video_duration_secs
    if model == "grok-imagine":
        return grok.submit(image=image, prompt=movement_prompt,
                           character=character_name,
                           aspect_ratio=effective_ar,
                           duration_secs=effective_dur,
                           app_job_id=job_id)

    # Lazy imports so older keyless installs don't pay the import cost for
    # providers they'll never use.
    from character_swap.clients import _stubs, google_genai, kling
    if model == "kling-v3":
        # Kling 3.0 routes through fal.ai (the official API caps at 5/10s;
        # fal's Kling v3 accepts 3–15s + an optional end frame).
        from character_swap.clients import fal_kling
        return fal_kling.submit_image_to_video(
            image=image, prompt=movement_prompt,
            duration_secs=effective_dur, end_image=end_image, app_job_id=job_id,
        )
    if model in {"veo", "veo-3-fast"}:
        return google_genai.submit_veo(
            image=image, prompt=movement_prompt,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    if model in kling.KLING_MODELS or model in kling.LEGACY_ALIASES:
        return kling.submit_kling(
            image=image, prompt=movement_prompt,
            model=model,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    if model in {"runway-gen4", "runway-gen3-alpha"}:
        return _stubs.submit_runway(
            image=image, prompt=movement_prompt,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    if model == "luma-ray2":
        return _stubs.submit_luma(
            image=image, prompt=movement_prompt,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    if model == "pika-2":
        return _stubs.submit_pika(
            image=image, prompt=movement_prompt,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    if model in {"hailuo-02", "hailuo-01"}:
        return _stubs.submit_minimax(
            image=image, prompt=movement_prompt, model=model,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    if model == "sora-2":
        return _stubs.submit_sora(
            image=image, prompt=movement_prompt,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    if model.startswith("wan-"):
        return _stubs.submit_wan(
            image=image, prompt=movement_prompt, model=model,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    if model == "seedance":
        return _stubs.submit_seedance(
            image=image, prompt=movement_prompt,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    if model.startswith("higgsfield-"):
        return _stubs.submit_higgsfield(
            image=image, prompt=movement_prompt, model=model,
            aspect_ratio=effective_ar,
            duration_secs=effective_dur,
            app_job_id=job_id,
        )
    raise ValueError(f"Unknown video model: {model}")


def poll_video_once(*, job_id: str, character_name: str,
                    app_job_id: str | None = None) -> tuple[str, str | None]:
    """One poll. Returns (status, download_url_or_none).

    Grok-only — used internally by `wait_for_video` for the grok-imagine
    branch. Other providers' clients expose their own blocking `wait_for_*`
    helpers that don't surface per-poll progress.
    """
    payload = grok.status(job_id=job_id, character=character_name, app_job_id=app_job_id)
    return _extract_status(payload)


def wait_for_video(
    *,
    job_id: str,
    character_name: str,
    dest: Path,
    on_progress=None,
    app_job_id: str | None = None,
    model: str = "grok-imagine",
) -> Path:
    """
    Blocking poll loop. Downloads to `dest` on success. Raises GrokError or
    provider-specific exceptions on timeout / terminal failure.

    `on_progress(status: str, url: str | None)` is called once per poll
    (Grok only — other providers don't expose intermediate status today, so
    callers should not rely on it firing for them).
    """
    if model == "grok-imagine":
        deadline = time.monotonic() + settings.video_timeout_secs
        interval = settings.video_poll_interval_secs
        while time.monotonic() < deadline:
            status, url = poll_video_once(job_id=job_id, character_name=character_name,
                                          app_job_id=app_job_id)
            if on_progress is not None:
                on_progress(status, url)
            if status in grok.SUCCESS_STATES:
                if not url:
                    raise grok.GrokError("Video reported done but no download URL")
                grok.download_video(url=url, dest=dest)
                return dest
            if status in grok.TERMINAL_STATES:
                raise grok.GrokError(f"Video job ended in state '{status}'")
            time.sleep(interval)
        raise grok.GrokError(f"Video job {job_id} timed out after {settings.video_timeout_secs}s")

    # Non-grok providers: delegate to their wait_for_* helpers, which block
    # until the output is downloaded to `dest`. We fire one synthetic
    # "processing" progress event up-front so the UI shows movement.
    if on_progress is not None:
        on_progress("processing", None)
    from character_swap.clients import _stubs, google_genai, kling
    if model == "kling-v3":
        from character_swap.clients import fal_kling
        fal_kling.wait_for_video(request_id=job_id, dest=dest, app_job_id=app_job_id)
    elif model in {"veo", "veo-3-fast"}:
        google_genai.wait_for_veo(op_id=job_id, dest=dest)
    elif model in kling.KLING_MODELS or model in kling.LEGACY_ALIASES:
        kling.wait_for_kling(task_id=job_id, dest=dest)
    elif model in {"runway-gen4", "runway-gen3-alpha"}:
        _stubs.wait_for_runway(task_id=job_id, dest=dest)
    elif model == "luma-ray2":
        _stubs.wait_for_luma(task_id=job_id, dest=dest)
    elif model == "pika-2":
        _stubs.wait_for_pika(task_id=job_id, dest=dest)
    elif model in {"hailuo-02", "hailuo-01"}:
        _stubs.wait_for_minimax(task_id=job_id, dest=dest)
    elif model == "sora-2":
        _stubs.wait_for_sora(task_id=job_id, dest=dest)
    elif model.startswith("wan-"):
        _stubs.wait_for_wan(task_id=job_id, dest=dest)
    elif model == "seedance":
        _stubs.wait_for_seedance(task_id=job_id, dest=dest)
    elif model.startswith("higgsfield-"):
        _stubs.wait_for_higgsfield(task_id=job_id, dest=dest)
    else:
        raise ValueError(f"Unknown video model: {model}")

    if on_progress is not None:
        on_progress("done", str(dest))
    return dest


def _extract_status(payload: dict) -> tuple[str, str | None]:
    """
    Read (status, download_url) from a Grok status payload. Handles all observed
    response shapes. Verified shape on completion:
      {"status": "done", "video": {"url": "...", "duration": 10}, "progress": 100}
    """
    status = str(
        payload.get("status")
        or payload.get("state")
        or "unknown"
    ).lower()

    url = None
    video = payload.get("video")
    if isinstance(video, dict):
        url = video.get("url")
    if not url:
        url = payload.get("video_url") or payload.get("url")
    if not url:
        data = payload.get("data") or {}
        if isinstance(data, dict):
            inner = data.get("video")
            if isinstance(inner, dict):
                url = inner.get("url")
            url = url or data.get("url") or data.get("video_url")
    if not url:
        outputs = payload.get("outputs") or []
        if outputs and isinstance(outputs, list):
            first = outputs[0]
            if isinstance(first, dict):
                url = first.get("url") or first.get("video_url")
    return status, url
