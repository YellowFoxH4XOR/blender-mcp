from types import SimpleNamespace

import pytest

from blender_adapter.operations import OperationExecutionError, apply_operations


def _scene() -> SimpleNamespace:
    return SimpleNamespace(
        frame_start=1,
        frame_end=24,
        render=SimpleNamespace(
            fps=24,
            resolution_x=640,
            resolution_y=360,
            resolution_percentage=100,
            engine="BLENDER_EEVEE_NEXT",
        ),
    )


def test_dispatcher_applies_a_validated_operation_batch() -> None:
    scene = _scene()
    hero = SimpleNamespace(
        location=(0.0, 0.0, 0.0),
        rotation_euler=(0.0, 0.0, 0.0),
        scale=(1.0, 1.0, 1.0),
        hide_viewport=False,
        hide_render=False,
    )

    applied = apply_operations(
        scene,
        [
            {
                "op": "set_transform",
                "object_id": "character.hero",
                "location": [1.0, 2.0, 3.0],
            },
            {
                "op": "set_visibility",
                "object_id": "character.hero",
                "viewport": False,
                "render": True,
            },
            {
                "op": "configure_scene",
                "frame_start": 5,
                "frame_end": 48,
                "fps": 30,
                "resolution_x": 1920,
                "resolution_y": 1080,
            },
        ],
        objects_by_id={"character.hero": hero},
        actions_by_id={},
    )

    assert hero.location == (1.0, 2.0, 3.0)
    assert hero.hide_viewport is True
    assert hero.hide_render is False
    assert scene.frame_start == 5
    assert scene.frame_end == 48
    assert scene.render.fps == 30
    assert [item["op"] for item in applied] == [
        "set_transform",
        "set_visibility",
        "configure_scene",
    ]


def test_dispatcher_validates_entire_batch_before_mutation() -> None:
    scene = _scene()
    hero = SimpleNamespace(
        location=(0.0, 0.0, 0.0),
        rotation_euler=(0.0, 0.0, 0.0),
        scale=(1.0, 1.0, 1.0),
        hide_viewport=False,
        hide_render=False,
    )

    with pytest.raises(OperationExecutionError) as raised:
        apply_operations(
            scene,
            [
                {
                    "op": "set_transform",
                    "object_id": "character.hero",
                    "location": [9.0, 9.0, 9.0],
                },
                {
                    "op": "set_visibility",
                    "object_id": "character.hero",
                    "render": False,
                    "python": "not_allowed()",
                },
            ],
            objects_by_id={"character.hero": hero},
            actions_by_id={},
        )

    assert raised.value.code == "INVALID_OPERATION"
    assert raised.value.operation_index == 1
    assert hero.location == (0.0, 0.0, 0.0)


def test_dispatcher_rejects_unknown_object_before_mutation() -> None:
    with pytest.raises(OperationExecutionError) as raised:
        apply_operations(
            _scene(),
            [
                {
                    "op": "set_visibility",
                    "object_id": "character.missing",
                    "render": False,
                }
            ],
            objects_by_id={},
            actions_by_id={},
        )

    assert raised.value.code == "OBJECT_NOT_FOUND"
