from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from blender_mcp.postproduction import RemotionRenderer


def _renderer_fixture(
    tmp_path: Path,
) -> tuple[RemotionRenderer, Path, Path]:
    job = tmp_path / ".blender-mcp" / "jobs" / "job_1"
    frames = job / "frames"
    frames.mkdir(parents=True)
    frame = frames / "frame_0001.png"
    frame.write_bytes(b"png")
    manifest = job / "frames.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "kind": "blender_frame_sequence",
                "fps": 24,
                "width": 96,
                "height": 64,
                "frames": [
                    {
                        "frame": 1,
                        "path": "frames/frame_0001.png",
                        "size_bytes": frame.stat().st_size,
                        "sha256": hashlib.sha256(frame.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    remotion_project = tmp_path / "remotion"
    (remotion_project / "src").mkdir(parents=True)
    (remotion_project / "src/index.ts").write_text("", encoding="utf-8")
    cli = remotion_project / "node_modules/@remotion/cli/remotion-cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("", encoding="utf-8")
    node = tmp_path / "node"
    node.write_text("", encoding="utf-8")
    node.chmod(0o700)

    return (
        RemotionRenderer(
            tmp_path,
            remotion_project=remotion_project,
            node_executable=node,
        ),
        manifest,
        tmp_path / "renders/final.mp4",
    )


def test_remotion_renderer_loads_only_project_local_frame_sequences(
    tmp_path: Path,
) -> None:
    renderer, manifest, output = _renderer_fixture(tmp_path)
    sequence = renderer.load_sequence(manifest)
    props = manifest.parent / "props.json"
    props.write_text("{}", encoding="utf-8")
    command = renderer.build_command(sequence, output, props)

    assert sequence.frame_paths == (
        ".blender-mcp/jobs/job_1/frames/frame_0001.png",
    )
    assert sequence.fps == 24
    assert "--public-dir=" + str(tmp_path) in command
    assert all("shell" not in argument for argument in command)
    assert command[:2] == [
        str((tmp_path / "node").resolve()),
        str(
            (
                tmp_path
                / "remotion/node_modules/@remotion/cli/remotion-cli.js"
            ).resolve()
        ),
    ]
    assert "npm" not in command


def test_remotion_renderer_runs_without_shell_and_verifies_mp4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer, manifest, output = _renderer_fixture(tmp_path)
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 1234
        returncode = 0

        def __init__(self, command: list[str], **kwargs: object) -> None:
            observed["command"] = command
            observed["shell"] = kwargs.get("shell")
            output = next(Path(value) for value in command if value.endswith(".mp4"))
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"mp4")

        def communicate(self, timeout: float) -> tuple[str, str]:
            return "", ""

    monkeypatch.setattr(subprocess, "Popen", FakeProcess)

    result = renderer.render(
        manifest,
        output,
        timeout_seconds=30,
    )

    assert result.output_path == output
    assert result.size_bytes == 3
    assert result.duration_seconds == pytest.approx(1 / 24)
    assert observed["shell"] is None
    assert not manifest.with_suffix(".remotion-props.json").exists()


def test_remotion_renderer_rejects_frame_traversal(tmp_path: Path) -> None:
    renderer, manifest, _ = _renderer_fixture(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["frames"][0]["path"] = "../../outside.png"
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(Exception, match="project-local"):
        renderer.load_sequence(manifest)


@pytest.mark.parametrize("field", ["size_bytes", "sha256"])
def test_remotion_renderer_rejects_changed_frame_content(
    tmp_path: Path,
    field: str,
) -> None:
    renderer, manifest, _ = _renderer_fixture(tmp_path)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    if field == "size_bytes":
        document["frames"][0]["size_bytes"] += 1
    else:
        document["frames"][0]["sha256"] = "0" * 64
    manifest.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(Exception, match="integrity"):
        renderer.load_sequence(manifest)


def test_remotion_renderer_requires_pinned_local_cli(tmp_path: Path) -> None:
    project = tmp_path / "remotion"
    (project / "src").mkdir(parents=True)
    (project / "src/index.ts").write_text("", encoding="utf-8")
    node = tmp_path / "node"
    node.write_text("", encoding="utf-8")
    node.chmod(0o700)

    with pytest.raises(Exception, match="CLI"):
        RemotionRenderer(
            tmp_path,
            remotion_project=project,
            node_executable=node,
        )
