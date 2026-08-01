"""Constrained Blender operation dispatcher.

This module intentionally has no dynamic imports, evaluation, filesystem access,
or network access. Callers supply maps of already-approved stable object and
action IDs. The full batch is compiled before the first scene mutation.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

MAX_OPERATIONS = 100
ALLOWED_RENDER_ENGINES = frozenset(
    {
        "BLENDER_EEVEE",
        "BLENDER_EEVEE_NEXT",
        "BLENDER_WORKBENCH",
        "CYCLES",
    }
)


class OperationExecutionError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation_index: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.operation_index = operation_index
        self.details = details or {}


@dataclass(frozen=True)
class ExternalFileReference:
    """One Blender datablock path considered before render or mutation."""

    kind: str
    name: str
    path: str
    packed: bool = False


def _security_finding(
    code: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": "blocker",
        "message": message,
        "details": details,
    }


def find_scene_security_violations(
    *,
    scene_path: Path,
    project_root: Path,
    file_references: Sequence[ExternalFileReference],
    compositor_file_outputs: Sequence[str],
) -> list[dict[str, Any]]:
    """Find file access that escapes the configured project boundary."""

    root = project_root.resolve(strict=True)
    scene = scene_path.resolve(strict=True)
    if not scene.is_relative_to(root):
        raise ValueError("scene_path must resolve inside project_root")

    findings: list[dict[str, Any]] = []
    for reference in file_references:
        raw = reference.path
        if reference.packed or not raw or raw == "<builtin>":
            continue
        if (
            "\x00" in raw
            or "\\" in raw
            or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", raw)
        ):
            findings.append(
                _security_finding(
                    "UNSAFE_FILE_REFERENCE",
                    "Scene contains a file reference with unsafe path semantics.",
                    kind=reference.kind,
                    name=reference.name,
                    path=raw,
                )
            )
            continue

        candidate = Path(raw)
        if raw.startswith("//"):
            candidate = scene.parent / raw[2:]
        elif not candidate.is_absolute():
            findings.append(
                _security_finding(
                    "UNSAFE_FILE_REFERENCE",
                    "Scene contains a working-directory-relative file reference.",
                    kind=reference.kind,
                    name=reference.name,
                    path=raw,
                )
            )
            continue

        resolved = candidate.resolve(strict=False)
        if not resolved.is_relative_to(root):
            findings.append(
                _security_finding(
                    "EXTERNAL_FILE_REFERENCE",
                    "Scene references a file outside the configured project root.",
                    kind=reference.kind,
                    name=reference.name,
                    path=raw,
                    resolved_path=str(resolved),
                )
            )

    for node_name in compositor_file_outputs:
        findings.append(
            _security_finding(
                "COMPOSITOR_FILE_OUTPUT",
                "Compositor File Output nodes are not allowed.",
                node=node_name,
            )
        )
    return findings


@dataclass(frozen=True)
class _CompiledOperation:
    index: int
    op: str
    values: dict[str, Any]
    target: Any = None
    action: Any = None


def _fail(index: int, message: str, *, fields: list[str] | None = None) -> None:
    details = {"fields": fields} if fields else None
    raise OperationExecutionError(
        "INVALID_OPERATION",
        message,
        operation_index=index,
        details=details,
    )


def _strict_fields(
    operation: dict[str, Any],
    *,
    index: int,
    allowed: frozenset[str],
    required: frozenset[str],
) -> None:
    unknown = sorted(set(operation) - allowed)
    missing = sorted(required - set(operation))
    if unknown:
        _fail(index, "Operation contains unknown fields.", fields=unknown)
    if missing:
        _fail(index, "Operation is missing required fields.", fields=missing)


def _identifier(value: Any, *, index: int, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        _fail(index, f"{field} must be a non-empty string of at most 200 characters.")
    if not all(character.isalnum() or character in "_.:-" for character in value):
        _fail(index, f"{field} contains unsupported characters.")
    return value


def _vector3(value: Any, *, index: int, field: str) -> tuple[float, float, float]:
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 3
        or any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value)
    ):
        _fail(index, f"{field} must contain exactly three numbers.")
    result = tuple(float(item) for item in value)
    if not all(math.isfinite(item) for item in result):
        _fail(index, f"{field} values must be finite.")
    if field == "scale" and any(item == 0 for item in result):
        _fail(index, "scale components must be non-zero.")
    return result  # type: ignore[return-value]


def _integer(
    value: Any,
    *,
    index: int,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        _fail(index, f"{field} must be an integer from {minimum} to {maximum}.")
    return value


def _target(
    object_id: str,
    objects_by_id: Mapping[str, Any],
    *,
    index: int,
) -> Any:
    target = objects_by_id.get(object_id)
    if target is None:
        raise OperationExecutionError(
            "OBJECT_NOT_FOUND",
            "Operation references an unknown approved object ID.",
            operation_index=index,
            details={"object_id": object_id},
        )
    return target


def _compile_operation(
    operation: Any,
    *,
    index: int,
    scene: Any,
    objects_by_id: Mapping[str, Any],
    actions_by_id: Mapping[str, Any],
) -> _CompiledOperation:
    if not isinstance(operation, dict):
        _fail(index, "Each operation must be an object.")
    op = operation.get("op")
    if op == "set_transform":
        allowed = frozenset(
            {"op", "object_id", "location", "rotation_euler", "scale"}
        )
        _strict_fields(
            operation,
            index=index,
            allowed=allowed,
            required=frozenset({"op", "object_id"}),
        )
        if not any(field in operation for field in ("location", "rotation_euler", "scale")):
            _fail(index, "set_transform requires at least one transform field.")
        object_id = _identifier(operation["object_id"], index=index, field="object_id")
        values = {
            field: _vector3(operation[field], index=index, field=field)
            for field in ("location", "rotation_euler", "scale")
            if field in operation
        }
        values["object_id"] = object_id
        return _CompiledOperation(
            index,
            op,
            values,
            target=_target(object_id, objects_by_id, index=index),
        )

    if op == "set_visibility":
        _strict_fields(
            operation,
            index=index,
            allowed=frozenset({"op", "object_id", "viewport", "render"}),
            required=frozenset({"op", "object_id"}),
        )
        if "viewport" not in operation and "render" not in operation:
            _fail(index, "set_visibility requires viewport or render.")
        for field in ("viewport", "render"):
            if field in operation and not isinstance(operation[field], bool):
                _fail(index, f"{field} must be a boolean.")
        object_id = _identifier(operation["object_id"], index=index, field="object_id")
        return _CompiledOperation(
            index,
            op,
            {key: operation[key] for key in ("object_id", "viewport", "render") if key in operation},
            target=_target(object_id, objects_by_id, index=index),
        )

    if op == "configure_scene":
        fields = {
            "frame_start": (0, 1_000_000),
            "frame_end": (0, 1_000_000),
            "fps": (1, 240),
            "resolution_x": (16, 16_384),
            "resolution_y": (16, 16_384),
            "resolution_percentage": (1, 100),
        }
        _strict_fields(
            operation,
            index=index,
            allowed=frozenset({"op", "render_engine", *fields}),
            required=frozenset({"op"}),
        )
        if len(operation) == 1:
            _fail(index, "configure_scene requires at least one setting.")
        values = {
            field: _integer(
                operation[field],
                index=index,
                field=field,
                minimum=bounds[0],
                maximum=bounds[1],
            )
            for field, bounds in fields.items()
            if field in operation
        }
        if "render_engine" in operation:
            engine = operation["render_engine"]
            if engine not in ALLOWED_RENDER_ENGINES:
                _fail(index, "render_engine is not approved.")
            values["render_engine"] = engine
        prospective_start = values.get("frame_start", scene.frame_start)
        prospective_end = values.get("frame_end", scene.frame_end)
        if prospective_end < prospective_start:
            _fail(index, "frame_end must be greater than or equal to frame_start.")
        return _CompiledOperation(index, op, values)

    if op == "apply_action":
        _strict_fields(
            operation,
            index=index,
            allowed=frozenset(
                {"op", "object_id", "action_id", "frame_start", "frame_end"}
            ),
            required=frozenset({"op", "object_id", "action_id"}),
        )
        object_id = _identifier(operation["object_id"], index=index, field="object_id")
        action_id = _identifier(operation["action_id"], index=index, field="action_id")
        frame_start = _integer(
            operation.get("frame_start", 1),
            index=index,
            field="frame_start",
            minimum=0,
            maximum=1_000_000,
        )
        frame_end = None
        if "frame_end" in operation:
            frame_end = _integer(
                operation["frame_end"],
                index=index,
                field="frame_end",
                minimum=0,
                maximum=1_000_000,
            )
            if frame_end < frame_start:
                _fail(index, "frame_end must be greater than or equal to frame_start.")
        action = actions_by_id.get(action_id)
        if action is None:
            raise OperationExecutionError(
                "ACTION_NOT_FOUND",
                "Operation references an unknown approved action ID.",
                operation_index=index,
                details={"action_id": action_id},
            )
        return _CompiledOperation(
            index,
            op,
            {
                "object_id": object_id,
                "action_id": action_id,
                "frame_start": frame_start,
                "frame_end": frame_end,
            },
            target=_target(object_id, objects_by_id, index=index),
            action=action,
        )

    _fail(index, "Operation is not in the adapter allowlist.")
    raise AssertionError("unreachable")


def _apply(compiled: _CompiledOperation, scene: Any) -> None:
    values = compiled.values
    if compiled.op == "set_transform":
        for field in ("location", "rotation_euler", "scale"):
            if field in values:
                setattr(compiled.target, field, values[field])
        return
    if compiled.op == "set_visibility":
        if "viewport" in values:
            compiled.target.hide_viewport = not values["viewport"]
        if "render" in values:
            compiled.target.hide_render = not values["render"]
        return
    if compiled.op == "configure_scene":
        for field in ("frame_start", "frame_end"):
            if field in values:
                setattr(scene, field, values[field])
        for field in ("fps", "resolution_x", "resolution_y", "resolution_percentage"):
            if field in values:
                setattr(scene.render, field, values[field])
        if "render_engine" in values:
            scene.render.engine = values["render_engine"]
        return
    if compiled.op == "apply_action":
        compiled.target.animation_data_create()
        animation_data = compiled.target.animation_data
        track = animation_data.nla_tracks.new()
        track.name = f"Blender MCP: {values['action_id']}"
        strip = track.strips.new(
            getattr(compiled.action, "name", values["action_id"]),
            values["frame_start"],
            compiled.action,
        )
        if values["frame_end"] is not None:
            strip.frame_end = values["frame_end"]
        return
    raise AssertionError(f"Unsupported compiled operation: {compiled.op}")


def apply_operations(
    scene: Any,
    operations: Sequence[dict[str, Any]],
    *,
    objects_by_id: Mapping[str, Any],
    actions_by_id: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate, resolve approved IDs, then apply a bounded batch in order."""

    if isinstance(operations, (str, bytes)) or not isinstance(operations, Sequence):
        raise OperationExecutionError("INVALID_OPERATIONS", "operations must be an array.")
    if not 1 <= len(operations) <= MAX_OPERATIONS:
        raise OperationExecutionError(
            "INVALID_OPERATIONS",
            f"operations must contain from 1 to {MAX_OPERATIONS} items.",
        )
    compiled = [
        _compile_operation(
            operation,
            index=index,
            scene=scene,
            objects_by_id=objects_by_id,
            actions_by_id=actions_by_id,
        )
        for index, operation in enumerate(operations)
    ]

    applied: list[dict[str, Any]] = []
    for item in compiled:
        try:
            _apply(item, scene)
        except OperationExecutionError:
            raise
        except Exception as exc:
            raise OperationExecutionError(
                "OPERATION_FAILED",
                "Blender failed while applying an approved operation.",
                operation_index=item.index,
                details={"op": item.op},
            ) from exc
        record: dict[str, Any] = {"index": item.index, "op": item.op}
        for field in ("object_id", "action_id"):
            if field in item.values:
                record[field] = item.values[field]
        applied.append(record)
    return applied
