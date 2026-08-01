import pytest
from pydantic import ValidationError

from blender_mcp.transactions import SceneTransaction
from blender_mcp.validation import (
    FindingSeverity,
    SceneValidationContext,
    ValidationFinding,
    ValidationReport,
    validate_transaction,
)


def test_validation_report_treats_only_error_findings_as_invalid() -> None:
    warning = ValidationFinding(
        severity=FindingSeverity.WARNING,
        code="NO_ACTIVE_CAMERA",
        message="Scene has no active camera.",
    )
    assert ValidationReport(findings=[warning]).valid is True

    error = ValidationFinding(
        severity=FindingSeverity.ERROR,
        code="OBJECT_NOT_FOUND",
        message="Object was not found.",
        operation_index=0,
        subject_id="character.missing",
    )
    report = ValidationReport(findings=[warning, error])

    assert report.valid is False
    assert report.error_count == 1
    assert report.warning_count == 1

    blocker = ValidationFinding(
        severity=FindingSeverity.BLOCKER,
        code="STALE_SCENE_REVISION",
        message="Scene changed.",
    )
    blocker_report = ValidationReport(findings=[blocker])
    assert blocker_report.valid is False
    assert blocker_report.blocker_count == 1
    assert blocker_report.model_dump()["valid"] is False

    with pytest.raises(ValidationError):
        ValidationFinding(
            severity="fatal",
            code="BAD_SEVERITY",
            message="Unsupported severity.",
        )


def test_transaction_validation_reports_stale_revision_and_missing_targets() -> None:
    transaction = SceneTransaction.model_validate(
        {
            "transaction_id": "txn_validate_001",
            "expected_scene_revision": "a" * 64,
            "operations": [
                {
                    "op": "set_transform",
                    "object_id": "character.missing",
                    "location": [0, 0, 0],
                },
                {
                    "op": "apply_action",
                    "object_id": "character.hero",
                    "action_id": "actions.missing",
                },
            ],
        }
    )
    context = SceneValidationContext(
        current_scene_revision="b" * 64,
        object_ids={"character.hero"},
        action_ids={"actions.idle"},
        has_active_camera=False,
    )

    report = validate_transaction(transaction, context)

    assert report.valid is False
    assert {finding.code for finding in report.findings} == {
        "STALE_SCENE_REVISION",
        "OBJECT_NOT_FOUND",
        "ACTION_NOT_FOUND",
        "NO_ACTIVE_CAMERA",
    }
    severities = {finding.code: finding.severity for finding in report.findings}
    assert severities["STALE_SCENE_REVISION"] is FindingSeverity.BLOCKER
    assert severities["OBJECT_NOT_FOUND"] is FindingSeverity.ERROR
    assert severities["ACTION_NOT_FOUND"] is FindingSeverity.ERROR
