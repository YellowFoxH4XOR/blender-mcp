# Integration fixture

The binary fixture is generated instead of committed:

```sh
/Applications/Blender.app/Contents/MacOS/Blender \
  --background --factory-startup \
  --python fixtures/generate_fixture.py \
  -- /tmp/blender-mcp-fixture.blend
```

It contains a cube, camera, and light with stable `_blender_mcp_id` custom
properties and deliberately small render dimensions.
