"""Local worker installation primitives.

The worker runs outside the Codex process sandbox and exposes only an
authenticated, loopback MCP endpoint.
"""

from __future__ import annotations

import errno
import os
import plistlib
import secrets
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    repository_root: Path
    python_executable: Path
    config_path: Path
    token_path: Path
    log_path: Path
    remotion_project: Path | None = None
    node_executable: Path | None = None
    npm_executable: Path | None = None
    port: int = 9876
    label: str = "com.yellowfox.blender-mcp"

    def __post_init__(self) -> None:
        for value in (
            self.repository_root,
            self.python_executable,
            self.config_path,
            self.token_path,
            self.log_path,
        ):
            if not value.is_absolute():
                raise ValueError("worker paths must be absolute")
        if (
            self.remotion_project is not None
            and not self.remotion_project.is_absolute()
        ):
            raise ValueError("worker paths must be absolute")
        for value in (self.node_executable, self.npm_executable):
            if value is not None and not value.is_absolute():
                raise ValueError("worker paths must be absolute")
        if not 1 <= self.port <= 65_535:
            raise ValueError("worker port must be between 1 and 65535")


def build_launch_agent_payload(spec: WorkerSpec) -> dict[str, object]:
    """Return a no-shell, loopback-only macOS LaunchAgent definition."""

    arguments = [
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
        str(spec.port),
        "--token-file",
        str(spec.token_path),
    ]
    environment: dict[str, str] = {}
    if spec.remotion_project is not None:
        environment["BLENDER_MCP_REMOTION_PROJECT"] = str(
            spec.remotion_project
        )
    if spec.node_executable is not None:
        environment["BLENDER_MCP_NODE_EXECUTABLE"] = str(spec.node_executable)
    if spec.npm_executable is not None:
        environment["BLENDER_MCP_NPM_EXECUTABLE"] = str(spec.npm_executable)
    return {
        "Label": spec.label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(spec.repository_root),
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "StandardOutPath": str(spec.log_path),
        "StandardErrorPath": str(spec.log_path),
    }


def write_launch_agent(spec: WorkerSpec, destination: Path) -> Path:
    """Atomically install a private LaunchAgent plist and its prerequisites."""

    target = destination.expanduser().resolve(strict=False)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    spec.log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    ensure_worker_token(spec.token_path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            plistlib.dump(build_launch_agent_payload(spec), handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def install_and_start_worker(
    spec: WorkerSpec,
    destination: Path,
    *,
    launchctl_executable: Path = Path("/bin/launchctl"),
) -> Path:
    """Install and immediately bootstrap one user-scoped macOS LaunchAgent."""

    if sys.platform != "darwin":
        raise ValueError("the launchd worker installer is available only on macOS")
    if not spec.repository_root.is_dir():
        raise ValueError(f"worker repository was not found: {spec.repository_root}")
    if not spec.python_executable.is_file() or not os.access(
        spec.python_executable, os.X_OK
    ):
        raise ValueError(
            f"worker Python is missing or not executable: {spec.python_executable}"
        )
    if not spec.config_path.is_file():
        raise ValueError(f"worker configuration was not found: {spec.config_path}")
    if not launchctl_executable.is_file() or not os.access(
        launchctl_executable, os.X_OK
    ):
        raise ValueError(f"launchctl is missing or not executable: {launchctl_executable}")

    target = write_launch_agent(spec, destination)
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{spec.label}"
    subprocess.run(
        [str(launchctl_executable), "bootout", service],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        shell=False,
    )
    bootstrapped = subprocess.run(
        [str(launchctl_executable), "bootstrap", domain, str(target)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        shell=False,
    )
    if bootstrapped.returncode != 0:
        # Modern macOS can auto-register a plist placed in
        # ~/Library/LaunchAgents while this installer is replacing it. That can
        # race the explicit bootout/bootstrap cycle and return EIO even when the
        # plist is valid. `load -w` is the compatible user-agent recovery path.
        loaded = subprocess.run(
            [str(launchctl_executable), "load", "-w", str(target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            shell=False,
        )
        if loaded.returncode != 0:
            bootstrap_error = (
                bootstrapped.stderr
                or bootstrapped.stdout
                or "unknown failure"
            )[-2000:]
            load_error = (
                loaded.stderr or loaded.stdout or "unknown failure"
            )[-2000:]
            raise ValueError(
                "launchctl bootstrap failed: "
                f"{bootstrap_error}; launchctl load fallback failed: {load_error}"
            )
    started = subprocess.run(
        [str(launchctl_executable), "kickstart", "-k", service],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        shell=False,
    )
    if started.returncode != 0:
        raise ValueError(
            "launchctl kickstart failed: "
            + (started.stderr or started.stdout or "unknown failure")[-2000:]
        )
    return target


def ensure_worker_token(token_path: Path) -> str:
    """Create or reuse one private bearer token without following symlinks."""

    path = Path(os.path.abspath(token_path.expanduser()))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    token = secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        existing = _read_private_token(path, repair_permissions=True).strip()
        if len(existing) < 48:
            raise ValueError(f"worker token is invalid: {path}")
        return existing

    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(f"{token}\n")
        handle.flush()
        os.fsync(handle.fileno())
    return token


def read_private_token(token_path: Path) -> str:
    """Read an owner-only regular token file without following its final link."""

    return _read_private_token(token_path, repair_permissions=False)


def _read_private_token(token_path: Path, *, repair_permissions: bool) -> str:
    path = Path(os.path.abspath(token_path.expanduser()))
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if not hasattr(os, "O_NOFOLLOW"):
        raise ValueError("token files require O_NOFOLLOW support")
    flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError(f"token file must not be a symlink: {path}") from exc
        if exc.errno in {errno.EISDIR, errno.ENXIO}:
            raise ValueError(f"token path is not a regular file: {path}") from exc
        raise ValueError(f"token file could not be opened safely: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"token path is not a regular file: {path}")
        if info.st_uid != os.getuid():
            raise ValueError(f"token file must be owned by the current user: {path}")
        if repair_permissions:
            os.fchmod(descriptor, 0o600)
        elif stat.S_IMODE(info.st_mode) & 0o077:
            raise ValueError(f"token file permissions must be owner-only: {path}")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            content = handle.read(4097)
        if len(content) > 4096:
            raise ValueError(f"token file is unexpectedly large: {path}")
        return content.rstrip("\r\n")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
