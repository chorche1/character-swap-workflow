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
    scene_id: str | None = None              # which Job.scene_ids[i] this variant was generated against
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
    # Legacy single-scene fields (still set for back-compat with old jobs).
    # Canonical source of truth is `scene_ids` + `scene_image_paths` below.
    scene_id: str
    scene_image_path: str
    # Multi-scene support: a job can have N scene reference images. Each
    # character gets `images_per_character` variants generated PER SCENE.
    # `scene_ids` mirrors `scene_id` (first element) for old jobs loaded
    # from disk that don't have this field yet — see `effective_scene_ids`.
    scene_ids: list[str] = Field(default_factory=list)
    scene_image_paths: list[str] = Field(default_factory=list)
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


class ReelFrameStatus(StrEnum):
    QUEUED = "queued"
    GENERATING = "generating"
    # Mini-approval mode only: the frame has rendered and is waiting for the
    # user to accept it or trigger a refine before the next frame starts.
    AWAITING_APPROVAL = "awaiting_approval"
    DONE = "done"
    FAILED = "failed"


class ReelJobStatus(StrEnum):
    QUEUED = "queued"
    GENERATING_ANCHOR = "generating_anchor"
    AWAITING_ANCHOR_APPROVAL = "awaiting_anchor_approval"
    GENERATING = "generating"
    DONE = "done"
    PARTIAL = "partial"
    FAILED = "failed"


class ReelPreset(BaseModel):
    """Named, reusable baseline prompt for batch-consistent image edits.

    The user's per-video tweak is appended to `baseline_prompt` at job
    submission. Presets are global (not per-project) so the same recipe
    works across reels.
    """
    preset_id: str
    name: str
    baseline_prompt: str
    is_default: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ReelFrame(BaseModel):
    """One input→output pair inside a ReelJob.

    Frame index 0 is the anchor — its output becomes a reference image for
    every subsequent frame so all outputs share clothing/background/style.
    """
    frame_id: str
    sort_index: int
    is_anchor: bool
    input_filename: str           # relative to output/reel/<job_id>/
    output_filename: str | None = None
    status: ReelFrameStatus = ReelFrameStatus.QUEUED
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    # gpt-4o vision summary of THIS input frame (people count, framing,
    # visible props). Generated at job-submit time; injected into the
    # follower prompt so the model is told exactly what composition to
    # preserve from the input.
    input_description: str | None = None
    # JSON-encoded drift audit from the most recent render of this frame.
    # Shown in the UI so the user knows whether auto-correction fired.
    last_drift_audit: str | None = None
    # True iff the user has approved this frame in mini-approval mode
    # (used by sequential render loop to advance to the next frame).
    approved: bool = False


class ReelJob(BaseModel):
    """Batch image-edit job that keeps consistency across N frames.

    Implementation strategy ("anchor-first"):
      1. Render frame 0 (the anchor) using only `input_filename` as a ref.
      2. For each remaining frame, render with refs = [anchor_output,
         input_filename]. Prompt instructs the model to take style/clothing/
         background from ref #1 and pose/composition from ref #2.
    """
    job_id: str
    title: str | None = None
    preset_id: str | None = None
    custom_prompt: str = ""
    full_prompt: str = ""           # baseline + custom, materialized at submit time
    image_model: str = "gpt-image"
    aspect_ratio: str | None = None
    frames: list[ReelFrame] = Field(default_factory=list)
    status: ReelJobStatus = ReelJobStatus.QUEUED
    error: str | None = None
    # Vision-extracted concrete description of the anchor's clothing, background,
    # and lighting (filled in by gpt-4o after the anchor renders). Injected into
    # every follower prompt as a hard spec so the model can't drift on colors.
    anchor_description: str | None = None
    # When True, followers render SEQUENTIALLY and pause for user approval
    # between each frame. When False (default), all followers run in parallel
    # after anchor approval (with the auto drift-correction loop).
    mini_approval: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AppState(BaseModel):
    scenes: dict[str, SceneAsset] = Field(default_factory=dict)
    characters: dict[str, CharacterAsset] = Field(default_factory=dict)
    projects: dict[str, ProjectAsset] = Field(default_factory=dict)
    jobs: dict[str, Job] = Field(default_factory=dict)
    generations: dict[str, MediaGeneration] = Field(default_factory=dict)
    reel_presets: dict[str, ReelPreset] = Field(default_factory=dict)
    reel_jobs: dict[str, ReelJob] = Field(default_factory=dict)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
