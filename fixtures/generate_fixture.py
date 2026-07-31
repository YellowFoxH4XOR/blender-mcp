"""Generate the small deterministic Blender scene used by integration tests.

Run with:

    blender --background --factory-startup --python fixtures/generate_fixture.py -- fixture.blend
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import bpy


def _output_path() -> Path:
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    if len(args) != 1:
        raise SystemExit("Expected exactly one output .blend path.")
    path = Path(args[0]).resolve()
    if path.suffix.lower() != ".blend":
        raise SystemExit("Fixture output must have a .blend suffix.")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def main() -> None:
    output = _output_path()
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.name = "M0 Fixture"
    scene.frame_start = 1
    scene.frame_end = 24
    scene.render.fps = 24
    scene.render.resolution_x = 96
    scene.render.resolution_y = 64
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    # Blender 5.2 reports the Eevee enum as BLENDER_EEVEE. Avoid relying on
    # display/product naming, which has changed independently of the bpy enum.
    scene.render.engine = "BLENDER_EEVEE"
    scene.world = bpy.data.worlds.new("FixtureWorld")
    scene.world.color = (0.025, 0.025, 0.025)

    bpy.ops.mesh.primitive_cube_add(location=(0.0, 0.0, 0.0))
    cube = bpy.context.object
    cube.name = "FixtureCube"
    cube["_blender_mcp_id"] = "fixture_cube_v1"

    bpy.ops.object.camera_add(location=(4.5, -4.5, 3.5))
    camera = bpy.context.object
    camera.name = "FixtureCamera"
    camera["_blender_mcp_id"] = "fixture_camera_v1"
    camera.rotation_euler = (math.radians(67.0), 0.0, math.radians(43.0))
    scene.camera = camera

    bpy.ops.object.light_add(type="AREA", location=(2.0, -2.0, 4.0))
    light = bpy.context.object
    light.name = "FixtureKeyLight"
    light["_blender_mcp_id"] = "fixture_light_v1"
    light.data.energy = 800.0
    light.data.shape = "DISK"
    light.data.size = 5.0

    bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=False)
    print(f"Generated Blender MCP fixture: {output}")


if __name__ == "__main__":
    main()
