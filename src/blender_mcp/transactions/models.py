"""Strict public models for the constrained scene-operation DSL."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator

Vector3 = tuple[FiniteFloat, FiniteFloat, FiniteFloat]
ObjectId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_.:-]+$")]


class _Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SetTransformOperation(_Operation):
    op: Literal["set_transform"]
    object_id: ObjectId
    location: Vector3 | None = None
    rotation_euler: Vector3 | None = None
    scale: Vector3 | None = None

    @model_validator(mode="after")
    def has_a_transform(self) -> "SetTransformOperation":
        if self.location is None and self.rotation_euler is None and self.scale is None:
            raise ValueError("set_transform requires at least one transform field")
        if self.scale is not None and any(component == 0 for component in self.scale):
            raise ValueError("scale components must be non-zero")
        return self


class SetVisibilityOperation(_Operation):
    op: Literal["set_visibility"]
    object_id: ObjectId
    viewport: bool | None = None
    render: bool | None = None

    @model_validator(mode="after")
    def has_a_visibility_target(self) -> "SetVisibilityOperation":
        if self.viewport is None and self.render is None:
            raise ValueError("set_visibility requires viewport or render")
        return self


class ConfigureSceneOperation(_Operation):
    op: Literal["configure_scene"]
    frame_start: int | None = Field(default=None, ge=0, le=1_000_000)
    frame_end: int | None = Field(default=None, ge=0, le=1_000_000)
    fps: int | None = Field(default=None, ge=1, le=240)
    resolution_x: int | None = Field(default=None, ge=16, le=16_384)
    resolution_y: int | None = Field(default=None, ge=16, le=16_384)
    resolution_percentage: int | None = Field(default=None, ge=1, le=100)
    render_engine: Literal[
        "BLENDER_EEVEE",
        "BLENDER_EEVEE_NEXT",
        "BLENDER_WORKBENCH",
        "CYCLES",
    ] | None = None

    @model_validator(mode="after")
    def is_a_valid_configuration(self) -> "ConfigureSceneOperation":
        values = (
            self.frame_start,
            self.frame_end,
            self.fps,
            self.resolution_x,
            self.resolution_y,
            self.resolution_percentage,
            self.render_engine,
        )
        if all(value is None for value in values):
            raise ValueError("configure_scene requires at least one setting")
        if (
            self.frame_start is not None
            and self.frame_end is not None
            and self.frame_end < self.frame_start
        ):
            raise ValueError("frame_end must be greater than or equal to frame_start")
        return self


class ApplyActionOperation(_Operation):
    op: Literal["apply_action"]
    object_id: ObjectId
    action_id: ObjectId
    frame_start: int = Field(default=1, ge=0, le=1_000_000)
    frame_end: int | None = Field(default=None, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def has_valid_frame_range(self) -> "ApplyActionOperation":
        if self.frame_end is not None and self.frame_end < self.frame_start:
            raise ValueError("frame_end must be greater than or equal to frame_start")
        return self


SceneOperation = Annotated[
    SetTransformOperation
    | SetVisibilityOperation
    | ConfigureSceneOperation
    | ApplyActionOperation,
    Field(discriminator="op"),
]


class SceneTransaction(BaseModel):
    """One bounded, optimistic transaction against a known scene revision."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(
        min_length=1,
        max_length=120,
        pattern=r"^txn_[A-Za-z0-9_-]+$",
    )
    expected_scene_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    operations: list[SceneOperation] = Field(min_length=1, max_length=100)
