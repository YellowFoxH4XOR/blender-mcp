from pathlib import Path

import pytest

from blender_mcp.errors import ErrorCode
from blender_mcp.policy.paths import PathPolicyError, ProjectPathPolicy
from blender_mcp.transactions import (
    TemplateCopyRequest,
    plan_template_copy,
)


def test_template_copy_plan_resolves_safe_project_paths(tmp_path: Path) -> None:
    template = tmp_path / "templates" / "character.blend"
    template.parent.mkdir()
    template.write_bytes(b"BLENDER")
    policy = ProjectPathPolicy(tmp_path)

    plan = plan_template_copy(
        policy,
        TemplateCopyRequest(
            template_path="templates/character.blend",
            destination_path="scenes/episode-01.blend",
        ),
    )

    assert plan.source == template
    assert plan.destination == tmp_path / "scenes" / "episode-01.blend"
    assert plan.overwrite_existing is False
    assert not plan.destination.exists()


def test_template_copy_plan_enforces_traversal_and_overwrite_policy(tmp_path: Path) -> None:
    template = tmp_path / "template.blend"
    template.write_bytes(b"BLENDER")
    destination = tmp_path / "scene.blend"
    destination.write_bytes(b"EXISTING")
    policy = ProjectPathPolicy(tmp_path)

    with pytest.raises(PathPolicyError) as collision:
        plan_template_copy(
            policy,
            TemplateCopyRequest(
                template_path="template.blend",
                destination_path="scene.blend",
            ),
        )
    assert collision.value.code == ErrorCode.PATH_ALREADY_EXISTS

    with pytest.raises(PathPolicyError) as traversal:
        plan_template_copy(
            policy,
            TemplateCopyRequest(
                template_path="template.blend",
                destination_path="../escaped.blend",
                allow_overwrite=True,
            ),
        )
    assert traversal.value.code == ErrorCode.INVALID_PATH
