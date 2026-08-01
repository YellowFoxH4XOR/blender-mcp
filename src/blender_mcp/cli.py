"""Command-line entry point for setup, diagnostics, and MCP serving."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import shutil
import socket
import sys
from pathlib import Path
from typing import Sequence

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.doctor import run_doctor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blender-mcp")
    parser.add_argument(
        "--config",
        type=Path,
        help="path to blender-mcp.toml (defaults to cwd or environment)",
    )
    subparsers = parser.add_subparsers(dest="command")

    serve = subparsers.add_parser("serve", help="run the STDIO MCP server")
    serve.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    serve.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default="stdio",
    )
    serve.add_argument("--host")
    serve.add_argument("--port", type=_port)
    serve.add_argument(
        "--token-file",
        type=Path,
        help="file containing the HTTP bearer token (never pass the token itself)",
    )

    init = subparsers.add_parser("init", help="initialize a Blender MCP project")
    init.add_argument("project_root", type=Path, nargs="?", default=Path.cwd())
    init.add_argument(
        "--blender-executable",
        type=Path,
        default=Path("/Applications/Blender.app/Contents/MacOS/Blender"),
    )

    doctor = subparsers.add_parser("doctor", help="check required capabilities")
    doctor.add_argument("--config", type=Path, default=argparse.SUPPRESS)
    doctor.add_argument("--json", action="store_true", dest="json_output")

    worker = subparsers.add_parser("worker", help="manage the macOS launchd worker")
    worker_commands = worker.add_subparsers(dest="worker_command", required=True)
    install = worker_commands.add_parser(
        "install",
        help="install and start the authenticated loopback worker",
    )
    install.add_argument("--config", type=Path, required=True)
    install.add_argument("--repository-root", type=Path, default=Path.cwd())
    install.add_argument("--python-executable", type=Path, default=Path(sys.executable))
    install.add_argument(
        "--token-file",
        type=Path,
        default=Path.home() / ".config/blender-mcp/worker-token",
    )
    install.add_argument(
        "--plist",
        type=Path,
        default=(
            Path.home()
            / "Library/LaunchAgents/com.yellowfox.blender-mcp.plist"
        ),
    )
    install.add_argument("--port", type=_port)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            config_path = initialize_project(
                args.project_root,
                blender_executable=args.blender_executable,
            )
            print(f"Initialized Blender MCP project: {config_path.parent}")
            print(f"Configuration: {config_path}")
            return 0

        config = BlenderMCPConfig.load(args.config)
        if args.command == "worker":
            report = run_doctor(config)
            if not report.ok:
                for check in report.checks:
                    if check.required and not check.ok:
                        print(
                            f"[FAIL] {check.name}: {check.message}",
                            file=sys.stderr,
                        )
                return 1
            from blender_mcp.worker import (
                WorkerSpec,
                install_and_start_worker,
            )

            repository = args.repository_root.expanduser().resolve(strict=False)
            node = config.node_executable or _which_path("node")
            npm = config.npm_executable or _which_path("npm")
            remotion = config.remotion_project or repository / "remotion"
            spec = WorkerSpec(
                repository_root=repository,
                python_executable=args.python_executable.expanduser().resolve(
                    strict=False
                ),
                config_path=args.config.expanduser().resolve(strict=True),
                token_path=Path(os.path.abspath(args.token_file.expanduser())),
                log_path=config.logs_dir / "worker.log",
                remotion_project=remotion.expanduser().resolve(strict=False),
                node_executable=node,
                npm_executable=npm,
                port=args.port or config.http_port,
            )
            installed = install_and_start_worker(
                spec,
                args.plist.expanduser().resolve(strict=False),
            )
            print(f"Installed and started worker: {installed}")
            print(f"Token file: {spec.token_path}")
            print(f"MCP URL: http://127.0.0.1:{spec.port}/mcp")
            return 0
        if args.command == "doctor":
            report = run_doctor(config)
            if args.json_output:
                print(json.dumps(report.as_dict(), indent=2))
            else:
                for check in report.checks:
                    marker = "ok" if check.ok else "FAIL"
                    print(f"[{marker}] {check.name}: {check.message}")
            return report.exit_code

        # No subcommand intentionally preserves the original `blender-mcp`
        # behavior while `serve` makes that behavior explicit.
        from blender_mcp.server import create_server

        transport = getattr(args, "transport", "stdio")
        if transport == "streamable-http":
            host = validate_loopback_host(args.host or config.http_host)
            port = args.port or config.http_port
            token = load_http_token(args.token_file or config.http_token_file)
            from blender_mcp.runtime import run_authenticated_http_server

            server = create_server(config)
            run_authenticated_http_server(
                server,
                host=host,
                port=port,
                token=token,
                max_request_body_size=config.max_request_bytes,
            )
        else:
            create_server(config).run(transport="stdio")
        return 0
    except (OSError, ValueError) as exc:
        print(f"blender-mcp: {exc}", file=sys.stderr)
        return 2


def validate_loopback_host(host: str) -> str:
    """Resolve a bind host and reject it unless every address is loopback."""

    try:
        addresses = socket.getaddrinfo(
            host,
            None,
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise ValueError(f"HTTP host could not be resolved: {host}") from exc
    resolved = {
        ipaddress.ip_address(address[4][0].split("%", maxsplit=1)[0])
        for address in addresses
    }
    if not resolved or not all(address.is_loopback for address in resolved):
        raise ValueError("Streamable HTTP host must resolve only to loopback")
    return host


def load_http_token(token_file: Path | None) -> str:
    direct = os.environ.get("BLENDER_MCP_HTTP_TOKEN")
    if direct is not None:
        return direct
    if token_file is None:
        raise ValueError(
            "Streamable HTTP requires BLENDER_MCP_HTTP_TOKEN or --token-file"
        )
    from blender_mcp.worker import read_private_token

    return read_private_token(token_file)


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _which_path(name: str) -> Path | None:
    discovered = shutil.which(name)
    return Path(discovered).resolve(strict=False) if discovered else None


def initialize_project(
    project_root: str | Path,
    *,
    blender_executable: str | Path,
) -> Path:
    """Create the safe default project layout without overwriting user files."""

    root = Path(project_root).expanduser().resolve(strict=False)
    config_path = root / "blender-mcp.toml"
    catalog_path = root / "assets" / "catalog.toml"
    if config_path.exists():
        raise ValueError(f"configuration already exists: {config_path}")
    if catalog_path.exists():
        raise ValueError(f"asset catalog already exists: {catalog_path}")

    directories = (
        "assets/models",
        "assets/materials",
        "assets/actions",
        "templates",
        "scenes",
        "renders",
        "previews",
        ".blender-mcp/jobs",
        ".blender-mcp/checkpoints",
        ".blender-mcp/logs",
        ".blender-mcp/quarantine",
        ".blender-mcp/cache",
    )
    for relative in directories:
        (root / relative).mkdir(parents=True, exist_ok=True)

    quoted_root = json.dumps(str(root))
    quoted_blender = json.dumps(
        str(Path(blender_executable).expanduser().resolve(strict=False))
    )
    config_path.write_text(
        "\n".join(
            [
                'schema_version = "1"',
                f"project_root = {quoted_root}",
                f"blender_executable = {quoted_blender}",
                'execution_mode = "headless"',
                "",
                "[policy]",
                "allow_network = false",
                "allow_overwrite = false",
                "create_checkpoints = true",
                "max_operations_per_transaction = 200",
                "max_request_bytes = 1048576",
                "",
                "[render]",
                "max_width = 3840",
                "max_height = 2160",
                "max_fps = 60",
                "max_frames = 54000",
                "max_timeout_seconds = 21600",
                "min_free_disk_gb = 20",
                "concurrent_jobs = 1",
                "",
                "[live_bridge]",
                "enabled = false",
                'host = "127.0.0.1"',
                "port = 9876",
                "request_timeout_seconds = 30",
                "",
                "[logging]",
                'level = "INFO"',
                "retention_days = 30",
                "",
            ]
        ),
        encoding="utf-8",
    )
    catalog_path.write_text(
        'schema_version = "1"\n',
        encoding="utf-8",
    )
    return config_path


if __name__ == "__main__":
    raise SystemExit(main())
