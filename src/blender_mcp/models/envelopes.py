"""Versioned messages exchanged with the bundled Blender adapter."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROTOCOL_VERSION = "1.0"
PayloadT = TypeVar("PayloadT")
ResultT = TypeVar("ResultT")


def new_request_id() -> str:
    return f"req_{uuid4().hex}"


class AdapterOperation(StrEnum):
    INSPECT_SCENE = "inspect_scene"
    VALIDATE_SCENE = "validate_scene"
    APPLY_SCENE_TRANSACTION = "apply_scene_transaction"
    RENDER_PREVIEW = "render_preview"
    RENDER_ANIMATION = "render_animation"


class StructuredError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class RequestEnvelope(BaseModel, Generic[PayloadT]):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = PROTOCOL_VERSION
    request_id: str = Field(
        default_factory=new_request_id,
        pattern=r"^req_[A-Za-z0-9_-]+$",
    )
    command: AdapterOperation
    payload: PayloadT


class ArtifactReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "preview",
        "scene",
        "render",
        "manifest",
        "checkpoint",
        "frame_sequence",
    ]
    path: str
    media_type: str = Field(pattern=r"^[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+$")
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    duration_seconds: float | None = Field(default=None, ge=0)
    frame_count: int | None = Field(default=None, ge=0)


class AdapterTiming(BaseModel):
    model_config = ConfigDict(extra="forbid")

    execution_ms: float = Field(ge=0)


class ResultEnvelope(BaseModel, Generic[ResultT]):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = PROTOCOL_VERSION
    request_id: str = Field(pattern=r"^req_[A-Za-z0-9_-]+$")
    command: AdapterOperation
    ok: bool
    data: ResultT | None = None
    error: StructuredError | None = None
    warnings: list[str] = Field(default_factory=list)
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    timing: AdapterTiming

    @model_validator(mode="after")
    def result_matches_status(self) -> "ResultEnvelope[ResultT]":
        if self.ok and self.error is not None:
            raise ValueError("successful results cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("failed results must contain an error")
        return self
