"""Filesystem policy enforcing confinement to one configured project root."""

from __future__ import annotations

from pathlib import Path

from blender_mcp.errors import BlenderMCPError, ErrorCode


class PathPolicyError(BlenderMCPError):
    pass


class ProjectPathPolicy:
    def __init__(
        self,
        project_root: Path,
        max_request_bytes: int = 1_048_576,
    ) -> None:
        root = project_root.expanduser()
        if not root.is_absolute():
            raise ValueError("project_root must be absolute")
        self.project_root = root.resolve(strict=True)
        if not self.project_root.is_dir():
            raise ValueError("project_root must be a directory")
        if max_request_bytes <= 0:
            raise ValueError("max_request_bytes must be positive")
        self.max_request_bytes = max_request_bytes

    def validate_request_size(self, size_bytes: int) -> None:
        if size_bytes < 0 or size_bytes > self.max_request_bytes:
            raise PathPolicyError(
                ErrorCode.REQUEST_TOO_LARGE,
                "Request exceeds the configured size limit",
                details={
                    "size_bytes": size_bytes,
                    "max_request_bytes": self.max_request_bytes,
                },
            )

    def resolve_input(
        self,
        relative_path: str | Path,
        *,
        allowed_suffixes: frozenset[str] = frozenset({".blend"}),
    ) -> Path:
        candidate = self._resolve(relative_path, allowed_suffixes)
        if not candidate.exists() or not candidate.is_file():
            raise PathPolicyError(
                ErrorCode.PATH_NOT_FOUND,
                "Input file does not exist",
                details={"path": str(relative_path)},
            )
        return candidate

    def resolve_output(
        self,
        relative_path: str | Path,
        *,
        allow_overwrite: bool,
        allowed_suffixes: frozenset[str],
    ) -> Path:
        candidate = self._resolve(relative_path, allowed_suffixes)
        self._verify_output_parent(candidate.parent)
        if candidate.exists():
            if candidate.is_symlink():
                raise PathPolicyError(
                    ErrorCode.INVALID_PATH,
                    "Output path cannot be a symbolic link",
                    details={"path": str(relative_path)},
                )
            if not allow_overwrite:
                raise PathPolicyError(
                    ErrorCode.PATH_ALREADY_EXISTS,
                    "Output file already exists",
                    details={"path": str(relative_path)},
                )
            if not candidate.is_file():
                raise PathPolicyError(
                    ErrorCode.INVALID_PATH,
                    "Output path is not a regular file",
                    details={"path": str(relative_path)},
                )
        return candidate

    def _resolve(
        self,
        relative_path: str | Path,
        allowed_suffixes: frozenset[str],
    ) -> Path:
        raw = str(relative_path)
        if "\x00" in raw or "\\" in raw:
            raise PathPolicyError(
                ErrorCode.INVALID_PATH,
                "Path contains a forbidden character or separator",
            )
        path = Path(raw)
        if not raw or path.is_absolute() or ".." in path.parts:
            raise PathPolicyError(
                ErrorCode.INVALID_PATH,
                "Path must be a non-empty project-relative path without traversal",
                details={"path": raw},
            )
        normalized_suffixes = frozenset(item.lower() for item in allowed_suffixes)
        if not normalized_suffixes or path.suffix.lower() not in normalized_suffixes:
            raise PathPolicyError(
                ErrorCode.UNSUPPORTED_FILE_TYPE,
                "File type is not allowed",
                details={"path": raw, "allowed_suffixes": sorted(normalized_suffixes)},
            )
        candidate = (self.project_root / path).resolve(strict=False)
        if not candidate.is_relative_to(self.project_root):
            raise PathPolicyError(
                ErrorCode.PATH_OUTSIDE_PROJECT,
                "Path resolves outside the configured project root",
                details={"path": raw},
            )
        return candidate

    def _verify_output_parent(self, parent: Path) -> None:
        current = parent
        while not current.exists() and current != self.project_root:
            current = current.parent
        resolved = current.resolve(strict=True)
        if not resolved.is_dir() or not resolved.is_relative_to(self.project_root):
            raise PathPolicyError(
                ErrorCode.PATH_OUTSIDE_PROJECT,
                "Output parent resolves outside the configured project root",
                details={"parent": str(parent)},
            )
