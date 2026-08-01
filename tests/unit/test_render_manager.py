from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.jobs import JobState, SQLiteJobStore
from blender_mcp.models import ResultEnvelope, StructuredError
from blender_mcp.postproduction import RemotionRenderResult
from blender_mcp.rendering import RenderJobConflict, RenderJobManager


class FakeLauncher:
    def invoke(
        self,
        operation: object,
        payload: dict[str, object],
        *,
        request_id: str,
        timeout_seconds: float | None = None,
        cancel_event: object | None = None,
    ) -> ResultEnvelope[dict]:
        manifest = Path(str(payload["artifact_path"]))
        frames = manifest.with_suffix("")
        frames.mkdir()
        frame = frames / "frame_0001.png"
        frame.write_bytes(b"png")
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
                            "path": f"{frames.name}/{frame.name}",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return ResultEnvelope[dict](
            request_id=request_id,
            command=operation,
            ok=True,
            data={},
            timing={"execution_ms": 1},
        )


class FakeRenderer:
    def __init__(self) -> None:
        self.output_paths: list[Path] = []

    def render(
        self,
        manifest_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float,
        cancel_event: object | None = None,
    ) -> RemotionRenderResult:
        self.output_paths.append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"mp4")
        return RemotionRenderResult(
            output_path=output_path,
            size_bytes=3,
            sha256=hashlib.sha256(b"mp4").hexdigest(),
            duration_seconds=1 / 24,
        )


class PartialFailingRenderer(FakeRenderer):
    def render(
        self,
        manifest_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float,
        cancel_event: object | None = None,
    ) -> RemotionRenderResult:
        self.output_paths.append(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"partial")
        raise RuntimeError("synthetic encoding failure")


class RacingOutputRenderer(FakeRenderer):
    def __init__(self, public_output: Path) -> None:
        super().__init__()
        self.public_output = public_output

    def render(
        self,
        manifest_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float,
        cancel_event: object | None = None,
    ) -> RemotionRenderResult:
        result = super().render(
            manifest_path,
            output_path,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        self.public_output.parent.mkdir(parents=True, exist_ok=True)
        self.public_output.write_bytes(b"user-owned")
        return result


class CancellingRenderer(FakeRenderer):
    def render(
        self,
        manifest_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float,
        cancel_event: object | None = None,
    ) -> RemotionRenderResult:
        result = super().render(
            manifest_path,
            output_path,
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        assert cancel_event is not None
        cancel_event.set()  # type: ignore[attr-defined]
        return result


class FailingLauncher:
    def invoke(
        self,
        operation: object,
        payload: dict[str, object],
        *,
        request_id: str,
        timeout_seconds: float | None = None,
        cancel_event: object | None = None,
    ) -> ResultEnvelope[dict]:
        return ResultEnvelope[dict](
            request_id=request_id,
            command=operation,
            ok=False,
            error=StructuredError(
                code="BLENDER_RENDER_FAILED",
                message="synthetic render failure",
            ),
            timing={"execution_ms": 1},
        )


def _config(tmp_path: Path) -> BlenderMCPConfig:
    executable = tmp_path / "Blender"
    executable.write_bytes(b"fake")
    adapter = tmp_path / "adapter.py"
    adapter.write_text("", encoding="utf-8")
    return BlenderMCPConfig(
        project_root=tmp_path,
        blender_executable=executable,
        adapter_script=adapter,
        min_free_disk_gb=0,
    )


def test_render_manager_persists_blender_to_remotion_success(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"scene")
    store = SQLiteJobStore(config.state_db)
    renderer = FakeRenderer()
    manager = RenderJobManager(
        config,
        launcher=FakeLauncher(),
        renderer=renderer,
        store=store,
    )
    try:
        queued = manager.start(
            scene,
            tmp_path / "renders/final.mp4",
            timeout_seconds=30,
            idempotency_key="episode-final",
            allow_overwrite=False,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = manager.get(queued.job_id)
            assert current is not None
            if current.state in {
                JobState.SUCCEEDED,
                JobState.FAILED,
            }:
                break
            time.sleep(0.01)

        assert current.state is JobState.SUCCEEDED
        assert current.metadata["artifact"]["path"] == "renders/final.mp4"
        assert (tmp_path / "renders/final.mp4").read_bytes() == b"mp4"
        assert (tmp_path / "renders/final.manifest.json").is_file()
        assert len(renderer.output_paths) == 1
        assert renderer.output_paths[0].is_relative_to(config.jobs_dir)
        assert renderer.output_paths[0] != tmp_path / "renders/final.mp4"
        assert not renderer.output_paths[0].exists()
        assert any(
            event["message"] == "Encoding MP4 with Remotion"
            for event in manager.events(queued.job_id)
        )
        assert manager.start(
            scene,
            tmp_path / "renders/ignored.mp4",
            timeout_seconds=30,
            idempotency_key="episode-final",
            allow_overwrite=False,
        ).job_id == queued.job_id
    finally:
        manager.shutdown()


def test_render_manager_does_not_overwrite_output_created_after_enqueue(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"scene")
    output = tmp_path / "renders/final.mp4"
    renderer = RacingOutputRenderer(output)
    manager = RenderJobManager(
        config,
        launcher=FakeLauncher(),
        renderer=renderer,
    )
    try:
        queued = manager.start(
            scene,
            output,
            timeout_seconds=30,
            idempotency_key="race-output",
            allow_overwrite=False,
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = manager.get(queued.job_id)
            assert current is not None
            if current.state is JobState.FAILED:
                break
            time.sleep(0.01)

        assert current.state is JobState.FAILED
        assert output.read_bytes() == b"user-owned"
        assert len(renderer.output_paths) == 1
        assert not renderer.output_paths[0].exists()
    finally:
        manager.shutdown()


def test_render_manager_cleans_private_partial_mp4_after_encoder_failure(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"scene")
    renderer = PartialFailingRenderer()
    manager = RenderJobManager(
        config,
        launcher=FakeLauncher(),
        renderer=renderer,
    )
    try:
        queued = manager.start(
            scene,
            tmp_path / "renders/final.mp4",
            timeout_seconds=30,
            idempotency_key="partial-failure",
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = manager.get(queued.job_id)
            assert current is not None
            if current.state is JobState.FAILED:
                break
            time.sleep(0.01)

        assert current.state is JobState.FAILED
        assert len(renderer.output_paths) == 1
        assert not renderer.output_paths[0].exists()
        assert not (tmp_path / "renders/final.mp4").exists()
    finally:
        manager.shutdown()


def test_render_manager_cleans_private_mp4_when_cancelled_after_encoding(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"scene")
    renderer = CancellingRenderer()
    manager = RenderJobManager(
        config,
        launcher=FakeLauncher(),
        renderer=renderer,
    )
    try:
        queued = manager.start(
            scene,
            tmp_path / "renders/final.mp4",
            timeout_seconds=30,
            idempotency_key="cancel-after-encoding",
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = manager.get(queued.job_id)
            assert current is not None
            if current.state is JobState.CANCELLED:
                break
            time.sleep(0.01)

        assert current.state is JobState.CANCELLED
        assert len(renderer.output_paths) == 1
        assert not renderer.output_paths[0].exists()
        assert not (tmp_path / "renders/final.mp4").exists()
    finally:
        manager.shutdown()


def test_render_manager_rejects_active_output_conflict(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"scene")
    store = SQLiteJobStore(config.state_db)
    manager = RenderJobManager(config, store=store)
    try:
        store.create_job(
            "final_render",
            metadata={"output_path": "renders/final.mp4"},
        )
        try:
            manager.start(
                scene,
                tmp_path / "renders/final.mp4",
                timeout_seconds=30,
                idempotency_key="different-request",
            )
        except RenderJobConflict as exc:
            assert "renders/final.mp4" in str(exc)
        else:
            raise AssertionError("active output conflict was not rejected")
    finally:
        manager.shutdown()


def test_render_manager_records_launcher_failure(tmp_path: Path) -> None:
    config = _config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"scene")
    manager = RenderJobManager(
        config,
        launcher=FailingLauncher(),
        renderer=FakeRenderer(),
    )
    try:
        queued = manager.start(
            scene,
            tmp_path / "renders/final.mp4",
            timeout_seconds=30,
            idempotency_key="failure-case",
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = manager.get(queued.job_id)
            assert current is not None
            if current.state is JobState.FAILED:
                break
            time.sleep(0.01)

        assert current.state is JobState.FAILED
        failure = manager.events(queued.job_id)[-1]
        assert failure["message"] == "Render pipeline failed"
        assert "BLENDER_RENDER_FAILED" in failure["details"]["error"]
    finally:
        manager.shutdown()


def test_render_manager_cancel_handles_missing_and_terminal_jobs(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    store = SQLiteJobStore(config.state_db)
    manager = RenderJobManager(config, store=store)
    try:
        assert manager.cancel("missing") is None

        record = store.create_job("final_render")
        store.transition_job(record.job_id, JobState.STARTING)
        store.transition_job(record.job_id, JobState.RUNNING)
        terminal = store.transition_job(record.job_id, JobState.SUCCEEDED)

        assert manager.cancel(record.job_id) == terminal
    finally:
        manager.shutdown()
