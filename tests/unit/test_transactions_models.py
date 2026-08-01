import math

import pytest
from pydantic import ValidationError

from blender_mcp.transactions import SceneTransaction


def test_transaction_accepts_only_constrained_operations() -> None:
    transaction = SceneTransaction.model_validate(
        {
            "transaction_id": "txn_character_intro_001",
            "expected_scene_revision": "a" * 64,
            "operations": [
                {
                    "op": "set_transform",
                    "object_id": "character.hero",
                    "location": [1.0, 2.0, 3.0],
                },
                {
                    "op": "set_visibility",
                    "object_id": "character.hero",
                    "render": False,
                },
                {
                    "op": "configure_scene",
                    "frame_start": 1,
                    "frame_end": 48,
                    "fps": 24,
                },
                {
                    "op": "apply_action",
                    "object_id": "character.hero",
                    "action_id": "actions.wave",
                    "frame_start": 12,
                },
            ],
        }
    )

    assert [operation.op for operation in transaction.operations] == [
        "set_transform",
        "set_visibility",
        "configure_scene",
        "apply_action",
    ]


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "execute_python", "code": "import os"},
        {
            "op": "set_transform",
            "object_id": "hero",
            "location": [0, 0, 0],
            "script": "boom()",
        },
        {"op": "set_transform", "object_id": "hero"},
        {"op": "set_visibility", "object_id": "hero"},
        {"op": "configure_scene", "frame_start": 20, "frame_end": 10},
    ],
)
def test_transaction_rejects_unknown_or_incomplete_operations(operation: dict) -> None:
    with pytest.raises(ValidationError):
        SceneTransaction.model_validate(
            {
                "transaction_id": "txn_rejected",
                "expected_scene_revision": "a" * 64,
                "operations": [operation],
            }
        )


def test_transaction_caps_operation_count() -> None:
    operation = {
        "op": "set_visibility",
        "object_id": "hero",
        "render": True,
    }

    with pytest.raises(ValidationError):
        SceneTransaction.model_validate(
            {
                "transaction_id": "txn_too_large",
                "expected_scene_revision": "a" * 64,
                "operations": [operation] * 101,
            }
        )


def test_transaction_rejects_non_finite_transform_values() -> None:
    with pytest.raises(ValidationError):
        SceneTransaction.model_validate(
            {
                "transaction_id": "txn_non_finite",
                "expected_scene_revision": "a" * 64,
                "operations": [
                    {
                        "op": "set_transform",
                        "object_id": "hero",
                        "location": [math.nan, 0, 0],
                    }
                ],
            }
        )
