from __future__ import annotations

import asyncio
from pathlib import Path

from mcp import Client

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.server import create_server


def test_mcp_surface_exposes_only_the_m0_tools(tmp_path: Path) -> None:
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
        "render_preview",
    }
    assert tools["get_blender_status"].annotations.read_only_hint is True
    assert tools["inspect_scene"].annotations.read_only_hint is True
    assert tools["render_preview"].annotations.open_world_hint is False
    assert all("code" not in tool.input_schema.get("properties", {}) for tool in tools.values())
