"""Validated runtime configuration for the server and Blender subprocess."""

from __future__ import annotations

import os
import tomllib
from importlib.resources import files
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator


_DEFAULT_BLENDER = "/Applications/Blender.app/Contents/MacOS/Blender"


class BlenderMCPConfig(BaseModel):
    """One immutable, security-relevant server configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    project_root: Path
    blender_executable: Path
    adapter_script: Path
    execution_mode: Literal["auto", "headless", "live"] = "auto"

    allow_network: bool = False
    allow_overwrite: bool = False
    create_checkpoints: bool = True
    max_operations_per_transaction: int = Field(default=200, gt=0, le=10_000)
    max_request_bytes: int = Field(default=1_048_576, gt=0, le=16_777_216)

    max_render_width: int = Field(default=3840, gt=0, le=16_384)
    max_render_height: int = Field(default=2160, gt=0, le=16_384)
    max_render_fps: int = Field(default=60, gt=0, le=240)
    max_render_frames: int = Field(default=54_000, gt=0)
    subprocess_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    max_render_timeout_seconds: float = Field(default=21_600, gt=0, le=86_400)
    min_free_disk_gb: float = Field(default=20.0, ge=0)
    concurrent_jobs: int = Field(default=1, gt=0, le=16)

    live_bridge_enabled: bool = False
    live_bridge_host: str = "127.0.0.1"
    live_bridge_port: int = Field(default=9876, gt=0, le=65_535)
    live_bridge_timeout_seconds: float = Field(default=30.0, gt=0, le=3600)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_retention_days: int = Field(default=30, ge=1, le=3650)
    http_host: str = "127.0.0.1"
    http_port: int = Field(default=8000, gt=0, le=65_535)
    http_token_file: Path | None = None
    remotion_project: Path | None = None
    node_executable: Path | None = None
    npm_executable: Path | None = None

    @field_validator(
        "project_root",
        "blender_executable",
        "adapter_script",
        "http_token_file",
        "remotion_project",
        "node_executable",
        "npm_executable",
    )
    @classmethod
    def absolute_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        path = value.expanduser()
        if not path.is_absolute():
            raise ValueError("must be an absolute path")
        return path.resolve(strict=False)

    @field_validator("allow_network")
    @classmethod
    def network_is_never_enabled(cls, value: bool) -> bool:
        if value:
            raise ValueError("network access is not supported")
        return value

    @field_validator("live_bridge_host")
    @classmethod
    def bridge_is_loopback_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("live bridge host must be loopback")
        return value

    @property
    def state_dir(self) -> Path:
        return self.project_root / ".blender-mcp"

    @property
    def state_db(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def jobs_dir(self) -> Path:
        return self.state_dir / "jobs"

    @property
    def checkpoints_dir(self) -> Path:
        return self.state_dir / "checkpoints"

    @property
    def logs_dir(self) -> Path:
        return self.state_dir / "logs"

    @property
    def quarantine_dir(self) -> Path:
        return self.state_dir / "quarantine"

    @property
    def cache_dir(self) -> Path:
        return self.state_dir / "cache"

    @classmethod
    def load(
        cls,
        config_path: str | Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> "BlenderMCPConfig":
        """Load TOML configuration, then apply explicit environment overrides."""

        env = os.environ if environ is None else environ
        selected_path: Path | None
        if config_path is not None:
            selected_path = Path(config_path).expanduser().resolve(strict=False)
            if not selected_path.is_file():
                raise ValueError(f"configuration file was not found: {selected_path}")
        elif env.get("BLENDER_MCP_CONFIG"):
            selected_path = (
                Path(env["BLENDER_MCP_CONFIG"]).expanduser().resolve(strict=False)
            )
            if not selected_path.is_file():
                raise ValueError(f"configuration file was not found: {selected_path}")
        else:
            candidate = Path.cwd() / "blender-mcp.toml"
            selected_path = candidate if candidate.is_file() else None

        raw: dict[str, object] = {}
        if selected_path is not None:
            try:
                with selected_path.open("rb") as handle:
                    parsed = tomllib.load(handle)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ValueError(f"invalid configuration: {exc}") from exc
            raw = cls._flatten_toml(parsed)

        configured_project_root = raw.pop("project_root", None)
        project_root_value = (
            env.get("BLENDER_MCP_PROJECT_ROOT") or configured_project_root
        )
        if project_root_value is None:
            project_root_value = os.getcwd()

        configured_blender = raw.pop("blender_executable", _DEFAULT_BLENDER)
        blender_value = (
            env.get("BLENDER_MCP_BLENDER_EXECUTABLE") or configured_blender
        )
        configured_adapter = raw.pop("adapter_script", None)
        adapter_value = env.get("BLENDER_MCP_ADAPTER_SCRIPT") or configured_adapter
        if adapter_value is None:
            adapter_value = str(files("blender_adapter").joinpath("bootstrap.py"))

        timeout_override = env.get("BLENDER_MCP_TIMEOUT_SECONDS")
        request_override = env.get("BLENDER_MCP_MAX_REQUEST_BYTES")
        http_host_override = env.get("BLENDER_MCP_HTTP_HOST")
        http_port_override = env.get("BLENDER_MCP_HTTP_PORT")
        http_token_file_override = env.get("BLENDER_MCP_HTTP_TOKEN_FILE")
        remotion_project_override = env.get("BLENDER_MCP_REMOTION_PROJECT")
        node_executable_override = env.get("BLENDER_MCP_NODE_EXECUTABLE")
        npm_executable_override = env.get("BLENDER_MCP_NPM_EXECUTABLE")
        if timeout_override is not None:
            raw["subprocess_timeout_seconds"] = timeout_override
        if request_override is not None:
            raw["max_request_bytes"] = request_override
        if http_host_override is not None:
            raw["http_host"] = http_host_override
        if http_port_override is not None:
            raw["http_port"] = http_port_override
        if http_token_file_override is not None:
            raw["http_token_file"] = http_token_file_override
        if remotion_project_override is not None:
            raw["remotion_project"] = remotion_project_override
        if node_executable_override is not None:
            raw["node_executable"] = node_executable_override
        if npm_executable_override is not None:
            raw["npm_executable"] = npm_executable_override

        return cls(
            project_root=Path(str(project_root_value)),
            blender_executable=Path(str(blender_value)),
            adapter_script=Path(str(adapter_value)),
            **raw,
        )

    @classmethod
    def from_env(cls) -> "BlenderMCPConfig":
        """Backward-compatible environment-only entry point."""

        return cls.load()

    @staticmethod
    def _flatten_toml(document: dict[str, object]) -> dict[str, object]:
        top = dict(document)
        policy = _table(top.pop("policy", {}), "policy")
        render = _table(top.pop("render", {}), "render")
        live = _table(top.pop("live_bridge", {}), "live_bridge")
        logging = _table(top.pop("logging", {}), "logging")
        http = _table(top.pop("http", {}), "http")
        remotion = _table(top.pop("remotion", {}), "remotion")
        flattened = {
            **top,
            **{
                key: value
                for key, value in {
                    "allow_network": policy.pop("allow_network", None),
                    "allow_overwrite": policy.pop("allow_overwrite", None),
                    "create_checkpoints": policy.pop("create_checkpoints", None),
                    "max_operations_per_transaction": policy.pop(
                        "max_operations_per_transaction", None
                    ),
                    "max_request_bytes": policy.pop("max_request_bytes", None),
                    "max_render_width": render.pop("max_width", None),
                    "max_render_height": render.pop("max_height", None),
                    "max_render_fps": render.pop("max_fps", None),
                    "max_render_frames": render.pop("max_frames", None),
                    "max_render_timeout_seconds": render.pop(
                        "max_timeout_seconds", None
                    ),
                    "min_free_disk_gb": render.pop("min_free_disk_gb", None),
                    "concurrent_jobs": render.pop("concurrent_jobs", None),
                    "live_bridge_enabled": live.pop("enabled", None),
                    "live_bridge_host": live.pop("host", None),
                    "live_bridge_port": live.pop("port", None),
                    "live_bridge_timeout_seconds": live.pop(
                        "request_timeout_seconds", None
                    ),
                    "log_level": logging.pop("level", None),
                    "log_retention_days": logging.pop("retention_days", None),
                    "http_host": http.pop("host", None),
                    "http_port": http.pop("port", None),
                    "http_token_file": http.pop("token_file", None),
                    "remotion_project": remotion.pop("project", None),
                    "node_executable": remotion.pop("node_executable", None),
                    "npm_executable": remotion.pop("npm_executable", None),
                }.items()
                if value is not None
            },
        }
        unknown = {
            "policy": policy,
            "render": render,
            "live_bridge": live,
            "logging": logging,
            "http": http,
            "remotion": remotion,
        }
        unknown = {name: values for name, values in unknown.items() if values}
        if unknown:
            names = ", ".join(
                f"{section}.{key}"
                for section, values in unknown.items()
                for key in values
            )
            raise ValueError(f"unknown configuration keys: {names}")
        return flattened


def _table(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a TOML table")
    return dict(value)
