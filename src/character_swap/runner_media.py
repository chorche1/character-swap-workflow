"""
Background runner for free-form Image / Video generations (the new tabs).

Independent of `runner.py`, which handles the multi-character swap flow.
Each entry point updates the `MediaGeneration` row in state as it progresses
and downloads outputs into `output/generations/<gen_id>/`.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from character_swap.clients import (
    ProviderNotConfigured,
    _stubs,
    elevenlabs,
    google_genai,
    grok,
    heygen,
    kling,
    openai_image,
)
from character_swap.config import settings
from character_swap.images import atomic_write_bytes
from character_swap.models import GenStatus, MediaGeneration
from character_swap.state import store


# --- model registry (used by API to surface availability) ----------------------------

IMAGE_MODELS: dict[str, dict] = {
    "gpt-image":            {"label": "GPT Image",                       "provider": "openai",     "price_setting": "openai_image_price_usd"},
    "dall-e-3":             {"label": "DALL·E 3",                        "provider": "openai",     "price_setting": "dall_e_3_price_usd"},
    "grok-image":           {"label": "Grok Imagine (still)",            "provider": "xai",        "price_setting": "grok_image_price_usd"},
    "nano-banana":          {"label": "Nano Banana (Gemini 2.5 Flash)",  "provider": "gemini",     "price_setting": "nano_banana_price_usd"},
    "nano-banana-pro":      {"label": "Nano Banana Pro (Gemini 2.5 Pro)","provider": "gemini",     "price_setting": "nano_banana_pro_price_usd"},
    "flux-pro-1.1-ultra":   {"label": "FLUX 1.1 Pro Ultra",              "provider": "bfl",        "price_setting": "flux_price_usd"},
    "flux-pro":             {"label": "FLUX Pro",                        "provider": "bfl",        "price_setting": "flux_price_usd"},
    "flux-schnell":         {"label": "FLUX Schnell",                    "provider": "bfl",        "price_setting": "flux_price_usd"},
    "flux-kontext":         {"label": "FLUX Kontext (edit)",             "provider": "bfl",        "price_setting": "flux_kontext_price_usd"},
    "ideogram-3":           {"label": "Ideogram 3",                      "provider": "ideogram",   "price_setting": "ideogram_price_usd"},
    "recraft-v3":           {"label": "Recraft v3",                      "provider": "recraft",    "price_setting": "recraft_price_usd"},
    "sd-3.5":               {"label": "Stable Diffusion 3.5",            "provider": "stability",  "price_setting": "stability_price_usd"},
    "seedream-3":           {"label": "Seedream 3.0",                    "provider": "bytedance",  "price_setting": "seedream_price_usd"},
    "seededit":             {"label": "SeedEdit",                        "provider": "bytedance",  "price_setting": "seedream_price_usd"},
    "higgsfield-soul-img":  {"label": "Higgsfield Soul (image)",         "provider": "higgsfield", "price_setting": "higgsfield_price_usd"},
}

AVATAR_MODELS: dict[str, dict] = {
    "heygen-avatar-5":     {"label": "HeyGen Avatar 5 (catalogue)",  "provider": "heygen", "price_setting": "heygen_price_usd"},
    "heygen-photo-avatar": {"label": "HeyGen Talking Photo",         "provider": "heygen", "price_setting": "heygen_price_usd"},
}

AUDIO_MODELS: dict[str, dict] = {
    "elevenlabs-vc":  {"label": "ElevenLabs Voice Changer",  "provider": "elevenlabs", "price_setting": "elevenlabs_vc_price_usd"},
    "elevenlabs-tts": {"label": "ElevenLabs Text-to-Speech", "provider": "elevenlabs", "price_setting": "elevenlabs_tts_price_usd"},
}

VIDEO_MODELS: dict[str, dict] = {
    "grok-imagine":         {"label": "Grok Imagine",                    "provider": "xai",        "price_setting": "grok_video_price_usd"},
    "veo":                  {"label": "Veo 3",                           "provider": "gemini",     "price_setting": "veo_price_usd"},
    "veo-3-fast":           {"label": "Veo 3 Fast",                      "provider": "gemini",     "price_setting": "veo_price_usd"},
    # Kling — every confirmed model_name string from Kling's official i2v API
    # (Singapore region, May 2026). Slug == API name to keep the mapping
    # trivial in `kling._resolve_model_name`. Legacy aliases (`kling`,
    # `kling-2.1-pro`) still resolve via LEGACY_ALIASES for old jobs.
    # NB: v3 / v3-omni / o1 are NOT included — Kling's marketing lists them
    # but no public-leaning source confirms the API model_name strings.
    # Add them here once Hugo verifies against the live dev dashboard.
    "kling-v1":             {"label": "Kling 1.0",                       "provider": "kling",      "price_setting": "kling_price_usd"},
    "kling-v1-5":           {"label": "Kling 1.5",                       "provider": "kling",      "price_setting": "kling_price_usd"},
    "kling-v1-6":           {"label": "Kling 1.6",                       "provider": "kling",      "price_setting": "kling_price_usd"},
    "kling-v2-master":      {"label": "Kling 2.0 Master",                "provider": "kling",      "price_setting": "kling_price_usd"},
    "kling-v2-1":           {"label": "Kling 2.1",                       "provider": "kling",      "price_setting": "kling_price_usd"},
    "kling-v2-1-master":    {"label": "Kling 2.1 Master",                "provider": "kling",      "price_setting": "kling_price_usd"},
    "kling-v2-5-turbo":     {"label": "Kling 2.5 Turbo",                 "provider": "kling",      "price_setting": "kling_price_usd"},
    "kling-v2-6":           {"label": "Kling 2.6",                       "provider": "kling",      "price_setting": "kling_price_usd"},
    # Legacy slug aliases — Hugo's old jobs reference these strings;
    # `kling.LEGACY_ALIASES` maps them to the new model_names. Kept in
    # the registry so the dropdown still shows a sensible label.
    "kling":                {"label": "Kling 2.0 (legacy alias)",        "provider": "kling",      "price_setting": "kling_price_usd"},
    "kling-2.1-pro":        {"label": "Kling 2.1 Pro (legacy alias)",    "provider": "kling",      "price_setting": "kling_price_usd"},
    "kling-1.6":            {"label": "Kling 1.6 (legacy alias)",        "provider": "kling",      "price_setting": "kling_price_usd"},
    "runway-gen4":          {"label": "Runway Gen-4",                    "provider": "runway",     "price_setting": "runway_price_usd"},
    "runway-gen3-alpha":    {"label": "Runway Gen-3 Alpha",              "provider": "runway",     "price_setting": "runway_price_usd"},
    "luma-ray2":            {"label": "Luma Ray-2",                      "provider": "luma",       "price_setting": "luma_price_usd"},
    "pika-2":               {"label": "Pika 2.2",                        "provider": "pika",       "price_setting": "pika_price_usd"},
    "hailuo-02":            {"label": "MiniMax Hailuo 02",               "provider": "minimax",    "price_setting": "minimax_price_usd"},
    "hailuo-01":            {"label": "MiniMax Hailuo 01",               "provider": "minimax",    "price_setting": "minimax_price_usd"},
    "sora-2":               {"label": "Sora 2",                          "provider": "openai",     "price_setting": "sora_price_usd"},
    "wan-2.2":              {"label": "Wan 2.2",                         "provider": "alibaba",    "price_setting": "wan_price_usd"},
    "wan-2.1":              {"label": "Wan 2.1",                         "provider": "alibaba",    "price_setting": "wan_price_usd"},
    "seedance":             {"label": "Seedance",                        "provider": "bytedance",  "price_setting": "seedance_price_usd"},
    "higgsfield-soul-vid":  {"label": "Higgsfield Soul (video)",         "provider": "higgsfield", "price_setting": "higgsfield_price_usd"},
    "higgsfield-dop":       {"label": "Higgsfield DoP",                  "provider": "higgsfield", "price_setting": "higgsfield_price_usd"},
    "higgsfield-lipsync":   {"label": "Higgsfield Lipsync",              "provider": "higgsfield", "price_setting": "higgsfield_price_usd"},
    "higgsfield-speak":     {"label": "Higgsfield Speak",                "provider": "higgsfield", "price_setting": "higgsfield_price_usd"},
}


def model_info(model: str) -> dict | None:
    return (IMAGE_MODELS.get(model) or VIDEO_MODELS.get(model)
            or AVATAR_MODELS.get(model) or AUDIO_MODELS.get(model))


def _output_dir(gen_id: str) -> Path:
    return settings.output_dir / "generations" / gen_id


def _persist(gen: MediaGeneration, **fields) -> MediaGeneration:
    for k, v in fields.items():
        setattr(gen, k, v)
    store().update_generation(gen)
    return gen


# --- image generation ----------------------------------------------------------------

async def run_image_gen(gen_id: str) -> None:
    s = store()
    gen = s.get_generation(gen_id)
    if gen is None:
        return
    _persist(gen, status=GenStatus.RUNNING)

    # AI Director — ONE Claude call to tailor the prompt around the actual
    # reference image. Skipped if there's no ref (Director needs vision input
    # to be useful). Treats the one ref as both "scene" and "character" from
    # the agent's POV so it can describe what's in the image and tailor a
    # single prompt back.
    if gen.use_director and not gen.director_prompt and gen.reference_paths:
        from character_swap import prompt_director
        ref_path = Path(gen.reference_paths[0])
        plan = await asyncio.to_thread(
            prompt_director.direct_swap,
            user_prompt=gen.prompt,
            characters=[("ref", "reference", ref_path)],
            scenes=[("scene", ref_path)],
            images_per_character=1,
            job_id=gen_id,
        )
        if plan is not None:
            tailored = plan.lookup("ref", "scene")
            if tailored:
                gen.director_prompt = tailored[0]
                _persist(gen)

    # Optional prompt enrichment — expand short text into a cinematic spec
    # so the downstream image model has more to work with. Mirrors what
    # web UIs do internally (Grok Imagine, etc.) and closes most of the
    # "API result is worse than web result" gap. Skipped if Director
    # already provided a tailored prompt.
    if gen.enrich_prompt and not gen.enriched_prompt and not gen.director_prompt:
        from character_swap import prompt_enrich
        enriched = await asyncio.to_thread(
            prompt_enrich.enrich_prompt, gen.prompt, "image", job_id=gen_id,
        )
        if enriched and enriched != gen.prompt:
            gen.enriched_prompt = enriched
            _persist(gen)

    try:
        refs = [Path(p) for p in gen.reference_paths]
        # Precedence: director > enriched > raw.
        effective_prompt = gen.director_prompt or gen.enriched_prompt or gen.prompt
        if gen.model == "gpt-image":
            data = await asyncio.to_thread(
                openai_image.generate,
                prompt=effective_prompt,
                reference_images=refs if refs else None,
                phase="generate", character="freeform",
                size=_openai_size_for(gen.aspect_ratio), job_id=gen_id,
            )
        elif gen.model == "dall-e-3":
            data = await asyncio.to_thread(
                openai_image.generate,
                prompt=effective_prompt,
                reference_images=refs if refs else None,
                phase="generate", character="freeform",
                size=_openai_size_for(gen.aspect_ratio), job_id=gen_id,
                model_override="dall-e-3",
            )
        elif gen.model == "grok-image":
            data = await asyncio.to_thread(
                grok.generate_image,
                prompt=effective_prompt, aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
            )
        elif gen.model in {"nano-banana", "nano-banana-pro"}:
            gemini_model = ("gemini-2.5-pro-image-preview"
                            if gen.model == "nano-banana-pro"
                            else "gemini-2.5-flash-image-preview")
            data = await asyncio.to_thread(
                google_genai.generate_nano_banana,
                prompt=effective_prompt, reference_images=refs,
                aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
                model=gemini_model,
            )
        elif gen.model in {"flux-pro-1.1-ultra", "flux-pro", "flux-schnell", "flux-kontext"}:
            data = await asyncio.to_thread(
                _stubs.generate_flux,
                prompt=effective_prompt, model=gen.model,
                reference_images=refs, aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
            )
        elif gen.model == "ideogram-3":
            data = await asyncio.to_thread(
                _stubs.generate_ideogram,
                prompt=effective_prompt, aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
            )
        elif gen.model == "recraft-v3":
            data = await asyncio.to_thread(
                _stubs.generate_recraft,
                prompt=effective_prompt, aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
            )
        elif gen.model == "sd-3.5":
            data = await asyncio.to_thread(
                _stubs.generate_stability,
                prompt=effective_prompt, aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
            )
        elif gen.model in {"seedream-3", "seededit"}:
            data = await asyncio.to_thread(
                _stubs.generate_seedream,
                prompt=effective_prompt, model=gen.model,
                reference_images=refs, aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
            )
        elif gen.model == "higgsfield-soul-img":
            data = await asyncio.to_thread(
                _stubs.generate_higgsfield_soul_img,
                prompt=effective_prompt, reference_images=refs,
                aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
            )
        else:
            raise ValueError(f"Unknown image model: {gen.model}")

        dest = _output_dir(gen.gen_id) / "result.png"
        atomic_write_bytes(dest, data)
        info = model_info(gen.model) or {}
        _persist(
            gen,
            output_path=str(dest),
            status=GenStatus.DONE,
            completed_at=datetime.utcnow(),
            cost_usd=getattr(settings, info.get("price_setting", ""), 0.0) or 0.0,
        )
    except ProviderNotConfigured as e:
        _persist(gen, status=GenStatus.FAILED, error=str(e),
                 completed_at=datetime.utcnow())
    except Exception as e:
        _persist(gen, status=GenStatus.FAILED, error=f"{type(e).__name__}: {e}",
                 completed_at=datetime.utcnow())


def _openai_size_for(aspect: str | None) -> str:
    """Map UI aspect-ratio chip to an OpenAI Image size."""
    if not aspect:
        return settings.image_size
    return {
        "1:1":  "1024x1024",
        "9:16": "1024x1792",
        "16:9": "1792x1024",
        "4:5":  "1024x1280",
    }.get(aspect, settings.image_size)


# --- video generation ----------------------------------------------------------------

async def run_video_gen(gen_id: str) -> None:
    s = store()
    gen = s.get_generation(gen_id)
    if gen is None or not gen.reference_paths:
        if gen is not None:
            _persist(gen, status=GenStatus.FAILED,
                     error="video generation requires one reference image",
                     completed_at=datetime.utcnow())
        return
    _persist(gen, status=GenStatus.RUNNING)

    # AI Director — vision-aware cinematic shot expansion that looks at the
    # actual reference frame. Higher quality than text-only enrichment for
    # video, because the agent can describe pose / lighting / what's in the
    # frame and write a shot direction that fits.
    if gen.use_director and not gen.director_prompt and gen.reference_paths:
        from character_swap import prompt_director
        ref_path = Path(gen.reference_paths[0])
        plan = await asyncio.to_thread(
            prompt_director.direct_movement,
            scenes=[("scene", ref_path, [ref_path], gen.prompt)],
            job_id=gen_id,
        )
        if plan is not None:
            mapping = plan.as_dict()
            if mapping.get("scene"):
                gen.director_prompt = mapping["scene"]
                _persist(gen)

    # Prompt enrichment for image-to-video: expand short directions
    # ("him pouring oil") into cinematic shot descriptions with camera
    # movement + performance cues. Closes the gap to Grok Imagine's web UI
    # which does this internally before sending to the video model. Skipped
    # if Director already provided a tailored prompt.
    if gen.enrich_prompt and not gen.enriched_prompt and not gen.director_prompt:
        from character_swap import prompt_enrich
        enriched = await asyncio.to_thread(
            prompt_enrich.enrich_prompt, gen.prompt, "video", job_id=gen_id,
        )
        if enriched and enriched != gen.prompt:
            gen.enriched_prompt = enriched
            _persist(gen)

    image_path = Path(gen.reference_paths[0])
    dest = _output_dir(gen.gen_id) / "result.mp4"
    # Precedence: director > enriched > raw.
    effective_prompt = gen.director_prompt or gen.enriched_prompt or gen.prompt

    try:
        if gen.model == "grok-imagine":
            provider_id = await asyncio.to_thread(
                grok.submit,
                image=image_path,
                prompt=effective_prompt,
                character="freeform",
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=provider_id)
            # Reuse pipeline's poll loop.
            from character_swap import pipeline
            await asyncio.to_thread(
                pipeline.wait_for_video,
                job_id=provider_id,
                character_name="freeform",
                dest=dest,
                app_job_id=gen_id,
            )
        elif gen.model == "veo":
            op_id = await asyncio.to_thread(
                google_genai.submit_veo,
                image=image_path,
                prompt=effective_prompt,
                aspect_ratio=gen.aspect_ratio,
                duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=op_id)
            await asyncio.to_thread(google_genai.wait_for_veo, op_id=op_id, dest=dest)
        elif (gen.model in kling.KLING_MODELS
              or gen.model in kling.LEGACY_ALIASES):
            # Pass the slug through — `submit_kling._resolve_model_name`
            # maps to the canonical Kling API model_name (handles legacy
            # aliases + falls back to v2-master for unknown slugs).
            task_id = await asyncio.to_thread(
                kling.submit_kling,
                image=image_path, prompt=effective_prompt,
                model=gen.model,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=task_id)
            await asyncio.to_thread(kling.wait_for_kling, task_id=task_id, dest=dest)
        elif gen.model == "veo-3-fast":
            op_id = await asyncio.to_thread(
                google_genai.submit_veo,
                image=image_path, prompt=effective_prompt,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=op_id)
            await asyncio.to_thread(google_genai.wait_for_veo, op_id=op_id, dest=dest)
        elif gen.model == "runway-gen4" or gen.model == "runway-gen3-alpha":
            task_id = await asyncio.to_thread(
                _stubs.submit_runway,
                image=image_path, prompt=effective_prompt,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=task_id)
            await asyncio.to_thread(_stubs.wait_for_runway, task_id=task_id, dest=dest)
        elif gen.model == "luma-ray2":
            task_id = await asyncio.to_thread(
                _stubs.submit_luma,
                image=image_path, prompt=effective_prompt,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=task_id)
            await asyncio.to_thread(_stubs.wait_for_luma, task_id=task_id, dest=dest)
        elif gen.model == "pika-2":
            task_id = await asyncio.to_thread(
                _stubs.submit_pika,
                image=image_path, prompt=effective_prompt,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=task_id)
            await asyncio.to_thread(_stubs.wait_for_pika, task_id=task_id, dest=dest)
        elif gen.model in {"hailuo-02", "hailuo-01"}:
            task_id = await asyncio.to_thread(
                _stubs.submit_minimax,
                image=image_path, prompt=effective_prompt, model=gen.model,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=task_id)
            await asyncio.to_thread(_stubs.wait_for_minimax, task_id=task_id, dest=dest)
        elif gen.model == "sora-2":
            task_id = await asyncio.to_thread(
                _stubs.submit_sora,
                image=image_path, prompt=effective_prompt,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=task_id)
            await asyncio.to_thread(_stubs.wait_for_sora, task_id=task_id, dest=dest)
        elif gen.model.startswith("wan-"):
            task_id = await asyncio.to_thread(
                _stubs.submit_wan,
                image=image_path, prompt=effective_prompt, model=gen.model,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=task_id)
            await asyncio.to_thread(_stubs.wait_for_wan, task_id=task_id, dest=dest)
        elif gen.model == "seedance":
            task_id = await asyncio.to_thread(
                _stubs.submit_seedance,
                image=image_path, prompt=effective_prompt,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=task_id)
            await asyncio.to_thread(_stubs.wait_for_seedance, task_id=task_id, dest=dest)
        elif gen.model.startswith("higgsfield-"):
            task_id = await asyncio.to_thread(
                _stubs.submit_higgsfield,
                image=image_path, prompt=effective_prompt, model=gen.model,
                aspect_ratio=gen.aspect_ratio, duration_secs=gen.duration_secs,
                app_job_id=gen_id,
            )
            _persist(gen, provider_job_id=task_id)
            await asyncio.to_thread(_stubs.wait_for_higgsfield, task_id=task_id, dest=dest)
        else:
            raise ValueError(f"Unknown video model: {gen.model}")

        info = model_info(gen.model) or {}
        _persist(
            gen,
            output_path=str(dest),
            status=GenStatus.DONE,
            completed_at=datetime.utcnow(),
            cost_usd=getattr(settings, info.get("price_setting", ""), 0.0) or 0.0,
        )
    except ProviderNotConfigured as e:
        _persist(gen, status=GenStatus.FAILED, error=str(e),
                 completed_at=datetime.utcnow())
    except Exception as e:
        _persist(gen, status=GenStatus.FAILED, error=f"{type(e).__name__}: {e}",
                 completed_at=datetime.utcnow())


# --- avatar generation (HeyGen) ------------------------------------------------------

async def run_avatar_gen(gen_id: str) -> None:
    """Talking-head avatar video. Two variants:
      - heygen-avatar-5: needs avatar_id + voice_id (HeyGen catalogue avatar)
      - heygen-photo-avatar: needs reference_paths[0] + voice_id (user's photo)"""
    s = store()
    gen = s.get_generation(gen_id)
    if gen is None:
        return
    if not gen.voice_id:
        _persist(gen, status=GenStatus.FAILED,
                 error="voice_id required for avatar generation",
                 completed_at=datetime.utcnow())
        return
    _persist(gen, status=GenStatus.RUNNING)
    dest = _output_dir(gen.gen_id) / "result.mp4"
    use_elevenlabs = (gen.voice_provider == "elevenlabs")

    try:
        # Step 1: when ElevenLabs is the voice source, render the script to mp3
        # first, then hand the audio file to HeyGen. Otherwise HeyGen does its
        # own TTS using its `voice_id`.
        audio_path: Path | None = None
        if use_elevenlabs:
            audio_bytes = await asyncio.to_thread(
                elevenlabs.text_to_speech,
                voice_id=gen.voice_id, text=gen.prompt, app_job_id=gen_id,
            )
            audio_path = _output_dir(gen.gen_id) / "voice.mp3"
            atomic_write_bytes(audio_path, audio_bytes)

        # Step 2: submit to HeyGen, using either audio-input or built-in TTS.
        if gen.model == "heygen-avatar-5":
            if not gen.avatar_id:
                raise ValueError("avatar_id required for heygen-avatar-5")
            if audio_path is not None:
                video_id = await asyncio.to_thread(
                    heygen.submit_avatar_video_with_audio,
                    avatar_id=gen.avatar_id, image=None, audio=audio_path,
                    aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
                )
            else:
                video_id = await asyncio.to_thread(
                    heygen.submit_avatar_video,
                    avatar_id=gen.avatar_id, voice_id=gen.voice_id,
                    script=gen.prompt, aspect_ratio=gen.aspect_ratio,
                    app_job_id=gen_id,
                )
        elif gen.model == "heygen-photo-avatar":
            if not gen.reference_paths:
                raise ValueError("reference image required for heygen-photo-avatar")
            image_path = Path(gen.reference_paths[0])
            if audio_path is not None:
                video_id = await asyncio.to_thread(
                    heygen.submit_avatar_video_with_audio,
                    avatar_id=None, image=image_path, audio=audio_path,
                    aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
                )
            else:
                video_id = await asyncio.to_thread(
                    heygen.submit_photo_avatar,
                    image=image_path,
                    voice_id=gen.voice_id, script=gen.prompt,
                    aspect_ratio=gen.aspect_ratio, app_job_id=gen_id,
                )
        else:
            raise ValueError(f"Unknown avatar model: {gen.model}")
        _persist(gen, provider_job_id=video_id)
        await asyncio.to_thread(heygen.wait_for_avatar_video,
                                 video_id=video_id, dest=dest)

        info = model_info(gen.model) or {}
        _persist(
            gen,
            output_path=str(dest),
            status=GenStatus.DONE,
            completed_at=datetime.utcnow(),
            cost_usd=getattr(settings, info.get("price_setting", ""), 0.0) or 0.0,
        )
    except ProviderNotConfigured as e:
        _persist(gen, status=GenStatus.FAILED, error=str(e),
                 completed_at=datetime.utcnow())
    except Exception as e:
        _persist(gen, status=GenStatus.FAILED, error=f"{type(e).__name__}: {e}",
                 completed_at=datetime.utcnow())


# --- audio generation (ElevenLabs Voice Changer / TTS) -------------------------------

_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}


async def run_audio_gen(gen_id: str) -> None:
    """ElevenLabs audio generation:
      - elevenlabs-vc: source audio (or video — we extract the audio first)
        + voice_id → re-rendered mp3. If the source was a video, we ALSO
        re-mux the new audio back into the original video stream and the
        output becomes an mp4 (so creators get a "same clip, new voice"
        result in one step).
      - elevenlabs-tts: prompt-as-text + voice_id → spoken mp3"""
    s = store()
    gen = s.get_generation(gen_id)
    if gen is None:
        return
    if not gen.voice_id:
        _persist(gen, status=GenStatus.FAILED,
                 error="voice_id required for audio generation",
                 completed_at=datetime.utcnow())
        return
    _persist(gen, status=GenStatus.RUNNING)
    out_dir = _output_dir(gen.gen_id)
    dest_mp3 = out_dir / "result.mp3"
    dest_mp4 = out_dir / "result.mp4"

    try:
        if gen.model == "elevenlabs-vc":
            if not gen.reference_paths:
                raise ValueError("source audio required for elevenlabs-vc")
            source = Path(gen.reference_paths[0])
            is_video = source.suffix.lower() in _VIDEO_EXTS

            # If the upload is a video, extract its audio track first so
            # ElevenLabs gets something it can chew on.
            if is_video:
                from character_swap import video_edit
                source_for_vc = source.with_suffix(".extracted.wav")
                # Extract 16kHz mono wav (Whisper-style — VC is fine with this).
                await asyncio.to_thread(
                    video_edit._run,
                    [video_edit._ffmpeg(), "-y", "-i", str(source),
                     "-vn", "-ac", "1", "-ar", "16000", str(source_for_vc)],
                )
            else:
                source_for_vc = source

            data = await asyncio.to_thread(
                elevenlabs.voice_changer,
                voice_id=gen.voice_id,
                source_audio=source_for_vc,
                app_job_id=gen_id,
            )
            atomic_write_bytes(dest_mp3, data)

            # Video in → re-mux the new audio onto the original video stream
            # and serve that as the primary output.
            output_path: Path = dest_mp3
            if is_video:
                from character_swap import video_edit
                await asyncio.to_thread(
                    video_edit.replace_audio, source, dest_mp3, dest_mp4,
                )
                output_path = dest_mp4
                # Clean up the intermediate audio extraction; keep result.mp3
                # so users can also pull the standalone voiced audio.
                source_for_vc.unlink(missing_ok=True)

            info = model_info(gen.model) or {}
            _persist(
                gen,
                output_path=str(output_path),
                status=GenStatus.DONE,
                completed_at=datetime.utcnow(),
                cost_usd=getattr(settings, info.get("price_setting", ""), 0.0) or 0.0,
            )
            return

        elif gen.model == "elevenlabs-tts":
            data = await asyncio.to_thread(
                elevenlabs.text_to_speech,
                voice_id=gen.voice_id, text=gen.prompt,
                app_job_id=gen_id,
            )
        else:
            raise ValueError(f"Unknown audio model: {gen.model}")

        atomic_write_bytes(dest_mp3, data)
        info = model_info(gen.model) or {}
        _persist(
            gen,
            output_path=str(dest_mp3),
            status=GenStatus.DONE,
            completed_at=datetime.utcnow(),
            cost_usd=getattr(settings, info.get("price_setting", ""), 0.0) or 0.0,
        )
    except ProviderNotConfigured as e:
        _persist(gen, status=GenStatus.FAILED, error=str(e),
                 completed_at=datetime.utcnow())
    except Exception as e:
        _persist(gen, status=GenStatus.FAILED, error=f"{type(e).__name__}: {e}",
                 completed_at=datetime.utcnow())
