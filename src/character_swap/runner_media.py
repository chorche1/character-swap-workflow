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
    # Veo 3.1 Fast on GOOGLE'S OWN API (clients/google_veo.py) — the DEFAULT
    # Veo path since 2026-08-10 (Hugo's directive, after the measurement in
    # that client's docstring: fal's Veo deployment refuses 42-79% of this
    # app's clips per day while Kling on the same fal key refuses 0-7%, and
    # five frames fal refuses 90-100% of the time rendered here first try).
    # Same model, ~20-33% cheaper, honors the 🎯 end pose via `lastFrame`
    # (verified live, not assumed), native audio always on. Bills on
    # GEMINI_API_KEY — the PAID tier; the free tier serves no video models.
    "veo-3.1-fast-google":  {"label": "Veo 3.1 Fast",                    "provider": "gemini",     "price_setting": "google_veo_price_usd",   "duration_options": [4, 6, 8], "duration_default": 8, "end_frame": True},
    # Veo 3.1 Fast on VERTEX AI (clients/vertex_veo.py) — the same model a
    # third time, reached with a service account instead of an API key. The
    # point is the QUOTA: per project and region, visible in the Cloud Console
    # and raised by request, rather than gated behind a spend threshold. Absent
    # from the picker and from the host chain until VERTEX_PROJECT_ID and a
    # credentials file are both set. Bills on the Google Cloud project.
    "veo-3.1-fast-vertex":  {"label": "Veo 3.1 Fast (Vertex)",           "provider": "vertex",     "price_setting": "google_veo_price_usd",   "duration_options": [4, 6, 8], "duration_default": 8, "end_frame": True},
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
    # NOTE — there is deliberately no `seedance-2.0-fast` slug. It was added
    # 2026-08-04 for the Veo rescue and removed the same day: ByteDance refuses
    # every frame containing a real person (11/11 measured, both tiers), so it
    # can render nothing this app produces. See VEO_MODERATION_FALLBACK_MODEL.
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

# VEO + SPANISH rescue — ALWAYS ON, no env flag (Hugo 2026-08-04, after the
# measurement below: "fallback to seedance 2.0 fast for all veo clips in swap
# that fails" — Seedance turned out to be impossible, Kling 3.0 replaced it).
#
# Veo is the model the 🗣 language redirect sends every German/Spanish clip to,
# and fal's content checker refuses a large share of them even at the least
# strict `safety_tolerance` — measured on j_619e0a2cf2, 11 of 24 redirected
# Spanish clips died refused, i.e. 46% of the spoken-language clips in one run,
# on dialogue as innocuous as "Mezcla una cucharada de aceite de coco". Those
# clips have nowhere to go under the generic rescue: it is off by default, and
# grok-imagine-1.5 would undo the redirect.
#
# WHY NOT SEEDANCE 2.0 FAST, which this rescue was originally built on:
# ByteDance refuses EVERY frame containing a real person. Measured 2026-08-04 —
# 11 of 11 submits refused (7 in-app + 4 probes), on BOTH tiers, across three
# different faces, always the same shape:
#     loc: [body, image_url] · content_policy_violation
#     "The images or videos provided may contain likenesses of real people…"
#     ctx.extra_info.reason = partner_validation_failed
# A control frame with NO person in it rendered fine on the same endpoint and
# key, so this is ByteDance's real-people policy, not our account and not the
# prompt. Every frame this app produces is a photoreal person, so Seedance can
# never rescue a single clip here — no prompt change reaches an image check.
# Do not re-try this model as a rescue; re-run the probe first if in doubt.
#
# WHY KLING 3.0: it is the only candidate that is both ACCEPTED and GOOD.
# Measured on the clips Veo had actually refused, re-rendered verbatim (same
# frame, same localized prompt): kling-v3 2/2 rendered, grok-imagine-1.5 2/2.
# Spanish speech fidelity over every Spanish clip on disk, scored the way video
# QC scores it (Scribe transcript vs the line the prompt asked for):
#     veo-3.1-fast      n=37   mean 0.992   min 0.909   100% ≥0.8
#     kling-v3          n=106  mean 0.953   min 0.000    98% ≥0.8
#     grok-imagine-1.5  n=6    mean 1.000                100% ≥0.8
# Kling costs ~0.04 mean against Veo and carries a ~1% English-leak tail (the
# one 0.000 clip spoke English on a Spanish prompt) — which the existing
# wrong-language net re-renders. It wins over Grok on the two things that make
# a rescued clip fit the reel it lands in: it is IN END_FRAME_VIDEO_MODELS so
# the 🎯 end pose survives, and it is the runs' own default model, so the
# rescued clip looks and sounds like its neighbours instead of announcing
# itself. Grok's 1.000 is 6 clips — too thin to outweigh either.
#
# SPANISH ONLY (Hugo 2026-08-04). A refused GERMAN clip still fails loudly.
# Kling is measured BAD at German — 0.48 mean word-similarity against 1.00
# English / 0.93 Spanish from the same runs, which is the whole reason
# SPOKEN_LANGUAGE_VIDEO_MODEL exists. Rescuing German onto it would ship a clip
# the language net then has to reject anyway: slower, more expensive, same
# outcome. English clips are not redirected to Veo in the first place.
#
# Scope note (superseded 2026-08-10 by VIDEO_FALLBACK_CHAIN below): this rescue
# used to be the ONLY always-on one, for (model ∈ VEO_VIDEO_MODELS) AND
# (language ∈ VEO_FALLBACK_LANGUAGES). Kling 3.0 is now simply the FIRST leg of
# the chain every non-German clip walks, so the Spanish case keeps exactly the
# model, order and measurements above — it is no longer a special case in code.
# Both hosts of Veo 3.1 Fast. Membership drives the `no_media_generated`
# refusal shape in `triggers_fallback` — a fal-ism, but harmless on the Google
# leg, which reports its own filtering as an explicit error instead.
VEO_VIDEO_MODELS: frozenset[str] = frozenset({"veo-3.1-fast",
                                              "veo-3.1-fast-google"})
VEO_MODERATION_FALLBACK_MODEL = "kling-v3"
VEO_FALLBACK_LANGUAGES: frozenset[str] = frozenset({"es"})

# THE REROUTE CHAIN (Hugo 2026-08-10: "för alla utom de tyska ska det
# automatiskt reroutas till andra modeller efter dessa försök, kling och sedan
# grok"). After the unchanged re-submits on its own model (VIDEO_REFUSAL_RETRIES)
# a refused clip walks this list, ONE take per model, and only fails once every
# leg has refused it.
#
# ORDER IS THE MEASUREMENT, not a preference. Kling 3.0 first because a rescued
# clip has to fit the reel it lands in: it is IN END_FRAME_VIDEO_MODELS so a 🎯
# end pose survives, it is the runs' own default model so the clip looks and
# sounds like its neighbours, and it refuses only 0-7% of its own calls (measured
# per day 08-03…08-09) against Veo's 32-79%. Grok last because it is the most
# permissive stack in the app (a different provider entirely, xAI via fal) but
# drops the end pose and announces itself in a reel — a last resort, which is
# what it always was.
#
# GERMAN IS EXCLUDED (Hugo's carve-out, unchanged from 2026-08-04): Kling scores
# 0.48 mean word-similarity on German against 1.00 English / 0.93 Spanish from
# the same runs — that gap is the entire reason SPOKEN_LANGUAGE_VIDEO_MODEL
# exists. Rerouting a German clip there ships one the wrong-language net has to
# reject anyway: slower, dearer, same outcome. A refused German clip therefore
# still fails LOUDLY after its unchanged takes.
#
# WHAT THIS COSTS, stated plainly because Hugo turned this exact rescue OFF on
# 2026-08-03 and is now turning it back on with eyes open: a rescued clip is
# rendered by a different provider than its neighbours (visibly and audibly), a
# Grok leg loses any resolved 🎯 end pose (flagged, never silent — see
# `drops_end_frame`), and for a 🗣 character it renders off
# SPOKEN_LANGUAGE_VIDEO_MODEL. The trade he chose: a clip that exists beats a
# clip that does not.
VIDEO_FALLBACK_CHAIN: tuple[str, ...] = (VEO_MODERATION_FALLBACK_MODEL,
                                         VIDEO_MODERATION_FALLBACK_MODEL)

# Languages whose refused clips get NO reroute at all — see the German note
# above. The 🗣 flag's ISO code; None (English) is never in here.
NO_FALLBACK_LANGUAGES: frozenset[str] = frozenset({"de"})

# Veo's SECOND refusal shape — fal `no_media_generated` (2026-08-04). Veo does
# not always answer a prompt it dislikes with `content_policy_violation`;
# sometimes it accepts the submit, runs, and returns NOTHING, which fal reports
# as: "The model did not generate the expected output for this prompt. This may
# occur for several reasons, including unsafe content, …". 4 of the 11 refused
# Spanish clips in j_619e0a2cf2 failed this way on re-render, on lines as
# innocuous as a teeth-whitening pitch — `content_policy.is_content_rejection`
# matches none of its wording, so those clips were dying with no rescue.
#
# It is treated as a refusal ONLY on the Veo leg, and it is NOT added to
# content_policy's global signal list: that detector also drives the image
# moderation ladder and the generic video rescue, and `no_media_generated` is
# a fal-wide catch-all whose other causes (incompatible media type, missing
# attachment) are real bugs that must keep failing loudly elsewhere. On Veo
# specifically none of those apply — we always send an image + prompt to an
# i2v endpoint — and it is definitively not a timeout / network / balance
# failure, which is the line Hugo drew when he scoped this rescue to genuine
# refusals.
VEO_EMPTY_OUTPUT_SIGNALS: tuple[str, ...] = (
    "no_media_generated", "did not generate the expected output",
)


def triggers_fallback(chosen_model: str, exc: BaseException) -> bool:
    """True when `exc` from `chosen_model` should be rescued on the fallback.

    A genuine content rejection always qualifies. On Veo, an empty-output
    refusal (`no_media_generated`) qualifies too — see VEO_EMPTY_OUTPUT_SIGNALS.
    Everything else (timeout, network, fal balance) keeps the loud-fail path.
    """
    from character_swap import content_policy
    if content_policy.is_content_rejection(exc):
        return True
    if chosen_model in VEO_VIDEO_MODELS:
        msg = str(exc).lower()
        return any(sig in msg for sig in VEO_EMPTY_OUTPUT_SIGNALS)
    return False

# Every clip of a 🗣 language-flagged character renders on THIS model, whatever
# model the run/scene/clip picked (Hugo 2026-08-03). Measured: Kling 3.0 scored
# 0.48 mean word-similarity on German against 1.00 English / 0.93 Spanish from
# the same runs, and shipped plain non-words; the same two lines re-rendered on
# veo-3.1-fast scored 0.95 / 0.93. See runner._language_video_model for the
# full numbers. It DOES honor 🎯 end frames (it is in END_FRAME_VIDEO_MODELS),
# so the redirect costs no end pose — but it accepts ONLY 4/6/8 s, hence
# `language_clip_secs` below. SINGLE SOURCE OF TRUTH — read by
# runner._language_video_model and the api duration/cost previews.
# Points at the GOOGLE-hosted Veo since 2026-08-10 (Hugo: make Google the
# default for every Veo clip). The measurement behind the move is in
# clients/google_veo.py; the model, the speech fidelity and the 🎯 end-frame
# support are identical, the refusals are not. Old clips keep whatever slug
# they were submitted under, so resume/salvage still polls the right host —
# that is the whole reason this is a NEW slug rather than a repointed one.
# The DEFAULT host; `language_video_model()` is what callers should use, since
# the chain is configurable and its first entry is what a clip actually starts
# on. Kept as a constant because a dozen call sites and tests read it.
SPOKEN_LANGUAGE_VIDEO_MODEL = "veo-3.1-fast-google"


def language_video_model() -> str:
    """The host every 🗣 clip STARTS on — the first reachable entry in the Veo
    host chain (`VEO_HOST_ORDER`). Falls back to the constant above when no
    host is configured, so the redirect can never resolve to nothing."""
    chain = veo_host_chain()
    return chain[0] if chain else SPOKEN_LANGUAGE_VIDEO_MODEL


def language_clip_secs(secs: float | int | None) -> int:
    """Snap a clip length to what `SPOKEN_LANGUAGE_VIDEO_MODEL` accepts.

    Rounds UP to the next allowed value (Hugo 2026-08-03) — a redirected clip
    must never come out SHORTER than asked for, or the tail of the line is cut
    off mid-word. An ask above the model's ceiling snaps DOWN to it: 9 s and
    10 s become 8 s, because the model cannot render longer. Callers flag that
    case (`language_clip_truncated`) so the shortening is never silent.
    """
    opts = sorted(VIDEO_MODELS[SPOKEN_LANGUAGE_VIDEO_MODEL]["duration_options"])
    if secs is None:
        return int(VIDEO_MODELS[SPOKEN_LANGUAGE_VIDEO_MODEL]["duration_default"])
    return int(next((o for o in opts if o >= secs), opts[-1]))


def language_clip_truncated(secs: float | int | None) -> bool:
    """True when `language_clip_secs` had to SHORTEN the requested length —
    the ask exceeds what the redirect model can render."""
    return secs is not None and language_clip_secs(secs) < secs


# THE VEO HOST CHAIN (Hugo 2026-08-10, explicitly temporary — see
# config.veo_host_order for why the order is what it is).
#
# Veo 3.1 Fast is reachable on two hosts today: fal (`veo-3.1-fast`) and
# Google's own API (`veo-3.1-fast-google`). They differ ONLY in who moderates
# and who rate-limits — same model, same speech, same 🎯 end-frame support:
#   fal     refuses 46% of these clips (measured over 127 identical frames)
#           but has no practical daily ceiling.
#   Google  refuses almost none of them (5/5 on frames fal refuses 90-100% of
#           the time) but accepted ~14 videos a day on Tier 1 — measured, and
#           not enough for five 40-clip runs.
# So a clip takes ONE swing at fal and only what fal refuses spends Google's
# scarce capacity. A third host (Vertex) slots in here the moment it has
# credentials — it is the same model again, with per-project quotas that can
# actually be raised.
_VEO_HOST_SLUGS: dict[str, str] = {
    "fal": "veo-3.1-fast",
    "google": "veo-3.1-fast-google",
    "vertex": "veo-3.1-fast-vertex",
}


def veo_host_chain() -> list[str]:
    """Model slugs for the Veo hosts, in the configured order, keeping only
    the ones whose provider key is actually present.

    A host we cannot reach must never appear: it would consume a leg of the
    chain and turn one clear error into two."""
    from character_swap.config import settings
    out: list[str] = []
    for name in (settings.veo_host_order or "").split(","):
        slug = _VEO_HOST_SLUGS.get(name.strip().lower())
        if not slug or slug in out:
            continue
        provider = (VIDEO_MODELS.get(slug) or {}).get("provider")
        if not provider or not settings.has_provider(provider):
            continue
        if provider == "vertex":
            # Stricter than a key check: the JSON file must actually exist, or
            # every clip routed here dies at token-mint time.
            from character_swap.clients import vertex_veo
            if not vertex_veo.configured():
                continue
        out.append(slug)
    return out or [_VEO_HOST_SLUGS["google"]]


def veo_host_takes(slug: str, *, is_last: bool) -> int:
    """How many takes a clip gets on one host before moving on.

    fal gets exactly one (Hugo): its refusals are image-driven, so a second
    identical take there is far less likely to help than moving on. The LAST
    host keeps the full VIDEO_REFUSAL_RETRIES budget — there is nowhere left
    to go, so patience is all that is left."""
    from character_swap.config import settings
    if is_last:
        return 1 + max(0, settings.video_refusal_retries)
    if slug == _VEO_HOST_SLUGS["fal"]:
        return max(1, settings.veo_fal_takes)
    return 1 + max(0, settings.video_refusal_retries)


def is_veo_host(slug: str | None) -> bool:
    return slug in set(_VEO_HOST_SLUGS.values())


def video_fallback_chain(chosen_model: str | None = None, *,
                         language: str | None = None) -> list[str]:
    """The ordered models a refused clip is rerouted to after its own model has
    refused every unchanged take — `VIDEO_FALLBACK_CHAIN` minus the model it is
    already on, empty when no reroute applies.

    Hugo 2026-08-10: every clip EXCEPT a German one walks Kling → Grok. German
    gets nothing (Kling is measured 0.48 on it); `VIDEO_MODERATION_FALLBACK=0`
    turns the reroute off for everyone, which is then the German behaviour for
    the whole run.

    The chosen model is filtered out rather than repeated: it has already had
    `1 + VIDEO_REFUSAL_RETRIES` takes by the time this list is walked, so
    another one of the same is not a reroute — it is the take budget over
    again. SINGLE SOURCE OF TRUTH for both runners' attempt loops.
    """
    if (language or "") in NO_FALLBACK_LANGUAGES:
        return []
    from character_swap.config import settings
    if not settings.video_moderation_fallback:
        return []
    return [m for m in VIDEO_FALLBACK_CHAIN if m != chosen_model]


def video_fallback_model(chosen_model: str | None = None, *,
                         language: str | None = None) -> str | None:
    """The FIRST model a refused clip on `chosen_model` reroutes to, or None
    when no reroute applies. Thin reader over `video_fallback_chain` — kept
    because callers that only need "is there a rescue, and what does it look
    like" (cost previews, log lines) predate the chain.
    """
    chain = video_fallback_chain(chosen_model, language=language)
    return chain[0] if chain else None


def video_attempt_models(chosen_model: str, *,
                         language: str | None = None) -> list[str]:
    """The ordered list of models one clip is attempted on, refusal by refusal.

    `[chosen] * (1 + VIDEO_REFUSAL_RETRIES)` followed by `video_fallback_chain`
    — so a REFUSED clip is first re-submitted UNCHANGED on its own model (Hugo
    2026-08-06) and only then rerouted, one take per rescue model (Hugo
    2026-08-10). Ordering is deliberate: a Spanish Veo clip that passes on take
    2 keeps Veo's 0.992 speech fidelity and still looks and sounds like the rest
    of a 🗣 reel, where every OTHER clip is on Veo too — Kling (0.953) is the
    last resort it always was, not the first, and Grok is behind it.
    Costs ~60 s per extra take, which is the trade Hugo chose.
    SINGLE SOURCE OF TRUTH for both runners' attempt loops.

    Callers step through the list and compare consecutive entries: a repeat of
    the same model is a plain re-submit, a CHANGE is the fallback leg (and only
    that leg may set `VideoVariant.fallback_model`).
    """
    from character_swap.config import settings
    if is_veo_host(chosen_model):
        # Veo walks its HOST chain first: one swing at fal, then Google's
        # takes (Hugo 2026-08-10). The chain starts at whichever host this
        # clip is already on, so a resumed or manually-retried clip does not
        # start over at the top.
        chain = veo_host_chain()
        if chosen_model in chain:
            chain = chain[chain.index(chosen_model):]
        else:
            chain = [chosen_model]
        models: list[str] = []
        for i, host in enumerate(chain):
            models += [host] * veo_host_takes(host, is_last=i == len(chain) - 1)
    else:
        models = [chosen_model] * (1 + max(0, settings.video_refusal_retries))
    models.extend(video_fallback_chain(chosen_model, language=language))
    return models


def drops_end_frame(from_model: str, to_model: str) -> bool:
    """True when moving a clip from `from_model` to `to_model` LOSES a resolved
    🎯 end pose — the source honors end frames and the target does not.

    Per LEG, because the chain has more than one: Veo → Kling keeps the pose,
    Kling → Grok loses it, and a clip can walk both in one run. A caller that
    computed this once up front would flag the wrong leg.
    """
    return supports_end_frame(from_model) and not supports_end_frame(to_model)


def fallback_drops_end_frame(chosen_model: str, *,
                             language: str | None = None) -> bool:
    """True when the FIRST reroute leg out of `chosen_model` would lose a
    resolved end frame. Thin reader over `drops_end_frame` for callers that
    only need the up-front answer; the runners flag per leg."""
    fb = video_fallback_model(chosen_model, language=language)
    return bool(fb) and drops_end_frame(chosen_model, fb)


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
