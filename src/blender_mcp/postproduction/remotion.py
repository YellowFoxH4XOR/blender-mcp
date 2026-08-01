"""Render trusted Blender frame-sequence manifests through bundled Remotion."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any


class RemotionRenderError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SequenceMetadata:
    manifest_path: Path
    frame_paths: tuple[str, ...]
    fps: float
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class RemotionRenderResult:
    output_path: Path
    size_bytes: int
    sha256: str
    duration_seconds: float


class RemotionRenderer:
    """No-shell subprocess boundary for one pinned local Remotion project."""

    def __init__(
        self,
        project_root: Path,
        *,
        remotion_project: Path | None = None,
        node_executable: str | Path | None = None,
        npm_executable: str | Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve(strict=True)
        default_project = Path(__file__).resolve().parents[3] / "remotion"
        self.remotion_project = (
            remotion_project or default_project
        ).resolve(strict=True)
        # Retained in the signature for configuration compatibility. npm is
        # intentionally never executed: `npm exec` may resolve from a registry.
        del npm_executable
        discovered_node = (
            Path(node_executable)
            if node_executable is not None
            else Path(shutil.which("node") or "")
        )
        if not str(discovered_node) or not discovered_node.is_absolute():
            raise RemotionRenderError("An absolute Node executable is required")
        try:
            self.node_executable = discovered_node.resolve(strict=True)
        except OSError as exc:
            raise RemotionRenderError("Node executable was not found") from exc
        if not self.node_executable.is_file() or not os.access(
            self.node_executable,
            os.X_OK,
        ):
            raise RemotionRenderError("Node executable is not executable")
        if not (self.remotion_project / "src" / "index.ts").is_file():
            raise RemotionRenderError("Remotion entry point was not found")
        try:
            self.remotion_cli = (
                self.remotion_project
                / "node_modules"
                / "@remotion"
                / "cli"
                / "remotion-cli.js"
            ).resolve(strict=True)
        except OSError as exc:
            raise RemotionRenderError(
                "Pinned local Remotion CLI was not found"
            ) from exc
        if (
            not self.remotion_cli.is_file()
            or not self.remotion_cli.is_relative_to(self.remotion_project)
        ):
            raise RemotionRenderError("Pinned local Remotion CLI is invalid")

    def load_sequence(self, manifest_path: Path) -> SequenceMetadata:
        manifest = self._inside_project(manifest_path, must_exist=True)
        try:
            document = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemotionRenderError("Frame-sequence manifest is invalid") from exc
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != "1.0"
            or document.get("kind") != "blender_frame_sequence"
        ):
            raise RemotionRenderError("Unsupported frame-sequence manifest")
        frames = document.get("frames")
        fps = document.get("fps")
        width = document.get("width")
        height = document.get("height")
        if (
            not isinstance(frames, list)
            or not frames
            or isinstance(fps, bool)
            or not isinstance(fps, (int, float))
            or fps <= 0
            or isinstance(width, bool)
            or not isinstance(width, int)
            or width <= 0
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height <= 0
        ):
            raise RemotionRenderError("Frame-sequence metadata is invalid")

        frame_paths: list[str] = []
        for expected_index, frame in enumerate(frames):
            if not isinstance(frame, dict) or not isinstance(frame.get("path"), str):
                raise RemotionRenderError("Frame entry is invalid")
            relative = Path(frame["path"])
            if relative.is_absolute() or ".." in relative.parts or "\\" in frame["path"]:
                raise RemotionRenderError("Frame path is not project-local")
            resolved = (manifest.parent / relative).resolve(strict=True)
            self._inside_project(resolved, must_exist=True)
            if resolved.suffix.lower() != ".png" or not resolved.is_file():
                raise RemotionRenderError("Frame must be a PNG file")
            frame_number = frame.get("frame")
            if isinstance(frame_number, bool) or not isinstance(frame_number, int):
                raise RemotionRenderError(
                    f"Frame entry {expected_index} has no integer frame number"
                )
            expected_size = frame.get("size_bytes")
            expected_sha256 = frame.get("sha256")
            if (
                isinstance(expected_size, bool)
                or not isinstance(expected_size, int)
                or expected_size < 0
                or not isinstance(expected_sha256, str)
                or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
            ):
                raise RemotionRenderError(
                    f"Frame entry {expected_index} has invalid integrity metadata"
                )
            if (
                resolved.stat().st_size != expected_size
                or self._sha256(resolved) != expected_sha256
            ):
                raise RemotionRenderError(
                    f"Frame entry {expected_index} failed integrity verification"
                )
            frame_paths.append(resolved.relative_to(self.project_root).as_posix())
        return SequenceMetadata(
            manifest_path=manifest,
            frame_paths=tuple(frame_paths),
            fps=float(fps),
            width=width,
            height=height,
        )

    def build_command(
        self,
        sequence: SequenceMetadata,
        output_path: Path,
        props_path: Path,
    ) -> list[str]:
        output = self._inside_project(output_path, must_exist=False)
        props = self._inside_project(props_path, must_exist=True)
        return [
            str(self.node_executable),
            str(self.remotion_cli),
            "render",
            "src/index.ts",
            "BlenderSequence",
            str(output),
            f"--props={props}",
            f"--public-dir={self.project_root}",
            "--log=error",
        ]

    def render(
        self,
        manifest_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float,
        cancel_event: Event | None = None,
    ) -> RemotionRenderResult:
        sequence = self.load_sequence(manifest_path)
        output = self._inside_project(output_path, must_exist=False)
        if output.suffix.lower() != ".mp4":
            raise RemotionRenderError("Remotion output must use a .mp4 suffix")
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        props_path = sequence.manifest_path.with_suffix(".remotion-props.json")
        self._atomic_write_json(
            props_path,
            {
                "framePaths": list(sequence.frame_paths),
                "fps": sequence.fps,
                "width": sequence.width,
                "height": sequence.height,
            },
        )
        command = self.build_command(sequence, output, props_path)
        process = subprocess.Popen(
            command,
            cwd=self.remotion_project,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._environment(),
            start_new_session=True,
        )
        started = time.monotonic()
        try:
            while True:
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    if cancel_event is not None and cancel_event.is_set():
                        self._terminate(process)
                        raise RemotionRenderError("Remotion render was cancelled")
                    if time.monotonic() - started > timeout_seconds:
                        self._terminate(process)
                        raise RemotionRenderError("Remotion render exceeded its timeout")
            if process.returncode != 0:
                raise RemotionRenderError(
                    "Remotion render failed: "
                    + (stderr or stdout or "unknown failure")[-4000:]
                )
        finally:
            props_path.unlink(missing_ok=True)
        if not output.is_file():
            raise RemotionRenderError("Remotion reported success without an MP4")
        return RemotionRenderResult(
            output_path=output,
            size_bytes=output.stat().st_size,
            sha256=self._sha256(output),
            duration_seconds=len(sequence.frame_paths) / sequence.fps,
        )

    def _inside_project(self, path: Path, *, must_exist: bool) -> Path:
        try:
            resolved = path.resolve(strict=must_exist)
        except OSError as exc:
            raise RemotionRenderError(f"Project path does not exist: {path}") from exc
        if not resolved.is_relative_to(self.project_root):
            raise RemotionRenderError("Path resolves outside project root")
        return resolved

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
            process.wait()

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL")
        return {key: os.environ[key] for key in allowed if key in os.environ}

    @staticmethod
    def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, path)
        finally:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
