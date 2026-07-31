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
            "Constrained headless Blender tools. All paths are relative to the "
            "configured project root; no arbitrary Python or shell is accepted."
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

    return server


def main() -> None:
    server = create_server(BlenderMCPConfig.from_env())
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
