from __future__ import annotations

import asyncio
from pathlib import Path

from mcp import Client

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.server import create_server


def test_v1_mcp_surface_exposes_the_dependable_workflow(tmp_path: Path) -> None:
    executable = tmp_path / "Blender"
    executable.write_bytes(b"placeholder")
    adapter = tmp_path / "blender_adapter" / "bootstrap.py"
    adapter.parent.mkdir()
    adapter.write_text("# placeholder", encoding="utf-8")
    server = create_server(
        BlenderMCPConfig(
            project_root=tmp_path,
            blender_executable=executable,
            adapter_script=adapter,
        )
    )

    async def inspect_tools() -> dict[str, object]:
        async with Client(server) as client:
            result = await client.list_tools()
            return {tool.name: tool for tool in result.tools}

    tools = asyncio.run(inspect_tools())

    assert set(tools) == {
        "get_blender_status",
        "inspect_scene",
        "list_project_assets",
        "validate_scene",
        "create_scene_from_template",
        "apply_scene_transaction",
        "render_preview",
        "start_render",
        "get_job",
        "cancel_job",
        "save_scene_as",
        "restore_checkpoint",
    }
    for name in {
        "get_blender_status",
        "inspect_scene",
        "list_project_assets",
        "validate_scene",
        "get_job",
    }:
        assert tools[name].annotations.read_only_hint is True
    assert tools["restore_checkpoint"].annotations.destructive_hint is True
    assert all(
        tool.annotations.open_world_hint is False for tool in tools.values()
    )
