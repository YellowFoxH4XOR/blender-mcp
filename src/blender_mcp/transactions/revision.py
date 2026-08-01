"""Scene revision calculation for optimistic concurrency control."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

REVISION_SCHEMA_VERSION = "1.0"


class SceneRevision(BaseModel):
    """Auditable inputs and digest identifying one on-disk scene state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = REVISION_SCHEMA_VERSION
    logical_path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    mtime_ns: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def compute_scene_revision(
    scene_path: Path,
    *,
    logical_path: str | None = None,
    schema_version: str = REVISION_SCHEMA_VERSION,
) -> SceneRevision:
    """Return a deterministic revision incorporating path, metadata, and bytes."""

    if not schema_version:
        raise ValueError("schema_version must not be empty")
    path = scene_path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("scene_path must be a regular file")
    stat = path.stat()
    identity = {
        "schema_version": schema_version,
        "logical_path": logical_path or str(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "content_sha256": _sha256(path),
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return SceneRevision(
        **identity,
        revision=hashlib.sha256(encoded).hexdigest(),
    )
