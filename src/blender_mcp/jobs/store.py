"""SQLite WAL persistence for job metadata, state transitions, and events."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from blender_mcp.jobs.models import (
    LEGAL_JOB_TRANSITIONS,
    NONTERMINAL_JOB_STATES,
    IllegalJobTransitionError,
    JobAlreadyExistsError,
    JobEvent,
    JobNotFoundError,
    JobRecord,
    JobState,
    RenderAdmissionConflictError,
    TERMINAL_JOB_STATES,
)

_SCHEMA_VERSION = "1"
_STATE_VALUES_SQL = ", ".join(f"'{state.value}'" for state in JobState)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _json_object(value: Mapping[str, Any] | None) -> tuple[dict[str, Any], str]:
    copied = dict(value or {})
    encoded = json.dumps(
        copied,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    # Round-tripping removes references to mutable caller-owned nested objects.
    return json.loads(encoded), encoded


class SQLiteJobStore:
    """Small process-safe job repository backed by a single SQLite WAL file."""

    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path).expanduser()
        if str(database_path) == ":memory:":
            raise ValueError("SQLiteJobStore requires a durable file path")
        self.database_path = path.resolve(strict=False)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def create_job(
        self,
        kind: str,
        *,
        metadata: Mapping[str, Any] | None = None,
        job_id: str | None = None,
    ) -> JobRecord:
        normalized_kind = self._nonempty(kind, "kind")
        normalized_id = self._nonempty(job_id or f"job_{uuid4().hex}", "job_id")
        metadata_value, metadata_json = _json_object(metadata)
        timestamp = _utc_now()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO jobs (
                        job_id, kind, state, metadata_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_id,
                        normalized_kind,
                        JobState.QUEUED.value,
                        metadata_json,
                        timestamp,
                        timestamp,
                    ),
                )
                self._insert_event(
                    connection,
                    normalized_id,
                    event_type="created",
                    state=JobState.QUEUED,
                    from_state=None,
                    to_state=JobState.QUEUED,
                    message=None,
                    details_json="{}",
                    created_at=timestamp,
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                if (
                    connection.execute(
                        "SELECT 1 FROM jobs WHERE job_id = ?",
                        (normalized_id,),
                    ).fetchone()
                    is not None
                ):
                    raise JobAlreadyExistsError(normalized_id) from exc
                raise
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return JobRecord(
            job_id=normalized_id,
            kind=normalized_kind,
            state=JobState.QUEUED,
            metadata=metadata_value,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def admit_render_job(
        self,
        *,
        metadata: Mapping[str, Any],
        output_path: str,
        idempotency_key: str,
        job_id: str | None = None,
    ) -> tuple[JobRecord, bool]:
        """Atomically reserve an output and bind an idempotency key to a request."""

        normalized_output = self._nonempty(output_path, "output_path")
        normalized_key = idempotency_key.strip() or None
        normalized_id = self._nonempty(job_id or f"job_{uuid4().hex}", "job_id")
        metadata_value, metadata_json = _json_object(metadata)
        request_fingerprint = hashlib.sha256(
            metadata_json.encode("utf-8")
        ).hexdigest()
        timestamp = _utc_now()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if normalized_key is not None:
                existing = connection.execute(
                    """
                    SELECT jobs.*, render_admissions.request_fingerprint
                    FROM render_admissions
                    JOIN jobs USING (job_id)
                    WHERE render_admissions.idempotency_key = ?
                    """,
                    (normalized_key,),
                ).fetchone()
                if existing is not None:
                    if existing["request_fingerprint"] != request_fingerprint:
                        raise RenderAdmissionConflictError(
                            "Idempotency key is already bound to a different "
                            "render request"
                        )
                    connection.commit()
                    return self._job_from_row(existing), False

            active = connection.execute(
                """
                SELECT job_id FROM render_admissions
                WHERE output_path = ? AND active = 1
                """,
                (normalized_output,),
            ).fetchone()
            if active is not None:
                raise RenderAdmissionConflictError(
                    f"An active or orphaned render already targets "
                    f"{normalized_output}"
                )

            connection.execute(
                """
                INSERT INTO jobs (
                    job_id, kind, state, metadata_json, created_at, updated_at
                ) VALUES (?, 'final_render', ?, ?, ?, ?)
                """,
                (
                    normalized_id,
                    JobState.QUEUED.value,
                    metadata_json,
                    timestamp,
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO render_admissions (
                    job_id, idempotency_key, request_fingerprint,
                    output_path, active
                ) VALUES (?, ?, ?, ?, 1)
                """,
                (
                    normalized_id,
                    normalized_key,
                    request_fingerprint,
                    normalized_output,
                ),
            )
            self._insert_event(
                connection,
                normalized_id,
                event_type="created",
                state=JobState.QUEUED,
                from_state=None,
                to_state=JobState.QUEUED,
                message=None,
                details_json="{}",
                created_at=timestamp,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return (
            JobRecord(
                job_id=normalized_id,
                kind="final_render",
                state=JobState.QUEUED,
                metadata=metadata_value,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            True,
        )

    def get_job(self, job_id: str) -> JobRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._job_from_row(row) if row is not None else None

    def list_jobs(
        self,
        *,
        states: Iterable[JobState | str] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[JobRecord]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset cannot be negative")

        parameters: list[Any] = []
        where = ""
        if states is not None:
            normalized_states = tuple(
                dict.fromkeys(JobState(state).value for state in states)
            )
            if not normalized_states:
                return []
            placeholders = ", ".join("?" for _ in normalized_states)
            where = f"WHERE state IN ({placeholders})"
            parameters.extend(normalized_states)
        parameters.extend((limit, offset))

        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                {where}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                parameters,
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def transition_job(
        self,
        job_id: str,
        to_state: JobState | str,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
        metadata_patch: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        target = JobState(to_state)
        _, details_json = _json_object(details)
        timestamp = _utc_now()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            current = JobState(row["state"])
            if target not in LEGAL_JOB_TRANSITIONS[current]:
                raise IllegalJobTransitionError(job_id, current, target)

            metadata = json.loads(row["metadata_json"])
            if metadata_patch is not None:
                patch, _ = _json_object(metadata_patch)
                metadata.update(patch)
            _, metadata_json = _json_object(metadata)

            connection.execute(
                """
                UPDATE jobs
                SET state = ?, metadata_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (target.value, metadata_json, timestamp, job_id),
            )
            if target in TERMINAL_JOB_STATES and target is not JobState.ORPHANED:
                connection.execute(
                    "UPDATE render_admissions SET active = 0 WHERE job_id = ?",
                    (job_id,),
                )
            self._insert_event(
                connection,
                job_id,
                event_type="state_changed",
                state=target,
                from_state=current,
                to_state=target,
                message=message,
                details_json=details_json,
                created_at=timestamp,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        result = self.get_job(job_id)
        if result is None:  # Defensive: the row was held under BEGIN IMMEDIATE.
            raise JobNotFoundError(job_id)
        return result

    def append_event(
        self,
        job_id: str,
        event_type: str,
        *,
        message: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> JobEvent:
        normalized_type = self._nonempty(event_type, "event_type")
        details_value, details_json = _json_object(details)
        timestamp = _utc_now()

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            state = JobState(row["state"])
            event_id = self._insert_event(
                connection,
                job_id,
                event_type=normalized_type,
                state=state,
                from_state=None,
                to_state=None,
                message=message,
                details_json=details_json,
                created_at=timestamp,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        return JobEvent(
            event_id=event_id,
            job_id=job_id,
            event_type=normalized_type,
            state=state,
            from_state=None,
            to_state=None,
            message=message,
            details=details_value,
            created_at=timestamp,
        )

    def list_events(
        self,
        job_id: str,
        *,
        after_event_id: int | None = None,
        limit: int = 1000,
    ) -> list[JobEvent]:
        if after_event_id is not None and after_event_id < 0:
            raise ValueError("after_event_id cannot be negative")
        if not 1 <= limit <= 10_000:
            raise ValueError("limit must be between 1 and 10000")

        query = "SELECT * FROM job_events WHERE job_id = ?"
        parameters: list[Any] = [job_id]
        if after_event_id is not None:
            query += " AND event_id > ?"
            parameters.append(after_event_id)
        query += " ORDER BY event_id ASC LIMIT ?"
        parameters.append(limit)

        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._event_from_row(row) for row in rows]

    def recover_orphaned_jobs(self) -> list[JobRecord]:
        """Mark jobs interrupted by a server restart as terminally orphaned."""

        nonterminal = tuple(state.value for state in NONTERMINAL_JOB_STATES)
        placeholders = ", ".join("?" for _ in nonterminal)
        timestamp = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE state IN ({placeholders})
                ORDER BY created_at ASC, rowid ASC
                """,
                nonterminal,
            ).fetchall()
            for row in rows:
                previous = JobState(row["state"])
                connection.execute(
                    "UPDATE jobs SET state = ?, updated_at = ? WHERE job_id = ?",
                    (JobState.ORPHANED.value, timestamp, row["job_id"]),
                )
                self._insert_event(
                    connection,
                    row["job_id"],
                    event_type="recovered",
                    state=JobState.ORPHANED,
                    from_state=previous,
                    to_state=JobState.ORPHANED,
                    message="Job was nonterminal when the job store restarted",
                    details_json="{}",
                    created_at=timestamp,
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

        recovered: list[JobRecord] = []
        for row in rows:
            recovered.append(
                JobRecord(
                    job_id=row["job_id"],
                    kind=row["kind"],
                    state=JobState.ORPHANED,
                    metadata=json.loads(row["metadata_json"]),
                    created_at=row["created_at"],
                    updated_at=timestamp,
                )
            )
        return recovered

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            journal_mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise RuntimeError(
                    f"SQLite refused WAL mode for {self.database_path}: {journal_mode}"
                )
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS job_store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                INSERT OR IGNORE INTO job_store_metadata (key, value)
                VALUES ('schema_version', '{_SCHEMA_VERSION}');

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ({_STATE_VALUES_SQL})),
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (state IN ({_STATE_VALUES_SQL})),
                    from_state TEXT CHECK (
                        from_state IS NULL OR from_state IN ({_STATE_VALUES_SQL})
                    ),
                    to_state TEXT CHECK (
                        to_state IS NULL OR to_state IN ({_STATE_VALUES_SQL})
                    ),
                    message TEXT,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS render_admissions (
                    job_id TEXT PRIMARY KEY
                        REFERENCES jobs(job_id) ON DELETE CASCADE,
                    idempotency_key TEXT,
                    request_fingerprint TEXT NOT NULL,
                    output_path TEXT NOT NULL,
                    active INTEGER NOT NULL CHECK (active IN (0, 1))
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_state_created
                    ON jobs(state, created_at);
                CREATE INDEX IF NOT EXISTS idx_job_events_job_event
                    ON job_events(job_id, event_id);
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_render_admissions_idempotency
                    ON render_admissions(idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS
                    idx_render_admissions_active_output
                    ON render_admissions(output_path)
                    WHERE active = 1;
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        job_id: str,
        *,
        event_type: str,
        state: JobState,
        from_state: JobState | None,
        to_state: JobState | None,
        message: str | None,
        details_json: str,
        created_at: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO job_events (
                job_id, event_type, state, from_state, to_state,
                message, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job_id,
                event_type,
                state.value,
                from_state.value if from_state is not None else None,
                to_state.value if to_state is not None else None,
                message,
                details_json,
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=row["job_id"],
            kind=row["kind"],
            state=JobState(row["state"]),
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> JobEvent:
        return JobEvent(
            event_id=row["event_id"],
            job_id=row["job_id"],
            event_type=row["event_type"],
            state=JobState(row["state"]),
            from_state=(
                JobState(row["from_state"]) if row["from_state"] is not None else None
            ),
            to_state=(
                JobState(row["to_state"]) if row["to_state"] is not None else None
            ),
            message=row["message"],
            details=json.loads(row["details_json"]),
            created_at=row["created_at"],
        )

    @staticmethod
    def _nonempty(value: str, field_name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name} must be a non-empty string")
        return value
