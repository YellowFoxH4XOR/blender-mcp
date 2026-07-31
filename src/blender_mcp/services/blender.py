"""Application services shared by MCP and direct Python callers."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import ValidationError

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.errors import BlenderMCPError, ErrorCode
from blender_mcp.headless import BlenderLauncher
from blender_mcp.models import (
    AdapterOperation,
    Artifact,
    InspectSceneInput,
    RenderPreviewInput,
    ServiceResponse,
    StatusResult,
    StructuredError,
    new_request_id,
)
from blender_mcp.policy import ProjectPathPolicy

T = TypeVar("T")


class BlenderService:
    def __init__(
        self,
        config: BlenderMCPConfig,
        *,
        launcher: BlenderLauncher | None = None,
    ) -> None:
        self.config = config
        self.paths = ProjectPathPolicy(
            config.project_root,
            max_request_bytes=config.max_request_bytes,
        )
        self.launcher = launcher or BlenderLauncher(config)

    def get_status(self, *, request_id: str | None = None) -> ServiceResponse[StatusResult]:
        return self._respond(request_id, self._get_status)

    def inspect_scene(
        self,
        scene_path: str,
        *,
        request_id: str | None = None,
    ) -> ServiceResponse[dict[str, Any]]:
        def action(resolved_request_id: str) -> dict[str, Any]:
            arguments = InspectSceneInput(scene_path=scene_path)
            scene = self.paths.resolve_input(arguments.scene_path)
            result = self.launcher.invoke(
                AdapterOperation.INSPECT_SCENE,
                {"scene_path": str(scene)},
                request_id=resolved_request_id,
            )
            if not result.ok:
                assert result.error is not None
                raise BlenderMCPError(
                    ErrorCode.BLENDER_FAILED,
                    result.error.message,
                    details={
                        "adapter_code": result.error.code,
                        **result.error.details,
                    },
                )
            return result.data or {}

        return self._respond(request_id, action)

    def render_preview(
        self,
        scene_path: str,
        output_path: str,
        *,
        max_width: int = 960,
        max_height: int = 540,
        allow_overwrite: bool = False,
        request_id: str | None = None,
    ) -> ServiceResponse[Artifact]:
        def action(resolved_request_id: str) -> Artifact:
            arguments = RenderPreviewInput(
                scene_path=scene_path,
                output_path=output_path,
                max_width=max_width,
                max_height=max_height,
                allow_overwrite=allow_overwrite,
            )
            scene = self.paths.resolve_input(arguments.scene_path)
            output = self.paths.resolve_output(
                arguments.output_path,
                allow_overwrite=arguments.allow_overwrite,
                allowed_suffixes=frozenset({".png"}),
            )
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            result = self.launcher.invoke(
                AdapterOperation.RENDER_PREVIEW,
                {
                    "scene_path": str(scene),
                    "artifact_path": str(output),
                    "max_width": arguments.max_width,
                    "max_height": arguments.max_height,
                },
                request_id=resolved_request_id,
            )
            if not result.ok:
                assert result.error is not None
                raise BlenderMCPError(
                    ErrorCode.BLENDER_FAILED,
                    result.error.message,
                    details={
                        "adapter_code": result.error.code,
                        **result.error.details,
                    },
                )
            if not output.is_file():
                raise BlenderMCPError(
                    ErrorCode.ARTIFACT_MISSING,
                    "Blender reported success without producing the preview",
                    details={"output_path": output_path},
                )
            return Artifact(
                path=output.relative_to(self.config.project_root).as_posix(),
                media_type="image/png",
                size_bytes=output.stat().st_size,
                sha256=self._sha256(output),
            )

        return self._respond(request_id, action)

    def _get_status(self, _: str) -> StatusResult:
        process = self.launcher.probe()
        if process.returncode != 0:
            raise BlenderMCPError(
                ErrorCode.BLENDER_FAILED,
                "Blender version probe failed",
                details={
                    "returncode": process.returncode,
                    "stderr": process.stderr[-4000:],
                },
            )
        version = next(
            (line.strip() for line in process.stdout.splitlines() if line.strip()),
            None,
        )
        return StatusResult(
            available=True,
            executable=str(self.config.blender_executable),
            version=version,
            adapter_available=self.config.adapter_script.is_file(),
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _respond(
        request_id: str | None,
        action: Callable[[str], T],
    ) -> ServiceResponse[T]:
        resolved_request_id = request_id or new_request_id()
        try:
            return ServiceResponse[T](
                request_id=resolved_request_id,
                ok=True,
                result=action(resolved_request_id),
            )
        except BlenderMCPError as exc:
            return ServiceResponse[T](
                request_id=resolved_request_id,
                ok=False,
                error=StructuredError(
                    code=exc.code.value,
                    message=exc.message,
                    details=exc.details,
                ),
            )
        except ValidationError as exc:
            return ServiceResponse[T](
                request_id=resolved_request_id,
                ok=False,
                error=StructuredError(
                    code=ErrorCode.INVALID_REQUEST.value,
                    message="Request validation failed",
                    details={"errors": exc.errors(include_url=False)},
                ),
            )
        except Exception:
            return ServiceResponse[T](
                request_id=resolved_request_id,
                ok=False,
                error=StructuredError(
                    code=ErrorCode.INTERNAL_ERROR.value,
                    message="Unexpected internal error",
                ),
            )
