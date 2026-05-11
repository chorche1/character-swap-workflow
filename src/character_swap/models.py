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


class SceneAsset(BaseModel):
    scene_id: str
    filename: str
    original_name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CharacterAsset(BaseModel):
    char_id: str
    filename: str
    name: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class GeneratedImage(BaseModel):
    """One generated image variant for a character within a job."""
    variant_id: str
    path: str
    prompt: str                              # GENERATION_PROMPT for fresh gens, custom for edits
    parent_variant_id: str | None = None     # set when this is an edit
    status: str = "ready"                    # "generating" | "ready" | "failed"
    error: str | None = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class VideoVariant(BaseModel):
    """One Grok-generated video for a character."""
    video_id: str
    grok_job_id: str
    status: str = "pending"                  # "pending"|"processing"|"done"|"failed"|"error"
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
    scene_id: str
    scene_image_path: str
    characters: dict[str, JobCharacter] = Field(default_factory=dict)
    movement_prompt: str | None = None
    images_per_character: int = 1
    videos_per_character: int = 1
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class AppState(BaseModel):
    scenes: dict[str, SceneAsset] = Field(default_factory=dict)
    characters: dict[str, CharacterAsset] = Field(default_factory=dict)
    jobs: dict[str, Job] = Field(default_factory=dict)
    last_updated: datetime = Field(default_factory=datetime.utcnow)
