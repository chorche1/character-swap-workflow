from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    heygen_api_key: str = Field(default="", validation_alias="HEYGEN_API_KEY")        # HeyGen Avatar 5 (talking heads)
    elevenlabs_api_key: str = Field(default="", validation_alias="ELEVENLABS_API_KEY") # ElevenLabs voice library + TTS + Voice Changer
    fal_api_key: str = Field(default="", validation_alias="FAL_API_KEY")              # fal.ai (hosts VEED Subtitle Styling — auto-captioning)
    # Higgsfield → Google Drive auto-import. User configures their Higgsfield
    # Supercomputer account to export outputs to a Drive folder, our server
    # polls that folder via Drive API every N seconds, downloads new MP4s to
    # `output/higgsfield-inbox/`, and surfaces them in the Editor multi-clip
    # tab. Folder can be specified by name (we resolve to ID on first run)
    # OR directly by Drive folder ID (cheaper at startup).
    higgsfield_drive_folder_name: str = Field(default="AI INF Videos",
                                              validation_alias="HIGGSFIELD_DRIVE_FOLDER_NAME")
    higgsfield_drive_folder_id: str = Field(default="",
                                            validation_alias="HIGGSFIELD_DRIVE_FOLDER_ID")
    higgsfield_drive_poll_secs: int = Field(default=60,
                                            validation_alias="HIGGSFIELD_DRIVE_POLL_SECS")
    # When True, every video extracted from a Drive-inbox ZIP is auto-fed
    # through the Editor's single-clip auto-edit (trim+captions only) and
    # the result is delivered to Telegram. Disable to revert to the
    # manual workflow where Hugo picks clips from the inbox by hand.
    higgsfield_auto_process: bool = Field(default=True,
                                          validation_alias="HIGGSFIELD_AUTO_PROCESS")
    # Telegram bot for the auto-delivery step. Get a token from @BotFather
    # and your chat_id by messaging the bot once and curling
    # https://api.telegram.org/bot<TOKEN>/getUpdates.
    telegram_bot_token: str = Field(default="", validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", validation_alias="TELEGRAM_CHAT_ID")

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
    video_poll_interval_secs: int = Field(default=12, validation_alias="VIDEO_POLL_INTERVAL_SECS")
    video_timeout_secs: int = Field(default=600, validation_alias="VIDEO_TIMEOUT_SECS")
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
    # fal Kling v3 tier: "pro" (1080p output) or "standard" (720p, cheaper).
    # Default PRO since 2026-06-12 (Hugo's call — quality over cost): the
    # whole downstream chain targets a 1080-px short edge, and standard-tier
    # 720p clips were being upscaled 1.5× into fake 1080p finals.
    kling_v3_tier: str = Field(default="pro", validation_alias="KLING_V3_TIER")
    # Local ffmpeg encode quality for EVERY intermediate/final re-encode in
    # video_edit.py (trims, concat, time-stretch, ASS caption burn). A clip
    # passes through 2-4 of these generations, so per-generation loss
    # compounds: the old hardcoded veryfast/CRF-20 measured ~2-3 Mbps off a
    # ~21 Mbps Kling master at the FIRST hop. CRF 16 + medium is near-
    # transparent per generation at acceptable encode speed (2026-06-12).
    ffmpeg_crf: int = Field(default=16, validation_alias="FFMPEG_CRF")
    ffmpeg_preset: str = Field(default="medium", validation_alias="FFMPEG_PRESET")
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
    heygen_price_usd: float = Field(default=0.30, validation_alias="HEYGEN_PRICE_USD")
    elevenlabs_tts_price_usd: float = Field(default=0.05, validation_alias="ELEVENLABS_TTS_PRICE_USD")
    elevenlabs_vc_price_usd: float = Field(default=0.05, validation_alias="ELEVENLABS_VC_PRICE_USD")
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
    swap_qc_price_usd: float = Field(default=0.04, validation_alias="SWAP_QC_PRICE_USD")
    # QC on generated video CLIPS: Whisper-vs-expected-dialogue (catches
    # garbled TTS like "baking goda") + frame-sampled vision check for
    # impossible motion/anatomy. Auto-resubmits the clip on failure — video is
    # the expensive step, so only 1 retry by default. VIDEO_QC=0 disables.
    video_qc_enabled: bool = Field(default=True, validation_alias="VIDEO_QC")
    video_qc_max_retries: int = Field(default=1, validation_alias="VIDEO_QC_MAX_RETRIES")
    video_qc_speech_threshold: float = Field(default=0.7,
                                             validation_alias="VIDEO_QC_SPEECH_THRESHOLD")

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
            "heygen":     bool(self.heygen_api_key),
            "elevenlabs": bool(self.elevenlabs_api_key),
            "fal":        bool(self.fal_api_key),
        }.get(provider, False)


settings = Settings()
