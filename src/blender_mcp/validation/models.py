"""Public validation result models."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field


class FindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ValidationFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    severity: FindingSeverity
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,79}$")
    message: str = Field(min_length=1, max_length=1_000)
    operation_index: int | None = Field(default=None, ge=0)
    subject_id: str | None = Field(default=None, min_length=1, max_length=200)
    details: dict[str, Any] = Field(default_factory=dict)


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: tuple[ValidationFinding, ...] = ()

    @computed_field
    @property
    def valid(self) -> bool:
        return not any(
            finding.severity in (FindingSeverity.ERROR, FindingSeverity.BLOCKER)
            for finding in self.findings
        )

    @computed_field
    @property
    def error_count(self) -> int:
        return sum(
            finding.severity is FindingSeverity.ERROR for finding in self.findings
        )

    @computed_field
    @property
    def warning_count(self) -> int:
        return sum(
            finding.severity is FindingSeverity.WARNING for finding in self.findings
        )

    @computed_field
    @property
    def blocker_count(self) -> int:
        return sum(
            finding.severity is FindingSeverity.BLOCKER for finding in self.findings
        )


class SceneValidationContext(BaseModel):
    """Small scene catalogue used to validate a transaction before Blender runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    current_scene_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    object_ids: frozenset[str] = Field(default_factory=frozenset)
    action_ids: frozenset[str] = Field(default_factory=frozenset)
    has_active_camera: bool = True
