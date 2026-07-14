"""
Model registry for image + video generation.

Formerly also hosted the free-form Image/Video/Audio/Avatar tab runners;
those tabs were removed (app reduced to Swap/Animate/Reengineer/Editor) and
only the registries + spec helpers remain. Consumers: the Swap Step-4 /
Reengineer model pickers and duration specs (api.py, runner.py,
runner_reengineer.py) and the end-frame gate (runner._resolve_end_image).
"""
from __future__ import annotations


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
    # fal-hosted instruction-edit swap engines. Set picked by the 2026-06-10
    # overnight bake-off (56 generations judged by Claude vision against
    # Hugo's criteria: scene fidelity / identity / integration / organic
    # realism / artifacts; gallery in eval_out/):
    #   nbp-swap won outright (7.2-7.6 composite, zero fatals, survives
    #   moderation-sensitive scenes that GPT Image refuses). nb2-swap gives
    #   nearly the same look at about half the price; Seedream 4.5 is the
    #   budget tier (weaker identity match).
    # Removed by the same data: higgsfield-swap (Soul regenerates an
    # unrelated scene — the "horrendous" failure mode), qwen-edit-swap
    # (ignored the scene entirely), kontext-max-swap (identity loss +
    # censorship blackouts). Their dispatch branches remain so OLD jobs keep
    # working, but the slugs are no longer offered in the picker.
    "nbp-swap":             {"label": "Nano Banana Pro Swap (Google via fal)", "provider": "fal",   "price_setting": "fal_swap_price_usd"},
    # GPT Image 2 identity-first swap: FLIPPED reference order ([char, scene])
    # — GPT preserves the FIRST input's face with extra richness — plus a
    # compact prompt with the organic phone-photo styling. The identity-
    # strongest option for hard cases (background swap + custom outfit);
    # OpenAI moderation still refuses some skin-heavy scenes.
    "gpt2-id-swap":         {"label": "GPT Image 2 — Identity First",          "provider": "openai", "price_setting": "openai_image_price_usd"},
    "nb2-swap":             {"label": "Nano Banana 2 Swap (Google via fal)",   "provider": "fal",   "price_setting": "fal_swap_price_usd"},
    "seedream-edit-swap":   {"label": "Seedream 4.5 Edit Swap (fal)",          "provider": "fal",   "price_setting": "fal_swap_price_usd"},
}

VIDEO_MODELS: dict[str, dict] = {
    # Per-model `duration_options` lists the seconds-values each provider's
    # API actually accepts (and `duration_default` is what we pre-select in
    # the Step-4 dropdown). When the user picks a duration, the value flows
    # through Job.duration_secs → pipeline.submit_video → the provider's
    # submit function. Sources for the numbers:
    # - Grok Imagine: xAI accepts an int in [5, 15], we clamp before submit
    # - Kling (all variants): API docs say `duration` is a STRING "5" or "10"
    # - Runway Gen-4 / Gen-3 Alpha: their REST exposes 5 and 10
    # - Luma Ray-2: 5 and 9
    # - Pika 2.2: 5 fixed
    # - MiniMax Hailuo 01/02: 6 fixed
    # - Sora 2: 5/10/15/20
    # - Wan 2.x: 5 fixed
    # - Seedance: 5 or 10
    # - Higgsfield Soul (video) / DoP: 5
    # - Higgsfield Lipsync / Speak: 10/15/20/30 (audio-length-driven)
    "grok-imagine":         {"label": "Grok Imagine",                    "provider": "xai",        "price_setting": "grok_video_price_usd",   "duration_options": [5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], "duration_default": 5},
    # Veo 3.1 Fast routed through fal.ai — fal's i2v endpoint accepts 4/6/8s
    # (sent as "4s"/"6s"/"8s") and renders at settings.veo_fal_resolution
    # (default 1080p). Supports a per-scene END FRAME (end_frame True → honors a
    # 🎯 end pose): when one is set the client routes to fal's separate
    # `first-last-frame-to-video` endpoint (start→end interpolation), else the
    # plain image-to-video endpoint. Bills on FAL.
    # (The Gemini-path "veo" / "veo-3-fast" entries were removed 2026-07 — their
    # submit path, google_genai.submit_veo, is an unimplemented stub.)
    "veo-3.1-fast":         {"label": "Veo 3.1 Fast (fal)",              "provider": "fal",        "price_setting": "veo_price_usd",          "duration_options": [4, 6, 8], "duration_default": 8, "end_frame": True},
    # Grok Imagine 1.5 routed through fal.ai (image-to-video) — xAI's newest
    # Grok video model. Integer duration 3–15s, native synced audio ALWAYS on,
    # renders at settings.grok_fal_resolution (default 720p). No end-frame on
    # this endpoint, so a scene overridden to it ignores its end pose (same
    # soft-degrade as veo-3.1-fast). Bills on FAL_API_KEY.
    "grok-imagine-1.5":     {"label": "Grok Imagine 1.5 (fal)",          "provider": "fal",        "price_setting": "grok_video_price_usd",   "duration_options": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], "duration_default": 5},
    # Seedance 2.0 routed through fal.ai (image-to-video). ByteDance's newest;
    # the ONLY fal video model besides Kling 3.0 with start→end-frame
    # interpolation (end_frame True → a scene on it honors its 🎯 end pose).
    # Integer duration 4–15s, native synced audio. Tier (standard/fast) +
    # resolution via SEEDANCE_FAL_* env (default standard/720p). Priciest model
    # in the stack ($0.302/s @720p standard). Bills on FAL_API_KEY.
    "seedance-2.0":         {"label": "Seedance 2.0 (fal)",              "provider": "fal",        "price_setting": "seedance_price_usd",     "duration_options": [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], "duration_default": 5, "end_frame": True},
    # Kling — every confirmed model_name string from Kling's official i2v API
    # (Singapore region, May 2026). Slug == API name to keep the mapping
    # trivial in `kling._resolve_model_name`. Legacy aliases (`kling`,
    # `kling-2.1-pro`) still resolve via LEGACY_ALIASES for old jobs.
    # NB: v3 / v3-omni / o1 are NOT included — Kling's marketing lists them
    # but no public-leaning source confirms the API model_name strings.
    # Add them here once Hugo verifies against the live dev dashboard.
    "kling-v1":             {"label": "Kling 1.0",                       "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-v1-5":           {"label": "Kling 1.5",                       "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-v1-6":           {"label": "Kling 1.6",                       "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-v2-master":      {"label": "Kling 2.0 Master",                "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-v2-1":           {"label": "Kling 2.1",                       "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-v2-1-master":    {"label": "Kling 2.1 Master",                "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-v2-5-turbo":     {"label": "Kling 2.5 Turbo",                 "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-v2-6":           {"label": "Kling 2.6",                       "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-v3":             {"label": "Kling 3.0",                       "provider": "fal",        "price_setting": "kling_price_usd",        "duration_options": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15], "duration_default": 5, "end_frame": True},
    # Legacy slug aliases — Hugo's old jobs reference these strings;
    # `kling.LEGACY_ALIASES` maps them to the new model_names. Kept in
    # the registry so the dropdown still shows a sensible label.
    "kling":                {"label": "Kling 2.0 (legacy alias)",        "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-2.1-pro":        {"label": "Kling 2.1 Pro (legacy alias)",    "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "kling-1.6":            {"label": "Kling 1.6 (legacy alias)",        "provider": "kling",      "price_setting": "kling_price_usd",        "duration_options": [5, 10], "duration_default": 5},
    "runway-gen4":          {"label": "Runway Gen-4",                    "provider": "runway",     "price_setting": "runway_price_usd",       "duration_options": [5, 10], "duration_default": 5},
    "runway-gen3-alpha":    {"label": "Runway Gen-3 Alpha",              "provider": "runway",     "price_setting": "runway_price_usd",       "duration_options": [5, 10], "duration_default": 5},
    "luma-ray2":            {"label": "Luma Ray-2",                      "provider": "luma",       "price_setting": "luma_price_usd",         "duration_options": [5, 9],  "duration_default": 5},
    "pika-2":               {"label": "Pika 2.2",                        "provider": "pika",       "price_setting": "pika_price_usd",         "duration_options": [5],     "duration_default": 5},
    "hailuo-02":            {"label": "MiniMax Hailuo 02",               "provider": "minimax",    "price_setting": "minimax_price_usd",      "duration_options": [6],     "duration_default": 6},
    "hailuo-01":            {"label": "MiniMax Hailuo 01",               "provider": "minimax",    "price_setting": "minimax_price_usd",      "duration_options": [6],     "duration_default": 6},
    "sora-2":               {"label": "Sora 2",                          "provider": "openai",     "price_setting": "sora_price_usd",         "duration_options": [5, 10, 15, 20], "duration_default": 10},
    "wan-2.2":              {"label": "Wan 2.2",                         "provider": "alibaba",    "price_setting": "wan_price_usd",          "duration_options": [5],     "duration_default": 5},
    "wan-2.1":              {"label": "Wan 2.1",                         "provider": "alibaba",    "price_setting": "wan_price_usd",          "duration_options": [5],     "duration_default": 5},
    "seedance":             {"label": "Seedance",                        "provider": "bytedance",  "price_setting": "seedance_price_usd",     "duration_options": [5, 10], "duration_default": 5},
    "higgsfield-soul-vid":  {"label": "Higgsfield Soul (video)",         "provider": "higgsfield", "price_setting": "higgsfield_price_usd",   "duration_options": [5],     "duration_default": 5},
    "higgsfield-dop":       {"label": "Higgsfield DoP",                  "provider": "higgsfield", "price_setting": "higgsfield_price_usd",   "duration_options": [5, 8],  "duration_default": 5},
    "higgsfield-lipsync":   {"label": "Higgsfield Lipsync",              "provider": "higgsfield", "price_setting": "higgsfield_price_usd",   "duration_options": [10, 15, 20, 30], "duration_default": 10},
    "higgsfield-speak":     {"label": "Higgsfield Speak",                "provider": "higgsfield", "price_setting": "higgsfield_price_usd",   "duration_options": [10, 15, 20, 30], "duration_default": 10},
}

# The free-form Audio tab is gone, but the /api/generations/models payload
# must still carry an `audio` list with an availability-flagged elevenlabs-vc
# entry: app.js `elevenlabsAvailable()` gates every Editor/Step-6/repurpose
# voice-swap picker on it.
AUDIO_MODELS: dict[str, dict] = {
    "elevenlabs-vc": {"label": "ElevenLabs Voice Changer", "provider": "elevenlabs"},
}

# Video models that support a per-scene END FRAME (start→end interpolation).
# SINGLE SOURCE OF TRUTH — the runner's end-frame gate (runner._resolve_end_image),
# the /api/generations/models payload flag, and the frontend's end-frame UI all
# read this (derived from each row's `end_frame` flag). A model NOT in this set
# silently ignores any 🎯 end pose (e.g. Grok soft-degrades — but Veo 3.1
# Fast IS in the set via its first-last-frame endpoint).
END_FRAME_VIDEO_MODELS: frozenset[str] = frozenset(
    slug for slug, info in VIDEO_MODELS.items() if info.get("end_frame"))


def supports_end_frame(model: str) -> bool:
    """True if `model` interpolates start→end via a per-scene end frame."""
    return model in END_FRAME_VIDEO_MODELS


# When a video clip is rejected on CONTENT-POLICY / NSFW grounds by its chosen
# model, the runner retries it ONCE on this model (Hugo 2026-07-14; switched
# from seedance-2.0 to grok-imagine-1.5 on 2026-07-26 at Hugo's direction) —
# a DIFFERENT provider stack (xAI Grok via fal) that is markedly more permissive
# than Kling/Veo, so it passes clips they refuse.
#
# CAVEAT — this model does NOT support end frames (it is absent from
# END_FRAME_VIDEO_MODELS; `pipeline.submit_video` never forwards `end_image` for
# it). A clip WITH a resolved 🎯 end pose still falls back (Hugo's decision: a
# clip without the end pose beats no clip at all) but the dropped pose is
# recorded on `VideoVariant.fallback_dropped_end_frame` and surfaced in the UI —
# never silently. Use `fallback_drops_end_frame(chosen)` to detect the case.
# SINGLE SOURCE OF TRUTH — read by runner._animate_one_video and
# runner_reengineer._render_direct_clip.
VIDEO_MODERATION_FALLBACK_MODEL = "grok-imagine-1.5"


def fallback_drops_end_frame(chosen_model: str) -> bool:
    """True when falling back from `chosen_model` to the moderation fallback
    would LOSE a resolved end frame — i.e. the chosen model honors end frames
    but the fallback does not. Callers use this to flag the degradation loudly
    instead of shipping a silently different clip."""
    return (supports_end_frame(chosen_model)
            and not supports_end_frame(VIDEO_MODERATION_FALLBACK_MODEL))


def video_duration_spec(model: str) -> dict:
    """Return the {options, default} spec for a video model. Falls back to
    a single-value [env-default] spec when the model isn't registered."""
    info = VIDEO_MODELS.get(model)
    if not info or "duration_options" not in info:
        from character_swap.config import settings as _s
        return {"options": [_s.video_duration_secs],
                "default": _s.video_duration_secs}
    return {"options": list(info["duration_options"]),
            "default": info.get("duration_default", info["duration_options"][0])}


def model_info(model: str) -> dict | None:
    return IMAGE_MODELS.get(model) or VIDEO_MODELS.get(model)
