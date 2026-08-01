"""Semantic validation of a constrained transaction against scene metadata."""

from __future__ import annotations

from blender_mcp.transactions.models import (
    ApplyActionOperation,
    ConfigureSceneOperation,
    SceneTransaction,
)
from blender_mcp.validation.models import (
    FindingSeverity,
    SceneValidationContext,
    ValidationFinding,
    ValidationReport,
)


def validate_transaction(
    transaction: SceneTransaction,
    context: SceneValidationContext,
) -> ValidationReport:
    """Return all known preflight findings without mutating the scene."""

    findings: list[ValidationFinding] = []
    if transaction.expected_scene_revision != context.current_scene_revision:
        findings.append(
            ValidationFinding(
                severity=FindingSeverity.BLOCKER,
                code="STALE_SCENE_REVISION",
                message="The scene changed after the transaction was prepared.",
                details={
                    "expected": transaction.expected_scene_revision,
                    "current": context.current_scene_revision,
                },
            )
        )

    for index, operation in enumerate(transaction.operations):
        object_id = getattr(operation, "object_id", None)
        if object_id is not None and object_id not in context.object_ids:
            findings.append(
                ValidationFinding(
                    severity=FindingSeverity.ERROR,
                    code="OBJECT_NOT_FOUND",
                    message="The operation references an unknown object ID.",
                    operation_index=index,
                    subject_id=object_id,
                )
            )
        if (
            isinstance(operation, ApplyActionOperation)
            and operation.action_id not in context.action_ids
        ):
            findings.append(
                ValidationFinding(
                    severity=FindingSeverity.ERROR,
                    code="ACTION_NOT_FOUND",
                    message="The operation references an unknown approved action ID.",
                    operation_index=index,
                    subject_id=operation.action_id,
                )
            )

    configures_render = any(
        isinstance(operation, ConfigureSceneOperation)
        for operation in transaction.operations
    )
    if not context.has_active_camera and configures_render:
        severity = FindingSeverity.ERROR
    elif not context.has_active_camera:
        severity = FindingSeverity.WARNING
    else:
        severity = None
    if severity is not None:
        findings.append(
            ValidationFinding(
                severity=severity,
                code="NO_ACTIVE_CAMERA",
                message="Scene has no active camera.",
            )
        )
    return ValidationReport(findings=tuple(findings))
