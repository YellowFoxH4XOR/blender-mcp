from __future__ import annotations

from blender_mcp.models.envelopes import (
    AdapterOperation,
    ArtifactReference,
    RequestEnvelope,
)
from blender_mcp.errors import ErrorCode


def test_transaction_command_is_a_versioned_allowlisted_operation() -> None:
    request = RequestEnvelope[dict[str, object]](
        request_id="req_transaction_1",
        command=AdapterOperation.APPLY_SCENE_TRANSACTION,
        payload={"operations": []},
    )

    assert request.command == "apply_scene_transaction"
    assert request.schema_version == "1.0"


def test_adapter_artifacts_support_scene_video_and_manifest_outputs() -> None:
    for kind, media_type, path in (
        ("scene", "application/x-blender", "/project/scenes/example.blend"),
        ("render", "video/mp4", "/project/renders/example.mp4"),
        ("manifest", "application/json", "/project/renders/example.json"),
    ):
        artifact = ArtifactReference(
            kind=kind,
            path=path,
            media_type=media_type,
            size_bytes=1,
            sha256="a" * 64,
        )
        assert artifact.kind == kind


def test_v1_errors_have_stable_public_codes() -> None:
    required = {
        "BLENDER_VERSION_UNSUPPORTED",
        "DISK_SPACE_INSUFFICIENT",
        "SCENE_REVISION_CONFLICT",
        "TRANSACTION_VALIDATION_FAILED",
        "TRANSACTION_ROLLED_BACK",
        "SCENE_VALIDATION_BLOCKED",
        "ASSET_NOT_APPROVED",
        "RENDER_BUDGET_EXCEEDED",
        "JOB_NOT_FOUND",
        "JOB_STATE_CONFLICT",
        "OUTPUT_QUARANTINED",
    }

    assert required.issubset({code.value for code in ErrorCode})
