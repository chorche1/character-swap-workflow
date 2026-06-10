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

from character_swap.clients import google_genai, grok, openai_image
from character_swap.config import settings
from character_swap.images import atomic_write_bytes

_log = logging.getLogger("pipeline")

# Verbatim user-specified prompt. Do not paraphrase.
GENERATION_PROMPT = (
    "Use Image 1 as the fixed master scene. Use Image 2 only as the identity "
    "and wardrobe reference for the replacement person.\n"
    "Create a photorealistic vertical 9:16 image. Preserve Image 1 exactly in "
    "terms of framing, composition, crop, camera angle, camera height, camera "
    "distance, focal-length appearance, perspective, subject scale, headroom, "
    "table placement, object placement, and overall scene layout. Do not "
    "reframe, recrop, zoom, widen, tighten, rotate, or shift the camera in any "
    "way.\n"
    "Replace the original person in Image 1 with the person from Image 2. The "
    "replacement person must occupy the same position in the frame and match "
    "the exact same body placement, pose, torso angle, shoulder position, arm "
    "placement, hand placement, and interaction with the objects as the "
    "original person in Image 1. Keep all objects, surfaces, and foreground "
    "elements from Image 1 in the exact same locations, with the same size, "
    "orientation, and arrangement.\n"
    "Transfer only the replacement person’s identity from Image 2 — face, "
    "hairstyle, and skin tone. Do NOT take any clothing from Image 2. The "
    "replacement person must wear exactly the same outfit as the original "
    "person in Image 1: keep the same clothing, garments, colors, patterns, "
    "accessories, and gloves that the original person in Image 1 is wearing, in "
    "the same fit and position. The replacement person "
    "must be looking directly at the camera, with eyes clearly facing the lens "
    "and a natural, composed expression, even if the original person in Image 1 "
    "was not.\n"
    "Keep the background visually consistent with Image 1, not Image 2. "
    "Preserve the same environment type, background structure, visible "
    "elements, and spatial layout from Image 1.\n"
    "Make the image look like a completely ordinary, unedited iPhone photo "
    "taken quickly by another person. It should not look staged, composed, "
    "retouched, filtered, color corrected, or professionally lit. Use plain, "
    "slightly dull phone-camera colors with a neutral white balance and no "
    "warm tint. Avoid golden tones, cinematic contrast, dramatic shadows, rich "
    "saturation, glossy highlights, crisp commercial sharpness, HDR "
    "processing, enhanced clarity, and polished skin. Use mundane natural "
    "daylight with slightly uneven exposure, mild softness, subtle sensor "
    "noise, ordinary shadows, imperfect framing, and small background "
    "distractions. Keep the intended action and objects, but do not make the "
    "image perfectly symmetrical or carefully centered. Do not beautify the "
    "scene. It should look like a normal photo from someone’s camera roll, not "
    "an advertisement or a professionally edited social-media image.\n"
    "Keep anatomy, hands, and fabric correctly formed and render skin with "
    "natural, non-polished texture. Remove all text overlays, captions, "
    "subtitles, logos, watermarks, and graphic elements. Keep the final image "
    "realistic, non-explicit, and non-NSFW.\n"
    "Negative prompt:\n"
    "No reframing, no recropping, no zoom, no camera shift, no changed "
    "perspective, no changed subject scale, no altered headroom, no background "
    "replacement from Image 2, no shallow depth of field, no portrait-mode "
    "effect, no professional lighting, no studio lighting, no cinematic "
    "contrast, no dramatic shadows, no HDR, no color grading, no warm or "
    "golden tint, no oversaturation, no glossy highlights, no crisp commercial "
    "sharpness, no enhanced clarity, no retouching, no beautification, no "
    "filters, no text, no captions, no subtitles, no watermark, no logos, no "
    "altered table placement, no moved objects, no missing objects, no "
    "duplicated objects, no clothing from Image 2, no changed outfit, no "
    "changed wardrobe, no different clothing than Image 1, "
    "no changed hand placement, no changed pose, no averted "
    "gaze, no looking away, no extra fingers, no malformed hands, no distorted "
    "anatomy, no nudity, no explicit content."
)


# Edit-engine swap prompt for the fal-hosted INSTRUCTION-EDIT models
# (Nano Banana Pro/2, Seedream Edit). Validated by the 2026-06-10 overnight
# bake-off — this "master" wording (Image 1/Image 2 role indexing + integration
# + style + a single constraints block) scored at parity-or-better with every
# alternative phrasing across both test scenes, and is the prompt behind the
# winning nbp-swap runs. Used automatically by the fal dispatch when the job
# carries the stock default prompt; a user-customized prompt always passes
# through verbatim.
#
# The OUTFIT is configurable (Reengineer's "Kläder" choice): the replacement
# person can wear the scene person's clothes (default — pixel-faithful
# rebuilds), their own clothes from the character reference, or a custom
# described outfit. build_edit_swap_prompt() assembles the prompt per mode.

_OUTFIT_CLAUSES = {
    # mode -> (identity sentence-tail + outfit directive, constraints line)
    "scene": (
        "Take only the face, hairstyle, hair color and skin tone from "
        "Image 2. The replacement person keeps the original person's exact "
        "pose, body position, torso angle, shoulder position, arm placement, "
        "hand placement and interaction with objects, and wears exactly the "
        "outfit from Image 1 — same garments, colors, patterns, accessories "
        "and fit; do not take any clothing from Image 2.",
        "do not alter the framing, camera, background, objects, pose or "
        "outfit from Image 1; do not carry any clothing, background or "
        "objects over from Image 2;",
    ),
    "character": (
        "Take the face, hairstyle, hair color, skin tone AND clothing from "
        "Image 2 — the replacement person wears their own outfit from Image "
        "2, fitted naturally to the original person's exact pose, body "
        "position, torso angle, shoulder position, arm placement, hand "
        "placement and interaction with objects. The outfit must follow the "
        "scene's lighting and drape realistically in the pose.",
        "do not alter the framing, camera, background, objects or pose from "
        "Image 1; do not carry any background or objects over from Image 2; "
        "do not keep the original person's clothing from Image 1;",
    ),
    "custom": (
        "Take only the face, hairstyle, hair color and skin tone from "
        "Image 2. The replacement person keeps the original person's exact "
        "pose, body position, torso angle, shoulder position, arm placement, "
        "hand placement and interaction with objects, and wears: {outfit}. "
        "The outfit must follow the scene's lighting and drape realistically "
        "in the pose; do not take any clothing from Image 2.",
        "do not alter the framing, camera, background, objects or pose from "
        "Image 1; do not carry any clothing, background or objects over from "
        "Image 2; do not keep the original person's clothing from Image 1 "
        "unless it matches the described outfit;",
    ),
}


def build_edit_swap_prompt(outfit_mode: str = "scene",
                           outfit_text: str | None = None,
                           background: bool = False) -> str:
    """Assemble the instruction-edit swap prompt.

    Outfit modes:
    - "scene" (default): wear exactly the original person's clothes from the
      scene — the bake-off-validated wording; byte-identical to
      EDIT_SWAP_PROMPT when background=False.
    - "character": wear the character reference's own clothes.
    - "custom": wear `outfit_text` (a free-text description).

    `background=True` adds Image 3 as a replacement environment: the person,
    pose, framing and every object they interact with stay from Image 1, but
    the surroundings become Image 3's location — and the person + kept
    foreground objects are RELIT with Image 3's light (direction, color
    temperature, shadows, white balance, grain) so nothing looks pasted in.
    """
    if outfit_mode not in _OUTFIT_CLAUSES:
        raise ValueError(f"Unknown outfit_mode '{outfit_mode}'")
    if outfit_mode == "custom" and not (outfit_text or "").strip():
        raise ValueError("outfit_mode 'custom' requires outfit_text")
    outfit_clause, constraint_line = _OUTFIT_CLAUSES[outfit_mode]
    if outfit_mode == "custom":
        outfit_clause = outfit_clause.format(outfit=outfit_text.strip())

    if background:
        roles = (
            "Image 1 is the master scene for the subject: it fixes the framing, "
            "the person's pose, and every object they interact with. Image 2 is "
            "only the identity reference for the replacement person. Image 3 is "
            "the NEW ENVIRONMENT: the finished photo takes place in Image 3's "
            "location.\n"
        )
        preserve = (
            "Keep from Image 1: the framing, composition, crop, camera angle, "
            "camera height, camera distance, focal-length appearance, "
            "perspective, subject scale and headroom, the person's exact "
            "placement in the frame, and every object, product and prop the "
            "person touches, holds or uses — same position relative to the "
            "person, size, orientation, color, material and physical state, with "
            "all text and brand labels legible and unchanged. Replace everything "
            "else — the surroundings, surfaces, walls, floor/ground, furniture "
            "and backdrop — with the environment from Image 3, matching Image "
            "3's setting faithfully and extending it naturally where Image 3 "
            "does not cover the frame.\n"
        )
        integration = (
            "Integration — this decides whether the image is usable: relight "
            "the person AND the kept foreground objects entirely with Image 3's "
            "light. Use Image 3's light direction, color temperature, intensity "
            "and softness; discard the lighting baked into Image 1 and Image 2. "
            "Ground the person in the new environment with correct contact "
            "shadows where body or objects meet Image 3's surfaces and a cast "
            "shadow consistent with Image 3's light source. Match Image 3's "
            "white balance, exposure, sharpness, depth of field and image grain "
            "across the ENTIRE frame — person, props and background must read "
            "as one single photograph taken in Image 3's location, never as a "
            "person cut out and pasted onto a backdrop.\n"
        )
        # The per-outfit-mode constraint line stays intact except that Image
        # 1's background is no longer protected (it is being replaced).
        bg_constraints = (
            "do not keep Image 1's background, walls, floors or surroundings; "
            "do not invent an environment that is not Image 3's; do not leave "
            "the person's lighting inconsistent with Image 3; "
        )
        constraint_line = bg_constraints + constraint_line.replace(
            "do not alter the framing, camera, background, objects",
            "do not alter the framing, camera, objects", 1)
    else:
        roles = (
            "Image 1 is the fixed master scene and ground truth. Image 2 is only the "
            "identity reference for the replacement person.\n"
        )
        preserve = (
            "Recreate Image 1 exactly — same framing, composition, crop, camera angle, "
            "camera height, camera distance, focal-length appearance, perspective, "
            "subject scale, headroom, and the exact placement, size, orientation, "
            "color, material and physical state of every object, surface and "
            "background element. Do not reframe, recrop, zoom, rotate, or shift the "
            "camera in any way. Keep all visible text and brand labels legible and "
            "unchanged.\n"
        )
        integration = (
            "Integration: light the replacement person with the scene's own light "
            "sources and color grade. Match skin texture, facial shadows, "
            "perspective, edge blending, white balance, sharpness, depth of field and "
            "image grain to Image 1 so the person belongs naturally in the photo, "
            "including correct cast shadows and contact shadows where the body meets "
            "surfaces.\n"
        )

    return (
        roles
        + preserve
        + "Replace the person in Image 1 with the person from Image 2, as if they "
        "had been standing there when the photo was taken — as if part of the "
        "same photo. " + outfit_clause + " The replacement person looks "
        "directly into the camera lens with a natural, composed expression, even "
        "if the original person was not.\n"
        + integration
        + "Style: a completely ordinary, unedited iPhone photo taken quickly by "
        "another person — plain, slightly dull phone-camera colors, neutral white "
        "balance, mundane ambient daylight, slightly uneven exposure, mild "
        "softness, subtle sensor noise, natural non-polished skin with visible "
        "pores, imperfect casual framing, small background distractions. It "
        "should look like a normal photo from someone's camera roll, not an "
        "advertisement or a professionally edited social-media image.\n"
        "Constraints — do not violate any of these: " + constraint_line + " do "
        "not blend the original person's facial features into the new face; do "
        "not add people, text, captions, subtitles, watermarks or logos, and "
        "remove any that are burnt into Image 1; do not apply professional "
        "lighting, studio lighting, cinematic contrast, dramatic shadows, HDR, "
        "warm or golden grading, oversaturation, glossy highlights, "
        "beautification, retouching, filters or portrait-mode background blur; "
        "keep hands and anatomy correctly formed with the correct number of "
        "fingers; keep the image realistic and non-explicit."
    )


EDIT_SWAP_PROMPT = build_edit_swap_prompt("scene")


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
    """Swap-variant generation on the chosen model only.

    Two-stage NSFW recovery, both inside each client:
      1. the chosen model with the prompt as-is,
      2. the chosen model retried with a minimally softened prompt
         (`content_policy`).

    There is intentionally NO cross-provider fallback: if the chosen model
    (e.g. GPT Image) still refuses on content-policy grounds, the variant
    fails and the real refusal surfaces to the user — we never silently switch
    to a different model/provider than the one selected. All errors propagate
    unchanged.
    """
    return _dispatch_variant(
        model=model, scene_image=scene_image, character_image=character_image,
        character_name=character_name, prompt=prompt, dest=dest, job_id=job_id,
        extra_reference_image=extra_reference_image,
    )


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
    if model in ("nbp-swap", "nb2-swap", "seedream-edit-swap",
                 "qwen-edit-swap", "kontext-max-swap"):
        # fal-hosted instruction-edit engines — strict scene preservation:
        # they EDIT the scene image in place guided by the prompt, taking the
        # new person's identity from the character image (reference #2).
        # Edit models want short imperative directives, so the stock long
        # default prompt is swapped for EDIT_SWAP_PROMPT; custom prompts pass
        # through verbatim.
        from character_swap.clients import fal_image
        effective = EDIT_SWAP_PROMPT if prompt == GENERATION_PROMPT else prompt
        data = fal_image.swap_image(
            model_slug=model,
            scene_image=scene_image,
            character_image=character_image,
            prompt=effective,
            aspect_ratio="9:16",
            app_job_id=job_id,
            extra_reference_image=extra_reference_image,
        )
        atomic_write_bytes(dest, data)
        return dest
    if model == "higgsfield-swap":
        # Higgsfield Character Swap (official REST API): the character is turned
        # into a custom-reference and composited into the scene via Soul.
        # scene = the scene to preserve, character = the person to insert.
        from character_swap.clients import higgsfield
        data = higgsfield.generate_swap(
            scene_image=scene_image,
            character_image=character_image,
            prompt=prompt,
            aspect_ratio="9:16",
            app_job_id=job_id,
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
    generate_audio: bool | None = None,
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
        # `generate_audio` per-call override (Reengineer jobs set True so the
        # swapped character speaks with Kling's native voice); None falls back
        # to the global setting (default OFF).
        from character_swap.clients import fal_kling
        audio = (generate_audio if generate_audio is not None
                 else settings.kling_generate_audio)
        return fal_kling.submit_image_to_video(
            image=image, prompt=movement_prompt,
            duration_secs=effective_dur, end_image=end_image,
            generate_audio=audio, app_job_id=job_id,
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
