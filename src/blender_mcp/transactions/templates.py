"""Side-effect-free planning for safe template scene copies."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from blender_mcp.policy.paths import ProjectPathPolicy

BLEND_SUFFIXES = frozenset({".blend"})


class TemplateCopyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_path: str = Field(min_length=1)
    destination_path: str = Field(min_length=1)
    allow_overwrite: bool = False


@dataclass(frozen=True)
class TemplateCopyPlan:
    source: Path
    destination: Path
    overwrite_existing: bool


def plan_template_copy(
    path_policy: ProjectPathPolicy,
    request: TemplateCopyRequest,
) -> TemplateCopyPlan:
    """Validate both paths and return a plan without changing the filesystem."""

    source = path_policy.resolve_input(
        request.template_path,
        allowed_suffixes=BLEND_SUFFIXES,
    )
    destination = path_policy.resolve_output(
        request.destination_path,
        allow_overwrite=request.allow_overwrite,
        allowed_suffixes=BLEND_SUFFIXES,
    )
    if source == destination:
        raise ValueError("template and destination must be different files")
    return TemplateCopyPlan(
        source=source,
        destination=destination,
        overwrite_existing=destination.exists(),
    )
