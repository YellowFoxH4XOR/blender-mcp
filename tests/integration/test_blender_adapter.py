from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPOSITORY_ROOT / "blender_adapter" / "bootstrap.py"
FIXTURE_GENERATOR = REPOSITORY_ROOT / "fixtures" / "generate_fixture.py"
UNSAFE_FIXTURE_GENERATOR = (
    REPOSITORY_ROOT / "fixtures" / "generate_unsafe_fixture.py"
)
BLENDER_CANDIDATES = (
    Path("/Applications/Blender.app/Contents/MacOS/Blender"),
    Path(shutil.which("blender") or ""),
)


def _blender() -> Path:
    configured = os.environ.get("BLENDER_EXECUTABLE")
    candidates = ((Path(configured),) if configured else ()) + BLENDER_CANDIDATES
    for candidate in candidates:
        if str(candidate) and candidate.is_file():
            return candidate
    pytest.skip("Blender executable is unavailable")


def _run_blender(arguments: list[str], timeout: int = 120) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        [str(_blender()), "--background", "--disable-autoexec", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode == 139:
        pytest.skip("Blender crashed in the current sandbox (USD Arch_ValidateAssumptions)")
    return completed


@pytest.fixture(scope="session")
def fixture_scene(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("blender-adapter") / "fixture.blend"
    completed = _run_blender(
        ["--factory-startup", "--python", str(FIXTURE_GENERATOR), "--", str(path)]
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert path.is_file(), completed.stdout + completed.stderr
    return path


@pytest.fixture(scope="session")
def unsafe_fixture_scene(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("blender-adapter-unsafe")
    path = root / "project" / "unsafe.blend"
    external = root / "outside-project"
    completed = _run_blender(
        [
            "--factory-startup",
            "--python",
            str(UNSAFE_FIXTURE_GENERATOR),
            "--",
            str(path),
            str(external),
        ]
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert path.is_file(), completed.stdout + completed.stderr
    return path


def _invoke(tmp_path: Path, request: dict[str, object]) -> tuple[subprocess.CompletedProcess[str], dict]:
    request_path = tmp_path / "request.json"
    result_path = tmp_path / "result.json"
    request_path.write_text(json.dumps(request), encoding="utf-8")
    completed = _run_blender(
        ["--factory-startup", "--python", str(BOOTSTRAP), "--", str(request_path), str(result_path)]
    )
    assert result_path.is_file(), completed.stdout + completed.stderr
    return completed, json.loads(result_path.read_text(encoding="utf-8"))


def _request(command: str, payload: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "request_id": f"req_{command}",
        "command": command,
        "payload": payload or {},
    }


def test_status_contract(tmp_path: Path) -> None:
    completed, result = _invoke(tmp_path, _request("status"))
    assert completed.returncode == 0
    assert result["schema_version"] == "1.0"
    assert result["request_id"] == "req_status"
    assert result["command"] == "status"
    assert result["ok"] is True
    assert result["data"]["background"] is True
    assert result["data"]["commands"] == [
        "apply_scene_transaction",
        "inspect_scene",
        "render_animation",
        "render_preview",
        "status",
        "validate_scene",
    ]
    assert result["warnings"] == []
    assert result["artifacts"] == []
    assert result["timing"]["execution_ms"] >= 0


def test_inspect_scene_returns_bounded_objects_and_stable_ids(
    tmp_path: Path, fixture_scene: Path
) -> None:
    completed, result = _invoke(
        tmp_path,
        _request("inspect_scene", {"scene_path": str(fixture_scene), "max_objects": 2}),
    )
    assert completed.returncode == 0
    assert result["ok"] is True
    data = result["data"]
    assert data["scene_name"] == "M0 Fixture"
    assert data["object_count"] == 3
    assert data["objects_returned"] == 2
    assert data["active_camera"]["identity"] == {
        "id": "fixture_camera_v1",
        "source": "custom_property",
        "stable": True,
    }
    assert data["frame"]["start"] == 1
    assert data["frame"]["end"] == 24
    assert data["render"]["resolution_x"] == 96
    assert result["warnings"]


def test_render_preview_is_atomic_and_does_not_modify_source(
    tmp_path: Path, fixture_scene: Path
) -> None:
    before = hashlib.sha256(fixture_scene.read_bytes()).hexdigest()
    artifact_path = tmp_path / "preview.png"
    completed, result = _invoke(
        tmp_path,
        _request(
            "render_preview",
            {
                "scene_path": str(fixture_scene),
                "artifact_path": str(artifact_path),
                "max_width": 64,
                "max_height": 64,
                "samples": 1,
            },
        ),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert result["ok"] is True
    assert artifact_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert result["artifacts"][0]["path"] == str(artifact_path)
    assert result["artifacts"][0]["sha256"] == hashlib.sha256(
        artifact_path.read_bytes()
    ).hexdigest()
    assert hashlib.sha256(fixture_scene.read_bytes()).hexdigest() == before


def test_unknown_command_is_rejected_without_execution(tmp_path: Path) -> None:
    completed, result = _invoke(tmp_path, _request("execute_python", {"code": "boom()"}))
    assert completed.returncode == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "COMMAND_NOT_ALLOWED"


def test_unknown_payload_field_is_rejected(tmp_path: Path, fixture_scene: Path) -> None:
    completed, result = _invoke(
        tmp_path,
        _request(
            "inspect_scene",
            {"scene_path": str(fixture_scene), "script_path": "/tmp/untrusted.py"},
        ),
    )
    assert completed.returncode == 2
    assert result["ok"] is False
    assert result["error"]["code"] == "INVALID_PAYLOAD"


def test_validate_scene_returns_revision_and_render_readiness(
    tmp_path: Path, fixture_scene: Path
) -> None:
    completed, result = _invoke(
        tmp_path,
        _request("validate_scene", {"scene_path": str(fixture_scene)}),
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert result["ok"] is True
    assert result["data"]["ready_for_render"] is True
    assert result["data"]["revision"].startswith("sha256:")
    assert result["data"]["findings"] == []


def test_external_files_and_compositor_outputs_block_rendering(
    tmp_path: Path,
    unsafe_fixture_scene: Path,
) -> None:
    completed, validation = _invoke(
        tmp_path,
        _request(
            "validate_scene",
            {"scene_path": str(unsafe_fixture_scene)},
        ),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert validation["data"]["ready_for_render"] is False
    assert {
        finding["code"] for finding in validation["data"]["findings"]
    } == {
        "EXTERNAL_FILE_REFERENCE",
        "COMPOSITOR_FILE_OUTPUT",
    }

    completed, render = _invoke(
        tmp_path,
        _request(
            "render_preview",
            {
                "scene_path": str(unsafe_fixture_scene),
                "artifact_path": str(tmp_path / "blocked.png"),
            },
        ),
    )
    assert completed.returncode == 2
    assert render["error"]["code"] == "SCENE_SECURITY_BLOCKED"
    assert not (tmp_path / "blocked.png").exists()


def test_render_animation_produces_frame_sequence_without_mutating_source(
    tmp_path: Path, fixture_scene: Path
) -> None:
    before = hashlib.sha256(fixture_scene.read_bytes()).hexdigest()
    artifact_path = tmp_path / "clip.frames.json"

    completed, result = _invoke(
        tmp_path,
        _request(
            "render_animation",
            {
                "scene_path": str(fixture_scene),
                "artifact_path": str(artifact_path),
                "frame_start": 1,
                "frame_end": 2,
                "max_width": 64,
                "max_height": 64,
            },
        ),
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert result["ok"] is True
    assert artifact_path.is_file()
    assert artifact_path.stat().st_size > 0
    manifest = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert len(manifest["frames"]) == 2
    assert all((tmp_path / frame["path"]).is_file() for frame in manifest["frames"])
    assert result["artifacts"][0]["kind"] == "frame_sequence"
    assert result["artifacts"][0]["media_type"] == "application/json"
    assert result["artifacts"][0]["frame_count"] == 2
    assert hashlib.sha256(fixture_scene.read_bytes()).hexdigest() == before


def test_apply_scene_transaction_writes_a_new_scene_atomically(
    tmp_path: Path, fixture_scene: Path
) -> None:
    before = hashlib.sha256(fixture_scene.read_bytes()).hexdigest()
    output = tmp_path / "transaction-result.blend"

    completed, result = _invoke(
        tmp_path,
        _request(
            "apply_scene_transaction",
            {
                "scene_path": str(fixture_scene),
                "output_path": str(output),
                "transaction_id": "txn_fixture_move",
                "expected_source_sha256": before,
                "operations": [
                    {
                        "op": "set_transform",
                        "object_id": "fixture_cube_v1",
                        "location": [1.0, 2.0, 3.0],
                    }
                ],
            },
        ),
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert result["ok"] is True
    assert output.is_file()
    assert hashlib.sha256(fixture_scene.read_bytes()).hexdigest() == before

    inspection_dir = tmp_path / "inspection"
    inspection_dir.mkdir()
    _, inspection = _invoke(
        inspection_dir,
        _request("inspect_scene", {"scene_path": str(output)}),
    )
    cube = next(
        item
        for item in inspection["data"]["objects"]
        if item["identity"]["id"] == "fixture_cube_v1"
    )
    assert cube["transform"]["location"] == [1.0, 2.0, 3.0]
