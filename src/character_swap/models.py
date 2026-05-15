from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class CharStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    ANIMATING = "animating"
    DONE = "done"
    FAILED = "failed"


class VariantStatus(StrEnum):
    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"


class VideoStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    ERROR = "error"


class SceneAsset(BaseModel):
    scene_id: str
    filename: str
    original_name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CharacterImage(BaseModel):
    """One reference image belonging to a character. Stored as a child of CharacterAsset."""
    image_id: str
    filename: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CharacterAsset(BaseModel):
    char_id: str
    # `filename` is the legacy field — points to the primary/first image. Kept for
    # backwards compatibility; the canonical list is `images`. Whenever
    # primary_image_id changes, `filename` is rewritten to match so old code paths
    # (JobCharacter.source_image_path snapshotting at job-create time) keep working.
    filename: str
    name: str
    images: list[CharacterImage] = Field(default_factory=list)
    primary_image_id: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProjectAsset(BaseModel):
    project_id: str
    name: str
    # Preset character library for this project. New jobs created inside the
    # project pre-select these characters; users can still adjust per job.
    character_ids: list[str] = Field(default_factory=list)
    # Optional per-project default Swap generation prompt. When set, new jobs
    # in this project use this instead of `pipeline.GENERATION_PROMPT`. The
    # "↺ reset to default" link in Step 2 also reverts to this. Empty/None
    # means "use the global default".
    default_prompt: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class GeneratedImage(BaseModel):
    """One generated image variant for a character within a job."""
    variant_id: str
    path: str
    prompt: str                              # GENERATION_PROMPT for fresh gens, custom for edits
    parent_variant_id: str | None = None     # set when this is an edit
    status: VariantStatus = VariantStatus.READY
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VideoVariant(BaseModel):
    """One Grok-generated video for a character."""
    video_id: str
    grok_job_id: str
    status: VideoStatus = VideoStatus.PENDING
    submitted_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None
    download_url: str | None = None
    final_video_path: str | None = None
    source_variant_id: str | None = None
    error: str | None = None


class JobCharacter(BaseModel):
    char_id: str
    name: str
    source_image_path: str
    status: CharStatus = CharStatus.QUEUED
    images: list[GeneratedImage] = Field(default_factory=list)
    approved_variant_id: str | None = None
    videos: list[VideoVariant] = Field(default_factory=list)
    error: str | None = None
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class Job(BaseModel):
    job_id: str
    title: str | None = None                 # user-editable; falls back to job_id in UI
    project_id: str | None = None            # None = Unfiled
    scene_id: str
    scene_image_path: str
    characters: dict[str, JobCharacter] = Field(default_factory=dict)
    prompt: str | None = None                # custom swap prompt; falls back to pipeline.GENERATION_PROMPT
    image_model: str = "gpt-image"           # which adapter generates the variants
    movement_prompt: str | None = None
    images_per_character: int = 1
    videos_per_character: int = 1
    compacted: bool = False                  # set true after `compact` strips non-approved files
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class GenKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AVATAR = "avatar"
    AUDIO = "audio"


class GenStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class MediaGeneration(BaseModel):
    """A free-form generation in the Image, Video, or Avatar tab. Independent of Jobs."""
    gen_id: str
    kind: GenKind
    model: str                                    # provider+model slug
    prompt: str                                   # for kind=avatar, this is the spoken script
    reference_paths: list[str] = Field(default_factory=list)
    aspect_ratio: str | None = None
    duration_secs: int | None = None              # video only
    avatar_id: str | None = None                  # kind=avatar: which HeyGen avatar
    voice_id: str | None = None                   # kind=avatar/audio: voice id (HeyGen or ElevenLabs depending on provider)
    voice_provider: str | None = None             # "heygen" or "elevenlabs"; defaults to "heygen" for avatars
    status: GenStatus = GenStatus.PENDING
    output_path: str | None = None
    provider_job_id: str | None = None            # external async id (Grok / Veo / Kling / HeyGen)
    cost_usd: float | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: datetime | None = None


class AppState(BaseModel):
    scenes: dict[str, SceneAsset] = Field(default_factory=dict)
    characters: dict[str, CharacterAsset] = Field(default_factory=dict)
    projects: dict[str, ProjectAsset] = Field(default_factory=dict)
    jobs: dict[str, Job] = Field(default_factory=dict)
    generations: dict[str, MediaGeneration] = Field(default_factory=dict)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
