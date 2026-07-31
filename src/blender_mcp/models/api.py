"""Typed public service inputs and outputs."""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from blender_mcp.models.envelopes import StructuredError

T = TypeVar("T")


class InspectSceneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_path: str = Field(min_length=1)


class RenderPreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene_path: str = Field(min_length=1)
    output_path: str = Field(min_length=1)
    max_width: int = Field(default=960, ge=64, le=7680)
    max_height: int = Field(default=540, ge=64, le=4320)
    allow_overwrite: bool = False


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    media_type: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class StatusResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    executable: str
    version: str | None = None
    adapter_available: bool


class ServiceResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1)
    ok: bool
    result: T | None = None
    error: StructuredError | None = None

    @model_validator(mode="after")
    def result_matches_status(self) -> "ServiceResponse[T]":
        if self.ok and self.error is not None:
            raise ValueError("successful responses cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("failed responses must contain an error")
        return self
