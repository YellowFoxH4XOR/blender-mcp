import hashlib
import json
from pathlib import Path

import pytest

from blender_mcp.artifacts import (
    ArtifactManifestExistsError,
    ArtifactManifestWriter,
    ArtifactPathError,
)


def test_manifest_contains_project_relative_artifact_metadata_and_hashes(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "renders" / "preview.png"
    artifact_path.parent.mkdir()
    artifact_path.write_bytes(b"dependable preview")
    writer = ArtifactManifestWriter(tmp_path)

    artifact = writer.describe(
        "renders/preview.png",
        kind="preview",
        media_type="image/png",
    )
    result = writer.write(
        "renders/preview.manifest.json",
        artifacts=[artifact],
        job_id="job_render_1",
        metadata={"scene_revision": "sha256:abc"},
    )

    manifest_path = tmp_path / result.path
    payload = json.loads(manifest_path.read_text())
    assert artifact.path == "renders/preview.png"
    assert artifact.sha256 == hashlib.sha256(b"dependable preview").hexdigest()
    assert payload["schema_version"] == "1.0"
    assert payload["job_id"] == "job_render_1"
    assert payload["artifacts"] == [artifact.to_dict()]
    assert result.sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert result.size_bytes == manifest_path.stat().st_size


def test_manifest_publication_is_atomic_and_does_not_leave_temp_files(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "frame.png"
    artifact.write_bytes(b"frame")
    writer = ArtifactManifestWriter(tmp_path)

    entry = writer.describe("frame.png", kind="frame", media_type="image/png")
    writer.write("manifests/render.json", artifacts=[entry])

    assert (tmp_path / "manifests" / "render.json").is_file()
    assert list((tmp_path / "manifests").glob(".render.json.*.tmp")) == []


def test_manifest_does_not_overwrite_without_explicit_permission(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "frame.png"
    artifact.write_bytes(b"frame")
    destination = tmp_path / "manifest.json"
    destination.write_text("original")
    writer = ArtifactManifestWriter(tmp_path)
    entry = writer.describe("frame.png", kind="frame", media_type="image/png")

    with pytest.raises(ArtifactManifestExistsError):
        writer.write("manifest.json", artifacts=[entry])

    assert destination.read_text() == "original"
    replaced = writer.write(
        "manifest.json",
        artifacts=[entry],
        allow_overwrite=True,
    )
    assert json.loads((tmp_path / replaced.path).read_text())["artifacts"]


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.png",
        "/tmp/outside.png",
        r"renders\outside.png",
        "",
    ],
)
def test_artifact_paths_must_be_project_relative(
    tmp_path: Path,
    unsafe_path: str,
) -> None:
    writer = ArtifactManifestWriter(tmp_path)

    with pytest.raises(ArtifactPathError):
        writer.describe(unsafe_path, kind="frame", media_type="image/png")


def test_artifact_symlinks_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.png"
    target.write_bytes(b"frame")
    (tmp_path / "linked.png").symlink_to(target)
    writer = ArtifactManifestWriter(tmp_path)

    with pytest.raises(ArtifactPathError):
        writer.describe("linked.png", kind="frame", media_type="image/png")
