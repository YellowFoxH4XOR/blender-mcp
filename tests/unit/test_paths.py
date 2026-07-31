from pathlib import Path

import pytest

from blender_mcp.errors import ErrorCode
from blender_mcp.policy.paths import PathPolicyError, ProjectPathPolicy


def test_resolves_existing_blend_inside_root(tmp_path: Path) -> None:
    scene = tmp_path / "scenes" / "fixture.blend"
    scene.parent.mkdir()
    scene.write_bytes(b"BLENDER")

    resolved = ProjectPathPolicy(tmp_path).resolve_input("scenes/fixture.blend")

    assert resolved == scene


@pytest.mark.parametrize(
    "value",
    [
        "/tmp/scene.blend",
        "../scene.blend",
        "a/../../b.blend",
        r"..\scene.blend",
        r"C:\scene.blend",
        r"\\server\share\scene.blend",
        "",
    ],
)
def test_rejects_absolute_traversal_and_empty_paths(
    tmp_path: Path,
    value: str,
) -> None:
    with pytest.raises(PathPolicyError) as raised:
        ProjectPathPolicy(tmp_path).resolve_input(value)

    assert raised.value.code == ErrorCode.INVALID_PATH


def test_rejects_input_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.blend"
    outside.write_bytes(b"outside")
    (tmp_path / "escaped.blend").symlink_to(outside)

    with pytest.raises(PathPolicyError) as raised:
        ProjectPathPolicy(tmp_path).resolve_input("escaped.blend")

    assert raised.value.code == ErrorCode.PATH_OUTSIDE_PROJECT


def test_rejects_output_parent_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "renders").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PathPolicyError) as raised:
        ProjectPathPolicy(tmp_path).resolve_output(
            "renders/preview.png",
            allow_overwrite=False,
            allowed_suffixes=frozenset({".png"}),
        )

    assert raised.value.code == ErrorCode.PATH_OUTSIDE_PROJECT


def test_rejects_output_collision_unless_explicitly_allowed(tmp_path: Path) -> None:
    output = tmp_path / "preview.png"
    output.write_bytes(b"existing")
    policy = ProjectPathPolicy(tmp_path)

    with pytest.raises(PathPolicyError) as raised:
        policy.resolve_output(
            "preview.png",
            allow_overwrite=False,
            allowed_suffixes=frozenset({".png"}),
        )

    assert raised.value.code == ErrorCode.PATH_ALREADY_EXISTS
    assert (
        policy.resolve_output(
            "preview.png",
            allow_overwrite=True,
            allowed_suffixes=frozenset({".png"}),
        )
        == output
    )


def test_enforces_request_size(tmp_path: Path) -> None:
    policy = ProjectPathPolicy(tmp_path, max_request_bytes=10)
    policy.validate_request_size(10)

    with pytest.raises(PathPolicyError) as raised:
        policy.validate_request_size(11)

    assert raised.value.code == ErrorCode.REQUEST_TOO_LARGE
