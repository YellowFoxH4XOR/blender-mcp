from __future__ import annotations

import os
from pathlib import Path

import pytest

from blender_mcp.errors import ErrorCode
from blender_mcp.policy.paths import PathPolicyError, ProjectPathPolicy


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "scenes").mkdir()
    (root / "artifacts").mkdir()
    (root / "scenes" / "fixture.blend").write_bytes(b"BLENDER-v300")
    return root


@pytest.fixture
def policy(project: Path) -> ProjectPathPolicy:
    return ProjectPathPolicy(project)


@pytest.mark.parametrize(
    "hostile_path",
    [
        "",
        "/tmp/outside.blend",
        "../outside.blend",
        "scenes/../../outside.blend",
        r"..\outside.blend",
        r"scenes\..\..\outside.blend",
        r"C:\outside.blend",
        r"\\server\share\outside.blend",
        "scenes/\x00fixture.blend",
    ],
)
def test_input_rejects_absolute_traversal_and_malformed_paths(
    policy: ProjectPathPolicy,
    hostile_path: str,
) -> None:
    with pytest.raises(PathPolicyError):
        policy.resolve_input(hostile_path)


def test_windows_style_parent_traversal_is_rejected_even_if_literal_file_exists(
    project: Path,
    policy: ProjectPathPolicy,
) -> None:
    # Backslash is a legal POSIX filename character. A host-independent policy
    # must still interpret it as a path separator for traversal detection.
    disguised = project / r"..\outside.blend"
    disguised.write_bytes(b"not a permitted public path")

    with pytest.raises(PathPolicyError) as caught:
        policy.resolve_input(r"..\outside.blend")

    assert caught.value.code == ErrorCode.INVALID_PATH


def test_input_symlink_escape_is_rejected(
    tmp_path: Path,
    project: Path,
    policy: ProjectPathPolicy,
) -> None:
    outside = tmp_path / "outside.blend"
    outside.write_bytes(b"outside")
    (project / "scenes" / "escape.blend").symlink_to(outside)

    with pytest.raises(PathPolicyError) as caught:
        policy.resolve_input("scenes/escape.blend")

    assert caught.value.code == ErrorCode.PATH_OUTSIDE_PROJECT


def test_output_parent_symlink_escape_is_rejected(
    tmp_path: Path,
    project: Path,
    policy: ProjectPathPolicy,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "escaped-artifacts").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathPolicyError) as caught:
        policy.resolve_output(
            "escaped-artifacts/preview.png",
            allow_overwrite=False,
            allowed_suffixes=frozenset({".png"}),
        )

    assert caught.value.code == ErrorCode.PATH_OUTSIDE_PROJECT
    assert not (outside / "preview.png").exists()


def test_output_collision_requires_explicit_overwrite(
    project: Path,
    policy: ProjectPathPolicy,
) -> None:
    output = project / "artifacts" / "preview.png"
    output.write_bytes(b"existing")

    with pytest.raises(PathPolicyError) as caught:
        policy.resolve_output(
            "artifacts/preview.png",
            allow_overwrite=False,
            allowed_suffixes=frozenset({".png"}),
        )

    assert caught.value.code == ErrorCode.PATH_ALREADY_EXISTS
    assert output.read_bytes() == b"existing"

    resolved = policy.resolve_output(
        "artifacts/preview.png",
        allow_overwrite=True,
        allowed_suffixes=frozenset({".png"}),
    )
    assert resolved == output
    assert output.read_bytes() == b"existing"


def test_output_overwrite_never_accepts_directory(
    project: Path,
    policy: ProjectPathPolicy,
) -> None:
    directory = project / "artifacts" / "fake.png"
    directory.mkdir()

    with pytest.raises(PathPolicyError) as caught:
        policy.resolve_output(
            "artifacts/fake.png",
            allow_overwrite=True,
            allowed_suffixes=frozenset({".png"}),
        )

    assert caught.value.code == ErrorCode.INVALID_PATH


def test_wrong_input_and_output_extensions_are_rejected(
    project: Path,
    policy: ProjectPathPolicy,
) -> None:
    (project / "scenes" / "script.py").write_text("print('unsafe')")

    with pytest.raises(PathPolicyError) as input_error:
        policy.resolve_input("scenes/script.py")
    assert input_error.value.code == ErrorCode.UNSUPPORTED_FILE_TYPE

    with pytest.raises(PathPolicyError) as output_error:
        policy.resolve_output(
            "artifacts/preview.py",
            allow_overwrite=False,
            allowed_suffixes=frozenset({".png"}),
        )
    assert output_error.value.code == ErrorCode.UNSUPPORTED_FILE_TYPE


def test_suffix_allowlist_is_case_insensitive(
    project: Path,
    policy: ProjectPathPolicy,
) -> None:
    upper = project / "scenes" / "fixture.BLEND"
    upper.write_bytes(b"BLENDER-v300")

    assert policy.resolve_input("scenes/fixture.BLEND") == upper


@pytest.mark.parametrize("size_bytes", [-1, 1_048_577])
def test_oversized_or_invalid_request_size_is_rejected(
    policy: ProjectPathPolicy,
    size_bytes: int,
) -> None:
    with pytest.raises(PathPolicyError) as caught:
        policy.validate_request_size(size_bytes)

    assert caught.value.code == ErrorCode.REQUEST_TOO_LARGE


def test_request_at_size_limit_is_accepted(policy: ProjectPathPolicy) -> None:
    policy.validate_request_size(1_048_576)


@pytest.mark.skipif(
    not hasattr(os, "symlink"),
    reason="platform does not support symlinks",
)
def test_existing_output_symlink_is_never_overwritten(
    tmp_path: Path,
    project: Path,
    policy: ProjectPathPolicy,
) -> None:
    outside = tmp_path / "valuable.png"
    outside.write_bytes(b"valuable")
    link = project / "artifacts" / "preview.png"
    link.symlink_to(outside)

    with pytest.raises(PathPolicyError):
        policy.resolve_output(
            "artifacts/preview.png",
            allow_overwrite=True,
            allowed_suffixes=frozenset({".png"}),
        )

    assert outside.read_bytes() == b"valuable"
