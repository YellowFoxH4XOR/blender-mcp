from pathlib import Path

from blender_mcp.config import BlenderMCPConfig


def test_from_env_uses_bundled_adapter_not_content_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("BLENDER_MCP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("BLENDER_MCP_BLENDER_EXECUTABLE", str(tmp_path / "Blender"))
    monkeypatch.delenv("BLENDER_MCP_ADAPTER_SCRIPT", raising=False)

    config = BlenderMCPConfig.from_env()

    assert config.project_root == tmp_path
    assert config.adapter_script.name == "bootstrap.py"
    assert config.adapter_script.parent.name == "blender_adapter"
    assert config.adapter_script != tmp_path / "blender_adapter" / "bootstrap.py"
