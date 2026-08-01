"""Atomic, content-addressed manifests for project-local render artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class ArtifactError(RuntimeError):
    """Base class for artifact verification and publication failures."""


class ArtifactPathError(ArtifactError, ValueError):
    pass


class ArtifactChangedError(ArtifactError):
    pass


class ArtifactManifestExistsError(ArtifactError, FileExistsError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactEntry:
    path: str
    kind: str
    media_type: str
    size_bytes: int
    sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    schema_version: str
    created_at: str
    job_id: str | None
    artifacts: tuple[ArtifactEntry, ...]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "job_id": self.job_id,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class ManifestWriteResult:
    path: str
    size_bytes: int
    sha256: str
    manifest: ArtifactManifest


class ArtifactManifestWriter:
    """Hash artifacts and atomically publish their JSON manifest."""

    def __init__(self, project_root: str | Path) -> None:
        root = Path(project_root).expanduser()
        if not root.is_absolute():
            raise ValueError("project_root must be absolute")
        self.project_root = root.resolve(strict=True)
        if not self.project_root.is_dir():
            raise ValueError("project_root must be a directory")

    def describe(
        self,
        artifact_path: str | Path,
        *,
        kind: str,
        media_type: str,
    ) -> ArtifactEntry:
        relative = self._relative_path(artifact_path)
        self._require_nonempty(kind, "kind")
        self._require_nonempty(media_type, "media_type")
        candidate = self._verified_input(relative)
        return ArtifactEntry(
            path=relative.as_posix(),
            kind=kind,
            media_type=media_type,
            size_bytes=candidate.stat().st_size,
            sha256=self._sha256_file(candidate),
        )

    def write(
        self,
        manifest_path: str | Path,
        *,
        artifacts: Iterable[ArtifactEntry],
        job_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        allow_overwrite: bool = False,
    ) -> ManifestWriteResult:
        relative_manifest = self._relative_path(manifest_path)
        if relative_manifest.suffix.lower() != ".json":
            raise ArtifactPathError("manifest_path must end in .json")
        if job_id is not None:
            self._require_nonempty(job_id, "job_id")

        artifact_entries = tuple(artifacts)
        for artifact in artifact_entries:
            self._verify_unchanged(artifact)

        metadata_copy = self._json_object(metadata)
        manifest = ArtifactManifest(
            schema_version="1.0",
            created_at=datetime.now(UTC).isoformat(timespec="microseconds"),
            job_id=job_id,
            artifacts=artifact_entries,
            metadata=metadata_copy,
        )
        encoded = (
            json.dumps(
                manifest.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

        destination = self._prepare_output(relative_manifest)
        self._atomic_publish(
            destination,
            encoded,
            allow_overwrite=allow_overwrite,
        )
        return ManifestWriteResult(
            path=relative_manifest.as_posix(),
            size_bytes=len(encoded),
            sha256=hashlib.sha256(encoded).hexdigest(),
            manifest=manifest,
        )

    def _verify_unchanged(self, expected: ArtifactEntry) -> None:
        actual = self.describe(
            expected.path,
            kind=expected.kind,
            media_type=expected.media_type,
        )
        if actual != expected:
            raise ArtifactChangedError(
                f"Artifact changed before manifest publication: {expected.path}"
            )

    def _verified_input(self, relative: Path) -> Path:
        candidate = self.project_root / relative
        self._reject_symlink_components(relative)
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ArtifactPathError(
                f"Artifact does not exist: {relative.as_posix()}"
            ) from exc
        if not resolved.is_relative_to(self.project_root):
            raise ArtifactPathError("Artifact resolves outside project_root")
        try:
            mode = candidate.lstat().st_mode
        except FileNotFoundError as exc:
            raise ArtifactPathError(
                f"Artifact does not exist: {relative.as_posix()}"
            ) from exc
        if not stat.S_ISREG(mode):
            raise ArtifactPathError("Artifact must be a regular file")
        return candidate

    def _prepare_output(self, relative: Path) -> Path:
        parent_relative = relative.parent
        current = self.project_root
        for part in parent_relative.parts:
            current = current / part
            if current.is_symlink():
                raise ArtifactPathError("Manifest parent cannot contain a symlink")
            if current.exists():
                if not current.is_dir():
                    raise ArtifactPathError(
                        "Manifest parent contains a non-directory component"
                    )
            else:
                current.mkdir()

        destination = self.project_root / relative
        if destination.is_symlink():
            raise ArtifactPathError("Manifest path cannot be a symlink")
        if destination.exists() and not destination.is_file():
            raise ArtifactPathError("Manifest path must be a regular file")
        return destination

    def _atomic_publish(
        self,
        destination: Path,
        payload: bytes,
        *,
        allow_overwrite: bool,
    ) -> None:
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{destination.name}.",
                suffix=".tmp",
                dir=destination.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(payload)
                temporary.flush()
                os.fsync(temporary.fileno())

            if allow_overwrite:
                os.replace(temporary_path, destination)
                temporary_path = None
            else:
                try:
                    os.link(temporary_path, destination)
                except FileExistsError as exc:
                    raise ArtifactManifestExistsError(
                        f"Manifest already exists: "
                        f"{destination.relative_to(self.project_root).as_posix()}"
                    ) from exc
            self._fsync_directory(destination.parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _reject_symlink_components(self, relative: Path) -> None:
        current = self.project_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ArtifactPathError("Artifact path cannot contain a symlink")

    @staticmethod
    def _relative_path(value: str | Path) -> Path:
        raw = str(value)
        path = Path(raw)
        if (
            not raw
            or "\x00" in raw
            or "\\" in raw
            or path.is_absolute()
            or ".." in path.parts
        ):
            raise ArtifactPathError(
                "Path must be project-relative and cannot contain traversal"
            )
        return path

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _json_object(value: Mapping[str, Any] | None) -> dict[str, Any]:
        encoded = json.dumps(
            dict(value or {}),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return json.loads(encoded)

    @staticmethod
    def _require_nonempty(value: str, field_name: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
