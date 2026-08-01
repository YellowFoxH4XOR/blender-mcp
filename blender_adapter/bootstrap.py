"""Headless Blender JSON adapter.

Canonical invocation:

    blender --background --disable-autoexec --python bootstrap.py -- request.json result.json

For launcher compatibility, ``--request request.json --result result.json`` is
also accepted. The adapter deliberately exposes a tiny command allowlist and
never evaluates client-provided code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import bpy

_ADAPTER_DIRECTORY = Path(__file__).resolve().parent
if str(_ADAPTER_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIRECTORY))
from operations import (
    ExternalFileReference,
    OperationExecutionError,
    apply_operations,
    find_scene_security_violations,
)


SCHEMA_VERSION = "1.0"
ALLOWED_COMMANDS = frozenset(
    {
        "status",
        "apply_scene_transaction",
        "inspect_scene",
        "validate_scene",
        "render_preview",
        "render_animation",
    }
)
REQUEST_ID_PATTERN = re.compile(r"^req_[A-Za-z0-9_-]{1,120}$")
MAX_OBJECTS_LIMIT = 1_000
DEFAULT_MAX_OBJECTS = 100
MAX_PREVIEW_DIMENSION = 4_096


class AdapterError(Exception):
    """A predictable error safe to return across the adapter boundary."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _parse_cli(argv: list[str]) -> tuple[Path, Path]:
    args = argv[argv.index("--") + 1 :] if "--" in argv else []
    if len(args) == 2 and not args[0].startswith("-"):
        return Path(args[0]).resolve(), Path(args[1]).resolve()

    request_path = None
    result_path = None
    index = 0
    while index < len(args):
        if args[index] == "--request" and index + 1 < len(args):
            request_path = Path(args[index + 1]).resolve()
            index += 2
        elif args[index] == "--result" and index + 1 < len(args):
            result_path = Path(args[index + 1]).resolve()
            index += 2
        else:
            raise AdapterError(
                "INVALID_ARGUMENT",
                "Expected positional request/result paths or --request/--result.",
            )
    if request_path is None or result_path is None:
        raise AdapterError("INVALID_ARGUMENT", "Request and result paths are required.")
    return request_path, result_path


def _read_request(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise AdapterError("REQUEST_READ_FAILED", "Could not read request file.") from exc
    if len(raw) > 1_048_576:
        raise AdapterError("REQUEST_TOO_LARGE", "Request exceeds the 1 MiB limit.")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AdapterError("INVALID_JSON", "Request is not valid UTF-8 JSON.") from exc
    if not isinstance(value, dict):
        raise AdapterError("INVALID_REQUEST", "Request must be a JSON object.")
    return value


def _validate_request(request: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    allowed_keys = {"schema_version", "request_id", "command", "payload"}
    unknown = sorted(set(request) - allowed_keys)
    if unknown:
        raise AdapterError("INVALID_REQUEST", "Unknown request fields.", {"fields": unknown})

    schema_version = request.get("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise AdapterError(
            "UNSUPPORTED_SCHEMA_VERSION",
            f"Expected schema_version {SCHEMA_VERSION}.",
        )
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise AdapterError("INVALID_REQUEST_ID", "request_id has an invalid format.")
    command = request.get("command")
    if command not in ALLOWED_COMMANDS:
        raise AdapterError("COMMAND_NOT_ALLOWED", "Command is not in the adapter allowlist.")
    payload = request.get("payload")
    if not isinstance(payload, dict):
        raise AdapterError("INVALID_PAYLOAD", "payload must be a JSON object.")
    return request_id, command, payload


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _strict_payload(
    payload: dict[str, Any], allowed: set[str], required: set[str]
) -> None:
    unknown = sorted(set(payload) - allowed)
    missing = sorted(required - set(payload))
    if unknown:
        raise AdapterError("INVALID_PAYLOAD", "Unknown payload fields.", {"fields": unknown})
    if missing:
        raise AdapterError("INVALID_PAYLOAD", "Required payload fields are missing.", {"fields": missing})


def _scene_path(payload: dict[str, Any]) -> Path:
    raw = payload.get("scene_path")
    if not isinstance(raw, str) or not raw:
        raise AdapterError("INVALID_SCENE_PATH", "scene_path must be a non-empty string.")
    path = Path(raw)
    if not path.is_absolute():
        raise AdapterError("INVALID_SCENE_PATH", "Adapter scene_path must be absolute.")
    path = path.resolve()
    if path.suffix.lower() != ".blend" or not path.is_file():
        raise AdapterError("SCENE_NOT_FOUND", "scene_path must reference an existing .blend file.")
    return path


def _open_scene(path: Path) -> None:
    try:
        bpy.ops.wm.open_mainfile(filepath=str(path))
    except Exception as exc:
        raise AdapterError("SCENE_OPEN_FAILED", "Blender could not open the scene.") from exc


def _project_root(payload: dict[str, Any], scene_path: Path) -> Path:
    raw = payload.get("project_root")
    if raw is None:
        # Direct adapter callers from older protocol clients are confined to the
        # scene directory. The launcher always supplies the wider project root.
        return scene_path.parent.resolve(strict=True)
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise AdapterError(
            "INVALID_PROJECT_ROOT",
            "project_root must be an absolute directory.",
        )
    try:
        root = Path(raw).resolve(strict=True)
    except OSError as exc:
        raise AdapterError(
            "INVALID_PROJECT_ROOT",
            "project_root does not exist.",
        ) from exc
    if not root.is_dir() or not scene_path.is_relative_to(root):
        raise AdapterError(
            "PATH_OUTSIDE_PROJECT",
            "scene_path must resolve inside project_root.",
        )
    return root


def _is_packed(value: Any) -> bool:
    if getattr(value, "packed_file", None) is not None:
        return True
    packed_files = getattr(value, "packed_files", None)
    if packed_files is None:
        return False
    try:
        return len(packed_files) > 0
    except (TypeError, AttributeError):
        return False


def _external_file_references() -> list[ExternalFileReference]:
    references: list[ExternalFileReference] = []
    collections = (
        ("cache_file", "cache_files"),
        ("font", "fonts"),
        ("image", "images"),
        ("library", "libraries"),
        ("movie_clip", "movieclips"),
        ("sound", "sounds"),
        ("volume", "volumes"),
    )
    for kind, collection_name in collections:
        for value in getattr(bpy.data, collection_name, ()):
            raw = getattr(value, "filepath", "")
            if not isinstance(raw, str):
                continue
            references.append(
                ExternalFileReference(
                    kind=kind,
                    name=str(getattr(value, "name_full", getattr(value, "name", ""))),
                    path=raw,
                    packed=_is_packed(value),
                )
            )
    return references


def _compositor_file_output_nodes() -> list[str]:
    outputs: list[str] = []
    for scene in bpy.data.scenes:
        node_tree = getattr(scene, "node_tree", None)
        if node_tree is None:
            node_tree = getattr(scene, "compositing_node_group", None)
        if node_tree is None:
            continue
        pending: list[tuple[Any, str]] = [(node_tree, scene.name)]
        visited: set[int] = set()
        while pending:
            tree, prefix = pending.pop()
            marker = id(tree)
            if marker in visited:
                continue
            visited.add(marker)
            for node in getattr(tree, "nodes", ()):
                node_name = str(getattr(node, "name", "unnamed"))
                qualified = f"{prefix}/{node_name}"
                if (
                    getattr(node, "type", None) == "OUTPUT_FILE"
                    or getattr(node, "bl_idname", None)
                    == "CompositorNodeOutputFile"
                ):
                    outputs.append(qualified)
                nested = getattr(node, "node_tree", None)
                if nested is not None:
                    pending.append((nested, qualified))
    return sorted(outputs)


def _scene_security_findings(
    payload: dict[str, Any],
    scene_path: Path,
) -> list[dict[str, Any]]:
    return find_scene_security_violations(
        scene_path=scene_path,
        project_root=_project_root(payload, scene_path),
        file_references=_external_file_references(),
        compositor_file_outputs=_compositor_file_output_nodes(),
    )


def _enforce_scene_security(
    payload: dict[str, Any],
    scene_path: Path,
) -> None:
    findings = _scene_security_findings(payload, scene_path)
    if findings:
        raise AdapterError(
            "SCENE_SECURITY_BLOCKED",
            "Scene contains external file access that is not allowed.",
            {"findings": findings},
        )


def _json_number_tuple(value: Any) -> list[float]:
    return [round(float(component), 8) for component in value]


def _object_identity(obj: Any, scene_path: Path) -> dict[str, Any]:
    custom_id = obj.get("_blender_mcp_id")
    if isinstance(custom_id, str) and custom_id.strip():
        return {"id": custom_id.strip(), "stable": True, "source": "custom_property"}

    data_name = getattr(getattr(obj, "data", None), "name_full", "")
    library = getattr(getattr(obj, "library", None), "filepath", "")
    fingerprint = "\0".join(
        (str(scene_path), obj.name_full, obj.type, data_name or "", library or "")
    ).encode("utf-8")
    fallback = hashlib.sha256(fingerprint).hexdigest()[:24]
    return {
        "id": f"unstable_{fallback}",
        "stable": False,
        "source": "deterministic_fallback",
    }


def _object_record(obj: Any, scene_path: Path) -> dict[str, Any]:
    return {
        "name": obj.name_full,
        "type": obj.type,
        "identity": _object_identity(obj, scene_path),
        "transform": {
            "location": _json_number_tuple(obj.location),
            "rotation_euler": _json_number_tuple(obj.rotation_euler),
            "scale": _json_number_tuple(obj.scale),
        },
        "visible_render": not bool(obj.hide_render),
    }


def _scene_summary(scene_path: Path, max_objects: int) -> tuple[dict[str, Any], list[str]]:
    scene = bpy.context.scene
    objects = sorted(scene.objects, key=lambda item: item.name_full)
    returned = objects[:max_objects]
    warnings: list[str] = []
    if len(returned) < len(objects):
        warnings.append(
            f"Object list truncated: returned {len(returned)} of {len(objects)}."
        )
    camera = scene.camera
    render = scene.render
    return (
        {
            "scene_path": str(scene_path),
            "scene_name": scene.name,
            "objects": [_object_record(obj, scene_path) for obj in returned],
            "object_count": len(objects),
            "objects_returned": len(returned),
            "active_camera": (
                {
                    "name": camera.name_full,
                    "identity": _object_identity(camera, scene_path),
                }
                if camera
                else None
            ),
            "frame": {
                "current": scene.frame_current,
                "start": scene.frame_start,
                "end": scene.frame_end,
                "step": scene.frame_step,
                "fps": render.fps,
                "fps_base": render.fps_base,
            },
            "render": {
                "engine": render.engine,
                "resolution_x": render.resolution_x,
                "resolution_y": render.resolution_y,
                "resolution_percentage": render.resolution_percentage,
                "image_format": render.image_settings.file_format,
            },
        },
        warnings,
    )


def _status(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    _strict_payload(payload, set(), set())
    return (
        {
            "blender_version": bpy.app.version_string,
            "blender_version_tuple": list(bpy.app.version),
            "background": bool(bpy.app.background),
            "adapter_schema_version": SCHEMA_VERSION,
            "commands": sorted(ALLOWED_COMMANDS),
        },
        [],
        [],
    )


def _inspect(payload: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    _strict_payload(
        payload,
        {"scene_path", "project_root", "include", "max_objects"},
        {"scene_path"},
    )
    include = payload.get("include")
    if include is not None and (
        not isinstance(include, list) or not all(isinstance(item, str) for item in include)
    ):
        raise AdapterError("INVALID_PAYLOAD", "include must be an array of strings.")
    max_objects = payload.get("max_objects", DEFAULT_MAX_OBJECTS)
    if (
        isinstance(max_objects, bool)
        or not isinstance(max_objects, int)
        or not 1 <= max_objects <= MAX_OBJECTS_LIMIT
    ):
        raise AdapterError(
            "INVALID_PAYLOAD",
            f"max_objects must be an integer from 1 to {MAX_OBJECTS_LIMIT}.",
        )
    path = _scene_path(payload)
    _open_scene(path)
    data, warnings = _scene_summary(path, max_objects)
    return data, [], warnings


def _validation_finding(
    code: str,
    severity: str,
    message: str,
    **details: Any,
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "details": details,
    }


def _validate_scene(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    _strict_payload(payload, {"scene_path", "project_root"}, {"scene_path"})
    scene_path = _scene_path(payload)
    _project_root(payload, scene_path)
    _open_scene(scene_path)
    scene = bpy.context.scene
    render = scene.render
    findings: list[dict[str, Any]] = []
    findings.extend(_scene_security_findings(payload, scene_path))

    if scene.camera is None:
        findings.append(
            _validation_finding(
                "CAMERA_REQUIRED",
                "blocker",
                "Scene has no active camera.",
            )
        )
    if scene.frame_end < scene.frame_start:
        findings.append(
            _validation_finding(
                "INVALID_FRAME_RANGE",
                "blocker",
                "Scene frame end is before frame start.",
                frame_start=scene.frame_start,
                frame_end=scene.frame_end,
            )
        )
    if render.fps <= 0 or render.fps_base <= 0:
        findings.append(
            _validation_finding(
                "INVALID_FPS",
                "blocker",
                "Scene FPS must be positive.",
                fps=render.fps,
                fps_base=render.fps_base,
            )
        )
    if render.resolution_x <= 0 or render.resolution_y <= 0:
        findings.append(
            _validation_finding(
                "INVALID_RESOLUTION",
                "blocker",
                "Render resolution must be positive.",
                width=render.resolution_x,
                height=render.resolution_y,
            )
        )

    stable_ids: dict[str, str] = {}
    for obj in sorted(scene.objects, key=lambda item: item.name_full):
        value = obj.get("_blender_mcp_id")
        if not isinstance(value, str) or not value.strip():
            continue
        stable_id = value.strip()
        if stable_id in stable_ids:
            findings.append(
                _validation_finding(
                    "DUPLICATE_STABLE_ID",
                    "blocker",
                    "Multiple objects use the same stable Blender MCP identifier.",
                    object_id=stable_id,
                    first_object=stable_ids[stable_id],
                    duplicate_object=obj.name_full,
                )
            )
        else:
            stable_ids[stable_id] = obj.name_full

    blockers = sum(item["severity"] == "blocker" for item in findings)
    errors = sum(item["severity"] == "error" for item in findings)
    return (
        {
            "scene_path": str(scene_path),
            "revision": f"sha256:{_file_sha256(scene_path)}",
            "ready_for_render": blockers == 0 and errors == 0,
            "summary": {
                "blockers": blockers,
                "errors": errors,
                "warnings": sum(
                    item["severity"] == "warning" for item in findings
                ),
                "info": sum(item["severity"] == "info" for item in findings),
            },
            "findings": findings,
        },
        [],
        [],
    )


def _preview_dimensions(scene: Any, max_width: int, max_height: int) -> tuple[int, int]:
    render = scene.render
    percentage = max(1, render.resolution_percentage) / 100.0
    source_width = max(1, int(render.resolution_x * percentage))
    source_height = max(1, int(render.resolution_y * percentage))
    scale = min(1.0, max_width / source_width, max_height / source_height)
    return max(1, int(source_width * scale)), max(1, int(source_height * scale))


def _integer(
    payload: dict[str, Any], key: str, default: int, minimum: int, maximum: int
) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AdapterError(
            "INVALID_PAYLOAD", f"{key} must be an integer from {minimum} to {maximum}."
        )
    return value


def _render_preview(
    payload: dict[str, Any], result_path: Path, request_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    _strict_payload(
        payload,
        {
            "scene_path",
            "project_root",
            "frame",
            "max_width",
            "max_height",
            "samples",
            "artifact_path",
        },
        {"scene_path"},
    )
    scene_path = _scene_path(payload)
    _project_root(payload, scene_path)
    source_hash_before = _file_sha256(scene_path)
    _open_scene(scene_path)
    _enforce_scene_security(payload, scene_path)
    scene = bpy.context.scene
    if scene.camera is None:
        raise AdapterError("CAMERA_REQUIRED", "Scene has no active camera.")

    frame = _integer(payload, "frame", scene.frame_current, scene.frame_start, scene.frame_end)
    max_width = _integer(payload, "max_width", 960, 1, MAX_PREVIEW_DIMENSION)
    max_height = _integer(payload, "max_height", 540, 1, MAX_PREVIEW_DIMENSION)
    samples = _integer(payload, "samples", 16, 1, 4096)
    artifact_raw = payload.get("artifact_path")
    if artifact_raw is None:
        artifact_path = result_path.parent / f"{request_id}.preview.png"
    elif not isinstance(artifact_raw, str) or not Path(artifact_raw).is_absolute():
        raise AdapterError("INVALID_ARTIFACT_PATH", "artifact_path must be absolute.")
    else:
        artifact_path = Path(artifact_raw).resolve()
    if artifact_path.suffix.lower() != ".png":
        raise AdapterError("INVALID_ARTIFACT_PATH", "Preview artifact must use a .png suffix.")
    if not artifact_path.parent.is_dir():
        raise AdapterError("INVALID_ARTIFACT_PATH", "Preview artifact directory does not exist.")

    width, height = _preview_dimensions(scene, max_width, max_height)
    scene.frame_set(frame)
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.use_file_extension = False
    if hasattr(scene, "cycles"):
        scene.cycles.samples = samples

    temporary_path = artifact_path.parent / f".{artifact_path.name}.{uuid.uuid4().hex}.tmp"
    scene.render.filepath = str(temporary_path)
    try:
        bpy.ops.render.render(write_still=True)
        if not temporary_path.is_file():
            raise AdapterError("RENDER_FAILED", "Blender did not produce a preview artifact.")
        os.replace(temporary_path, artifact_path)
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("RENDER_FAILED", "Blender preview render failed.") from exc
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass

    source_hash_after = _file_sha256(scene_path)
    if source_hash_after != source_hash_before:
        raise AdapterError("SOURCE_MUTATED", "Source .blend changed during preview rendering.")
    artifact = {
        "kind": "preview",
        "path": str(artifact_path),
        "media_type": "image/png",
        "size_bytes": artifact_path.stat().st_size,
        "sha256": _file_sha256(artifact_path),
        "width": width,
        "height": height,
    }
    return (
        {
            "scene_path": str(scene_path),
            "source_sha256": source_hash_after,
            "frame": frame,
            "artifact": artifact,
        },
        [artifact],
        [],
    )


def _render_animation(
    payload: dict[str, Any], result_path: Path
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    _strict_payload(
        payload,
        {
            "scene_path",
            "project_root",
            "artifact_path",
            "frame_start",
            "frame_end",
            "max_width",
            "max_height",
        },
        {"scene_path", "artifact_path"},
    )
    scene_path = _scene_path(payload)
    _project_root(payload, scene_path)
    source_hash_before = _file_sha256(scene_path)
    _open_scene(scene_path)
    _enforce_scene_security(payload, scene_path)
    scene = bpy.context.scene
    if scene.camera is None:
        raise AdapterError("CAMERA_REQUIRED", "Scene has no active camera.")

    frame_start = _integer(
        payload,
        "frame_start",
        scene.frame_start,
        0,
        1_000_000,
    )
    frame_end = _integer(
        payload,
        "frame_end",
        scene.frame_end,
        frame_start,
        1_000_000,
    )
    max_width = _integer(payload, "max_width", 1920, 16, MAX_PREVIEW_DIMENSION)
    max_height = _integer(payload, "max_height", 1080, 16, MAX_PREVIEW_DIMENSION)
    artifact_raw = payload.get("artifact_path")
    if not isinstance(artifact_raw, str) or not Path(artifact_raw).is_absolute():
        raise AdapterError("INVALID_ARTIFACT_PATH", "artifact_path must be absolute.")
    artifact_path = Path(artifact_raw).resolve()
    if artifact_path.suffix.lower() != ".json":
        raise AdapterError(
            "INVALID_ARTIFACT_PATH",
            "Animation frame-sequence manifest must use a .json suffix.",
        )
    if not artifact_path.parent.is_dir():
        raise AdapterError("INVALID_ARTIFACT_PATH", "Render artifact directory does not exist.")
    sequence_path = artifact_path.with_suffix("")
    if artifact_path.exists() or sequence_path.exists():
        raise AdapterError(
            "ARTIFACT_EXISTS",
            "Animation artifact already exists.",
        )

    width, height = _preview_dimensions(scene, max_width, max_height)
    scene.frame_start = frame_start
    scene.frame_end = frame_end
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.use_file_extension = True

    temporary_path = Path(
        tempfile.mkdtemp(
            prefix=f".{sequence_path.name}.",
            suffix=".tmp",
            dir=artifact_path.parent,
        )
    )
    scene.render.filepath = str(temporary_path / "frame_")
    try:
        bpy.ops.render.render(animation=True)
        rendered_frames = sorted(temporary_path.glob("frame_*.png"))
        expected_count = frame_end - frame_start + 1
        if len(rendered_frames) != expected_count:
            raise AdapterError(
                "RENDER_FAILED",
                "Blender did not produce the expected frame sequence.",
                {
                    "expected_frames": expected_count,
                    "rendered_frames": len(rendered_frames),
                },
            )
        os.replace(temporary_path, sequence_path)
    except AdapterError:
        raise
    except Exception as exc:
        raise AdapterError("RENDER_FAILED", "Blender animation render failed.") from exc
    finally:
        shutil.rmtree(temporary_path, ignore_errors=True)

    source_hash_after = _file_sha256(scene_path)
    if source_hash_after != source_hash_before:
        raise AdapterError("SOURCE_MUTATED", "Source .blend changed during animation rendering.")
    frame_count = frame_end - frame_start + 1
    fps = float(scene.render.fps) / float(scene.render.fps_base)
    frame_paths = sorted(sequence_path.glob("frame_*.png"))
    frames = [
        {
            "frame": frame_start + index,
            "path": frame_path.relative_to(artifact_path.parent).as_posix(),
            "size_bytes": frame_path.stat().st_size,
            "sha256": _file_sha256(frame_path),
        }
        for index, frame_path in enumerate(frame_paths)
    ]
    sequence_manifest = {
        "schema_version": SCHEMA_VERSION,
        "kind": "blender_frame_sequence",
        "scene_path": str(scene_path),
        "source_sha256": source_hash_after,
        "frame_start": frame_start,
        "frame_end": frame_end,
        "frame_count": frame_count,
        "fps": fps,
        "width": width,
        "height": height,
        "frames": frames,
    }
    _atomic_write_json(artifact_path, sequence_manifest)
    artifact = {
        "kind": "frame_sequence",
        "path": str(artifact_path),
        "media_type": "application/json",
        "size_bytes": artifact_path.stat().st_size,
        "sha256": _file_sha256(artifact_path),
        "width": width,
        "height": height,
        "duration_seconds": frame_count / fps,
        "frame_count": frame_count,
    }
    return (
        {
            "scene_path": str(scene_path),
            "source_sha256": source_hash_after,
            "frame_start": frame_start,
            "frame_end": frame_end,
            "artifact": artifact,
        },
        [artifact],
        [],
    )


def _stable_id_map(values: Any, *, kind: str) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for value in values:
        stable_id = value.get("_blender_mcp_id")
        if not isinstance(stable_id, str) or not stable_id.strip():
            continue
        normalized = stable_id.strip()
        if normalized in indexed:
            raise AdapterError(
                "DUPLICATE_STABLE_ID",
                f"Multiple {kind} values use the same stable ID.",
                {"stable_id": normalized},
            )
        indexed[normalized] = value
    return indexed


def _apply_scene_transaction(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    _strict_payload(
        payload,
        {
            "scene_path",
            "project_root",
            "output_path",
            "transaction_id",
            "expected_source_sha256",
            "operations",
        },
        {
            "scene_path",
            "output_path",
            "transaction_id",
            "expected_source_sha256",
            "operations",
        },
    )
    scene_path = _scene_path(payload)
    _project_root(payload, scene_path)
    transaction_id = payload.get("transaction_id")
    if (
        not isinstance(transaction_id, str)
        or not transaction_id.startswith("txn_")
        or not REQUEST_ID_PATTERN.fullmatch(f"req_{transaction_id[4:]}")
    ):
        raise AdapterError(
            "INVALID_TRANSACTION_ID",
            "transaction_id has an invalid format.",
        )
    expected_source_sha256 = payload.get("expected_source_sha256")
    if (
        not isinstance(expected_source_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", expected_source_sha256)
    ):
        raise AdapterError(
            "INVALID_PAYLOAD",
            "expected_source_sha256 must be a lowercase SHA-256 digest.",
        )
    source_sha256 = _file_sha256(scene_path)
    if source_sha256 != expected_source_sha256:
        raise AdapterError(
            "SCENE_REVISION_CONFLICT",
            "The scene changed after the transaction was prepared.",
            {
                "expected_source_sha256": expected_source_sha256,
                "actual_source_sha256": source_sha256,
            },
        )
    output_raw = payload.get("output_path")
    if not isinstance(output_raw, str) or not Path(output_raw).is_absolute():
        raise AdapterError("INVALID_OUTPUT_PATH", "output_path must be absolute.")
    output_path = Path(output_raw).resolve()
    if output_path.suffix.lower() != ".blend":
        raise AdapterError("INVALID_OUTPUT_PATH", "output_path must use a .blend suffix.")
    if not output_path.parent.is_dir():
        raise AdapterError("INVALID_OUTPUT_PATH", "output directory does not exist.")
    if output_path.exists():
        raise AdapterError("ARTIFACT_EXISTS", "Transaction output already exists.")

    operations = payload.get("operations")
    _open_scene(scene_path)
    _enforce_scene_security(payload, scene_path)
    scene = bpy.context.scene
    objects_by_id = _stable_id_map(scene.objects, kind="object")
    actions_by_id = _stable_id_map(bpy.data.actions, kind="action")
    try:
        applied = apply_operations(
            scene,
            operations,
            objects_by_id=objects_by_id,
            actions_by_id=actions_by_id,
        )
    except OperationExecutionError as exc:
        raise AdapterError(
            exc.code,
            exc.message,
            {
                "operation_index": exc.operation_index,
                **exc.details,
            },
        ) from exc

    try:
        bpy.ops.wm.save_as_mainfile(
            filepath=str(output_path),
            check_existing=False,
        )
    except Exception as exc:
        raise AdapterError(
            "SCENE_SAVE_FAILED",
            "Blender could not save the transaction result.",
        ) from exc
    if not output_path.is_file():
        raise AdapterError(
            "SCENE_SAVE_FAILED",
            "Blender did not produce the transaction output.",
        )
    if _file_sha256(scene_path) != source_sha256:
        raise AdapterError(
            "SOURCE_MUTATED",
            "Source .blend changed during transaction execution.",
        )
    artifact = {
        "kind": "scene",
        "path": str(output_path),
        "media_type": "application/x-blender",
        "size_bytes": output_path.stat().st_size,
        "sha256": _file_sha256(output_path),
    }
    return (
        {
            "transaction_id": transaction_id,
            "scene_path": str(scene_path),
            "source_sha256": source_sha256,
            "output_path": str(output_path),
            "output_sha256": artifact["sha256"],
            "applied_operations": applied,
        },
        [artifact],
        [],
    )


def _dispatch(
    command: str,
    payload: dict[str, Any],
    result_path: Path,
    request_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    if command == "status":
        return _status(payload)
    if command == "inspect_scene":
        return _inspect(payload)
    if command == "validate_scene":
        return _validate_scene(payload)
    if command == "apply_scene_transaction":
        return _apply_scene_transaction(payload)
    if command == "render_preview":
        return _render_preview(payload, result_path, request_id)
    if command == "render_animation":
        return _render_animation(payload, result_path)
    raise AdapterError("COMMAND_NOT_ALLOWED", "Command is not in the adapter allowlist.")


def _response_base(request_id: str | None, command: str | None, started: float) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "command": command,
        "warnings": [],
        "artifacts": [],
        "timing": {"execution_ms": round((time.monotonic() - started) * 1000, 3)},
    }


def main() -> int:
    started = time.monotonic()
    result_path: Path | None = None
    request_id: str | None = None
    command: str | None = None
    try:
        request_path, result_path = _parse_cli(sys.argv)
        request = _read_request(request_path)
        request_id_value = request.get("request_id")
        command_value = request.get("command")
        request_id = request_id_value if isinstance(request_id_value, str) else None
        command = command_value if isinstance(command_value, str) else None
        request_id, command, payload = _validate_request(request)
        data, artifacts, warnings = _dispatch(command, payload, result_path, request_id)
        response = _response_base(request_id, command, started)
        response.update({"ok": True, "data": data, "artifacts": artifacts, "warnings": warnings})
        _atomic_write_json(result_path, response)
        return 0
    except AdapterError as exc:
        response = _response_base(request_id, command, started)
        response.update(
            {
                "ok": False,
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                },
            }
        )
    except Exception:
        response = _response_base(request_id, command, started)
        response.update(
            {
                "ok": False,
                "error": {
                    "code": "INTERNAL_ERROR",
                    "message": "Unexpected adapter failure.",
                    "details": {},
                },
            }
        )
        traceback.print_exc(file=sys.stderr)

    if result_path is not None:
        try:
            _atomic_write_json(result_path, response)
        except Exception:
            traceback.print_exc(file=sys.stderr)
    else:
        print(json.dumps(response, sort_keys=True), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
