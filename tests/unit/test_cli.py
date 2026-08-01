import os
from pathlib import Path

import pytest

from blender_mcp.cli import load_http_token, main
from blender_mcp.doctor import DoctorReport


def test_init_creates_safe_project_layout_and_configuration(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "show"
    blender = tmp_path / "Blender"

    exit_code = main(
        [
            "init",
            str(project_root),
            "--blender-executable",
            str(blender),
        ]
    )

    assert exit_code == 0
    assert (project_root / "blender-mcp.toml").is_file()
    assert (project_root / "assets" / "catalog.toml").is_file()
    for relative in (
        "templates",
        "scenes",
        "renders",
        "previews",
        ".blender-mcp/jobs",
        ".blender-mcp/checkpoints",
        ".blender-mcp/logs",
        ".blender-mcp/quarantine",
        ".blender-mcp/cache",
    ):
        assert (project_root / relative).is_dir()


def test_doctor_command_returns_nonzero_for_missing_blender(
    tmp_path: Path,
    capsys,
) -> None:
    adapter = tmp_path / "adapter.py"
    adapter.touch()
    config_path = tmp_path / "blender-mcp.toml"
    config_path.write_text(
        "\n".join(
            [
                'schema_version = "1"',
                f'project_root = "{tmp_path}"',
                f'blender_executable = "{tmp_path / "missing-blender"}"',
                f'adapter_script = "{adapter}"',
            ]
        )
    )

    exit_code = main(["doctor", "--config", str(config_path)])

    assert exit_code == 1
    assert "[FAIL] blender_executable:" in capsys.readouterr().out


def test_streamable_http_rejects_non_loopback_host(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("BLENDER_MCP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "BLENDER_MCP_BLENDER_EXECUTABLE",
        str(tmp_path / "Blender"),
    )

    exit_code = main(
        [
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "0.0.0.0",
            "--port",
            "9876",
        ]
    )

    assert exit_code == 2
    assert "loopback" in capsys.readouterr().err


def test_serve_keeps_stdio_as_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    class Server:
        def run(self, transport: str, **kwargs: object) -> None:
            calls.append((transport, kwargs))

    monkeypatch.setenv("BLENDER_MCP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "BLENDER_MCP_BLENDER_EXECUTABLE",
        str(tmp_path / "Blender"),
    )
    monkeypatch.setattr("blender_mcp.server.create_server", lambda config: Server())

    exit_code = main(["serve"])

    assert exit_code == 0
    assert calls == [("stdio", {})]


def test_streamable_http_requires_bearer_token(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("BLENDER_MCP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "BLENDER_MCP_BLENDER_EXECUTABLE",
        str(tmp_path / "Blender"),
    )
    monkeypatch.delenv("BLENDER_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.delenv("BLENDER_MCP_HTTP_TOKEN_FILE", raising=False)

    exit_code = main(["serve", "--transport", "streamable-http"])

    assert exit_code == 2
    assert "requires BLENDER_MCP_HTTP_TOKEN" in capsys.readouterr().err


def test_streamable_http_runs_authenticated_loopback_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    calls: list[dict[str, object]] = []
    server = object()
    token = "cli-test-token-" + ("a" * 32)
    monkeypatch.setenv("BLENDER_MCP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "BLENDER_MCP_BLENDER_EXECUTABLE",
        str(tmp_path / "Blender"),
    )
    monkeypatch.setenv("BLENDER_MCP_HTTP_TOKEN", token)
    monkeypatch.setattr("blender_mcp.server.create_server", lambda config: server)
    monkeypatch.setattr(
        "blender_mcp.runtime.run_authenticated_http_server",
        lambda actual_server, **kwargs: calls.append(
            {"server": actual_server, **kwargs}
        ),
    )

    exit_code = main(
        [
            "serve",
            "--transport",
            "streamable-http",
            "--host",
            "localhost",
            "--port",
            "9876",
        ]
    )

    assert exit_code == 0
    assert calls == [
        {
            "server": server,
            "host": "localhost",
            "port": 9876,
            "token": token,
            "max_request_body_size": 1_048_576,
        }
    ]


def test_streamable_http_reads_token_file_without_exposing_it_as_argument(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token = "file-test-token-" + ("b" * 32)
    token_file = tmp_path / "worker-token"
    token_file.write_text(f"{token}\n")
    token_file.chmod(0o600)
    captured: dict[str, object] = {}
    monkeypatch.setenv("BLENDER_MCP_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv(
        "BLENDER_MCP_BLENDER_EXECUTABLE",
        str(tmp_path / "Blender"),
    )
    monkeypatch.delenv("BLENDER_MCP_HTTP_TOKEN", raising=False)
    monkeypatch.setattr("blender_mcp.server.create_server", lambda config: object())
    monkeypatch.setattr(
        "blender_mcp.runtime.run_authenticated_http_server",
        lambda server, **kwargs: captured.update(kwargs),
    )

    exit_code = main(
        [
            "serve",
            "--transport",
            "streamable-http",
            "--token-file",
            str(token_file),
        ]
    )

    assert exit_code == 0
    assert captured["token"] == token


def test_http_token_file_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "real-token"
    target.write_text("a" * 48, encoding="utf-8")
    target.chmod(0o600)
    link = tmp_path / "worker-token"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        load_http_token(link)


def test_http_token_file_rejects_group_or_other_permissions(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "worker-token"
    token_file.write_text("a" * 48, encoding="utf-8")
    token_file.chmod(0o640)

    with pytest.raises(ValueError, match="owner-only"):
        load_http_token(token_file)


def test_http_token_file_rejects_wrong_owner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    token_file = tmp_path / "worker-token"
    token_file.write_text("a" * 48, encoding="utf-8")
    token_file.chmod(0o600)
    monkeypatch.setattr(os, "getuid", lambda: token_file.stat().st_uid + 1)

    with pytest.raises(ValueError, match="current user"):
        load_http_token(token_file)


def test_http_token_file_rejects_non_regular_file(tmp_path: Path) -> None:
    token_directory = tmp_path / "worker-token"
    token_directory.mkdir()

    with pytest.raises(ValueError, match="regular file"):
        load_http_token(token_directory)


def test_worker_install_command_builds_and_starts_launch_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    python = tmp_path / "worker-python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    config_path = tmp_path / "project/blender-mcp.toml"
    config_path.parent.mkdir()
    config_path.write_text(
        "\n".join(
            [
                f'project_root = "{config_path.parent}"',
                f'blender_executable = "{tmp_path / "Blender"}"',
            ]
        )
    )
    token_file = tmp_path / "secrets/token"
    plist = tmp_path / "Library/LaunchAgents/worker.plist"
    installed: list[tuple[object, Path]] = []
    monkeypatch.setattr(
        "blender_mcp.cli.run_doctor",
        lambda config: DoctorReport(tuple()),
    )
    monkeypatch.setattr(
        "blender_mcp.worker.install_and_start_worker",
        lambda spec, destination: installed.append((spec, destination)) or destination,
    )

    exit_code = main(
        [
            "worker",
            "install",
            "--config",
            str(config_path),
            "--repository-root",
            str(repository),
            "--python-executable",
            str(python),
            "--token-file",
            str(token_file),
            "--plist",
            str(plist),
            "--port",
            "9876",
        ]
    )

    assert exit_code == 0
    spec, destination = installed[0]
    assert destination == plist
    assert spec.repository_root == repository
    assert spec.python_executable == python
    assert spec.config_path == config_path
    assert spec.token_path == token_file
    assert spec.port == 9876
