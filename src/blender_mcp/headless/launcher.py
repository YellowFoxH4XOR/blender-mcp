"""Safe subprocess boundary for Blender. No shell and no generated code."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Any

from pydantic import ValidationError

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.errors import BlenderMCPError, ErrorCode
from blender_mcp.models import AdapterOperation, RequestEnvelope, ResultEnvelope


@dataclass(frozen=True)
class ProcessResult:
    stdout: str
    stderr: str
    returncode: int
    duration_seconds: float


class BlenderLauncher:
    def __init__(self, config: BlenderMCPConfig) -> None:
        self.config = config

    def probe(self, *, timeout_seconds: float = 10.0) -> ProcessResult:
        if not self.config.blender_executable.is_file():
            raise BlenderMCPError(
                ErrorCode.BLENDER_NOT_FOUND,
                "Configured Blender executable was not found",
                details={"executable": str(self.config.blender_executable)},
            )
        return self._run(
            [str(self.config.blender_executable), "--version"],
            timeout_seconds=timeout_seconds,
        )

    def invoke(
        self,
        operation: AdapterOperation,
        payload: dict[str, Any],
        *,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> ResultEnvelope[dict[str, Any]]:
        if not self.config.blender_executable.is_file():
            raise BlenderMCPError(
                ErrorCode.BLENDER_NOT_FOUND,
                "Configured Blender executable was not found",
                details={"executable": str(self.config.blender_executable)},
            )
        if not self.config.adapter_script.is_file():
            raise BlenderMCPError(
                ErrorCode.BLENDER_FAILED,
                "Bundled Blender adapter was not found",
                details={"adapter_script": str(self.config.adapter_script)},
            )

        request = RequestEnvelope[dict[str, Any]](
            request_id=request_id,
            command=operation,
            payload=payload,
        )
        request_bytes = request.model_dump_json().encode("utf-8")
        if len(request_bytes) > self.config.max_request_bytes:
            raise BlenderMCPError(
                ErrorCode.REQUEST_TOO_LARGE,
                "Adapter request exceeds the configured size limit",
                details={
                    "size_bytes": len(request_bytes),
                    "max_request_bytes": self.config.max_request_bytes,
                },
            )

        run_root = self.config.project_root / ".blender-mcp"
        run_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with TemporaryDirectory(prefix="request-", dir=run_root) as temporary:
            temporary_path = Path(temporary)
            request_path = temporary_path / "request.json"
            result_path = temporary_path / "result.json"
            self._write_private(request_path, request_bytes)

            command = [
                str(self.config.blender_executable),
                "--background",
                "--disable-autoexec",
            ]
            command.extend(
                [
                    "--python",
                    str(self.config.adapter_script),
                    "--",
                    "--request",
                    str(request_path),
                    "--result",
                    str(result_path),
                ]
            )
            process = self._run(
                command,
                timeout_seconds=timeout_seconds
                or self.config.subprocess_timeout_seconds,
            )
            if not result_path.is_file():
                if process.returncode != 0:
                    if process.returncode in {-11, 139}:
                        raise BlenderMCPError(
                            ErrorCode.BLENDER_CRASHED,
                            "Blender crashed before producing a structured result",
                            details={
                                "returncode": process.returncode,
                                "stderr": process.stderr[-4000:],
                            },
                        )
                    raise BlenderMCPError(
                        ErrorCode.BLENDER_FAILED,
                        "Blender adapter process failed without a structured result",
                        details={
                            "returncode": process.returncode,
                            "stderr": process.stderr[-4000:],
                        },
                    )
                raise BlenderMCPError(
                    ErrorCode.ADAPTER_INVALID_RESPONSE,
                    "Blender adapter did not write a result",
                )
            try:
                raw_result = result_path.read_bytes()
                if len(raw_result) > self.config.max_request_bytes:
                    raise ValueError("result exceeds configured size limit")
                result = ResultEnvelope[dict[str, Any]].model_validate_json(raw_result)
            except (OSError, ValueError, ValidationError) as exc:
                raise BlenderMCPError(
                    ErrorCode.ADAPTER_INVALID_RESPONSE,
                    "Blender adapter returned an invalid result",
                    details={"reason": str(exc)},
                ) from exc
            if result.request_id != request_id:
                raise BlenderMCPError(
                    ErrorCode.ADAPTER_INVALID_RESPONSE,
                    "Blender adapter result request ID does not match",
                )
            if result.command != operation:
                raise BlenderMCPError(
                    ErrorCode.ADAPTER_INVALID_RESPONSE,
                    "Blender adapter result command does not match",
                )
            return result

    def _run(
        self,
        command: list[str],
        *,
        timeout_seconds: float,
    ) -> ProcessResult:
        started = monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=self.config.project_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                shell=False,
                env=self._environment(),
            )
        except FileNotFoundError as exc:
            raise BlenderMCPError(
                ErrorCode.BLENDER_NOT_FOUND,
                "Configured Blender executable could not be launched",
                details={"executable": str(self.config.blender_executable)},
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise BlenderMCPError(
                ErrorCode.BLENDER_TIMEOUT,
                "Blender process exceeded its timeout",
                details={"timeout_seconds": timeout_seconds},
            ) from exc
        return ProcessResult(
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
            duration_seconds=monotonic() - started,
        )

    @staticmethod
    def _write_private(path: Path, content: bytes) -> None:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            remaining = memoryview(content)
            while remaining:
                written = os.write(descriptor, remaining)
                remaining = remaining[written:]
        finally:
            os.close(descriptor)

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
        return {key: os.environ[key] for key in allowed if key in os.environ}
