"""Safe, deterministic scene transaction primitives."""

from blender_mcp.transactions.idempotency import (
    IdempotentExecution,
    InMemoryTransactionIdempotency,
    TransactionIdConflict,
    TransactionIdempotency,
    transaction_fingerprint,
)
from blender_mcp.transactions.models import (
    ApplyActionOperation,
    ConfigureSceneOperation,
    SceneOperation,
    SceneTransaction,
    SetTransformOperation,
    SetVisibilityOperation,
)
from blender_mcp.transactions.revision import SceneRevision, compute_scene_revision
from blender_mcp.transactions.templates import (
    TemplateCopyPlan,
    TemplateCopyRequest,
    plan_template_copy,
)

__all__ = [
    "ApplyActionOperation",
    "ConfigureSceneOperation",
    "IdempotentExecution",
    "InMemoryTransactionIdempotency",
    "SceneOperation",
    "SceneRevision",
    "SceneTransaction",
    "SetTransformOperation",
    "SetVisibilityOperation",
    "TemplateCopyPlan",
    "TemplateCopyRequest",
    "TransactionIdConflict",
    "TransactionIdempotency",
    "compute_scene_revision",
    "plan_template_copy",
    "transaction_fingerprint",
]
