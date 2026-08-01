"""Read-only installation and runtime diagnostics."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.errors import BlenderMCPError
from blender_mcp.headless.launcher import BlenderLauncher


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    message: str
    required: bool = True


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]

    @property
    def ok(self) -> bool:
        return all(check.ok for check in self.checks if check.required)

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [asdict(check) for check in self.checks],
        }


def run_doctor(config: BlenderMCPConfig) -> DoctorReport:
    """Check capabilities required to start the current headless server."""

    checks: list[DoctorCheck] = []
    root_ok = (
        config.project_root.is_dir()
        and os.access(config.project_root, os.R_OK | os.W_OK | os.X_OK)
    )
    checks.append(
        DoctorCheck(
            name="project_root",
            ok=root_ok,
            message=(
                f"readable and writable: {config.project_root}"
                if root_ok
                else f"missing or inaccessible: {config.project_root}"
            ),
        )
    )

    executable_ok = config.blender_executable.is_file() and os.access(
        config.blender_executable, os.X_OK
    )
    checks.append(
        DoctorCheck(
            name="blender_executable",
            ok=executable_ok,
            message=(
                f"executable: {config.blender_executable}"
                if executable_ok
                else f"missing or not executable: {config.blender_executable}"
            ),
        )
    )

    if root_ok and executable_ok:
        try:
            probe = BlenderLauncher(config).probe(timeout_seconds=10)
            output = "\n".join((probe.stdout, probe.stderr)).strip()
            match = re.search(r"\bBlender\s+([0-9]+(?:\.[0-9]+){1,2})\b", output)
            version_ok = probe.returncode == 0 and match is not None
            message = (
                f"detected Blender {match.group(1)}"
                if version_ok and match is not None
                else f"version probe failed with exit code {probe.returncode}"
            )
        except BlenderMCPError as exc:
            version_ok = False
            message = f"version probe failed: {exc.message}"
    else:
        version_ok = False
        message = "version probe skipped because executable or project root failed"
    checks.append(DoctorCheck("blender_version", version_ok, message))

    if root_ok and executable_ok:
        try:
            runtime_probe = BlenderLauncher(config).probe_runtime(
                timeout_seconds=15
            )
            runtime_output = "\n".join(
                (runtime_probe.stdout, runtime_probe.stderr)
            )
            runtime_ok = (
                runtime_probe.returncode == 0
                and "BLENDER_RUNTIME_READY" in runtime_output
            )
            runtime_message = (
                "factory-default background runtime initialized"
                if runtime_ok
                else (
                    "runtime initialization failed with exit code "
                    f"{runtime_probe.returncode}"
                )
            )
        except BlenderMCPError as exc:
            runtime_ok = False
            runtime_message = f"runtime initialization failed: {exc.message}"
    else:
        runtime_ok = False
        runtime_message = (
            "runtime probe skipped because executable or project root failed"
        )
    checks.append(
        DoctorCheck("blender_runtime", runtime_ok, runtime_message)
    )

    adapter_ok = config.adapter_script.is_file() and os.access(
        config.adapter_script, os.R_OK
    )
    checks.append(
        DoctorCheck(
            name="adapter",
            ok=adapter_ok,
            message=(
                f"available: {config.adapter_script}"
                if adapter_ok
                else f"missing or unreadable: {config.adapter_script}"
            ),
        )
    )
    node_path = _configured_executable(config.node_executable, "node")
    node_ok, node_message = _probe_node(node_path, config.project_root)
    checks.append(DoctorCheck("node_runtime", node_ok, node_message))

    npm_path = _configured_executable(config.npm_executable, "npm")
    remotion_project = config.remotion_project or (
        Path(__file__).resolve().parents[2] / "remotion"
    )
    remotion_ok, remotion_message = _check_remotion(
        remotion_project,
        npm_path=npm_path,
    )
    checks.append(
        DoctorCheck(
            "remotion_dependencies",
            remotion_ok,
            remotion_message,
        )
    )
    return DoctorReport(tuple(checks))


def _configured_executable(configured: Path | None, name: str) -> Path | None:
    if configured is not None:
        return configured
    discovered = shutil.which(name)
    return Path(discovered).resolve(strict=False) if discovered else None


def _probe_node(
    executable: Path | None,
    project_root: Path,
) -> tuple[bool, str]:
    if executable is None or not executable.is_file() or not os.access(
        executable, os.X_OK
    ):
        return False, "Node executable was not found; configure [remotion].node_executable"
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            cwd=project_root,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            shell=False,
            env={
                key: os.environ[key]
                for key in ("HOME", "TMPDIR", "LANG", "LC_ALL")
                if key in os.environ
            },
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Node version probe failed: {exc}"
    output = "\n".join((completed.stdout, completed.stderr)).strip()
    match = re.search(r"\bv?(\d+\.\d+\.\d+)\b", output)
    if completed.returncode != 0 or match is None:
        return False, f"Node version probe failed with exit code {completed.returncode}"
    return True, f"detected Node {match.group(1)} at {executable}"


def _check_remotion(
    project: Path,
    *,
    npm_path: Path | None,
) -> tuple[bool, str]:
    try:
        root = project.resolve(strict=True)
    except OSError:
        return False, f"Remotion project was not found: {project}"
    package_path = root / "package.json"
    lock_path = root / "package-lock.json"
    cli_path = root / "node_modules/@remotion/cli/remotion-cli.js"
    if npm_path is None or not npm_path.is_file() or not os.access(npm_path, os.X_OK):
        return (
            False,
            "npm executable was not found; configure [remotion].npm_executable",
        )
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return (
            False,
            f"Pinned Remotion metadata is missing or invalid; run npm ci in {root}",
        )
    package_dependencies = package.get("dependencies")
    lock_dependencies = lock.get("packages", {}).get("", {}).get("dependencies")
    if not isinstance(package_dependencies, dict) or not isinstance(
        lock_dependencies, dict
    ):
        return False, f"Remotion dependency lock is invalid; run npm ci in {root}"
    cli_version = package_dependencies.get("@remotion/cli")
    runtime_version = package_dependencies.get("remotion")
    pinned = (
        isinstance(cli_version, str)
        and re.fullmatch(r"\d+\.\d+\.\d+", cli_version) is not None
        and runtime_version == cli_version
        and lock_dependencies.get("@remotion/cli") == cli_version
        and lock_dependencies.get("remotion") == runtime_version
    )
    if not pinned or not cli_path.is_file():
        return (
            False,
            f"Pinned Remotion CLI dependencies are not installed; run npm ci in {root}",
        )
    return True, f"pinned Remotion {runtime_version} CLI available at {root}"
