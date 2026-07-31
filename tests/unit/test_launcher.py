import json
import subprocess
from pathlib import Path

import pytest

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.errors import BlenderMCPError, ErrorCode
from blender_mcp.headless.launcher import BlenderLauncher
from blender_mcp.models import AdapterOperation


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


def test_launcher_uses_argument_array_and_validates_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed["command"] = command
        observed["shell"] = kwargs["shell"]
        request_path = Path(command[command.index("--request") + 1])
        result_path = Path(command[command.index("--result") + 1])
        request = json.loads(request_path.read_text())
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "request_id": request["request_id"],
                    "command": request["command"],
                    "ok": True,
                    "data": {"objects": []},
                    "warnings": [],
                    "artifacts": [],
                    "timing": {"execution_ms": 2.5},
                }
            )
        )
        return subprocess.CompletedProcess(command, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = BlenderLauncher(config).invoke(
        AdapterOperation.INSPECT_SCENE,
        {"scene_path": str(tmp_path / "fixture.blend")},
        request_id="req_1",
    )

    assert result.ok
    assert result.data == {"objects": []}
    assert observed["shell"] is False
    command = observed["command"]
    assert isinstance(command, list)
    assert command[:3] == [
        str(config.blender_executable),
        "--background",
        "--disable-autoexec",
    ]
    assert "--python" in command


def test_launcher_maps_timeout_to_stable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["Blender"], timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(BlenderMCPError) as raised:
        BlenderLauncher(config).invoke(
            AdapterOperation.INSPECT_SCENE,
            {},
            request_id="req_1",
            timeout_seconds=1,
        )

    assert raised.value.code == ErrorCode.BLENDER_TIMEOUT


def test_launcher_preserves_structured_adapter_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = make_config(tmp_path)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        request_path = Path(command[command.index("--request") + 1])
        result_path = Path(command[command.index("--result") + 1])
        request = json.loads(request_path.read_text())
        result_path.write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "request_id": request["request_id"],
                    "command": request["command"],
                    "ok": False,
                    "error": {
                        "code": "INVALID_PAYLOAD",
                        "message": "Payload rejected",
                        "details": {},
                    },
                    "warnings": [],
                    "artifacts": [],
                    "timing": {"execution_ms": 0.1},
                }
            )
        )
        return subprocess.CompletedProcess(command, 2, "", "rejected")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = BlenderLauncher(config).invoke(
        AdapterOperation.INSPECT_SCENE,
        {},
        request_id="req_rejected",
    )

    assert not result.ok
    assert result.error is not None
    assert result.error.code == "INVALID_PAYLOAD"


@pytest.mark.parametrize("returncode", [-11, 139])
def test_launcher_classifies_blender_sigsegv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
) -> None:
    config = make_config(tmp_path)

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            returncode,
            "",
            "Arch_ValidateAssumptions",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(BlenderMCPError) as raised:
        BlenderLauncher(config).invoke(
            AdapterOperation.INSPECT_SCENE,
            {},
            request_id="req_crashed",
        )

    assert raised.value.code == ErrorCode.BLENDER_CRASHED
    assert raised.value.details["returncode"] == returncode
