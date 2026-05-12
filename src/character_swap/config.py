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

    openai_image_model: str = Field(default="gpt-image-2", validation_alias="OPENAI_IMAGE_MODEL")
    grok_video_model: str = Field(default="grok-imagine-video", validation_alias="GROK_VIDEO_MODEL")
    grok_base_url: str = Field(default="https://api.x.ai/v1", validation_alias="XAI_BASE_URL")

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
    grok_video_price_usd: float = Field(default=0.40, validation_alias="GROK_VIDEO_PRICE_USD")

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


settings = Settings()
