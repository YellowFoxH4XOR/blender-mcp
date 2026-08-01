"""Asynchronous Blender-to-Remotion render pipeline."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from typing import Any

from blender_mcp.artifacts import ArtifactManifestWriter
from blender_mcp.config import BlenderMCPConfig
from blender_mcp.headless import BlenderLauncher
from blender_mcp.jobs import JobRecord, JobState, SQLiteJobStore
from blender_mcp.models import AdapterOperation
from blender_mcp.postproduction import RemotionRenderer


class RenderJobConflict(RuntimeError):
    pass


class RenderJobManager:
    """Own one bounded worker pool and durable render state."""

    def __init__(
        self,
        config: BlenderMCPConfig,
        *,
        launcher: BlenderLauncher | None = None,
        renderer: RemotionRenderer | None = None,
        store: SQLiteJobStore | None = None,
    ) -> None:
        self.config = config
        self.launcher = launcher or BlenderLauncher(config)
        self.renderer = renderer or RemotionRenderer(
            config.project_root,
            remotion_project=config.remotion_project,
            node_executable=config.node_executable,
        )
        self.store = store or SQLiteJobStore(config.state_db)
        self.store.recover_orphaned_jobs()
        self._executor = ThreadPoolExecutor(
            max_workers=config.concurrent_jobs,
            thread_name_prefix="blender-mcp-render",
        )
        self._cancellations: dict[str, Event] = {}
        self._lock = Lock()

    def start(
        self,
        scene_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float,
        idempotency_key: str,
        allow_overwrite: bool = False,
    ) -> JobRecord:
        existing_jobs = self.store.list_jobs(limit=1000)
        if idempotency_key:
            for existing in existing_jobs:
                if existing.metadata.get("idempotency_key") == idempotency_key:
                    return existing
        relative_output = output_path.relative_to(
            self.config.project_root
        ).as_posix()
        for existing in existing_jobs:
            if (
                existing.state
                not in {
                    JobState.SUCCEEDED,
                    JobState.FAILED,
                    JobState.CANCELLED,
                    JobState.ORPHANED,
                }
                and existing.metadata.get("output_path") == relative_output
            ):
                raise RenderJobConflict(
                    f"An active render already targets {relative_output}"
                )
        record = self.store.create_job(
            "final_render",
            metadata={
                "scene_path": scene_path.relative_to(
                    self.config.project_root
                ).as_posix(),
                "output_path": relative_output,
                "timeout_seconds": timeout_seconds,
                "idempotency_key": idempotency_key,
                "allow_overwrite": allow_overwrite,
            },
        )
        cancellation = Event()
        with self._lock:
            self._cancellations[record.job_id] = cancellation
        self._executor.submit(
            self._run,
            record.job_id,
            scene_path,
            output_path,
            timeout_seconds,
            cancellation,
            allow_overwrite,
        )
        return record

    def get(self, job_id: str) -> JobRecord | None:
        return self.store.get_job(job_id)

    def events(self, job_id: str) -> list[dict[str, Any]]:
        return [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "state": event.state.value,
                "from_state": (
                    event.from_state.value if event.from_state is not None else None
                ),
                "to_state": (
                    event.to_state.value if event.to_state is not None else None
                ),
                "message": event.message,
                "details": event.details,
                "created_at": event.created_at,
            }
            for event in self.store.list_events(job_id)
        ]

    def cancel(self, job_id: str) -> JobRecord | None:
        record = self.store.get_job(job_id)
        if record is None or record.state in {
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.ORPHANED,
        }:
            return record
        if record.state is not JobState.CANCELLING:
            record = self.store.transition_job(
                job_id,
                JobState.CANCELLING,
                message="Cancellation requested",
            )
        with self._lock:
            cancellation = self._cancellations.get(job_id)
        if cancellation is not None:
            cancellation.set()
        return record

    def shutdown(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _run(
        self,
        job_id: str,
        scene_path: Path,
        output_path: Path,
        timeout_seconds: float,
        cancellation: Event,
        allow_overwrite: bool,
    ) -> None:
        job_dir = self.config.jobs_dir / job_id
        frame_manifest = job_dir / "frames.json"
        staged_output = job_dir / "encoded.mp4"
        try:
            job_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            if cancellation.is_set():
                self._finish_cancelled(job_id)
                return
            self.store.transition_job(job_id, JobState.STARTING)
            if cancellation.is_set():
                self._finish_cancelled(job_id)
                return
            self.store.transition_job(
                job_id,
                JobState.RUNNING,
                message="Rendering Blender frame sequence",
            )
            result = self.launcher.invoke(
                AdapterOperation.RENDER_ANIMATION,
                {
                    "scene_path": str(scene_path),
                    "artifact_path": str(frame_manifest),
                    "max_width": self.config.max_render_width,
                    "max_height": self.config.max_render_height,
                },
                request_id=f"req_{job_id}",
                timeout_seconds=timeout_seconds,
                cancel_event=cancellation,
            )
            if not result.ok:
                assert result.error is not None
                raise RuntimeError(
                    f"{result.error.code}: {result.error.message}"
                )
            if cancellation.is_set():
                self._finish_cancelled(job_id)
                return
            self.store.append_event(
                job_id,
                "stage",
                message="Encoding MP4 with Remotion",
            )
            rendered = self.renderer.render(
                frame_manifest,
                staged_output,
                timeout_seconds=timeout_seconds,
                cancel_event=cancellation,
            )
            if cancellation.is_set():
                self._finish_cancelled(job_id)
                return
            try:
                rendered_path = rendered.output_path.resolve(strict=True)
            except OSError as exc:
                raise RuntimeError(
                    "Remotion did not return its private staged MP4"
                ) from exc
            if rendered_path != staged_output.resolve(strict=True):
                raise RuntimeError(
                    "Remotion returned an unexpected output path"
                )
            self._publish_staged_output(
                staged_output,
                output_path,
                allow_overwrite=allow_overwrite,
            )
            manifest_writer = ArtifactManifestWriter(self.config.project_root)
            artifact = manifest_writer.describe(
                output_path.relative_to(self.config.project_root),
                kind="render",
                media_type="video/mp4",
            )
            published = manifest_writer.write(
                output_path.with_suffix(".manifest.json").relative_to(
                    self.config.project_root
                ),
                artifacts=[artifact],
                job_id=job_id,
                metadata={
                    "scene_path": scene_path.relative_to(
                        self.config.project_root
                    ).as_posix(),
                    "frame_sequence_manifest": frame_manifest.relative_to(
                        self.config.project_root
                    ).as_posix(),
                    "duration_seconds": rendered.duration_seconds,
                },
                allow_overwrite=allow_overwrite,
            )
            self.store.transition_job(
                job_id,
                JobState.SUCCEEDED,
                message="Final MP4 render completed",
                metadata_patch={
                    "artifact": artifact.to_dict(),
                    "manifest": {
                        "path": published.path,
                        "sha256": published.sha256,
                        "size_bytes": published.size_bytes,
                    },
                },
            )
        except Exception as exc:
            current = self.store.get_job(job_id)
            if current is not None and current.state is JobState.CANCELLING:
                self._finish_cancelled(job_id)
            elif current is not None and current.state not in {
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.SUCCEEDED,
                JobState.ORPHANED,
            }:
                self.store.transition_job(
                    job_id,
                    JobState.FAILED,
                    message="Render pipeline failed",
                    details={"error": str(exc)[-4000:]},
                )
        finally:
            staged_output.unlink(missing_ok=True)
            with self._lock:
                self._cancellations.pop(job_id, None)

    def _publish_staged_output(
        self,
        staged_output: Path,
        output_path: Path,
        *,
        allow_overwrite: bool,
    ) -> None:
        """Durably publish one job-local file without a check-then-write race."""
        output_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        output_parent = output_path.parent.resolve(strict=True)
        if not output_parent.is_relative_to(self.config.project_root):
            raise RuntimeError("Render output parent resolves outside project root")
        destination = output_parent / output_path.name

        with staged_output.open("rb") as staged_file:
            os.fsync(staged_file.fileno())

        if allow_overwrite:
            os.replace(staged_output, destination)
        else:
            try:
                os.link(staged_output, destination)
            except FileExistsError as exc:
                relative = destination.relative_to(self.config.project_root)
                raise RenderJobConflict(
                    f"Render output already exists: {relative.as_posix()}"
                ) from exc
            staged_output.unlink()
        self._fsync_directory(output_parent)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _finish_cancelled(self, job_id: str) -> None:
        current = self.store.get_job(job_id)
        if current is None or current.state is JobState.CANCELLED:
            return
        if current.state is not JobState.CANCELLING:
            self.store.transition_job(job_id, JobState.CANCELLING)
        self.store.transition_job(
            job_id,
            JobState.CANCELLED,
            message="Render job cancelled",
        )
