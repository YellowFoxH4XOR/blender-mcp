"""Immutable records and state rules for durable background jobs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class JobState(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    CANCELLING = "cancelling"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ORPHANED = "orphaned"


TERMINAL_JOB_STATES = frozenset(
    {
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.ORPHANED,
    }
)
NONTERMINAL_JOB_STATES = frozenset(set(JobState) - TERMINAL_JOB_STATES)

# ORPHANED is deliberately absent: only startup recovery may assign it.
LEGAL_JOB_TRANSITIONS: Mapping[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset(
        {
            JobState.STARTING,
            JobState.CANCELLING,
            JobState.CANCELLED,
            JobState.FAILED,
        }
    ),
    JobState.STARTING: frozenset(
        {
            JobState.RUNNING,
            JobState.CANCELLING,
            JobState.CANCELLED,
            JobState.FAILED,
        }
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.CANCELLING,
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.CANCELLING: frozenset(
        {
            JobState.CANCELLED,
            JobState.FAILED,
        }
    ),
    JobState.SUCCEEDED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.ORPHANED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: str
    kind: str
    state: JobState
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class JobEvent:
    event_id: int
    job_id: str
    event_type: str
    state: JobState
    from_state: JobState | None
    to_state: JobState | None
    message: str | None
    details: dict[str, Any]
    created_at: str


class JobStoreError(RuntimeError):
    """Base class for durable job-store failures."""


class JobNotFoundError(JobStoreError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"Job does not exist: {job_id}")
        self.job_id = job_id


class JobAlreadyExistsError(JobStoreError):
    def __init__(self, job_id: str) -> None:
        super().__init__(f"Job already exists: {job_id}")
        self.job_id = job_id


class RenderAdmissionConflictError(JobStoreError):
    def __init__(self, message: str) -> None:
        super().__init__(message)


class IllegalJobTransitionError(JobStoreError):
    def __init__(
        self,
        job_id: str,
        from_state: JobState,
        to_state: JobState,
    ) -> None:
        super().__init__(
            f"Illegal job transition for {job_id}: {from_state.value} -> "
            f"{to_state.value}"
        )
        self.job_id = job_id
        self.from_state = from_state
        self.to_state = to_state
