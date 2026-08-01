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
    assert config.allow_network is False
    assert config.allow_overwrite is False
    assert config.state_db == tmp_path / ".blender-mcp" / "state.db"
    assert config.checkpoints_dir == tmp_path / ".blender-mcp" / "checkpoints"
    assert config.http_port == 9876


def test_load_reads_toml_and_applies_environment_overrides(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    configured_blender = tmp_path / "Configured Blender"
    overridden_blender = tmp_path / "Override Blender"
    config_path = tmp_path / "blender-mcp.toml"
    config_path.write_text(
        "\n".join(
            [
                'schema_version = "1"',
                f'project_root = "{project_root}"',
                f'blender_executable = "{configured_blender}"',
                'execution_mode = "headless"',
                "",
                "[policy]",
                "allow_network = false",
                "allow_overwrite = true",
                "max_request_bytes = 2048",
                "",
                "[render]",
                "max_timeout_seconds = 45",
            ]
        )
    )

    config = BlenderMCPConfig.load(
        config_path,
        environ={"BLENDER_MCP_BLENDER_EXECUTABLE": str(overridden_blender)},
    )

    assert config.project_root == project_root
    assert config.blender_executable == overridden_blender
    assert config.execution_mode == "headless"
    assert config.allow_overwrite is True
    assert config.max_request_bytes == 2048
    assert config.max_render_timeout_seconds == 45
    assert config.subprocess_timeout_seconds == 120


def test_load_reads_http_bind_and_secret_file_configuration(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "http-token"
    config_path = tmp_path / "blender-mcp.toml"
    config_path.write_text(
        "\n".join(
            [
                f'project_root = "{tmp_path}"',
                f'blender_executable = "{tmp_path / "Blender"}"',
                "",
                "[http]",
                'host = "localhost"',
                "port = 9876",
                f'token_file = "{token_file}"',
            ]
        )
    )

    config = BlenderMCPConfig.load(config_path, environ={})

    assert config.http_host == "localhost"
    assert config.http_port == 9876
    assert config.http_token_file == token_file


def test_load_reads_external_remotion_project(tmp_path: Path) -> None:
    remotion_project = tmp_path / "installed-remotion"
    node = tmp_path / "node"
    npm = tmp_path / "npm-cli.js"
    config_path = tmp_path / "blender-mcp.toml"
    config_path.write_text(
        "\n".join(
            [
                f'project_root = "{tmp_path}"',
                f'blender_executable = "{tmp_path / "Blender"}"',
                "",
                "[remotion]",
                f'project = "{remotion_project}"',
                f'node_executable = "{node}"',
                f'npm_executable = "{npm}"',
            ]
        )
    )

    config = BlenderMCPConfig.load(config_path, environ={})

    assert config.remotion_project == remotion_project
    assert config.node_executable == node
    assert config.npm_executable == npm
