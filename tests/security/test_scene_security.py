from pathlib import Path

from blender_adapter.operations import (
    ExternalFileReference,
    find_scene_security_violations,
)


def test_scene_security_rejects_external_and_compositor_outputs(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    scene = project / "scenes" / "shot.blend"
    allowed = project / "assets" / "texture.png"
    outside = tmp_path / "secret.png"
    scene.parent.mkdir(parents=True)
    allowed.parent.mkdir()
    scene.touch()
    allowed.touch()
    outside.touch()

    findings = find_scene_security_violations(
        scene_path=scene,
        project_root=project,
        file_references=[
            ExternalFileReference(
                kind="image",
                name="Allowed",
                path="//../assets/texture.png",
            ),
            ExternalFileReference(
                kind="image",
                name="Outside",
                path=str(outside),
            ),
            ExternalFileReference(
                kind="sound",
                name="Packed",
                path=str(outside),
                packed=True,
            ),
        ],
        compositor_file_outputs=["Scene/Write Secrets"],
    )

    assert {finding["code"] for finding in findings} == {
        "EXTERNAL_FILE_REFERENCE",
        "COMPOSITOR_FILE_OUTPUT",
    }
    external = next(
        finding
        for finding in findings
        if finding["code"] == "EXTERNAL_FILE_REFERENCE"
    )
    assert external["severity"] == "blocker"
    assert external["details"]["name"] == "Outside"


def test_scene_security_rejects_symlink_escape_and_ambiguous_paths(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    scene = project / "scene.blend"
    outside = tmp_path / "outside"
    project.mkdir()
    outside.mkdir()
    scene.touch()
    (project / "linked").symlink_to(outside, target_is_directory=True)

    findings = find_scene_security_violations(
        scene_path=scene,
        project_root=project,
        file_references=[
            ExternalFileReference(
                kind="library",
                name="Symlink escape",
                path="//linked/asset.blend",
            ),
            ExternalFileReference(
                kind="image",
                name="URI",
                path="https://example.test/image.png",
            ),
            ExternalFileReference(
                kind="image",
                name="Working-directory relative",
                path="textures/image.png",
            ),
        ],
        compositor_file_outputs=[],
    )

    assert [finding["code"] for finding in findings] == [
        "EXTERNAL_FILE_REFERENCE",
        "UNSAFE_FILE_REFERENCE",
        "UNSAFE_FILE_REFERENCE",
    ]
