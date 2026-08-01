import json
from pathlib import Path

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.doctor import run_doctor


def _postproduction_runtime(tmp_path: Path) -> tuple[Path, Path, Path]:
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nprintf 'v20.19.0\\n'\n")
    node.chmod(0o700)
    npm = tmp_path / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n")
    npm.chmod(0o700)
    remotion = tmp_path / "remotion"
    cli = remotion / "node_modules/@remotion/cli/remotion-cli.js"
    cli.parent.mkdir(parents=True)
    cli.touch()
    dependencies = {
        "@remotion/cli": "4.0.502",
        "remotion": "4.0.502",
    }
    (remotion / "package.json").write_text(
        json.dumps({"dependencies": dependencies})
    )
    (remotion / "package-lock.json").write_text(
        json.dumps({"lockfileVersion": 3, "packages": {"": {"dependencies": dependencies}}})
    )
    return node, npm, remotion


def test_doctor_reports_required_capabilities(tmp_path: Path) -> None:
    blender = tmp_path / "blender"
    blender.write_text(
        "#!/bin/sh\n"
        "printf 'Blender 5.2.0\\n'\n"
        "printf 'BLENDER_RUNTIME_READY 5.2.0\\n'\n"
    )
    blender.chmod(0o700)
    adapter = tmp_path / "bootstrap.py"
    adapter.touch()
    node, npm, remotion = _postproduction_runtime(tmp_path)
    config = BlenderMCPConfig(
        project_root=tmp_path,
        blender_executable=blender,
        adapter_script=adapter,
        node_executable=node,
        npm_executable=npm,
        remotion_project=remotion,
    )

    report = run_doctor(config)

    assert report.ok is True
    assert report.exit_code == 0
    assert {check.name for check in report.checks if check.ok} >= {
        "project_root",
        "blender_executable",
        "blender_version",
        "blender_runtime",
        "adapter",
        "node_runtime",
        "remotion_dependencies",
    }


def test_doctor_fails_when_pinned_remotion_dependencies_are_missing(
    tmp_path: Path,
) -> None:
    blender = tmp_path / "blender"
    blender.write_text(
        "#!/bin/sh\n"
        "printf 'Blender 5.2.0\\n'\n"
        "printf 'BLENDER_RUNTIME_READY 5.2.0\\n'\n"
    )
    blender.chmod(0o700)
    adapter = tmp_path / "bootstrap.py"
    adapter.touch()
    node = tmp_path / "node"
    node.write_text("#!/bin/sh\nprintf 'v20.19.0\\n'\n")
    node.chmod(0o700)
    npm = tmp_path / "npm"
    npm.write_text("#!/bin/sh\nexit 0\n")
    npm.chmod(0o700)
    remotion = tmp_path / "remotion"
    remotion.mkdir()
    config = BlenderMCPConfig(
        project_root=tmp_path,
        blender_executable=blender,
        adapter_script=adapter,
        node_executable=node,
        npm_executable=npm,
        remotion_project=remotion,
    )

    report = run_doctor(config)

    remotion_check = next(
        check for check in report.checks if check.name == "remotion_dependencies"
    )
    assert report.exit_code == 1
    assert remotion_check.ok is False
    assert "npm ci" in remotion_check.message


def test_doctor_returns_nonzero_when_required_adapter_is_missing(
    tmp_path: Path,
) -> None:
    blender = tmp_path / "blender"
    blender.write_text("#!/bin/sh\nprintf 'Blender 5.2.0\\n'\n")
    blender.chmod(0o700)
    config = BlenderMCPConfig(
        project_root=tmp_path,
        blender_executable=blender,
        adapter_script=tmp_path / "missing.py",
    )

    report = run_doctor(config)

    assert report.ok is False
    assert report.exit_code == 1
    assert next(check for check in report.checks if check.name == "adapter").ok is False


def test_doctor_rejects_version_only_blender_that_crashes_during_startup(
    tmp_path: Path,
) -> None:
    blender = tmp_path / "blender"
    blender.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then\n"
        "  printf 'Blender 5.2.0\\n'\n"
        "  exit 0\n"
        "fi\n"
        "exit 139\n"
    )
    blender.chmod(0o700)
    adapter = tmp_path / "bootstrap.py"
    adapter.touch()
    config = BlenderMCPConfig(
        project_root=tmp_path,
        blender_executable=blender,
        adapter_script=adapter,
    )

    report = run_doctor(config)

    runtime = next(
        check for check in report.checks if check.name == "blender_runtime"
    )
    assert report.ok is False
    assert runtime.ok is False
    assert "exit code 139" in runtime.message
