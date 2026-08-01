"""Generate a scene containing file access that the adapter must reject."""

from __future__ import annotations

import sys
from pathlib import Path

import bpy


def main() -> None:
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(args) != 2:
        raise SystemExit("Expected output scene and external target paths.")
    output = Path(args[0]).resolve()
    external = Path(args[1]).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = "Unsafe Fixture"
    scene.render.resolution_x = 64
    scene.render.resolution_y = 64

    bpy.ops.object.camera_add()
    scene.camera = bpy.context.object

    image = bpy.data.images.new("ExternalImage", width=1, height=1)
    image.source = "FILE"
    image.filepath = str(external / "outside.png")

    scene.use_nodes = True
    node_tree = getattr(scene, "node_tree", None)
    if node_tree is None:
        node_tree = bpy.data.node_groups.new(
            "UnsafeCompositor",
            "CompositorNodeTree",
        )
        scene.compositing_node_group = node_tree
    output_node = node_tree.nodes.new("CompositorNodeOutputFile")
    output_node.name = "UnsafeFileOutput"
    image_node = node_tree.nodes.new("CompositorNodeImage")
    image_node.name = "UnsafeExternalImage"
    image_node.image = image

    bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=False)


if __name__ == "__main__":
    main()
