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
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any

import bpy


SCHEMA_VERSION = "1.0"
ALLOWED_COMMANDS = frozenset({"status", "inspect_scene", "render_preview"})
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
    _strict_payload(payload, {"scene_path", "include", "max_objects"}, {"scene_path"})
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
        {"scene_path", "frame", "max_width", "max_height", "samples", "artifact_path"},
        {"scene_path"},
    )
    scene_path = _scene_path(payload)
    source_hash_before = _file_sha256(scene_path)
    _open_scene(scene_path)
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
    if command == "render_preview":
        return _render_preview(payload, result_path, request_id)
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
