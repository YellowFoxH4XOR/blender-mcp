"""Thin MCP STDIO surface over the importable application service."""

from __future__ import annotations

from typing import Any

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.services import BlenderService


def create_server(config: BlenderMCPConfig) -> Any:
    # MCP SDK v2 renamed FastMCP to MCPServer. Importing lazily keeps the core
    # testable without starting a server or touching stdio.
    from mcp.server import MCPServer

    service = BlenderService(config)
    server = MCPServer(
        "dependable-blender-mcp",
        instructions=(
            "Inspect before editing. Validate and render a preview before starting "
            "a final render. Never overwrite a scene or output unless the tool "
            "request explicitly allows it. Prefer headless execution for batch and "
            "final-render work. All paths are relative to the configured project "
            "root; no arbitrary Python or shell is accepted."
        ),
    )

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def get_blender_status() -> dict[str, Any]:
        """Check whether the configured Blender and bundled adapter are available."""
        return service.get_status().model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def inspect_scene(scene_path: str) -> dict[str, Any]:
        """Inspect a project-relative .blend file without modifying it."""
        return service.inspect_scene(scene_path).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def list_project_assets(kind: str | None = None) -> dict[str, Any]:
        """List approved project templates, models, materials, actions, audio, and fonts."""
        return service.list_project_assets(kind=kind).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def validate_scene(scene_path: str) -> dict[str, Any]:
        """Check whether a scene is technically safe and ready to render."""
        return service.validate_scene(scene_path).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    def create_scene_from_template(
        template_id: str,
        destination: str,
        variables: dict[str, Any] | None = None,
        allow_overwrite: bool = False,
    ) -> dict[str, Any]:
        """Create a new scene from an approved project template."""
        return service.create_scene_from_template(
            template_id,
            destination,
            variables=variables or {},
            allow_overwrite=allow_overwrite,
        ).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def apply_scene_transaction(
        scene_path: str,
        expected_revision: str,
        transaction_id: str,
        operations: list[dict[str, Any]],
        create_checkpoint: bool = True,
        save: bool = True,
    ) -> dict[str, Any]:
        """Atomically apply allowlisted Blender operations with revision checking."""
        return service.apply_scene_transaction(
            scene_path,
            expected_revision=expected_revision,
            transaction_id=transaction_id,
            operations=operations,
            create_checkpoint=create_checkpoint,
            save=save,
        ).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    def render_preview(
        scene_path: str,
        output_path: str,
        max_width: int = 960,
        max_height: int = 540,
        allow_overwrite: bool = False,
    ) -> dict[str, Any]:
        """Render a PNG preview to a project-relative output path."""
        return service.render_preview(
            scene_path,
            output_path,
            max_width=max_width,
            max_height=max_height,
            allow_overwrite=allow_overwrite,
        ).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def start_render(
        scene_path: str,
        output: str,
        preset: str = "youtube-1080p",
        allow_overwrite: bool = False,
        timeout_seconds: int = 14_400,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Queue an asynchronous final render and return its durable job ID."""
        return service.start_render(
            scene_path,
            output,
            preset=preset,
            allow_overwrite=allow_overwrite,
            timeout_seconds=timeout_seconds,
            idempotency_key=idempotency_key,
        ).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def get_job(job_id: str) -> dict[str, Any]:
        """Return durable render-job state, progress, events, and artifacts."""
        return service.get_job(job_id).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def cancel_job(job_id: str) -> dict[str, Any]:
        """Request cancellation of a queued or running render job."""
        return service.cancel_job(job_id).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    def save_scene_as(
        scene_path: str,
        destination: str,
        allow_overwrite: bool = False,
    ) -> dict[str, Any]:
        """Atomically save a project scene to a new in-root path."""
        return service.save_scene_as(
            scene_path,
            destination,
            allow_overwrite=allow_overwrite,
        ).model_dump(mode="json")

    @server.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    def restore_checkpoint(
        checkpoint_id: str,
        scene_path: str,
        allow_overwrite: bool = False,
    ) -> dict[str, Any]:
        """Restore a checkpoint after explicit destructive approval."""
        return service.restore_checkpoint(
            checkpoint_id,
            scene_path,
            allow_overwrite=allow_overwrite,
        ).model_dump(mode="json")

    return server


def main() -> None:
    server = create_server(BlenderMCPConfig.from_env())
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
