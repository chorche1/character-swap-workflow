from __future__ import annotations

import subprocess
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
KEYCHAIN_SERVICE = "Character Swap Studio"
TELEGRAM_CHARACTER_KEYCHAIN_ACCOUNT = "telegram-character-bot-token"
TELEGRAM_LOCAL_API_ID_KEYCHAIN_ACCOUNT = "telegram-local-api-id"
TELEGRAM_LOCAL_API_HASH_KEYCHAIN_ACCOUNT = "telegram-local-api-hash"


def load_keychain_secret(account: str) -> str:
    """Read a local macOS Keychain secret without exposing it in process args."""
    try:
        result = subprocess.run(
            [
                "security", "find-generic-password",
                "-s", KEYCHAIN_SERVICE,
                "-a", account,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def save_keychain_secret(account: str, value: str) -> None:
    """Store a secret in the user's macOS Keychain."""
    result = subprocess.run(
        [
            "security", "add-generic-password",
            "-U",
            "-s", KEYCHAIN_SERVICE,
            "-a", account,
            "-w", value,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "macOS Keychain kunde inte spara Telegram-token: "
            + (result.stderr.strip() or "security command failed"))


class Settings(BaseSettings):
    # `.env` takes precedence over `.env.example` if both exist.
    # `env_ignore_empty=True` ensures an empty shell var doesn't override a file value.
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env.example", PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    anthropic_api_key: str = Field(default="", validation_alias="ANTHROPIC_API_KEY")
    xai_api_key: str = Field(default="", validation_alias="XAI_API_KEY")
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")
    kling_access_key: str = Field(default="", validation_alias="KLING_ACCESS_KEY")
    kling_secret_key: str = Field(default="", validation_alias="KLING_SECRET_KEY")
    bfl_api_key: str = Field(default="", validation_alias="BFL_API_KEY")             # Black Forest Labs (FLUX)
    ideogram_api_key: str = Field(default="", validation_alias="IDEOGRAM_API_KEY")
    recraft_api_key: str = Field(default="", validation_alias="RECRAFT_API_KEY")
    stability_api_key: str = Field(default="", validation_alias="STABILITY_API_KEY")
    runway_api_key: str = Field(default="", validation_alias="RUNWAY_API_KEY")
    luma_api_key: str = Field(default="", validation_alias="LUMA_API_KEY")
    pika_api_key: str = Field(default="", validation_alias="PIKA_API_KEY")
    minimax_api_key: str = Field(default="", validation_alias="MINIMAX_API_KEY")
    bytedance_api_key: str = Field(default="", validation_alias="BYTEDANCE_API_KEY")  # Seedream/SeedEdit/SeedDance (Volcano ARK)
    alibaba_api_key: str = Field(default="", validation_alias="ALIBABA_API_KEY")      # Wan 2.x (DashScope)
    higgsfield_api_key: str = Field(default="", validation_alias="HIGGSFIELD_API_KEY")
    # Higgsfield official REST API uses a key + SECRET pair (Authorization: Key
    # {key}:{secret}). Distinct from the CLI/MCP device-login. Create at
    # cloud.higgsfield.ai/api-keys. Powers the Swap "Higgsfield Character Swap" model.
    higgsfield_api_secret: str = Field(default="", validation_alias="HIGGSFIELD_API_SECRET")
    higgsfield_base_url: str = Field(default="https://platform.higgsfield.ai",
                                     validation_alias="HIGGSFIELD_BASE_URL")
    # Field name carrying the scene image on POST /v1/text2image/soul. The
    # community MCP uses "image_reference"; override to "input_images" if a live
    # probe shows that's what the account's API expects.
    higgsfield_scene_field: str = Field(default="image_reference",
                                        validation_alias="HIGGSFIELD_SCENE_FIELD")
    elevenlabs_api_key: str = Field(default="", validation_alias="ELEVENLABS_API_KEY") # ElevenLabs voice library + TTS + Voice Changer
    fal_api_key: str = Field(default="", validation_alias="FAL_API_KEY")              # fal.ai (hosts VEED Subtitle Styling — auto-captioning)
    # Telegram delivery uses TWO bots/identities:
    #   character bot → each character's own channel (chat id stored on the
    #                   CharacterAsset in the library)
    #   editor bot    → one shared channel for standalone Single/Multi-clip
    #                   Editor finals and their repurposed copies.
    # Bot API tokens come from @BotFather. A channel destination may be a
    # numeric "-100…" chat id or a public "@channelusername".
    telegram_character_bot_token: str = Field(
        default="", validation_alias="TELEGRAM_CHARACTER_BOT_TOKEN")
    telegram_editor_bot_token: str = Field(
        default="", validation_alias="TELEGRAM_EDITOR_BOT_TOKEN")
    telegram_editor_chat_id: str = Field(
        default="", validation_alias="TELEGRAM_EDITOR_CHAT_ID")
    # Official cloud Bot API by default (50 MB upload cap). Point this at a
    # local telegram-bot-api server to gain its 2 GB upload cap. Finals are
    # always sent byte-identically as documents; they are never re-encoded.
    telegram_api_base: str = Field(
        default="https://api.telegram.org",
        validation_alias="TELEGRAM_API_BASE")

    openai_image_model: str = Field(default="gpt-image-2", validation_alias="OPENAI_IMAGE_MODEL")
    # OpenAI image `quality`: "low" | "medium" | "high" | "auto". Defaults to
    # "high" so every Swap variant renders at full detail (~1.5× cost vs auto).
    # Set OPENAI_IMAGE_QUALITY="" to let OpenAI pick, or "medium"/"low" for cheap
    # test runs.
    openai_image_quality: str = Field(default="high", validation_alias="OPENAI_IMAGE_QUALITY")
    grok_video_model: str = Field(default="grok-imagine-video", validation_alias="GROK_VIDEO_MODEL")
    grok_image_model: str = Field(default="grok-imagine-image", validation_alias="GROK_IMAGE_MODEL")
    grok_base_url: str = Field(default="https://api.x.ai/v1", validation_alias="XAI_BASE_URL")
    gemini_image_model: str = Field(default="gemini-2.5-flash-image-preview",
                                    validation_alias="GEMINI_IMAGE_MODEL")
    gemini_video_model: str = Field(default="veo-3.0-generate-preview",
                                    validation_alias="GEMINI_VIDEO_MODEL")

    # Per-PROVIDER image-generation parallelism for the Swap/Reengineer
    # variant runner. The single global cap of 2 was the measured wall-clock
    # bottleneck (effective concurrency 1.78 on a 65-image burst; fal queues
    # server-side and ran 202/202 ok, so client-side throttling buys nothing
    # there). `image_concurrency` remains the fallback for providers without
    # a dedicated knob (grok, etc.) and for unknown model slugs.
    image_concurrency: int = Field(default=2, validation_alias="IMAGE_CONCURRENCY")
    image_concurrency_fal: int = Field(default=8, validation_alias="IMAGE_CONCURRENCY_FAL")
    image_concurrency_openai: int = Field(default=4, validation_alias="IMAGE_CONCURRENCY_OPENAI")
    image_concurrency_gemini: int = Field(default=2, validation_alias="IMAGE_CONCURRENCY_GEMINI")
    # Remotion caption renders are LOCAL CPU work (one headless Chrome +
    # OffthreadVideo ffmpeg frame extractors per render), unlike the
    # network-bound providers above — so fan-out callers (Step-6 compile is
    # per-character-parallel) must NOT each get their own render. Measured
    # 2026-06-10: a 12-character compile ran 11 renders simultaneously →
    # 430s median per render (vs 71s solo), delayRender 30s timeouts on
    # single frames, one Chrome launch crash. remotion_render.py gates
    # renders process-wide at `remotion_max_concurrent_renders` and gives
    # each render `remotion_concurrency` browser tabs (the old hardcoded
    # --concurrency=1 left 17 of 18 cores idle once renders were serialized).
    # `remotion_timeout_ms` is the per-frame delayRender budget — insurance
    # for long compile concats where a cold OffthreadVideo seek can be slow.
    remotion_max_concurrent_renders: int = Field(
        default=2, validation_alias="REMOTION_MAX_CONCURRENT_RENDERS")
    remotion_concurrency: int = Field(default=4, validation_alias="REMOTION_CONCURRENCY")
    remotion_timeout_ms: int = Field(default=120_000, validation_alias="REMOTION_TIMEOUT_MS")
    # TOTAL OffthreadVideo frame-cache budget across ALL concurrent renders
    # (2026-07-31, after an OOM crash: 64GB RAM exhausted + 65GB swap).
    # Remotion's default for --offthreadvideo-cache-size-in-bytes is null =
    # "half of system memory", read PER RENDER PROCESS at ITS OWN start —
    # so N concurrent renders each budget ~half the machine (4 renders on a
    # 64GB box = ~100GB of intent). The cache holds DECODED frames
    # (1080x1920 RGB = 6.2MB each), so cost scales with the source video's
    # LENGTH: a 110s reel is 3330 frames ≈ 20GB, which fits under "half the
    # memory" and therefore never evicts. Short clips never hit this (a 10s
    # clip is ~1.9GB), which is why it only bit on full-length reels.
    # remotion_render.py divides this total by the concurrency gate to get
    # each render's share, so the ceiling holds regardless of fan-out.
    remotion_offthread_cache_bytes: int = Field(
        default=8 * 1024**3, validation_alias="REMOTION_OFFTHREAD_CACHE_BYTES")
    # Whole-subprocess backstop (backlog #11, 2026-06-12): without it a hung
    # headless Chrome held 1 of the 2 gate slots FOREVER. 30 min is ~4x the
    # worst measured contended render (430s) — generous, but finite.
    remotion_render_timeout_secs: int = Field(
        default=1800, validation_alias="REMOTION_RENDER_TIMEOUT_SECS")
    # Per-clip loudness equalization in assemble_clips (backlog #10,
    # 2026-06-12): finals measured -20 LUFS with 3 dB jumps between Kling
    # clips. One static volume gain per clip toward the target (analysis
    # pass only — no extra encode generation), true-peak-capped at -1 dBTP.
    # LOUDNORM_ENABLED=0 restores the old untouched audio.
    loudnorm_enabled: bool = Field(default=True, validation_alias="LOUDNORM_ENABLED")
    loudnorm_target_lufs: float = Field(
        default=-14.0, validation_alias="LOUDNORM_TARGET_LUFS")
    # Per-clip silencedetect threshold derived from measured loudness
    # (backlog #37): silence ≈ 16 LU below the clip's integrated level,
    # clamped to [-45, -25] dB. Off → the flow's fixed threshold_db.
    adaptive_silence_threshold: bool = Field(
        default=True, validation_alias="ADAPTIVE_SILENCE_THRESHOLD")
    # Hybrid captions (Hugo 2026-06-17): when a known script (scene says-clauses)
    # is available and Whisper's transcript matches it below this ratio, the
    # captions are rebuilt from the script (Whisper hallucinates on Kling's
    # synthetic voice). 1.0 = always prefer the script; 0.0 = always trust
    # Whisper. 0.55 catches outright hallucinations + near-empty transcripts
    # while keeping accurate Whisper timing on clean audio.
    caption_script_fallback_ratio: float = Field(
        default=0.55, validation_alias="CAPTION_SCRIPT_FALLBACK_RATIO")
    video_poll_interval_secs: int = Field(default=12, validation_alias="VIDEO_POLL_INTERVAL_SECS")
    video_timeout_secs: int = Field(default=600, validation_alias="VIDEO_TIMEOUT_SECS")
    # Reengineer video-PHASE ceiling (all clips together) — the absolute backstop
    # for the whole animate/reanimate phase, distinct from the per-clip
    # video_timeout_secs. Keep it comfortably ABOVE video_timeout_secs so one
    # slow clip can't blow the whole phase. Default 1 h; raise via env.
    video_phase_timeout_secs: int = Field(default=3600,
                                          validation_alias="VIDEO_PHASE_TIMEOUT_SECS")
    video_duration_secs: int = Field(default=10, validation_alias="VIDEO_DURATION_SECS")
    video_aspect_ratio: str = Field(default="9:16", validation_alias="VIDEO_ASPECT_RATIO")
    video_resolution: str = Field(default="720p", validation_alias="VIDEO_RESOLUTION")
    # Kling 3.0 (fal) native speech/audio. ON by default (Hugo, 2026-06-10):
    # every Swap/Reengineer video comes out with sound — the character speaks
    # with Kling's own voice. To make it say specific lines, put the dialogue
    # in the motion prompt (e.g. The person says: "...") — without it Kling
    # improvises from the prompt. Set KLING_GENERATE_AUDIO=0 for silent clips
    # (the pre-2026-06-10 behavior, when audio was added downstream instead).
    kling_generate_audio: bool = Field(default=True, validation_alias="KLING_GENERATE_AUDIO")
    # Kling v3 negative prompt (research 2026-06-12). Without it fal applies
    # only "blur, distort, and low quality"; this default adds the
    # talking-head terms practitioners converge on (5-8 terms beats long
    # lists; earlier terms weigh more). KLING_NEGATIVE_PROMPT= (empty)
    # falls back to fal's own default.
    kling_negative_prompt: str = Field(
        default=("blur, distort, low quality, morphing face, frozen lips, "
                 "warping fingers, extra limbs"),
        validation_alias="KLING_NEGATIVE_PROMPT")
    # Veo 3.1 negative prompt. Veo used to be handed KLING_NEGATIVE_PROMPT,
    # which is tuned for Kling's failure modes ("frozen lips", "warping
    # fingers") and says nothing about Veo's own signature defect: burned-in,
    # often garbled SUBTITLES on dialogue clips. Those terms lead here because
    # earlier terms weigh more (same research as the Kling set: 5-8 terms beats
    # a long list). Empty → the field is omitted and fal applies its own
    # default.
    veo_negative_prompt: str = Field(
        default=("subtitles, captions, burned-in text, on-screen text, "
                 "watermark, blur, distorted face, extra limbs"),
        validation_alias="VEO_NEGATIVE_PROMPT")
    # fal Kling v3 tier: "pro" (1080p output) or "standard" (720p, cheaper).
    # Default PRO since 2026-06-12 (Hugo's call — quality over cost): the
    # whole downstream chain targets a 1080-px short edge, and standard-tier
    # 720p clips were being upscaled 1.5× into fake 1080p finals.
    kling_v3_tier: str = Field(default="pro", validation_alias="KLING_V3_TIER")
    # fal Veo 3.1 Fast (image-to-video) render resolution: "720p" / "1080p" /
    # "4k". Default 1080p (Hugo 2026-06-18 — quality parity with kling_v3_tier=
    # pro so clips from different models in one reel share sharpness); 720p is
    # fal's own default (cheaper/faster). fal-routed Veo bills on FAL_API_KEY.
    veo_fal_resolution: str = Field(default="1080p", validation_alias="VEO_FAL_RESOLUTION")
    # fal's own content-moderation dial on the Veo 3.1 endpoints: "1" (strictest)
    # … "6" (least strict), fal's default is "4". Set to 6 on Hugo's call
    # 2026-08-04 after the 2026-08-03 failure wave — 33 of 43 failed clips came
    # back `content_policy_violation`, almost all of them Spanish health-claim
    # dialogue that passed verbatim in English. This only relaxes the layer fal
    # controls; Google's own Veo filter sits underneath and cannot be dialled,
    # so a clip Google itself refuses still fails (usually as the silent
    # `no_media_generated`). The sibling `auto_fix` knob is deliberately NOT
    # used: it rewrites the PROMPT, which carries the character's exact line.
    veo_safety_tolerance: str = Field(default="6",
                                      validation_alias="VEO_SAFETY_TOLERANCE")
    # fal Grok Imagine 1.5 (image-to-video) render resolution: "480p" /
    # "720p" / "1080p". Default 720p (Hugo 2026-06-29 — verified fal pricing
    # tier at $0.14/s with native audio included; 480p is ~half cost, 1080p
    # is Grok-1.5-only but fal doesn't publish its per-second price). Grok
    # 1.5 accepts every resolution at every duration (no per-clip downgrade).
    grok_fal_resolution: str = Field(default="720p", validation_alias="GROK_FAL_RESOLUTION")
    # fal Seedance 2.0 (image-to-video) tier: "standard" (default, up to 4k,
    # $0.302/s @720p) or "fast" (cheaper $0.242/s, caps at 720p — identical
    # schema/capabilities otherwise per fal). Submit + poll must use the same
    # tier (fal request_ids are endpoint-scoped).
    seedance_fal_tier: str = Field(default="standard", validation_alias="SEEDANCE_FAL_TIER")
    # fal Seedance 2.0 render resolution: "480p" / "720p" / "1080p" / "4k".
    # Default 720p (Hugo 2026-06-29). The FAST tier auto-downgrades 1080p/4k
    # to 720p. Seedance is the priciest video model in the stack — 1080p is
    # $0.682/s (~4x Kling 3.0 Pro audio-on).
    seedance_fal_resolution: str = Field(default="720p", validation_alias="SEEDANCE_FAL_RESOLUTION")
    # Local ffmpeg encode quality for EVERY intermediate/final re-encode in
    # video_edit.py (trims, concat, time-stretch, ASS caption burn). A clip
    # passes through 2-4 of these generations, so per-generation loss
    # compounds: the old hardcoded veryfast/CRF-20 measured ~2-3 Mbps off a
    # ~21 Mbps Kling master at the FIRST hop. CRF 16 + medium is near-
    # transparent per generation at acceptable encode speed (2026-06-12).
    ffmpeg_crf: int = Field(default=16, validation_alias="FFMPEG_CRF")
    ffmpeg_preset: str = Field(default="medium", validation_alias="FFMPEG_PRESET")
    # Hard ceiling on every local ffmpeg invocation (video_edit._run). The
    # longest legitimate encode (multi-scene Step-6 concat at CRF 16) is
    # minutes, not hours — anything past this is a wedged process (e.g. an
    # unbounded lavfi source) and must be killed + failed loudly rather than
    # hang the pipeline and grow the output file until the disk fills.
    ffmpeg_timeout_secs: int = Field(default=3600, validation_alias="FFMPEG_TIMEOUT_SECS")
    # Process-wide cap on simultaneous multi-input concat encodes
    # (`assemble_clips` / `concat_videos` in video_edit.py — 2026-07-31,
    # diagnosed from "ffmpeg uses 17GB" reports). Each concat decodes every
    # source clip concurrently into ONE filter_complex; a multi-character
    # Step-6 compile or Reengineer assemble fans one of these out PER
    # CHARACTER via asyncio.gather with no cap, so N characters compiling
    # at once multiplied a single build's ~4-5GB peak by N. Same pattern as
    # REMOTION_MAX_CONCURRENT_RENDERS below.
    assemble_concurrency: int = Field(default=2, validation_alias="ASSEMBLE_CONCURRENCY")
    # Automatic black-bar removal in the final builds (Hugo 2026-07-24):
    # every clip entering a final is cropdetect-scanned and minimally
    # crop-zoomed to the target aspect so neither model-baked letterboxing
    # nor the pipeline's own scale+pad leaves black bars. The cap is the
    # maximum fraction of the frame (per axis) the fix may crop away —
    # beyond it the clip keeps today's padded behavior (protects against
    # cropdetect misreading a dark scene and against wildly off-aspect
    # imports). BLACKBAR_FIX=0 disables the whole mechanism.
    blackbar_fix: bool = Field(default=True, validation_alias="BLACKBAR_FIX")
    blackbar_max_crop_frac: float = Field(
        default=0.05, validation_alias="BLACKBAR_MAX_CROP_FRAC")
    # Remotion caption-render quality. Remotion's defaults (CRF 23-ish for
    # h264 + JPEG-80 frame captures) were the last lossy hop — measured
    # ~3.2 Mbps finals. JPEG 100 + CRF 16 makes the caption pass nearly
    # transparent; render time impact is small next to the per-frame
    # OffthreadVideo extraction.
    remotion_crf: int = Field(default=16, validation_alias="REMOTION_CRF")
    remotion_jpeg_quality: int = Field(default=100, validation_alias="REMOTION_JPEG_QUALITY")
    # TRUE 9:16 (0.5625) AND both dims divisible by 16 — gpt-image rejects sizes
    # that aren't (400 "must both be divisible by 16"; 1080 is NOT ÷16). 1008x1792
    # = exactly 9:16 (1008=16×63, 1792=16×112). The old 1024x1792 was ÷16 but
    # 0.5714 (wider than 9:16) → letterbox bars once the seed fed a 9:16 video /
    # the 1080x1920 caption canvas.
    image_size: str = Field(default="1008x1792", validation_alias="IMAGE_SIZE")

    host: str = Field(default="127.0.0.1", validation_alias="HOST")
    port: int = Field(default=8000, validation_alias="PORT")

    # Phone push via ntfy (https://ntfy.sh) — opt-in. When NTFY_TOPIC is empty
    # every push is a silent no-op (nothing changes for users who haven't set
    # it up). The server POSTs a short message to <ntfy_server>/<ntfy_topic>
    # at milestone events (approval gates + finished jobs) so Hugo gets pinged
    # on his phone even when no browser is open. See clients-free push.py.
    # ntfy_click is an optional URL opened when the notification is tapped —
    # set it to the Tailscale app URL so a tap jumps straight into the app.
    ntfy_topic: str = Field(default="", validation_alias="NTFY_TOPIC")
    ntfy_server: str = Field(default="https://ntfy.sh", validation_alias="NTFY_SERVER")
    ntfy_click: str = Field(default="", validation_alias="NTFY_CLICK")

    max_upload_bytes: int = Field(
        default=25 * 1024 * 1024,
        validation_alias="MAX_UPLOAD_BYTES",
    )

    # Per-call cost estimates used by call_log + the UI cost banner.
    # Override per-model rates if pricing drifts.
    openai_image_price_usd: float = Field(default=0.04, validation_alias="OPENAI_IMAGE_PRICE_USD")
    grok_image_price_usd: float = Field(default=0.04, validation_alias="GROK_IMAGE_PRICE_USD")
    grok_video_price_usd: float = Field(default=0.40, validation_alias="GROK_VIDEO_PRICE_USD")
    nano_banana_price_usd: float = Field(default=0.04, validation_alias="NANO_BANANA_PRICE_USD")
    veo_price_usd: float = Field(default=0.50, validation_alias="VEO_PRICE_USD")
    kling_price_usd: float = Field(default=0.50, validation_alias="KLING_PRICE_USD")
    flux_price_usd: float = Field(default=0.05, validation_alias="FLUX_PRICE_USD")
    ideogram_price_usd: float = Field(default=0.08, validation_alias="IDEOGRAM_PRICE_USD")
    recraft_price_usd: float = Field(default=0.04, validation_alias="RECRAFT_PRICE_USD")
    stability_price_usd: float = Field(default=0.04, validation_alias="STABILITY_PRICE_USD")
    runway_price_usd: float = Field(default=0.50, validation_alias="RUNWAY_PRICE_USD")
    luma_price_usd: float = Field(default=0.40, validation_alias="LUMA_PRICE_USD")
    pika_price_usd: float = Field(default=0.40, validation_alias="PIKA_PRICE_USD")
    minimax_price_usd: float = Field(default=0.40, validation_alias="MINIMAX_PRICE_USD")
    dall_e_3_price_usd: float = Field(default=0.04, validation_alias="DALL_E_3_PRICE_USD")
    nano_banana_pro_price_usd: float = Field(default=0.10, validation_alias="NANO_BANANA_PRO_PRICE_USD")
    flux_kontext_price_usd: float = Field(default=0.05, validation_alias="FLUX_KONTEXT_PRICE_USD")
    seedream_price_usd: float = Field(default=0.04, validation_alias="SEEDREAM_PRICE_USD")
    seedance_price_usd: float = Field(default=0.40, validation_alias="SEEDANCE_PRICE_USD")
    wan_price_usd: float = Field(default=0.40, validation_alias="WAN_PRICE_USD")
    sora_price_usd: float = Field(default=0.60, validation_alias="SORA_PRICE_USD")
    higgsfield_price_usd: float = Field(default=0.50, validation_alias="HIGGSFIELD_PRICE_USD")
    # Rough flat estimate per fal-hosted swap edit (Qwen Edit+ / Kontext Max /
    # Seedream Edit are all in the $0.03–0.08/image band).
    fal_swap_price_usd: float = Field(default=0.06, validation_alias="FAL_SWAP_PRICE_USD")
    # VEED Subtitle Styling on fal.ai is billed at ~$0.10/min input duration.
    # We record per-call instead of per-minute so the cost banner shows the
    # incremental hit; recompute via duration*rate inside fal_veed.render.
    fal_caption_price_per_minute_usd: float = Field(default=0.10,
                                                    validation_alias="FAL_CAPTION_PRICE_PER_MINUTE_USD")

    # AI Director: Claude/Opus agent that writes tailored per-variant prompts.
    # Model is env-overridable so a fresher Opus version can drop in without code changes.
    # claude_opus_price_usd is a rough per-call estimate (one Director call ≈ one Opus
    # request with vision); recorded in calls.jsonl via call_log._cost_usd.
    claude_opus_model: str = Field(default="claude-opus-4-5", validation_alias="CLAUDE_OPUS_MODEL")
    claude_opus_price_usd: float = Field(default=0.05, validation_alias="CLAUDE_OPUS_PRICE_USD")
    # Vision QC on every generated swap variant (Swap + Reengineer): a Claude
    # call checks identity (right person?), props/action fidelity (holding
    # the same thing, doing the same thing) + obvious defects, and the runner
    # regenerates failed slots with a corrective hint. SWAP_QC=0 disables.
    # Default judge bumped Haiku → Sonnet 4.6 (2026-06-11): wrong-prop images
    # passed the Haiku judge in Hugo's runs; the fine-grained scene-vs-result
    # comparison needs the stronger vision model. ~4x the QC cost (~$0.04 vs
    # $0.01/call) — noise next to the image+video spend it protects. Set
    # SWAP_QC_MODEL=claude-haiku-4-5-20251001 to go back to the cheap judge.
    swap_qc_enabled: bool = Field(default=True, validation_alias="SWAP_QC")
    # Cross-engine rescue when the chosen swap engine rejects on content
    # policy (after the softening ladder): retry the slot once on the
    # fal-hosted nbp-swap. OFF by default since 2026-06-12 — Hugo's "100%
    # GPT Image 2" directive: a rejected slot now fails loudly with the
    # moderation reason instead of shipping another model's look. Set
    # SWAP_MODERATION_FALLBACK=1 to restore the old rescue behavior.
    swap_moderation_fallback: bool = Field(
        default=False, validation_alias="SWAP_MODERATION_FALLBACK")
    swap_qc_model: str = Field(default="claude-sonnet-4-6",
                               validation_alias="SWAP_QC_MODEL")
    swap_qc_max_retries: int = Field(default=2, validation_alias="SWAP_QC_MAX_RETRIES")
    # "BACKGROUND NOT REPLACED" re-rolls, in CHARACTER background mode only
    # (Hugo 2026-08-08). Independent of swap_qc_max_retries because this class
    # behaves differently from the rest: an image that kept the scene's room
    # when the character's own environment was ordered is unusable, not a
    # judgment call, and unlike every other class an exhausted budget FAILS
    # THE SLOT LOUDLY instead of keeping the last take with a ⚠ chip — Hugo's
    # explicit instruction ("3 omförsök, och om det fortfarande är fel så
    # faila högt"). 0 = flag-only, never re-roll and never fail.
    swap_qc_background_max_retries: int = Field(
        default=3, validation_alias="SWAP_QC_BACKGROUND_MAX_RETRIES")
    # Speaker-attribution agent (speaker_fix.py, Hugo 2026-08-02): one vision
    # call per (FEMALE character × scene ticked 👥 two-person) that rewrites the
    # movement prompt so she — not the man beside her — says the line. Same
    # Sonnet judge class as image QC; the job is "read the frame, edit one
    # clause", not creative direction, so Opus is not warranted.
    speaker_fix_model: str = Field(default="claude-sonnet-4-6",
                                   validation_alias="SPEAKER_FIX_MODEL")
    speaker_fix_price_usd: float = Field(
        default=0.01, validation_alias="SPEAKER_FIX_PRICE_USD")
    # People detection for swap scenes (scene_people.py, Hugo 2026-08-06): one
    # vision call per scene IMAGE — not per character — that answers "is it
    # ambiguous which person should be replaced here?". Same read-the-frame
    # judge class as image QC and the speaker fix, so Sonnet, not Opus.
    scene_people_model: str = Field(default="claude-sonnet-4-6",
                                    validation_alias="SCENE_PEOPLE_MODEL")
    scene_people_price_usd: float = Field(
        default=0.01, validation_alias="SCENE_PEOPLE_PRICE_USD")
    # WRONG-LANGUAGE clip retries (Hugo 2026-08-02). Independent of
    # VIDEO_QC_MAX_RETRIES, which he deliberately runs at 0 (flag-only) for the
    # fuzzy garbled-speech check: a 🇪🇸/🇩🇪 character whose clip came out
    # ENGLISH is not a judgment call, it is an unusable clip, and shipping it
    # quietly is exactly what happened to 8 of 10 German clips in
    # re_b3170d2118. Re-render up to this many times with a hardened prompt;
    # still wrong after that → the clip FAILS loudly instead of entering a
    # final. 0 turns the check back into flag-only.
    video_qc_language_max_retries: int = Field(
        default=2, validation_alias="VIDEO_QC_LANGUAGE_MAX_RETRIES")
    # How well the clip must match the ENGLISH source line before we call it
    # "spoke the wrong language". High on purpose: this gates a loud clip
    # failure, and the check only ever fires when the heard text ALSO matches
    # English better than the translated line. (Whisper's own `language` field
    # is not usable here — measured "english" on a plainly German clip.)
    video_qc_language_threshold: float = Field(
        default=0.6, validation_alias="VIDEO_QC_LANGUAGE_THRESHOLD")
    swap_qc_price_usd: float = Field(default=0.04, validation_alias="SWAP_QC_PRICE_USD")
    # QC on generated video CLIPS: Whisper-vs-expected-dialogue (catches
    # garbled TTS like "baking goda") + frame-sampled vision check for
    # impossible motion/anatomy. Auto-resubmits the clip on failure — video is
    # the expensive step, so only 1 retry by default. VIDEO_QC=0 disables.
    #   - VIDEO_QC_MAX_RETRIES=0 turns the checks into FLAG-ONLY: a failing clip
    #     is KEPT and marked qc_status="failed" (⚠ in the UI) but never
    #     re-rendered (no extra cost). Hugo 2026-07-03: "bara flagga, behåll".
    #   - VIDEO_QC_VISUAL=0 runs ONLY the speech/dialogue check (skips the
    #     anatomy vision call).
    #   - VIDEO_QC_SPEECH_THRESHOLD is the min word-similarity to PASS; lower it
    #     (e.g. 0.35) so only clips saying something *completely* different are
    #     flagged, letting minor garble through.
    video_qc_enabled: bool = Field(default=True, validation_alias="VIDEO_QC")
    video_qc_visual_enabled: bool = Field(default=True, validation_alias="VIDEO_QC_VISUAL")
    video_qc_max_retries: int = Field(default=1, validation_alias="VIDEO_QC_MAX_RETRIES")
    video_qc_speech_threshold: float = Field(default=0.7,
                                             validation_alias="VIDEO_QC_SPEECH_THRESHOLD")

    # Speech-to-text engine for BOTH captions and video QC (Hugo 2026-08-03).
    # "scribe" = ElevenLabs Scribe, the default; "whisper" pins the old
    # whisper-1 path. Scribe is chosen for its per-word TIMINGS: measured over
    # 54 of Hugo's clips, 88-97% of whisper-1's adjacent word pairs had no gap
    # at all (interpolated inside each segment) against 2-6% for Scribe, and
    # every Remotion caption template animates per word off those numbers. It
    # also scored equal-or-better on text in all three languages, costs less
    # ($0.22/h vs $0.36/h) and is faster. whisper-1 stays the automatic
    # fallback on any Scribe failure, so this is never a single point of
    # failure — see video_edit._transcribe_scribe.
    # Content-policy fallback for VIDEO clips. OFF since 2026-08-03 (Hugo:
    # "ta bort fallbacken till en annan modell om ett klipp failar") — a clip
    # its model refuses now FAILS LOUDLY with the real reason instead of being
    # re-rendered on a different provider. Mirrors the image side's
    # SWAP_MODERATION_FALLBACK, which has been opt-in since 2026-06-12 for the
    # same reason: a silent provider switch produces a clip that doesn't match
    # the rest of the reel. Set to 1 to restore the old rescue.
    video_moderation_fallback: bool = Field(
        default=False, validation_alias="VIDEO_MODERATION_FALLBACK")

    # How many times a REFUSED clip is re-submitted UNCHANGED on its own model
    # before the fallback (or the loud failure) takes over — Hugo 2026-08-06,
    # "gör så fler videor inte blir nekade utan att ändra prompt".
    #
    # The refusal is not deterministic: the same line on the same start frame
    # passes sometimes and is refused other times (measured 2026-08-03, and
    # again 08-06 — Veo refused 50-60% of its calls across four days while the
    # same prompts rendered fine on other attempts). Nothing about the request
    # changes between takes, so this costs no prompt edit and no quality drop:
    # the clip stays on the model the run picked, next to its neighbours.
    #
    # Billing: fal prices Veo 3.1 Fast PER GENERATED SECOND ("for every second
    # of video you generate you will be charged $0.10/$0.15"), and a refused
    # take generates none — so a re-submit of a refused clip has nothing to
    # bill against. (fal's general FAQ warns that 422s "may still be charged if
    # a runner spent GPU time"; the endpoint's own per-second price is the
    # narrower rule. If a fal invoice ever shows overhead against the count of
    # FINISHED clips, drop this to 0 — that is the number to watch.)
    #
    # 2 is Hugo's pick: at the measured ~50% per-attempt pass rate two extra
    # takes recover ~75% of the stochastic refusals. It does NOT help the hard
    # blocks (`no_media_generated` sits on the IMAGE and the scene, refused 6/6
    # in the 2026-08-03 probe) — those just fail ~26s later. 0 disables.
    video_refusal_retries: int = Field(
        default=2, validation_alias="VIDEO_REFUSAL_RETRIES")

    stt_engine: str = Field(default="scribe", validation_alias="STT_ENGINE")
    stt_scribe_model: str = Field(default="scribe_v2",
                                  validation_alias="STT_SCRIBE_MODEL")

    # Reengineer swap-phase watchdog. PROGRESS-based, not a fixed deadline:
    # the old fixed 30-min ceiling fired below the realistic duration of any
    # gpt-image run > ~27 slots (128s median/call at concurrency 4) and marked
    # runs failed while generation kept going (and billing). "Progress" =
    # any variant reaching a terminal state OR any qc_attempts bump — each
    # generation attempt is ≤ ~131s measured, so 10 min of true silence means
    # a hung provider call, not a slow run. The max ceiling is a generous
    # backstop against a watchdog bug, not a pacing expectation.
    swap_stall_timeout_secs: int = Field(default=600,
                                         validation_alias="SWAP_STALL_TIMEOUT_SECS")
    swap_phase_max_secs: int = Field(default=7200,
                                     validation_alias="SWAP_PHASE_MAX_SECS")

    # Per-attempt timeout for the Reengineer scene analyst (one Claude vision
    # call that reads every scene's dense frame sequence + dialogue and writes
    # the action-describing motion prompts). The shared Anthropic client caps
    # at 120s, but a multi-scene recipe sends ~40 images to Opus and routinely
    # needs >120s — at 120s it timed out (2 attempts ≈ 250s) and SILENTLY fell
    # back to a generic "continues the action" prompt that doesn't describe the
    # motion (Hugo 2026-06-20). The analyst runs alone before image gen and has
    # no other watchdog, so a generous single-attempt budget is safe.
    reengineer_analyst_timeout_secs: float = Field(
        default=300.0, validation_alias="REENGINEER_ANALYST_TIMEOUT_SECS")

    # Opt-in SQLite state backend. Default off — JSON file remains canonical
    # until the user runs `character-swap migrate` + flips this on. Once stable
    # the JSON path will be deleted.
    use_sqlite_state: bool = Field(default=False, validation_alias="USE_SQLITE_STATE")

    project_root: Path = PROJECT_ROOT
    # Data dirs default to <repo>/<name>, but can be overridden via env vars
    # so multiple worktrees + the main checkout can share a single data store
    # (uploaded scenes, character library, generated jobs, SQLite DB). When
    # set, point them ALL at the same location; the four dirs are siblings.
    characters_dir: Path = Field(default=PROJECT_ROOT / "characters", validation_alias="CHARACTERS_DIR")
    input_dir: Path = Field(default=PROJECT_ROOT / "input", validation_alias="INPUT_DIR")
    output_dir: Path = Field(default=PROJECT_ROOT / "output", validation_alias="OUTPUT_DIR")
    state_dir: Path = Field(default=PROJECT_ROOT / "state", validation_alias="STATE_DIR")
    web_dir: Path = PROJECT_ROOT / "web"

    @property
    def scenes_dir(self) -> Path:
        return self.input_dir / "scenes"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def state_db(self) -> Path:
        return self.state_dir / "state.sqlite3"

    @property
    def call_log_file(self) -> Path:
        return self.state_dir / "calls.jsonl"

    def require_keys(self, *names: str) -> None:
        missing = [n for n in names if not getattr(self, f"{n}_api_key", "")]
        if missing:
            raise RuntimeError(
                f"Missing required API keys: {', '.join(missing)}. "
                f"Add them to {self.project_root / '.env'} (see .env.example)."
            )

    def has_provider(self, provider: str) -> bool:
        """Cheap UI-facing 'is this provider's credentials present' check."""
        return {
            "openai":     bool(self.openai_api_key),
            "anthropic":  bool(self.anthropic_api_key),
            "xai":        bool(self.xai_api_key),
            "gemini":     bool(self.gemini_api_key),
            "kling":      bool(self.kling_access_key and self.kling_secret_key),
            "bfl":        bool(self.bfl_api_key),
            "ideogram":   bool(self.ideogram_api_key),
            "recraft":    bool(self.recraft_api_key),
            "stability":  bool(self.stability_api_key),
            "runway":     bool(self.runway_api_key),
            "luma":       bool(self.luma_api_key),
            "pika":       bool(self.pika_api_key),
            "minimax":    bool(self.minimax_api_key),
            "bytedance":  bool(self.bytedance_api_key),
            "alibaba":    bool(self.alibaba_api_key),
            "higgsfield": bool(self.higgsfield_api_key and self.higgsfield_api_secret),
            "elevenlabs": bool(self.elevenlabs_api_key),
            "fal":        bool(self.fal_api_key),
        }.get(provider, False)


settings = Settings()
if not settings.telegram_character_bot_token:
    settings.telegram_character_bot_token = load_keychain_secret(
        TELEGRAM_CHARACTER_KEYCHAIN_ACCOUNT)
