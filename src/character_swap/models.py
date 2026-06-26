from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

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
    # Spoken language for this character's videos. "es" → every motion/video
    # prompt's quoted dialogue is auto-translated to neutral Latin American
    # Spanish + the Spanish accent clause is enforced (Hugo 2026-06-26). None/
    # "en" = default English (no change). Additive to the per-run 🗣 picker.
    language: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)

    def resolve_source_filename(self, image_id: str | None = None) -> str:
        """Filename to use as this character's reference image.

        A valid `image_id` (a per-job gallery pick, e.g. a specific outfit)
        beats the primary; unknown/None falls back to the primary silently —
        the user picked from a server-populated list, so a mismatch likely
        means the character was edited between picker-open and submit.
        """
        if image_id:
            picked = next((img for img in self.images
                           if img.image_id == image_id), None)
            if picked is not None:
                return picked.filename
        return self.filename


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


class QCReject(BaseModel):
    """One QC-rejected take, preserved so the user can SEE what QC threw away
    (Hugo 2026-06-20). When a generated image/clip fails vision/clip QC and is
    regenerated, the rejected file is normally overwritten by the next attempt;
    we snapshot it to a sidecar path and record it here instead. The FINAL
    kept-after-exhausted-retries take is NOT recorded here — it stays at the
    variant's own `path`/`final_video_path` with qc_status="failed" (⚠ chip)."""
    path: str
    reason: str | None = None
    attempt: int = 0                         # 1-based attempt that produced this reject
    kind: str = "swap"                       # "swap" (image) | "video" (clip)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class GeneratedImage(BaseModel):
    """One generated image variant for a character within a job."""
    variant_id: str
    path: str
    prompt: str                              # GENERATION_PROMPT for fresh gens, custom for edits
    parent_variant_id: str | None = None     # set when this is an edit
    scene_id: str | None = None              # which Job.scene_ids[i] this variant was generated against
    status: VariantStatus = VariantStatus.READY
    error: str | None = None
    # True when the image was UPLOADED by the user (not generated here) via the
    # Step-3 "Import" action — e.g. when the app can't generate it (content
    # policy). Surfaced as an "imported" badge in the UI.
    imported: bool = False
    # Vision-QC outcome (swap_qc.py): every generated variant is inspected by
    # a cheap Claude vision call. "passed" | "failed" (kept after exhausted
    # auto-retries — ⚠ chip in UI) | "skipped" (QC unavailable) | None
    # (pre-QC variants / imports).
    qc_status: str | None = None
    qc_reason: str | None = None
    qc_attempts: int = 0                     # total generation attempts incl. QC retries
    # The user's EXPLICIT intent for this slot — set when a prompt override
    # (✎↻ / scene-level "ändra bild") is supplied. Fed to the QC judge as
    # authoritative USER INTENT so it never fails — and REPAIR never reverts —
    # a deviation the user asked for (review 2026-06-13: the judge restored
    # the original prop because it only ever saw job.prompt).
    qc_intent: str | None = None
    # Every image QC rejected on the way to this slot's final result, each
    # snapshotted before the next attempt overwrote it (Hugo 2026-06-20 — "show
    # me every image failed by QC"). Empty for slots that passed first try.
    qc_rejects: list[QCReject] = Field(default_factory=list)
    # Set when the slot auto-fell-back to a different engine after the job's
    # chosen model rejected the prompt on CONTENT-POLICY grounds even after
    # prompt softening (e.g. gpt-image → "nbp-swap"). This is the sanctioned,
    # LOUD exception to the no-silent-cross-provider-fallback doctrine
    # (pipeline.generate_variant docstring): recorded here, emitted as a
    # `variant.fallback` event, and rendered as a ⇄ chip in the UI.
    fallback_model: str | None = None
    # Set when the prompt was auto-reworded by the Director to pass the
    # engine's safety system (moderation rescue, Hugo 2026-06-13) — same
    # scene, neutral phrasing, same engine. The reworded prompt persists on
    # `prompt` (visible in ✎↻); 🪄 chip in the Reengineer strip.
    moderation_rewritten: bool = False
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
    # Clip-QC outcome (video_qc.py): dialogue transcript match + frame-sampled
    # anatomy check. Same semantics as GeneratedImage.qc_*.
    qc_status: str | None = None
    qc_reason: str | None = None
    qc_attempts: int = 0
    # Every clip QC rejected before this take's final result (Hugo 2026-06-20),
    # snapshotted before the next take overwrote it. Empty for clips that
    # passed first try.
    qc_rejects: list[QCReject] = Field(default_factory=list)
    # User-imported clip (Hugo 2026-06-21): this slot's video was replaced by
    # an uploaded file instead of being generated. QC is skipped, re-animation
    # never clobbers it, and assembly prefers it over a generated take.
    imported: bool = False


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
    # Generated END FRAMES per scene (scene_id → path) — this character swapped
    # into that scene's uploaded end-pose ref (Job.end_frames_by_scene). Filled
    # during Step 3 generation (same time as the variants) when a scene has an
    # end pose; used as the Kling 3.0 end frame at animate time. Empty when no
    # end poses were given.
    end_frame_paths: dict[str, str] = Field(default_factory=dict)
    # Per-scene END-FRAME GENERATION ERRORS (scene_id → message). Set when the
    # swap-into-pose failed (e.g. a content-policy block that survived the
    # Nano-Banana-Pro fallback). Surfaced in Step 3 so failures are visible
    # instead of silently swallowed — that bare-except swallow was why the
    # first version of this feature was reverted.
    end_frame_errors: dict[str, str] = Field(default_factory=dict)
    # Step 6 (Compile) per-character output. When the user clicks "Compile
    # final videos" in Step 6, runner_compile concatenates every scene's
    # approved-variant video for this character and runs them through the
    # Editor pipeline (silence trim → voice swap → captions → WPM normalize)
    # into ONE stitched MP4. Fields updated by runner_compile.compile_job_videos.
    compiled_video_path: str | None = None
    compile_edit_id: str | None = None       # the editor edit_id used (re-render / debug)
    compile_status: str | None = None        # None | "compiling" | "done" | "failed"
    compile_error: str | None = None
    # Non-fatal compile caveat surfaced in the UI (backlog #9, 2026-06-12):
    # e.g. "final is missing 2 scene(s): s3 (no finished video)" — the
    # compile still succeeds, but never silently.
    compile_warning: str | None = None
    # Phase 4 (Full pipeline) per-character status. The "🚀 Run full pipeline"
    # button in Step 6 chains: compile-no-captions → package zip into a temp
    # dir → spawn `python automate.py` (Resolve render → Drive upload) → wait
    # for completion. runner_pipeline.run_full_pipeline updates these fields.
    pipeline_status: str | None = None
    # Progression: None → "compiling" → "packaging" → "rendering" → "uploading"
    # → "done" | "failed" (terminal). "rendering" + "uploading" come from
    # parsing the spawned automate.py's stdout for marker lines.
    pipeline_error: str | None = None
    pipeline_temp_dir: str | None = None     # where the zip was unpacked
    pipeline_drive_link: str | None = None   # final Drive URL on success
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
    # Reengineer "direct image — no swap" scenes (subset of scene_ids): the
    # image is used AS-IS (no per-character swap), so `_kick_char` generates no
    # variants for them and one shared Kling clip is reused for every character.
    direct_scene_ids: list[str] = Field(default_factory=list)
    characters: dict[str, JobCharacter] = Field(default_factory=dict)
    prompt: str | None = None                # custom swap prompt; falls back to pipeline.GENERATION_PROMPT
    image_model: str = "gpt-image"           # which adapter generates the variants
    # Outfit choice carried from Reengineer (or future Swap UI): drives stock
    # prompt construction per engine — incl. gpt2-id-swap's flipped-role
    # rebuild. "scene" (original person's clothes) | "character" | "custom".
    outfit_mode: str = "scene"
    outfit_text: str | None = None
    # Video provider used in Step 4 to animate every approved variant. Defaults
    # to Grok Imagine for back-compat; the Step-4 UI lets the user switch to
    # Kling / Veo / Runway / etc. before submitting the movement prompt.
    video_model: str = "grok-imagine"
    # Per-job native-audio override for video generation (Kling v3 via fal).
    # None → fall back to settings.kling_generate_audio (global default OFF).
    # Reengineer jobs set True: the swapped character's voice comes from the
    # video model itself, not ElevenLabs.
    video_audio: bool | None = None
    # Provenance tag: "reengineer:<re_id>" when this job was created by the
    # Reengineer pipeline (video → scenes → swap). None for normal Swap jobs.
    origin: str | None = None

    @property
    def from_reengineer(self) -> bool:
        """True for jobs created by the Reengineer pipeline. The Reengineer
        EDIT MODE deliberately mutates approvals/variants after movement was
        submitted (its own approval flow gates the expensive work), so the
        Swap flow's movement locks are relaxed for these jobs only."""
        return (self.origin or "").startswith("reengineer:")
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
    # Per-APPROVED-VARIANT movement prompts (variant_id → prompt). The granular
    # layer above `movement_prompts`: when set, each approved image animates
    # with its OWN prompt (the Higgsfield "per-slot prompt" model) instead of
    # sharing its scene's prompt. Empty/missing → fall back to the scene prompt.
    movement_prompts_by_variant: dict[str, str] = Field(default_factory=dict)
    images_per_character: int = 1
    videos_per_character: int = 1
    # Per-job video duration override (seconds). None → use the env default
    # (settings.video_duration_secs). Picker in Step 4 sets this from the
    # selected video_model's `duration_options` registry. Each per-provider
    # submit function still defends with its own clamp.
    duration_secs: int | None = None
    # Per-APPROVED-VARIANT duration override (variant_id → seconds). Mirrors
    # movement_prompts_by_variant: each approved image can have its own clip
    # length (the Higgsfield "per-slot duration" model). Missing → fall back
    # to `duration_secs`, then the env default.
    durations_by_variant: dict[str, int] = Field(default_factory=dict)
    # Per-SCENE duration override (scene_id → seconds). The granularity the
    # Step 4 UI actually uses: one duration per scene, shared by all that
    # scene's approved images. Resolution order in the runner:
    # per-variant → per-scene → `duration_secs` → env default.
    durations_by_scene: dict[str, int] = Field(default_factory=dict)
    # Per-SCENE video-model override (scene_id → model slug). Opt-in: empty →
    # every scene animates with `video_model` (the job-wide default). When a
    # scene has an entry, THAT scene's clip uses the override provider instead.
    # Resolution order in the runner: per-scene → `video_model` → "grok-imagine".
    # Mirrors `durations_by_scene` / `movement_prompts`; old jobs load empty.
    # NOTE: only `kling-v3` honors per-scene END FRAMES — a scene overridden to
    # a non-Kling model ignores its end pose (the Step-4 UI warns; Reengineer
    # hides the control for that scene).
    video_models_by_scene: dict[str, str] = Field(default_factory=dict)
    # Optional per-scene END-POSE reference (scene_id → uploaded image path).
    # Set on a scene in Step 1. During Step 3 the runner SWAPS each character
    # into the pose (so the end frame features the same person) and hands the
    # result to Kling 3.0 as the end frame — first/last-frame interpolation.
    # Keyed by scene_id, so a duplicated scene can carry a DIFFERENT end pose
    # (same start, different end → different clip). Only kling-v3 honors it;
    # other models ignore it.
    end_frames_by_scene: dict[str, str] = Field(default_factory=dict)
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
    # Optional third reference image passed to the image model after
    # (scene, character) — useful for "match this background" / "use this
    # outfit" / general visual context. Stored as an absolute path on disk;
    # None when the user didn't upload one. Image models that don't support
    # 3+ references (e.g. grok-image is text-only) just ignore it.
    extra_reference_path: str | None = None
    # Where the OUTPUT background comes from in the swap phase (Hugo 2026-06-21).
    # New standard "character": the surroundings/environment are taken from the
    # CHARACTER reference image (the scene only supplies pose, action, framing
    # and held props; the person is relit to the character's own environment).
    # Opt-out "scene": preserve the scene's background exactly (the pre-2026-06-21
    # default — "Option B"). An explicitly-uploaded replacement (extra_reference_path
    # = "Image 3") always wins over both — see runner._swap_background_mode().
    background_source: str = "character"
    # Per-job Step-6 compile settings (Hugo 2026-06-17): the ⚙ panel values the
    # job was last compiled with, so each job keeps its own editable preset
    # (seeded from the global default for a fresh job). Stored as the snake_case
    # CompileVideosBody shape; surfaced in _job_to_dict for the frontend to
    # rehydrate the panel per job. None = never compiled → use the global default.
    compile_settings: dict | None = None
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


class ChatSession(BaseModel):
    """One conversation with the Claude-driven agent in the Chat tab.

    `messages` is the raw Anthropic Messages API shape: a list of
    `{role, content}` dicts where `content` can be a string or a list of
    content blocks (text / tool_use / tool_result). We replay the entire
    list to Anthropic on every turn so the model has full context.

    `media` is a flat side-list of generations this chat produced
    (images / videos / audio). The UI renders them inline next to the
    assistant message that produced them via the per-message `media_refs`
    field on text content blocks.
    """
    chat_id: str
    title: str = "New chat"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    # Flat record of generations this chat triggered. Each entry is
    # `{kind, generation_id, url, prompt, created_at}` — kind ∈
    # {image, video, audio, avatar, swap_variant, swap_video, broll_clip, edit}.
    media: list[dict[str, Any]] = Field(default_factory=list)


class AppState(BaseModel):
    scenes: dict[str, SceneAsset] = Field(default_factory=dict)
    characters: dict[str, CharacterAsset] = Field(default_factory=dict)
    projects: dict[str, ProjectAsset] = Field(default_factory=dict)
    jobs: dict[str, Job] = Field(default_factory=dict)
    generations: dict[str, MediaGeneration] = Field(default_factory=dict)
    chats: dict[str, ChatSession] = Field(default_factory=dict)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
