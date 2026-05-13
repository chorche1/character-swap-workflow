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

    openai_image_model: str = Field(default="gpt-image-2", validation_alias="OPENAI_IMAGE_MODEL")
    grok_video_model: str = Field(default="grok-imagine-video", validation_alias="GROK_VIDEO_MODEL")
    grok_image_model: str = Field(default="grok-2-image-1212", validation_alias="GROK_IMAGE_MODEL")
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

    # Opt-in SQLite state backend. Default off — JSON file remains canonical
    # until the user runs `character-swap migrate` + flips this on. Once stable
    # the JSON path will be deleted.
    use_sqlite_state: bool = Field(default=False, validation_alias="USE_SQLITE_STATE")

    project_root: Path = PROJECT_ROOT
    characters_dir: Path = PROJECT_ROOT / "characters"
    input_dir: Path = PROJECT_ROOT / "input"
    output_dir: Path = PROJECT_ROOT / "output"
    state_dir: Path = PROJECT_ROOT / "state"
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
        }.get(provider, False)


settings = Settings()
