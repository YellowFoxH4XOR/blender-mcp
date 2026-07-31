from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from blender_mcp.models.api import InspectSceneInput, RenderPreviewInput
from blender_mcp.models.envelopes import RequestEnvelope


SCHEMA_ROOT = Path(__file__).resolve().parents[2] / "schemas"


def test_all_contract_schemas_are_valid_json_objects() -> None:
    schema_paths = sorted(SCHEMA_ROOT.glob("*.schema.json"))
    assert schema_paths

    for path in schema_paths:
        schema = json.loads(path.read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert isinstance(schema.get("$id"), str)
        assert isinstance(schema.get("title"), str)


@pytest.mark.parametrize(
    "forbidden_command",
    ["run_python", "exec", "eval", "shell", "install_addon", "download_url"],
)
def test_adapter_envelope_rejects_forbidden_commands(
    forbidden_command: str,
) -> None:
    with pytest.raises(ValidationError):
        RequestEnvelope[dict[str, object]].model_validate(
            {
                "schema_version": "1.0",
                "request_id": "req_security",
                "command": forbidden_command,
                "payload": {},
            }
        )


@pytest.mark.parametrize(
    "forbidden_field",
    ["python", "python_source", "script", "shell_command", "url"],
)
def test_public_inspect_input_rejects_command_like_fields(
    forbidden_field: str,
) -> None:
    with pytest.raises(ValidationError):
        InspectSceneInput.model_validate(
            {
                "scene_path": "scenes/fixture.blend",
                forbidden_field: "malicious content",
            }
        )


def test_public_preview_input_rejects_unexpected_execution_fields() -> None:
    with pytest.raises(ValidationError):
        RenderPreviewInput.model_validate(
            {
                "scene_path": "scenes/fixture.blend",
                "output_path": "artifacts/preview.png",
                "allow_overwrite": False,
                "command": "sh -c whoami",
            }
        )


def test_envelope_rejects_unknown_top_level_fields() -> None:
    with pytest.raises(ValidationError):
        RequestEnvelope[dict[str, object]].model_validate(
            {
                "schema_version": "1.0",
                "request_id": "req_security",
                "command": "inspect_scene",
                "payload": {"scene_path": "/resolved/fixture.blend"},
                "environment": {"PATH": "/attacker-controlled"},
            }
        )


def test_envelope_rejects_unsupported_schema_version() -> None:
    with pytest.raises(ValidationError):
        RequestEnvelope[dict[str, object]].model_validate(
            {
                "schema_version": "999.0",
                "request_id": "req_security",
                "command": "inspect_scene",
                "payload": {"scene_path": "/resolved/fixture.blend"},
            }
        )
