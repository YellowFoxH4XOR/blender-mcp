from __future__ import annotations

import os
import plistlib
import stat
import sys
from pathlib import Path

import pytest

from blender_mcp.worker import (
    WorkerSpec,
    build_launch_agent_payload,
    ensure_worker_token,
    install_and_start_worker,
    write_launch_agent,
)


def test_worker_token_is_created_once_with_private_permissions(
    tmp_path: Path,
) -> None:
    token_path = tmp_path / "secrets" / "worker-token"

    first = ensure_worker_token(token_path)
    second = ensure_worker_token(token_path)

    assert first == second
    assert len(first) >= 48
    assert token_path.read_text(encoding="utf-8").strip() == first
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_worker_token_rejects_symlink_instead_of_following_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-token"
    target.write_text("x" * 64, encoding="utf-8")
    token_path = tmp_path / "worker-token"
    token_path.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        ensure_worker_token(token_path)

    assert target.read_text(encoding="utf-8") == "x" * 64


def test_launch_agent_runs_authenticated_loopback_worker_without_a_shell(
    tmp_path: Path,
) -> None:
    spec = WorkerSpec(
        repository_root=tmp_path / "repo",
        python_executable=tmp_path / "repo/.venv/bin/python",
        config_path=tmp_path / "project/blender-mcp.toml",
        token_path=tmp_path / "secrets/worker-token",
        log_path=tmp_path / "project/.blender-mcp/logs/worker.log",
        remotion_project=tmp_path / "runtime/remotion",
        node_executable=tmp_path / "runtime/node",
        npm_executable=tmp_path / "runtime/npm-cli.js",
        port=9876,
    )

    payload = build_launch_agent_payload(spec)

    assert payload["Label"] == "com.yellowfox.blender-mcp"
    assert payload["ProgramArguments"] == [
        str(spec.python_executable),
        "-m",
        "blender_mcp",
        "serve",
        "--config",
        str(spec.config_path),
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        "9876",
        "--token-file",
        str(spec.token_path),
    ]
    assert payload["WorkingDirectory"] == str(spec.repository_root)
    assert "PYTHONDONTWRITEBYTECODE" not in payload["EnvironmentVariables"]
    assert payload["EnvironmentVariables"]["BLENDER_MCP_REMOTION_PROJECT"] == str(
        spec.remotion_project
    )
    assert payload["EnvironmentVariables"]["BLENDER_MCP_NODE_EXECUTABLE"] == str(
        spec.node_executable
    )
    assert payload["EnvironmentVariables"]["BLENDER_MCP_NPM_EXECUTABLE"] == str(
        spec.npm_executable
    )
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True


def test_launch_agent_is_written_atomically_with_private_permissions(
    tmp_path: Path,
) -> None:
    spec = WorkerSpec(
        repository_root=tmp_path / "repo",
        python_executable=tmp_path / "repo/.venv/bin/python",
        config_path=tmp_path / "project/blender-mcp.toml",
        token_path=tmp_path / "secrets/worker-token",
        log_path=tmp_path / "project/.blender-mcp/logs/worker.log",
    )
    destination = tmp_path / "Library/LaunchAgents/worker.plist"

    written = write_launch_agent(spec, destination)

    assert written == destination
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    with destination.open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"][0] == str(
            spec.python_executable
        )


def test_install_and_start_worker_bootstraps_launch_agent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    log = tmp_path / "launchctl-calls"
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{log}'\n"
        "exit 0\n"
    )
    launchctl.chmod(0o700)
    repository = tmp_path / "repo"
    repository.mkdir()
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    config = tmp_path / "project/blender-mcp.toml"
    config.parent.mkdir()
    config.touch()
    spec = WorkerSpec(
        repository_root=repository,
        python_executable=python,
        config_path=config,
        token_path=tmp_path / "secrets/worker-token",
        log_path=tmp_path / "project/.blender-mcp/logs/worker.log",
    )
    plist = tmp_path / "Library/LaunchAgents/worker.plist"
    monkeypatch.setattr(sys, "platform", "darwin")

    installed = install_and_start_worker(
        spec,
        plist,
        launchctl_executable=launchctl,
    )

    assert installed == plist
    calls = log.read_text().splitlines()
    assert calls[0] == f"bootout gui/{os.getuid()}/{spec.label}"
    assert calls[1] == f"bootstrap gui/{os.getuid()} {plist}"
    assert calls[2] == f"kickstart -k gui/{os.getuid()}/{spec.label}"
