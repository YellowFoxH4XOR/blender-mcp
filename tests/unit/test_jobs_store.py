import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from blender_mcp.jobs import (
    IllegalJobTransitionError,
    JobState,
    RenderAdmissionConflictError,
    SQLiteJobStore,
)


def test_job_lifecycle_is_durable_and_emits_ordered_events(tmp_path: Path) -> None:
    database = tmp_path / "state" / "jobs.sqlite3"
    store = SQLiteJobStore(database)

    created = store.create_job(
        "render",
        metadata={"scene_path": "scenes/hero.blend"},
        job_id="job_render_1",
    )
    running = store.transition_job(created.job_id, JobState.STARTING)
    running = store.transition_job(running.job_id, JobState.RUNNING)
    store.append_event(
        running.job_id,
        "progress",
        message="Rendered frame 1",
        details={"completed_frames": 1, "total_frames": 2},
    )
    succeeded = store.transition_job(
        running.job_id,
        JobState.SUCCEEDED,
        metadata_patch={"manifest_path": "renders/job_render_1.json"},
    )

    reopened = SQLiteJobStore(database)
    loaded = reopened.get_job(created.job_id)
    assert loaded == succeeded
    assert loaded is not None
    assert loaded.metadata == {
        "scene_path": "scenes/hero.blend",
        "manifest_path": "renders/job_render_1.json",
    }

    events = reopened.list_events(created.job_id)
    assert [event.event_type for event in events] == [
        "created",
        "state_changed",
        "state_changed",
        "progress",
        "state_changed",
    ]
    assert [event.event_id for event in events] == sorted(
        event.event_id for event in events
    )
    assert events[-1].state is JobState.SUCCEEDED


def test_sqlite_store_enables_wal_and_serializes_metadata_as_json(
    tmp_path: Path,
) -> None:
    database = tmp_path / "jobs.sqlite3"
    store = SQLiteJobStore(database)
    store.create_job("render", metadata={"frames": [1, 2, 3]})

    with sqlite3.connect(database) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        raw_metadata = connection.execute(
            "SELECT metadata_json FROM jobs"
        ).fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert json.loads(raw_metadata) == {"frames": [1, 2, 3]}


def test_illegal_or_terminal_transition_is_rejected_without_an_event(
    tmp_path: Path,
) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    job = store.create_job("render", job_id="job_illegal")

    with pytest.raises(IllegalJobTransitionError):
        store.transition_job(job.job_id, JobState.SUCCEEDED)

    store.transition_job(job.job_id, JobState.CANCELLED)
    with pytest.raises(IllegalJobTransitionError):
        store.transition_job(job.job_id, JobState.RUNNING)

    assert [event.event_type for event in store.list_events(job.job_id)] == [
        "created",
        "state_changed",
    ]


def test_list_jobs_filters_by_state_and_is_newest_first(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    first = store.create_job("render", job_id="job_first")
    second = store.create_job("preview", job_id="job_second")
    store.transition_job(second.job_id, JobState.STARTING)

    assert [job.job_id for job in store.list_jobs()] == [
        second.job_id,
        first.job_id,
    ]
    assert [
        job.job_id for job in store.list_jobs(states={JobState.STARTING})
    ] == [second.job_id]


def test_startup_recovery_atomically_orphans_every_nonterminal_job(
    tmp_path: Path,
) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    queued = store.create_job("render", job_id="job_queued")
    running = store.create_job("render", job_id="job_running")
    completed = store.create_job("render", job_id="job_completed")
    store.transition_job(running.job_id, JobState.STARTING)
    store.transition_job(running.job_id, JobState.RUNNING)
    store.transition_job(completed.job_id, JobState.CANCELLED)

    recovered = store.recover_orphaned_jobs()

    assert {job.job_id for job in recovered} == {queued.job_id, running.job_id}
    assert all(job.state is JobState.ORPHANED for job in recovered)
    assert store.get_job(completed.job_id).state is JobState.CANCELLED  # type: ignore[union-attr]
    assert store.list_events(queued.job_id)[-1].event_type == "recovered"
    assert store.recover_orphaned_jobs() == []


def test_render_admission_binds_idempotency_key_to_exact_request(
    tmp_path: Path,
) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    metadata = {
        "scene_path": "scenes/hero.blend",
        "output_path": "renders/hero.mp4",
        "timeout_seconds": 30,
    }

    first, created = store.admit_render_job(
        metadata=metadata,
        output_path="renders/hero.mp4",
        idempotency_key="episode-1",
    )
    replay, replay_created = store.admit_render_job(
        metadata=metadata,
        output_path="renders/hero.mp4",
        idempotency_key="episode-1",
    )

    assert created is True
    assert replay_created is False
    assert replay.job_id == first.job_id
    with pytest.raises(RenderAdmissionConflictError, match="different"):
        store.admit_render_job(
            metadata={**metadata, "output_path": "renders/other.mp4"},
            output_path="renders/other.mp4",
            idempotency_key="episode-1",
        )


def test_render_admission_serializes_concurrent_output_reservations(
    tmp_path: Path,
) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")

    def admit(index: int) -> str:
        record, _ = store.admit_render_job(
            metadata={
                "scene_path": f"scenes/{index}.blend",
                "output_path": "renders/final.mp4",
            },
            output_path="renders/final.mp4",
            idempotency_key=f"request-{index}",
        )
        return record.job_id

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            executor.submit(admit, index)
            for index in range(2)
        ]
    successes = [future.result() for future in outcomes if future.exception() is None]
    failures = [future.exception() for future in outcomes if future.exception() is not None]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], RenderAdmissionConflictError)


def test_orphaned_render_keeps_output_reserved(tmp_path: Path) -> None:
    store = SQLiteJobStore(tmp_path / "jobs.sqlite3")
    first, _ = store.admit_render_job(
        metadata={"output_path": "renders/final.mp4"},
        output_path="renders/final.mp4",
        idempotency_key="first",
    )

    recovered = store.recover_orphaned_jobs()

    assert recovered[0].job_id == first.job_id
    with pytest.raises(RenderAdmissionConflictError, match="orphaned"):
        store.admit_render_job(
            metadata={"output_path": "renders/final.mp4"},
            output_path="renders/final.mp4",
            idempotency_key="second",
        )
