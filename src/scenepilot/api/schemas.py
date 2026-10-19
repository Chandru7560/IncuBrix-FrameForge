"""Validated request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SceneInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=5000)
    visual_hint: str | None = Field(default=None, max_length=1000)


class CaptionsOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    burn_in: bool = True
    srt: bool = True


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool
    details: dict[str, object] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: ErrorDetail


class JobAccepted(BaseModel):
    job_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    progress: int = Field(ge=0, le=100)
    status_url: str


class JobFailure(BaseModel):
    code: str
    message: str


class JobStatus(BaseModel):
    job_id: str
    status: Literal["queued", "running", "succeeded", "failed"]
    progress: int = Field(ge=0, le=100)
    warnings: list[str]
    result_links: dict[str, str]
    transcript: dict[str, object] | None = None
    scene_plan: dict[str, object] | None = None
    narration_duration_seconds: float | None = None
    created_at: datetime
    updated_at: datetime
    error: JobFailure | None = None


class JobOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    voice: str = Field(default="en-IN-female", min_length=1, max_length=100)
    aspect_ratios: list[Literal["16:9", "9:16", "1:1"]] = Field(
        default_factory=lambda: ["16:9"], min_length=1, max_length=3
    )
    style: str = Field(default="bright, modern, real-world footage", max_length=1000)
    avoid: list[str] = Field(default_factory=list, max_length=100)
    captions: CaptionsOptions = Field(default_factory=CaptionsOptions)

    @field_validator("aspect_ratios")
    @classmethod
    def ratios_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("aspect_ratios must not contain duplicates")
        return value


class JobCreateRequest(JobOptions):
    audio_url: str | None = Field(default=None, max_length=2048)
    script: str | None = Field(default=None, max_length=100_000)
    scenes: list[SceneInput] | None = None

    @field_validator("script")
    @classmethod
    def script_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("script must not be empty")
        return value

    @model_validator(mode="after")
    def require_exactly_one_input(self) -> JobCreateRequest:
        selected = sum(
            (
                self.audio_url is not None,
                self.script is not None,
                self.scenes is not None,
            )
        )
        if selected != 1:
            raise ValueError("provide exactly one of audio_url, script, or scenes")
        if self.scenes is not None and not self.scenes:
            raise ValueError("scenes must contain at least one scene")
        return self


class SceneRatingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    scene_id: str = Field(min_length=1, max_length=128)
    score: int = Field(ge=1, le=5)
    rationale: str | None = Field(default=None, max_length=2000)


class SceneRatingResponse(BaseModel):
    rating_id: str
    job_id: str
    scene_id: str
    score: int
    rationale: str | None
    created_at: datetime
    idempotent_replay: bool = False
