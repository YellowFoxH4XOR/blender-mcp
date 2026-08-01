"""Execution-time transaction idempotency contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from threading import RLock
from typing import Any, Generic, Protocol, TypeVar

from blender_mcp.transactions.models import SceneTransaction

ResultT = TypeVar("ResultT")


class TransactionIdConflict(ValueError):
    """Raised when one transaction ID is reused with different semantics."""


@dataclass(frozen=True)
class IdempotentExecution(Generic[ResultT]):
    value: ResultT
    replayed: bool


class TransactionIdempotency(Protocol):
    """Persistence boundary implemented by memory now and SQLite later."""

    def execute_once(
        self,
        transaction: SceneTransaction,
        execute: Callable[[], ResultT],
    ) -> IdempotentExecution[ResultT]: ...


@dataclass(frozen=True)
class _StoredExecution:
    fingerprint: str
    value: Any


def transaction_fingerprint(transaction: SceneTransaction) -> str:
    encoded = json.dumps(
        transaction.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class InMemoryTransactionIdempotency:
    """Process-local reference implementation with atomic execute-once behavior."""

    def __init__(self) -> None:
        self._executions: dict[str, _StoredExecution] = {}
        self._lock = RLock()

    def execute_once(
        self,
        transaction: SceneTransaction,
        execute: Callable[[], ResultT],
    ) -> IdempotentExecution[ResultT]:
        fingerprint = transaction_fingerprint(transaction)
        with self._lock:
            stored = self._executions.get(transaction.transaction_id)
            if stored is not None:
                if stored.fingerprint != fingerprint:
                    raise TransactionIdConflict(
                        f"transaction_id {transaction.transaction_id!r} "
                        "was already used with a different payload"
                    )
                return IdempotentExecution(value=stored.value, replayed=True)

            value = execute()
            self._executions[transaction.transaction_id] = _StoredExecution(
                fingerprint=fingerprint,
                value=value,
            )
            return IdempotentExecution(value=value, replayed=False)
