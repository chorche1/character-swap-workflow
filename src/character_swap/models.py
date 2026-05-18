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
    # Preset voice for this character. Auto-applied when generating a video
    # for the character via the Editor tab's "Character" dropdown OR the
    # Swap-flow per-character compile feature (Step 6). Currently always
    # ElevenLabs — `voice_provider` is kept for forward-compat with HeyGen.
    voice_id: str | None = None
    voice_provider: str | None = None  # "elevenlabs" (default when voice_id set)
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
    # Per-video movement-prompt override. Set when the user regenerates this
    # specific video (Step 5 regen button) with a tweaked prompt. Falls back
    # to the job's per-scene movement_prompt when null. Persisted so a second
    # regen of the same video pre-fills the modal with the LAST override
    # the user iterated on instead of going back to the original.
    movement_prompt_override: str | None = None


class JobCharacter(BaseModel):
    char_id: str
    name: str
    source_image_path: str
    status: CharStatus = CharStatus.QUEUED
    images: list[GeneratedImage] = Field(default_factory=list)
    # Multi-variant approval (added when multi-scene support landed): the
    # canonical list of variant_ids the user picked for this character. With
    # N scenes, this can hold up to N entries — one per scene — so every
    # scene's chosen image animates in parallel in Step 4.
    # `approved_variant_id` (singular, below) is kept in sync with the
    # FIRST entry so older code paths that read it directly keep working.
    approved_variant_ids: list[str] = Field(default_factory=list)
    approved_variant_id: str | None = None
    videos: list[VideoVariant] = Field(default_factory=list)
    error: str | None = None
    # Step 6 (Compile) per-character output. When the user clicks "Compile
    # final videos" in Step 6, runner_compile concatenates every scene's
    # approved-variant video for this character and runs them through the
    # Editor pipeline (silence trim → voice swap → captions → WPM normalize)
    # into ONE stitched MP4. Fields updated by runner_compile.compile_job_videos.
    compiled_video_path: str | None = None
    compile_edit_id: str | None = None       # the editor edit_id used (re-render / debug)
    compile_status: str | None = None        # None | "compiling" | "done" | "failed"
    compile_error: str | None = None
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
    # Video provider used in Step 4 to animate every approved variant. Defaults
    # to Grok Imagine for back-compat; the Step-4 UI lets the user switch to
    # Kling / Veo / Runway / etc. before submitting the movement prompt.
    video_model: str = "grok-imagine"
    # Legacy single movement prompt. Kept in sync with the FIRST scene's
    # entry in `movement_prompts` so all "is the job in movement state?"
    # checks (`if job.movement_prompt:`) still work for callers that haven't
    # been updated for per-scene prompts.
    movement_prompt: str | None = None
    # Per-scene movement prompts (scene_id → prompt). One prompt drives every
    # approved variant for that scene across ALL characters — so e.g. in a
    # 3-scene reel each scene gets its own "guy pours oil" / "guy waves" /
    # "guy walks away" direction, applied uniformly across characters.
    movement_prompts: dict[str, str] = Field(default_factory=dict)
    images_per_character: int = 1
    videos_per_character: int = 1
    compacted: bool = False                  # set true after `compact` strips non-approved files
    # Prompt enrichment for the swap flow: when True, the user's custom
    # `prompt` AND the `movement_prompt` are expanded through GPT-4o before
    # being sent to the image / video models. Enriched text stashed so the
    # UI can show what was actually sent.
    enrich_prompt: bool = False
    enriched_image_prompt: str | None = None
    # Legacy single enriched movement (mirror of `movement_prompt`).
    enriched_movement_prompt: str | None = None
    # Per-scene enriched movement (mirror of `movement_prompts`). Each scene's
    # prompt is enriched independently so the cinematic expansion stays
    # focused on what happens IN THAT SHOT.
    enriched_movement_prompts: dict[str, str] = Field(default_factory=dict)
    # AI Director (opt-in): when True, runner calls `prompt_director.direct_swap`
    # before `_kick_char` and caches the full SwapDirectorPlan as JSON on
    # `director_prompts_json`. Per-variant tailored prompts populate
    # `GeneratedImage.prompt` directly. Movement step similarly populates
    # `enriched_movement_prompts` from `direct_movement`. Falls back silently
    # to the legacy enrich/raw path on any failure.
    use_director: bool = False
    director_prompts_json: str | None = None
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
    # Prompt enrichment — when True, the user's `prompt` is expanded through
    # GPT-4o before being sent to the image/video model (mirrors what web UIs
    # like Grok Imagine do internally). `enriched_prompt` stashes the actual
    # text used so users can inspect what the downstream model saw.
    enrich_prompt: bool = False
    enriched_prompt: str | None = None
    # AI Director (opt-in): when True, runner_media calls prompt_director
    # before the actual gen call and stores the single tailored prompt here.
    # Takes precedence over `enriched_prompt` when present.
    use_director: bool = False
    director_prompt: str | None = None
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
