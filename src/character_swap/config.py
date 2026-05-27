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
    grok_video_model: str = Field(default="grok-imagine-video", validation_alias="GROK_VIDEO_MODEL")
    grok_image_model: str = Field(default="grok-imagine-image", validation_alias="GROK_IMAGE_MODEL")
    grok_base_url: str = Field(default="https://api.x.ai/v1", validation_alias="XAI_BASE_URL")
    gemini_image_model: str = Field(default="gemini-2.5-flash-image-preview",
                                    validation_alias="GEMINI_IMAGE_MODEL")
    gemini_video_model: str = Field(default="veo-3.0-generate-preview",
                                    validation_alias="GEMINI_VIDEO_MODEL")

    image_concurrency: int = Field(default=2, validation_alias="IMAGE_CONCURRENCY")
    video_poll_interval_secs: int = Field(default=12, validation_alias="VIDEO_POLL_INTERVAL_SECS")
    video_timeout_secs: int = Field(default=600, validation_alias="VIDEO_TIMEOUT_SECS")
    video_duration_secs: int = Field(default=10, validation_alias="VIDEO_DURATION_SECS")
    video_aspect_ratio: str = Field(default="9:16", validation_alias="VIDEO_ASPECT_RATIO")
    video_resolution: str = Field(default="720p", validation_alias="VIDEO_RESOLUTION")
    image_size: str = Field(default="1024x1792", validation_alias="IMAGE_SIZE")

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
            "higgsfield": bool(self.higgsfield_api_key),
            "heygen":     bool(self.heygen_api_key),
            "elevenlabs": bool(self.elevenlabs_api_key),
            "fal":        bool(self.fal_api_key),
        }.get(provider, False)


settings = Settings()
