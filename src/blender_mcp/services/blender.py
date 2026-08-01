"""Application services shared by MCP and direct Python callers."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from pydantic import ValidationError

from blender_mcp.assets import AssetCatalog, AssetCatalogError
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
from blender_mcp.transactions import (
    InMemoryTransactionIdempotency,
    SceneTransaction,
    TransactionIdConflict,
    TransactionIdempotency,
    compute_scene_revision,
)

if TYPE_CHECKING:
    from blender_mcp.rendering import RenderJobManager

T = TypeVar("T")


class BlenderService:
    def __init__(
        self,
        config: BlenderMCPConfig,
        *,
        launcher: BlenderLauncher | None = None,
        transaction_idempotency: TransactionIdempotency | None = None,
        render_manager: "RenderJobManager | None" = None,
    ) -> None:
        self.config = config
        self.paths = ProjectPathPolicy(
            config.project_root,
            max_request_bytes=config.max_request_bytes,
        )
        self.launcher = launcher or BlenderLauncher(config)
        self.transaction_idempotency = (
            transaction_idempotency or InMemoryTransactionIdempotency()
        )
        self._render_manager = render_manager

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
            data = dict(result.data or {})
            revision = compute_scene_revision(
                scene,
                logical_path=Path(scene_path).as_posix(),
            )
            data["scene_revision"] = revision.revision
            data["scene_sha256"] = revision.content_sha256
            return data

        return self._respond(request_id, action)

    def validate_scene(
        self,
        scene_path: str,
        *,
        request_id: str | None = None,
    ) -> ServiceResponse[dict[str, Any]]:
        def action(resolved_request_id: str) -> dict[str, Any]:
            arguments = InspectSceneInput(scene_path=scene_path)
            scene = self.paths.resolve_input(arguments.scene_path)
            result = self.launcher.invoke(
                AdapterOperation.VALIDATE_SCENE,
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

    def list_project_assets(
        self,
        *,
        kind: str | None = None,
        request_id: str | None = None,
    ) -> ServiceResponse[dict[str, Any]]:
        def action(_: str) -> dict[str, Any]:
            try:
                catalog = AssetCatalog.load(self.config.project_root)
                records = catalog.list(kind)
            except AssetCatalogError as exc:
                raise BlenderMCPError(
                    ErrorCode.ASSET_NOT_APPROVED,
                    str(exc),
                ) from exc
            return {
                "assets": [
                    record.model_dump(mode="json")
                    for record in records
                ],
                "count": len(records),
            }

        return self._respond(request_id, action)

    def create_scene_from_template(
        self,
        template_id: str,
        destination: str,
        *,
        variables: dict[str, Any] | None = None,
        allow_overwrite: bool = False,
        request_id: str | None = None,
    ) -> ServiceResponse[Artifact]:
        def action(resolved_request_id: str) -> Artifact:
            try:
                catalog = AssetCatalog.load(self.config.project_root)
                template = catalog.get(template_id)
                source = catalog.resolve(template_id)
            except AssetCatalogError as exc:
                raise BlenderMCPError(
                    ErrorCode.ASSET_NOT_APPROVED,
                    str(exc),
                ) from exc
            if template.kind != "template":
                raise BlenderMCPError(
                    ErrorCode.ASSET_NOT_APPROVED,
                    "The approved asset is not a scene template",
                    details={"template_id": template_id},
                )
            if variables:
                supported = {"fps", "resolution"}
                unknown = sorted(set(variables) - supported)
                if unknown:
                    raise BlenderMCPError(
                        ErrorCode.INVALID_REQUEST,
                        "Template variables contain unsupported fields",
                        details={"fields": unknown},
                    )
            output = self.paths.resolve_output(
                destination,
                allow_overwrite=allow_overwrite,
                allowed_suffixes=frozenset({".blend"}),
            )
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            temporary = output.parent / (
                f".{output.stem}.{resolved_request_id}.template.blend"
            )
            if temporary.exists():
                raise BlenderMCPError(
                    ErrorCode.PATH_ALREADY_EXISTS,
                    "Temporary template destination already exists",
                )
            try:
                self._atomic_copy(source, temporary)
                configure: dict[str, Any] = {"op": "configure_scene"}
                if variables and "fps" in variables:
                    configure["fps"] = variables["fps"]
                if variables and "resolution" in variables:
                    resolution = variables["resolution"]
                    if (
                        not isinstance(resolution, (list, tuple))
                        or len(resolution) != 2
                    ):
                        raise BlenderMCPError(
                            ErrorCode.INVALID_REQUEST,
                            "resolution must contain width and height",
                        )
                    configure["resolution_x"] = resolution[0]
                    configure["resolution_y"] = resolution[1]
                if len(configure) > 1:
                    revision = compute_scene_revision(
                        temporary,
                        logical_path=destination,
                    )
                    configured = output.parent / (
                        f".{output.stem}.{resolved_request_id}.configured.blend"
                    )
                    configured.unlink(missing_ok=True)
                    transaction = SceneTransaction.model_validate(
                        {
                            "transaction_id": (
                                f"txn_template_{resolved_request_id[4:]}"
                            ),
                            "expected_scene_revision": revision.revision,
                            "operations": [configure],
                        }
                    )
                    configured_result = self.launcher.invoke(
                        AdapterOperation.APPLY_SCENE_TRANSACTION,
                        {
                            "scene_path": str(temporary),
                            "output_path": str(configured),
                            "transaction_id": transaction.transaction_id,
                            "expected_source_sha256": revision.content_sha256,
                            "operations": [
                                operation.model_dump(
                                    mode="json",
                                    exclude_none=True,
                                )
                                for operation in transaction.operations
                            ],
                        },
                        request_id=resolved_request_id,
                    )
                    if not configured_result.ok or not configured.is_file():
                        raise BlenderMCPError(
                            ErrorCode.TRANSACTION_ROLLED_BACK,
                            "Template variables could not be applied",
                            details={
                                "adapter_error": (
                                    configured_result.error.model_dump(
                                        mode="json"
                                    )
                                    if configured_result.error is not None
                                    else None
                                )
                            },
                        )
                    os.replace(configured, temporary)
                validation = self.launcher.invoke(
                    AdapterOperation.VALIDATE_SCENE,
                    {"scene_path": str(temporary)},
                    request_id=resolved_request_id,
                )
                if not validation.ok:
                    assert validation.error is not None
                    raise BlenderMCPError(
                        ErrorCode.SCENE_VALIDATION_BLOCKED,
                        validation.error.message,
                        details=validation.error.details,
                    )
                if (validation.data or {}).get("ready_for_render") is False:
                    raise BlenderMCPError(
                        ErrorCode.SCENE_VALIDATION_BLOCKED,
                        "Template scene failed render-readiness validation",
                        details={
                            "findings": (validation.data or {}).get(
                                "findings",
                                [],
                            )
                        },
                    )
                os.replace(temporary, output)
            finally:
                temporary.unlink(missing_ok=True)
                if "configured" in locals():
                    configured.unlink(missing_ok=True)
            return Artifact(
                path=output.relative_to(self.config.project_root).as_posix(),
                media_type="application/x-blender",
                size_bytes=output.stat().st_size,
                sha256=self._sha256(output),
            )

        return self._respond(request_id, action)

    def apply_scene_transaction(
        self,
        scene_path: str,
        *,
        expected_revision: str,
        transaction_id: str,
        operations: list[dict[str, Any]],
        create_checkpoint: bool = True,
        save: bool = True,
        request_id: str | None = None,
    ) -> ServiceResponse[dict[str, Any]]:
        def action(resolved_request_id: str) -> dict[str, Any]:
            if not save:
                raise BlenderMCPError(
                    ErrorCode.INVALID_REQUEST,
                    "V1 transactions must be saved atomically",
                )
            transaction = SceneTransaction.model_validate(
                {
                    "transaction_id": transaction_id,
                    "expected_scene_revision": expected_revision,
                    "operations": operations,
                }
            )
            scene = self.paths.resolve_input(
                scene_path,
                allowed_suffixes=frozenset({".blend"}),
            )

            def execute() -> dict[str, Any]:
                current = compute_scene_revision(
                    scene,
                    logical_path=Path(scene_path).as_posix(),
                )
                if current.revision != transaction.expected_scene_revision:
                    raise BlenderMCPError(
                        ErrorCode.SCENE_REVISION_CONFLICT,
                        "The scene changed after the transaction was prepared",
                        details={
                            "expected_revision": transaction.expected_scene_revision,
                            "actual_revision": current.revision,
                        },
                    )

                checkpoint_id: str | None = None
                if create_checkpoint or self.config.create_checkpoints:
                    checkpoint_id = (
                        f"{transaction.transaction_id}-{current.revision[:16]}"
                    )
                    checkpoint = (
                        self.config.checkpoints_dir
                        / f"{checkpoint_id}.blend"
                    )
                    self.config.checkpoints_dir.mkdir(
                        mode=0o700,
                        parents=True,
                        exist_ok=True,
                    )
                    if not checkpoint.exists():
                        self._atomic_copy(scene, checkpoint)

                temporary = scene.parent / (
                    f".{scene.stem}.{resolved_request_id}.transaction.blend"
                )
                temporary.unlink(missing_ok=True)
                try:
                    result = self.launcher.invoke(
                        AdapterOperation.APPLY_SCENE_TRANSACTION,
                        {
                            "scene_path": str(scene),
                            "output_path": str(temporary),
                            "transaction_id": transaction.transaction_id,
                            "expected_source_sha256": current.content_sha256,
                            "operations": [
                                operation.model_dump(
                                    mode="json",
                                    exclude_none=True,
                                )
                                for operation in transaction.operations
                            ],
                        },
                        request_id=resolved_request_id,
                    )
                    if not result.ok:
                        assert result.error is not None
                        raise BlenderMCPError(
                            ErrorCode.TRANSACTION_ROLLED_BACK,
                            result.error.message,
                            details={
                                "adapter_code": result.error.code,
                                **result.error.details,
                            },
                        )
                    if not temporary.is_file():
                        raise BlenderMCPError(
                            ErrorCode.TRANSACTION_ROLLED_BACK,
                            "Blender did not produce a transaction output",
                        )
                    os.replace(temporary, scene)
                finally:
                    temporary.unlink(missing_ok=True)

                updated = compute_scene_revision(
                    scene,
                    logical_path=Path(scene_path).as_posix(),
                )
                return {
                    "transaction_id": transaction.transaction_id,
                    "scene_path": Path(scene_path).as_posix(),
                    "previous_revision": current.revision,
                    "scene_revision": updated.revision,
                    "scene_sha256": updated.content_sha256,
                    "checkpoint_id": checkpoint_id,
                    "applied_operations": len(transaction.operations),
                }

            try:
                execution = self.transaction_idempotency.execute_once(
                    transaction,
                    execute,
                )
            except TransactionIdConflict as exc:
                raise BlenderMCPError(
                    ErrorCode.JOB_STATE_CONFLICT,
                    str(exc),
                ) from exc
            return {
                **execution.value,
                "replayed": execution.replayed,
            }

        return self._respond(request_id, action)

    def save_scene_as(
        self,
        scene_path: str,
        destination: str,
        *,
        allow_overwrite: bool = False,
        request_id: str | None = None,
    ) -> ServiceResponse[Artifact]:
        def action(_: str) -> Artifact:
            source = self.paths.resolve_input(
                scene_path,
                allowed_suffixes=frozenset({".blend"}),
            )
            output = self.paths.resolve_output(
                destination,
                allow_overwrite=allow_overwrite,
                allowed_suffixes=frozenset({".blend"}),
            )
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._atomic_copy(source, output)
            return Artifact(
                path=output.relative_to(self.config.project_root).as_posix(),
                media_type="application/x-blender",
                size_bytes=output.stat().st_size,
                sha256=self._sha256(output),
            )

        return self._respond(request_id, action)

    def start_render(
        self,
        scene_path: str,
        output: str,
        *,
        preset: str = "youtube-1080p",
        allow_overwrite: bool = False,
        timeout_seconds: int = 14_400,
        idempotency_key: str = "",
        request_id: str | None = None,
    ) -> ServiceResponse[dict[str, Any]]:
        def action(resolved_request_id: str) -> dict[str, Any]:
            if preset not in {"youtube-1080p", "draft"}:
                raise BlenderMCPError(
                    ErrorCode.INVALID_REQUEST,
                    "Render preset is not approved",
                    details={"preset": preset},
                )
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or not 1 <= timeout_seconds <= self.config.max_render_timeout_seconds
            ):
                raise BlenderMCPError(
                    ErrorCode.RENDER_BUDGET_EXCEEDED,
                    "Render timeout exceeds the configured budget",
                    details={
                        "timeout_seconds": timeout_seconds,
                        "max_timeout_seconds": (
                            self.config.max_render_timeout_seconds
                        ),
                    },
                )
            if len(idempotency_key) > 200:
                raise BlenderMCPError(
                    ErrorCode.INVALID_REQUEST,
                    "idempotency_key is too long",
                )
            scene = self.paths.resolve_input(
                scene_path,
                allowed_suffixes=frozenset({".blend"}),
            )
            output_path = self.paths.resolve_output(
                output,
                allow_overwrite=allow_overwrite,
                allowed_suffixes=frozenset({".mp4"}),
            )
            output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            free_bytes = shutil.disk_usage(self.config.project_root).free
            required_bytes = int(self.config.min_free_disk_gb * 1024**3)
            if free_bytes < required_bytes:
                raise BlenderMCPError(
                    ErrorCode.DISK_SPACE_INSUFFICIENT,
                    "Project disk has less free space than the configured margin",
                    details={
                        "free_bytes": free_bytes,
                        "required_bytes": required_bytes,
                    },
                )
            validation = self.launcher.invoke(
                AdapterOperation.VALIDATE_SCENE,
                {"scene_path": str(scene)},
                request_id=resolved_request_id,
            )
            if not validation.ok or (
                validation.data or {}
            ).get("ready_for_render") is False:
                raise BlenderMCPError(
                    ErrorCode.SCENE_VALIDATION_BLOCKED,
                    "Scene is not ready for final rendering",
                    details={
                        "findings": (validation.data or {}).get("findings", []),
                        "adapter_error": (
                            validation.error.model_dump(mode="json")
                            if validation.error is not None
                            else None
                        ),
                    },
                )
            inspection = self.launcher.invoke(
                AdapterOperation.INSPECT_SCENE,
                {"scene_path": str(scene)},
                request_id=resolved_request_id,
            )
            if not inspection.ok:
                raise BlenderMCPError(
                    ErrorCode.SCENE_VALIDATION_BLOCKED,
                    "Scene metadata could not be inspected before rendering",
                )
            metadata = inspection.data or {}
            frame = metadata.get("frame", {})
            render = metadata.get("render", {})
            if isinstance(frame, dict):
                frame_start = frame.get("start")
                frame_end = frame.get("end")
                fps = frame.get("fps")
                if (
                    isinstance(frame_start, int)
                    and isinstance(frame_end, int)
                    and frame_end - frame_start + 1
                    > self.config.max_render_frames
                ):
                    raise BlenderMCPError(
                        ErrorCode.RENDER_BUDGET_EXCEEDED,
                        "Scene frame count exceeds the configured render budget",
                    )
                if (
                    isinstance(fps, int)
                    and fps > self.config.max_render_fps
                ):
                    raise BlenderMCPError(
                        ErrorCode.RENDER_BUDGET_EXCEEDED,
                        "Scene FPS exceeds the configured render budget",
                    )
            if isinstance(render, dict):
                width = render.get("resolution_x")
                height = render.get("resolution_y")
                if (
                    isinstance(width, int)
                    and width > self.config.max_render_width
                ) or (
                    isinstance(height, int)
                    and height > self.config.max_render_height
                ):
                    raise BlenderMCPError(
                        ErrorCode.RENDER_BUDGET_EXCEEDED,
                        "Scene resolution exceeds the configured render budget",
                    )
            try:
                record = self._render_jobs().start(
                    scene,
                    output_path,
                    timeout_seconds=timeout_seconds,
                    idempotency_key=idempotency_key,
                    allow_overwrite=allow_overwrite,
                )
            except Exception as exc:
                from blender_mcp.rendering import RenderJobConflict

                if isinstance(exc, RenderJobConflict):
                    raise BlenderMCPError(
                        ErrorCode.JOB_STATE_CONFLICT,
                        str(exc),
                    ) from exc
                raise
            return self._job_result(record, events=[])

        return self._respond(request_id, action)

    def get_job(
        self,
        job_id: str,
        *,
        request_id: str | None = None,
    ) -> ServiceResponse[dict[str, Any]]:
        def action(_: str) -> dict[str, Any]:
            record = self._render_jobs().get(job_id)
            if record is None:
                raise BlenderMCPError(
                    ErrorCode.JOB_NOT_FOUND,
                    "Render job was not found",
                    details={"job_id": job_id},
                )
            return self._job_result(
                record,
                events=self._render_jobs().events(job_id),
            )

        return self._respond(request_id, action)

    def cancel_job(
        self,
        job_id: str,
        *,
        request_id: str | None = None,
    ) -> ServiceResponse[dict[str, Any]]:
        def action(_: str) -> dict[str, Any]:
            record = self._render_jobs().cancel(job_id)
            if record is None:
                raise BlenderMCPError(
                    ErrorCode.JOB_NOT_FOUND,
                    "Render job was not found",
                    details={"job_id": job_id},
                )
            return self._job_result(
                record,
                events=self._render_jobs().events(job_id),
            )

        return self._respond(request_id, action)

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        scene_path: str,
        *,
        allow_overwrite: bool = False,
        request_id: str | None = None,
    ) -> ServiceResponse[Artifact]:
        def action(_: str) -> Artifact:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", checkpoint_id):
                raise BlenderMCPError(
                    ErrorCode.INVALID_REQUEST,
                    "checkpoint_id has an invalid format",
                )
            checkpoint_candidate = (
                self.config.checkpoints_dir / f"{checkpoint_id}.blend"
            )
            checkpoint = checkpoint_candidate.resolve(strict=False)
            checkpoint_root = self.config.checkpoints_dir.resolve(
                strict=False
            )
            if (
                not checkpoint.is_relative_to(checkpoint_root)
                or not checkpoint.is_file()
                or checkpoint_candidate.is_symlink()
            ):
                raise BlenderMCPError(
                    ErrorCode.PATH_NOT_FOUND,
                    "Checkpoint was not found",
                    details={"checkpoint_id": checkpoint_id},
                )
            output = self.paths.resolve_output(
                scene_path,
                allow_overwrite=allow_overwrite,
                allowed_suffixes=frozenset({".blend"}),
            )
            output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._atomic_copy(checkpoint, output)
            return Artifact(
                path=output.relative_to(self.config.project_root).as_posix(),
                media_type="application/x-blender",
                size_bytes=output.stat().st_size,
                sha256=self._sha256(output),
            )

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

    def _render_jobs(self) -> "RenderJobManager":
        if self._render_manager is None:
            from blender_mcp.rendering import RenderJobManager

            self._render_manager = RenderJobManager(
                self.config,
                launcher=self.launcher,
            )
        return self._render_manager

    @staticmethod
    def _job_result(
        record: Any,
        *,
        events: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "job_id": record.job_id,
            "kind": record.kind,
            "state": record.state.value,
            "metadata": record.metadata,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "events": events,
        }

    @staticmethod
    def _atomic_copy(source: Path, destination: Path) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        try:
            with source.open("rb") as source_file, os.fdopen(
                descriptor, "wb"
            ) as destination_file:
                shutil.copyfileobj(source_file, destination_file)
                destination_file.flush()
                os.fsync(destination_file.fileno())
            os.replace(temporary_name, destination)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
            raise

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
