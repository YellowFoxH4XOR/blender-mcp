import pytest

from blender_mcp.transactions import (
    InMemoryTransactionIdempotency,
    SceneTransaction,
    TransactionIdConflict,
)


def _transaction(*, render: bool) -> SceneTransaction:
    return SceneTransaction.model_validate(
        {
            "transaction_id": "txn_visibility_001",
            "expected_scene_revision": "a" * 64,
            "operations": [
                {
                    "op": "set_visibility",
                    "object_id": "character.hero",
                    "render": render,
                }
            ],
        }
    )


def test_idempotency_executes_once_and_replays_the_receipt() -> None:
    coordinator = InMemoryTransactionIdempotency()
    executions = 0

    def execute() -> dict[str, str]:
        nonlocal executions
        executions += 1
        return {"scene_path": "scenes/output.blend"}

    first = coordinator.execute_once(_transaction(render=True), execute)
    replayed = coordinator.execute_once(_transaction(render=True), execute)

    assert first.replayed is False
    assert replayed.replayed is True
    assert replayed.value == first.value
    assert executions == 1


def test_idempotency_rejects_reuse_with_different_payload() -> None:
    coordinator = InMemoryTransactionIdempotency()
    coordinator.execute_once(_transaction(render=True), lambda: {"ok": "yes"})

    with pytest.raises(TransactionIdConflict):
        coordinator.execute_once(_transaction(render=False), lambda: {"ok": "no"})
