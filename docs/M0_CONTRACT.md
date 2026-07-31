# Blender MCP M0 Contract

Status: frozen for M0  
Schema version: `1.0`

M0 proves one dependable path:

`MCP client -> server validation -> headless Blender adapter -> structured result`

It includes status discovery, scene inspection, and a single-frame PNG preview. It
does not include scene mutation, arbitrary Python, animation rendering, live
Blender control, external downloads, or final MP4 production.

## 1. Trust boundaries

The public MCP boundary accepts project-relative paths only. The server resolves
and validates those paths before launching Blender.

The server-to-adapter boundary is private and may contain absolute paths created
by the server. The adapter must not be directly exposed as an MCP tool. An
adapter `artifact_path`, when supplied, is the absolute `.png` target already
validated by the server's project-root policy. When omitted during direct
adapter testing, the adapter derives `<job_dir>/<request_id>.preview.png`.

No M0 request may contain Python source, a shell command, an executable path, an
add-on, a URL, or a network location.

## 2. File IPC

Requests and results are UTF-8 JSON files. The server writes a request to a
private per-job directory and invokes a bundled, versioned adapter. The adapter
writes the result to a temporary sibling and atomically renames it to the agreed
result path.

The JSON Schemas in `schemas/` formalize the private adapter file-IPC
envelopes. Public MCP inputs are the typed service models described separately
below. The common adapter request envelope is:

```json
{
  "schema_version": "1.0",
  "request_id": "req_01J...",
  "command": "inspect_scene",
  "payload": {
    "scene_path": "/resolved/project/scenes/example.blend"
  }
}
```

Required request fields:

- `schema_version`: exactly `1.0`.
- `request_id`: `req_` followed by 1-120 ASCII letters, digits, `_`, or `-`.
- `command`: exactly `status`, `inspect_scene`, or `render_preview`.
- `payload`: the command-specific object; unknown properties are rejected.

The maximum serialized request size is 1,048,576 bytes. The limit is checked
before JSON parsing when possible and again before dispatch. The server never
dispatches an unknown command.

The common adapter result envelope is:

```json
{
  "schema_version": "1.0",
  "request_id": "req_01J...",
  "command": "inspect_scene",
  "ok": true,
  "data": {},
  "warnings": [],
  "artifacts": [],
  "timing": {
    "execution_ms": 216
  }
}
```

Every result includes `schema_version`, `request_id`, `command`, `ok`,
`warnings`, `artifacts`, and `timing`. A successful result contains `data` and
must not contain `error`. A failed result contains `error` and must not contain
`data`.

The server must verify that a result's request ID, command, and schema version
match the request. A mismatch is a protocol failure, never a successful tool
result.

### Error object

```json
{
  "code": "PATH_OUTSIDE_PROJECT",
  "message": "The requested path is outside the configured project root.",
  "details": {}
}
```

`code` is a stable, uppercase identifier. `message` is safe for users and must
not include secrets or raw environment dumps. `details` is structured JSON.
Retry guidance is deferred until it is implemented consistently across the
server and adapter.

M0 reserves these codes:

| Code | Meaning |
|---|---|
| `INVALID_REQUEST` | JSON or schema validation failed |
| `REQUEST_TOO_LARGE` | Request exceeded 1 MiB |
| `COMMAND_NOT_ALLOWED` | Adapter command is not allowlisted |
| `INVALID_PATH` | Empty, malformed, NUL-containing, or absolute public path |
| `PATH_OUTSIDE_PROJECT` | Traversal or symlink resolution escaped the root |
| `UNSUPPORTED_FILE_TYPE` | File extension is not allowed for the operation |
| `PATH_NOT_FOUND` | Required input does not exist as a regular file |
| `PATH_ALREADY_EXISTS` | Output collision without explicit overwrite authorization |
| `BLENDER_NOT_FOUND` | Configured/discovered executable was not found |
| `BLENDER_TIMEOUT` | Command exceeded its wall-clock timeout |
| `BLENDER_CRASHED` | Blender terminated with a segmentation fault before returning a result |
| `BLENDER_FAILED` | Blender or its adapter exited unsuccessfully |
| `ADAPTER_INVALID_RESPONSE` | Adapter result was missing, invalid, or mismatched |
| `ARTIFACT_MISSING` | Adapter reported success without the requested output |
| `INTERNAL_ERROR` | Sanitized unexpected server failure |

## 3. Command payloads

### `status`

Payload is exactly `{}`.

At the adapter boundary, the result reports Blender version, background mode,
schema version, and the command allowlist. The public status tool reports
executable availability, its version, and whether the bundled adapter exists.
M0 does not promise GPU enumeration or a live-bridge status.

### `inspect_scene`

Public payload:

```json
{
  "scene_path": "scenes/example.blend"
}
```

`scene_path` is a project-relative `.blend` file. The private adapter contract
also accepts an allowlisted `include` array and `max_objects` from 1-1,000;
those controls are not public MCP parameters in M0.

The result is deterministic: objects are ordered by Blender full name. Each
object includes a stable custom identifier when present or a clearly marked
deterministic fallback. If output is truncated, the result includes
`object_count`, `objects_returned`, and a warning. M0 does not expose binary
mesh data.

### `render_preview`

Public payload:

```json
{
  "scene_path": "scenes/example.blend",
  "output_path": "artifacts/example-preview.png",
  "max_width": 854,
  "max_height": 480,
  "allow_overwrite": false
}
```

M0 renders one PNG frame only. Public width and height limits are 64-7680 and
64-4320. The caller supplies a project-relative `.png` output and must
explicitly authorize replacement.

The trusted adapter payload uses a server-resolved absolute `artifact_path` and
also accepts `frame` and `samples`; these two controls are not yet public MCP
parameters. The source `.blend` must remain byte-for-byte unchanged. Adapter
artifacts use `kind: "preview"` and `media_type: "image/png"`; the public
service returns a validated project-relative path, media type, size, and hash.

## 4. Path and overwrite policy

The M0 policy interface is:

```python
from blender_mcp.policy.paths import PathPolicyError, ProjectPathPolicy

policy = ProjectPathPolicy(
    project_root,
    max_request_bytes=1_048_576,
)
policy.resolve_input(relative_path, allowed_suffixes=frozenset({".blend"}))
policy.resolve_output(
    relative_path,
    allow_overwrite=False,
    allowed_suffixes=frozenset({".png"}),
)
policy.validate_request_size(size_bytes)
```

Rules:

1. `project_root` is resolved to its real path when policy is constructed.
2. Public paths must be non-empty, relative, and free of NUL bytes.
3. POSIX absolute paths, Windows drive paths, UNC paths, and both slash styles
   of parent traversal are rejected on every host.
4. Inputs must resolve to an existing regular file inside the real project root.
5. Existing symlinks are resolved. A symlink that escapes the root is rejected.
6. For outputs that do not yet exist, the nearest existing parent is resolved;
   a symlinked parent that escapes the root is rejected.
7. Suffix checks are case-insensitive and use the final filename suffix.
8. Existing outputs are rejected unless `allow_overwrite` is exactly `True`.
9. Overwrite authorization applies only to the exact validated target. It does
   not authorize deleting directories or following a changed symlink.
10. The adapter renders to a temporary sibling and atomically replaces the
    exact validated target.
11. `render_preview` exposes `allow_overwrite`; it defaults to false.

`PathPolicyError` exposes a stable `.code` corresponding to the error table.

## 5. Timeouts and limits

| Limit | M0 value |
|---|---:|
| Serialized request | 1 MiB |
| Blender version probe | 10 seconds |
| Default adapter command wall clock | 120 seconds |
| Adapter inspect objects accepted | 1,000 |
| Public preview dimensions | 7680 x 4320 |
| Adapter preview dimension per axis | 4,096 |
| Adapter preview samples | 4,096 |

M0 uses Python's subprocess timeout. Process-group termination, bounded process
output capture, and separate per-command budgets remain hardening work. Partial
results are not accepted as success; PNG publication itself is atomic.

### Codex macOS sandbox diagnostic

On the reference machine, Blender 5.2 can terminate with `SIGSEGV` during native
startup when launched inside the Codex application sandbox. The macOS report
identifies the `com.openai.codex` coalition, and Blender emits
`Arch_ValidateAssumptions` before the adapter begins executing. The identical
headless commands pass outside that sandbox. The launcher reports this as
`BLENDER_CRASHED`; release integration tests must therefore be run in the same
unsandboxed execution environment intended for production Blender jobs.

## 6. M0 acceptance criteria

M0 passes only when all of the following are automated:

1. Status discovers a supported Blender executable and returns a matching,
   schema-valid result.
2. Inspection opens a fixture `.blend` and returns structured scene metadata.
3. Preview produces a valid PNG with size and verified SHA-256; the adapter
   result additionally reports dimensions.
4. The fixture `.blend` hash is identical before and after inspection/preview.
5. Invalid JSON, unknown adapter-envelope fields, unknown commands, forbidden
   command-like fields, and requests over 1 MiB are rejected before dispatch.
6. Absolute paths, POSIX and Windows traversal, missing inputs, wrong
   extensions, and symlink escapes are rejected.
7. Output collision is rejected unless the internal caller explicitly passes
   `allow_overwrite=True`.
8. Timeout returns `BLENDER_TIMEOUT` and does not report a partial artifact as
   successful. Descendant process-tree verification is deferred.
9. Adapter failure, malformed result, and request/result correlation mismatch
   return structured errors.
10. Unit/security tests run without Blender. Integration tests that require
    Blender are separately marked and report a clear skip when unavailable.

The PRD's broader V1 claims—transactions, rollback, persistent jobs,
cancellation, recovery, live control, and cross-platform certification—are
explicitly outside M0 and must not be inferred from this milestone.
