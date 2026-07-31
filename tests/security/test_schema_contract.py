from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource


SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas"


def _schemas() -> dict[str, dict[str, Any]]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in SCHEMA_ROOT.glob("*.schema.json")
    }


@pytest.fixture(scope="module")
def registry() -> Registry[Any]:
    resources = [
        (schema["$id"], Resource.from_contents(schema))
        for schema in _schemas().values()
    ]
    return Registry().with_resources(resources)


def _validator(name: str, registry: Registry[Any]) -> Draft202012Validator:
    schema = _schemas()[name]
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=registry)


def _base_result(command: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "request_id": "req_schema_test",
        "command": command,
        "ok": True,
        "data": data,
        "warnings": [],
        "artifacts": [],
        "timing": {"execution_ms": 1.25},
    }


def _artifact() -> dict[str, Any]:
    return {
        "kind": "preview",
        "path": "/private/job/preview.png",
        "media_type": "image/png",
        "size_bytes": 128,
        "sha256": "a" * 64,
        "width": 640,
        "height": 360,
    }


def test_all_schemas_pass_draft_2020_12_meta_validation(
    registry: Registry[Any],
) -> None:
    for name in _schemas():
        _validator(name, registry)


def test_status_request_and_result_examples_validate(
    registry: Registry[Any],
) -> None:
    request = {
        "schema_version": "1.0",
        "request_id": "req_schema_test",
        "command": "status",
        "payload": {},
    }
    result = _base_result(
        "status",
        {
            "blender_version": "5.2.0 LTS",
            "blender_version_tuple": [5, 2, 0],
            "background": True,
            "adapter_schema_version": "1.0",
            "commands": ["inspect_scene", "render_preview", "status"],
        },
    )

    _validator("status.request.schema.json", registry).validate(request)
    _validator("status.result.schema.json", registry).validate(result)


def test_inspect_request_and_result_examples_validate(
    registry: Registry[Any],
) -> None:
    request = {
        "schema_version": "1.0",
        "request_id": "req_schema_test",
        "command": "inspect_scene",
        "payload": {
            "scene_path": "/project/scenes/fixture.blend",
            "include": ["summary", "objects"],
            "max_objects": 100,
        },
    }
    result = _base_result(
        "inspect_scene",
        {
            "scene_path": "/project/scenes/fixture.blend",
            "scene_name": "Scene",
            "objects": [],
            "object_count": 0,
            "objects_returned": 0,
            "active_camera": None,
            "frame": {
                "current": 1,
                "start": 1,
                "end": 250,
                "step": 1,
                "fps": 24,
                "fps_base": 1.0,
            },
            "render": {
                "engine": "BLENDER_EEVEE_NEXT",
                "resolution_x": 1920,
                "resolution_y": 1080,
                "resolution_percentage": 100,
                "image_format": "PNG",
            },
        },
    )

    _validator("inspect_scene.request.schema.json", registry).validate(request)
    _validator("inspect_scene.result.schema.json", registry).validate(result)


def test_preview_request_and_result_examples_validate(
    registry: Registry[Any],
) -> None:
    artifact = _artifact()
    request = {
        "schema_version": "1.0",
        "request_id": "req_schema_test",
        "command": "render_preview",
        "payload": {
            "scene_path": "/project/scenes/fixture.blend",
            "artifact_path": "/project/artifacts/preview.png",
            "frame": 1,
            "max_width": 640,
            "max_height": 360,
            "samples": 16,
        },
    }
    result = _base_result(
        "render_preview",
        {
            "scene_path": "/project/scenes/fixture.blend",
            "source_sha256": "b" * 64,
            "frame": 1,
            "artifact": artifact,
        },
    )
    result["artifacts"] = [artifact]

    _validator("render_preview.request.schema.json", registry).validate(request)
    _validator("render_preview.result.schema.json", registry).validate(result)


def test_command_specific_schema_rejects_arbitrary_script_payload(
    registry: Registry[Any],
) -> None:
    request = {
        "schema_version": "1.0",
        "request_id": "req_schema_test",
        "command": "inspect_scene",
        "payload": {
            "scene_path": "/project/scenes/fixture.blend",
            "python_source": "import os; os.system('whoami')",
        },
    }

    with pytest.raises(ValidationError):
        _validator("inspect_scene.request.schema.json", registry).validate(request)


def test_result_status_controls_data_or_error_exclusivity(
    registry: Registry[Any],
) -> None:
    validator = _validator("common-result.schema.json", registry)
    failure = {
        "schema_version": "1.0",
        "request_id": "req_schema_test",
        "command": "inspect_scene",
        "ok": False,
        "error": {
            "code": "SCENE_OPEN_FAILED",
            "message": "Blender could not open the scene.",
            "details": {},
        },
        "warnings": [],
        "artifacts": [],
        "timing": {"execution_ms": 2.5},
    }
    validator.validate(failure)

    invalid = {**failure, "data": {}}
    with pytest.raises(ValidationError):
        validator.validate(invalid)
