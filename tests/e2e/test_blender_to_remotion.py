from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.jobs import JobState
from blender_mcp.postproduction import RemotionRenderer
from blender_mcp.rendering import RenderJobManager
from blender_mcp.services import BlenderService

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ADAPTER = REPOSITORY_ROOT / "blender_adapter" / "bootstrap.py"
FIXTURE_GENERATOR = REPOSITORY_ROOT / "fixtures" / "generate_fixture.py"
REMOTION_PROJECT = REPOSITORY_ROOT / "remotion"


def _blender() -> Path:
    configured = os.environ.get("BLENDER_EXECUTABLE")
    candidates = (
        Path(configured) if configured else Path(""),
        Path("/Applications/Blender.app/Contents/MacOS/Blender"),
        Path(shutil.which("blender") or ""),
    )
    for candidate in candidates:
        if str(candidate) and candidate.is_file():
            return candidate
    pytest.skip("Blender executable is unavailable")


def test_scene_renders_through_remotion_to_durable_mp4(tmp_path: Path) -> None:
    if not (REMOTION_PROJECT / "node_modules").is_dir():
        pytest.skip("Remotion dependencies are not installed")
    scene = tmp_path / "templates" / "character-stage.blend"
    scene.parent.mkdir()
    generated = subprocess.run(
        [
            str(_blender()),
            "--background",
            "--factory-startup",
            "--python",
            str(FIXTURE_GENERATOR),
            "--",
            str(scene),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if generated.returncode in {-11, 139}:
        pytest.skip("Blender crashed in the current Codex sandbox")
    assert generated.returncode == 0, generated.stdout + generated.stderr
    catalog = tmp_path / "assets" / "catalog.toml"
    catalog.parent.mkdir()
    catalog.write_text(
        "\n".join(
            (
                'schema_version = "1"',
                "[[templates]]",
                'id = "character-stage-v1"',
                'path = "templates/character-stage.blend"',
                'version = "1.0.0"',
                'license = "CC0-1.0"',
            )
        ),
        encoding="utf-8",
    )

    config = BlenderMCPConfig(
        project_root=tmp_path,
        blender_executable=_blender(),
        adapter_script=ADAPTER,
        min_free_disk_gb=0,
        max_render_timeout_seconds=180,
    )
    manager = RenderJobManager(
        config,
        renderer=RemotionRenderer(
            tmp_path,
            remotion_project=REMOTION_PROJECT,
        ),
    )
    service = BlenderService(config, render_manager=manager)
    try:
        created = service.create_scene_from_template(
            "character-stage-v1",
            "scenes/episode.blend",
            request_id="req_e2e_create",
        )
        assert created.ok, created.error
        inspected = service.inspect_scene(
            "scenes/episode.blend",
            request_id="req_e2e_inspect",
        )
        assert inspected.ok, inspected.error
        assert inspected.result is not None
        transaction = service.apply_scene_transaction(
            "scenes/episode.blend",
            expected_revision=inspected.result["scene_revision"],
            transaction_id="txn_e2e_move_character",
            operations=[
                {
                    "op": "set_transform",
                    "object_id": "fixture_cube_v1",
                    "location": [0.5, 0.0, 0.0],
                }
            ],
            request_id="req_e2e_transaction",
        )
        assert transaction.ok, transaction.error
        validation = service.validate_scene(
            "scenes/episode.blend",
            request_id="req_e2e_validate",
        )
        assert validation.ok, validation.error
        assert validation.result is not None
        assert validation.result["ready_for_render"] is True
        preview = service.render_preview(
            "scenes/episode.blend",
            "previews/episode.png",
            max_width=96,
            max_height=64,
            request_id="req_e2e_preview",
        )
        assert preview.ok, preview.error

        started = service.start_render(
            "scenes/episode.blend",
            "renders/final.mp4",
            timeout_seconds=180,
            idempotency_key="e2e-fixture-v1",
            request_id="req_e2e_start",
        )
        assert started.ok, started.error
        assert started.result is not None
        job_id = started.result["job_id"]
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            response = service.get_job(job_id, request_id="req_e2e_poll")
            assert response.ok, response.error
            assert response.result is not None
            state = JobState(response.result["state"])
            if state in {
                JobState.SUCCEEDED,
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.ORPHANED,
            }:
                break
            time.sleep(0.1)

        assert state is JobState.SUCCEEDED, response.result
        output = tmp_path / "renders" / "final.mp4"
        assert output.is_file()
        assert output.stat().st_size > 100
        assert (tmp_path / "renders" / "final.manifest.json").is_file()
    finally:
        manager.shutdown()
