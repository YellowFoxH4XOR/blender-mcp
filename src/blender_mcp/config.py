"""Runtime configuration for the MCP server and Blender subprocess."""

from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BlenderMCPConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_root: Path
    blender_executable: Path
    adapter_script: Path
    subprocess_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_request_bytes: int = Field(default=1_048_576, gt=0, le=16_777_216)

    @field_validator("project_root", "blender_executable", "adapter_script")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("must be an absolute path")
        return path.resolve(strict=False)

    @classmethod
    def from_env(cls) -> "BlenderMCPConfig":
        project_root = Path(
            os.environ.get("BLENDER_MCP_PROJECT_ROOT", os.getcwd())
        ).expanduser().resolve()
        blender_executable = Path(
            os.environ.get(
                "BLENDER_MCP_BLENDER_EXECUTABLE",
                "/Applications/Blender.app/Contents/MacOS/Blender",
            )
        )
        adapter_override = os.environ.get("BLENDER_MCP_ADAPTER_SCRIPT")
        adapter_script = (
            Path(adapter_override)
            if adapter_override
            else Path(str(files("blender_adapter").joinpath("bootstrap.py")))
        )
        timeout = float(os.environ.get("BLENDER_MCP_TIMEOUT_SECONDS", "120"))
        return cls(
            project_root=project_root,
            blender_executable=blender_executable,
            adapter_script=adapter_script,
            subprocess_timeout_seconds=timeout,
        )
