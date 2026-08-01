import hashlib
from datetime import UTC, datetime
from pathlib import Path

from blender_mcp.config import BlenderMCPConfig
from blender_mcp.errors import BlenderMCPError, ErrorCode
from blender_mcp.headless import ProcessResult
from blender_mcp.jobs import JobRecord, JobState
from blender_mcp.models import ResultEnvelope
from blender_mcp.services import BlenderService
from blender_mcp.transactions import compute_scene_revision


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
        min_free_disk_gb=0,
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
        if str(operation) == "apply_scene_transaction":
            Path(str(payload["output_path"])).write_bytes(b"mutated-scene")
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


class FakeRenderManager:
    def __init__(self) -> None:
        self.record: JobRecord | None = None

    def start(
        self,
        scene_path: Path,
        output_path: Path,
        *,
        timeout_seconds: float,
        idempotency_key: str,
        allow_overwrite: bool = False,
    ) -> JobRecord:
        now = datetime.now(UTC).isoformat()
        self.record = JobRecord(
            job_id="job_render_1",
            kind="final_render",
            state=JobState.QUEUED,
            metadata={
                "scene_path": scene_path.name,
                "output_path": output_path.relative_to(
                    scene_path.parent
                ).as_posix(),
            },
            created_at=now,
            updated_at=now,
        )
        return self.record

    def get(self, job_id: str) -> JobRecord | None:
        if self.record is not None and self.record.job_id == job_id:
            return self.record
        return None

    def events(self, job_id: str) -> list[dict[str, object]]:
        return []

    def cancel(self, job_id: str) -> JobRecord | None:
        record = self.get(job_id)
        if record is None:
            return None
        self.record = JobRecord(
            job_id=record.job_id,
            kind=record.kind,
            state=JobState.CANCELLING,
            metadata=record.metadata,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        return self.record


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


def test_inspect_includes_revision_for_optimistic_transactions(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"scene")

    response = BlenderService(config, launcher=FakeLauncher()).inspect_scene(
        "scene.blend",
        request_id="req_inspect_revision",
    )

    assert response.ok
    assert response.result is not None
    assert len(response.result["scene_revision"]) == 64
    assert response.result["scene_sha256"] == hashlib.sha256(b"scene").hexdigest()


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


def test_validate_scene_returns_structured_readiness(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (tmp_path / "scene.blend").write_bytes(b"scene")

    response = BlenderService(config, launcher=FakeLauncher()).validate_scene(
        "scene.blend",
        request_id="req_validate_1",
    )

    assert response.ok
    assert response.result == {"objects": [{"name": "Cube"}]}


def test_save_scene_as_copies_atomically_without_overwriting(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (tmp_path / "source.blend").write_bytes(b"blend-scene")
    service = BlenderService(config, launcher=FakeLauncher())

    response = service.save_scene_as(
        "source.blend",
        "scenes/copy.blend",
        request_id="req_save_1",
    )

    assert response.ok
    assert response.result is not None
    assert response.result.path == "scenes/copy.blend"
    assert (tmp_path / "scenes/copy.blend").read_bytes() == b"blend-scene"

    collision = service.save_scene_as(
        "source.blend",
        "scenes/copy.blend",
        request_id="req_save_2",
    )
    assert not collision.ok
    assert collision.error is not None
    assert collision.error.code == ErrorCode.PATH_ALREADY_EXISTS


def test_list_assets_and_create_scene_from_approved_template(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    template = tmp_path / "templates" / "base.blend"
    template.parent.mkdir()
    template.write_bytes(b"approved-template")
    catalog = tmp_path / "assets" / "catalog.toml"
    catalog.parent.mkdir()
    catalog.write_text(
        "\n".join(
            (
                'schema_version = "1"',
                "[[templates]]",
                'id = "character-stage-v1"',
                'path = "templates/base.blend"',
                'version = "1.0.0"',
                'license = "CC0-1.0"',
            )
        ),
        encoding="utf-8",
    )
    service = BlenderService(config, launcher=FakeLauncher())

    assets = service.list_project_assets(request_id="req_assets_1")
    assert assets.ok
    assert assets.result is not None
    assert assets.result["assets"][0]["id"] == "character-stage-v1"

    created = service.create_scene_from_template(
        "character-stage-v1",
        "scenes/episode.blend",
        request_id="req_template_1",
    )
    assert created.ok
    assert created.result is not None
    assert created.result.path == "scenes/episode.blend"
    assert (tmp_path / "scenes/episode.blend").read_bytes() == b"approved-template"

    configured = service.create_scene_from_template(
        "character-stage-v1",
        "scenes/configured.blend",
        variables={"fps": 30, "resolution": [1920, 1080]},
        request_id="req_template_2",
    )
    assert configured.ok
    assert (tmp_path / "scenes/configured.blend").read_bytes() == b"mutated-scene"


def test_transaction_is_revision_checked_checkpointed_and_idempotent(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"original-scene")
    revision = compute_scene_revision(
        scene,
        logical_path="scene.blend",
    ).revision
    service = BlenderService(config, launcher=FakeLauncher())
    operations = [
        {
            "op": "set_transform",
            "object_id": "fixture_cube_v1",
            "location": [1.0, 2.0, 3.0],
        }
    ]

    response = service.apply_scene_transaction(
        "scene.blend",
        expected_revision=revision,
        transaction_id="txn_move_cube",
        operations=operations,
        request_id="req_txn_1",
    )

    assert response.ok
    assert scene.read_bytes() == b"mutated-scene"
    assert response.result is not None
    assert response.result["replayed"] is False
    assert response.result["checkpoint_id"].startswith("txn_move_cube-")
    checkpoint = (
        config.checkpoints_dir / f"{response.result['checkpoint_id']}.blend"
    )
    assert checkpoint.read_bytes() == b"original-scene"

    replay = service.apply_scene_transaction(
        "scene.blend",
        expected_revision=revision,
        transaction_id="txn_move_cube",
        operations=operations,
        request_id="req_txn_2",
    )
    assert replay.ok
    assert replay.result is not None
    assert replay.result["replayed"] is True


def test_transaction_rejects_stale_revision_without_mutation(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"original-scene")

    response = BlenderService(
        config,
        launcher=FakeLauncher(),
    ).apply_scene_transaction(
        "scene.blend",
        expected_revision="0" * 64,
        transaction_id="txn_stale",
        operations=[
            {
                "op": "set_visibility",
                "object_id": "fixture_cube_v1",
                "render": False,
            }
        ],
        request_id="req_txn_stale",
    )

    assert not response.ok
    assert response.error is not None
    assert response.error.code == ErrorCode.SCENE_REVISION_CONFLICT
    assert scene.read_bytes() == b"original-scene"


def test_render_job_tools_queue_poll_and_cancel(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    (tmp_path / "scene.blend").write_bytes(b"scene")
    manager = FakeRenderManager()
    service = BlenderService(
        config,
        launcher=FakeLauncher(),
        render_manager=manager,
    )

    started = service.start_render(
        "scene.blend",
        "renders/final.mp4",
        timeout_seconds=30,
        idempotency_key="episode-v1",
        request_id="req_render_job",
    )
    assert started.ok
    assert started.result is not None
    assert started.result["job_id"] == "job_render_1"
    assert started.result["state"] == "queued"

    polled = service.get_job("job_render_1", request_id="req_get_job")
    assert polled.ok
    assert polled.result is not None
    assert polled.result["events"] == []

    cancelled = service.cancel_job(
        "job_render_1",
        request_id="req_cancel_job",
    )
    assert cancelled.ok
    assert cancelled.result is not None
    assert cancelled.result["state"] == "cancelling"


def test_restore_checkpoint_requires_explicit_overwrite_and_restores_bytes(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    scene = tmp_path / "scene.blend"
    scene.write_bytes(b"mutated")
    config.checkpoints_dir.mkdir(parents=True)
    checkpoint_id = "txn_restore-0123456789abcdef"
    (config.checkpoints_dir / f"{checkpoint_id}.blend").write_bytes(b"original")
    service = BlenderService(config, launcher=FakeLauncher())

    denied = service.restore_checkpoint(
        checkpoint_id,
        "scene.blend",
        request_id="req_restore_denied",
    )
    assert not denied.ok
    assert scene.read_bytes() == b"mutated"

    restored = service.restore_checkpoint(
        checkpoint_id,
        "scene.blend",
        allow_overwrite=True,
        request_id="req_restore_allowed",
    )
    assert restored.ok
    assert scene.read_bytes() == b"original"
