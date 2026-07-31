import hashlib
from pathlib import Path

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.errors import BlenderMCPError, ErrorCode
from blender_mcp.headless import ProcessResult
from blender_mcp.models import ResultEnvelope
from blender_mcp.services import BlenderService


def make_config(tmp_path: Path) -> BlenderMCPConfig:
    executable = tmp_path / "Blender"
    executable.write_bytes(b"fake")
    adapter = tmp_path / "blender_adapter" / "bootstrap.py"
    adapter.parent.mkdir()
    adapter.write_text("# adapter")
    return BlenderMCPConfig(
        project_root=tmp_path,
        blender_executable=executable,
        adapter_script=adapter,
    )


class FakeLauncher:
    def probe(self) -> ProcessResult:
        return ProcessResult("Blender 5.2.0 LTS\n", "", 0, 0.01)

    def invoke(
        self,
        operation: object,
        payload: dict[str, object],
        *,
        request_id: str,
        timeout_seconds: float | None = None,
    ) -> ResultEnvelope[dict]:
        if str(operation) == "render_preview":
            Path(str(payload["artifact_path"])).write_bytes(b"preview")
        return ResultEnvelope[dict](
            request_id=request_id,
            command=operation,
            ok=True,
            data={"objects": [{"name": "Cube"}]},
            timing={"execution_ms": 1},
        )


class FailingLauncher:
    def probe(self) -> ProcessResult:
        raise BlenderMCPError(ErrorCode.BLENDER_NOT_FOUND, "missing")


def test_status_returns_typed_success(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    response = BlenderService(config, launcher=FakeLauncher()).get_status(
        request_id="status-1"
    )

    assert response.ok
    assert response.request_id == "status-1"
    assert response.result is not None
    assert response.result.version == "Blender 5.2.0 LTS"


def test_expected_failure_is_structured(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    response = BlenderService(config, launcher=FailingLauncher()).get_status(
        request_id="status-1"
    )

    assert not response.ok
    assert response.error is not None
    assert response.error.code == ErrorCode.BLENDER_NOT_FOUND.value


def test_inspect_rejects_paths_outside_project_before_launch(tmp_path: Path) -> None:
    config = make_config(tmp_path)

    response = BlenderService(config, launcher=FakeLauncher()).inspect_scene(
        "../escape.blend",
        request_id="inspect-1",
    )

    assert not response.ok
    assert response.error is not None
    assert response.error.code == ErrorCode.INVALID_PATH.value


def test_render_preview_returns_verified_artifact(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"scene")

    response = BlenderService(config, launcher=FakeLauncher()).render_preview(
        "scene.blend",
        "renders/preview.png",
        request_id="req_render_1",
    )

    assert response.ok
    assert response.result is not None
    assert response.result.path == "renders/preview.png"
    assert response.result.sha256 == hashlib.sha256(b"preview").hexdigest()


def test_render_preview_rejects_invalid_dimensions(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (tmp_path / "scene.blend").write_bytes(b"scene")

    response = BlenderService(config, launcher=FakeLauncher()).render_preview(
        "scene.blend",
        "renders/preview.png",
        max_width=0,
        request_id="req_invalid_dimensions",
    )

    assert not response.ok
    assert response.error is not None
    assert response.error.code == ErrorCode.INVALID_REQUEST.value
