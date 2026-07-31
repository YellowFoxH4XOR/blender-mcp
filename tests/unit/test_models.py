import pytest
from pydantic import ValidationError

from blender_mcp.models import AdapterOperation, RequestEnvelope, ResultEnvelope


def test_request_envelope_is_versioned_and_forbids_unknown_fields() -> None:
    request = RequestEnvelope[dict](
        request_id="req_1",
        command=AdapterOperation.INSPECT_SCENE,
        payload={"scene_path": "scene.blend"},
    )

    assert request.schema_version == "1.0"
    with pytest.raises(ValidationError):
        RequestEnvelope[dict].model_validate(
            {
                **request.model_dump(),
                "unexpected": True,
            }
        )


def test_failed_result_requires_structured_error() -> None:
    with pytest.raises(ValidationError):
        ResultEnvelope[dict].model_validate(
            {
                "schema_version": "1.0",
                "request_id": "req_1",
                "command": "inspect_scene",
                "ok": False,
                "data": None,
                "warnings": [],
                "artifacts": [],
                "timing": {"execution_ms": 1},
            }
        )
