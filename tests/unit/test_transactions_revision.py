from pathlib import Path

from blender_mcp.transactions import compute_scene_revision


def test_scene_revision_is_deterministic_and_content_sensitive(tmp_path: Path) -> None:
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"BLENDER-v1")

    first = compute_scene_revision(scene, logical_path="scenes/scene.blend")
    repeated = compute_scene_revision(scene, logical_path="scenes/scene.blend")

    assert repeated == first
    assert first.logical_path == "scenes/scene.blend"
    assert first.size_bytes == len(b"BLENDER-v1")
    assert len(first.content_sha256) == 64
    assert len(first.revision) == 64

    scene.write_bytes(b"BLENDER-v2")
    changed = compute_scene_revision(scene, logical_path="scenes/scene.blend")

    assert changed.revision != first.revision
