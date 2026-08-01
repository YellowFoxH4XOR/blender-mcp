# Blender MCP Headless V1 Workflow

Status: implemented vertical slice on macOS Apple Silicon
Protocol schema: `1.0`

## Workflow

1. `list_project_assets` resolves only catalog-approved project assets.
2. `create_scene_from_template` copies and validates a template before atomic
   publication.
3. `inspect_scene` returns stable object IDs and an optimistic scene revision.
4. `apply_scene_transaction` validates a bounded operation list, creates a
   checkpoint, executes in an isolated Blender process, and atomically replaces
   the scene only after success.
5. `validate_scene` blocks a render when required camera, frame, FPS,
   resolution, or stable-ID rules fail.
6. `render_preview` publishes a non-mutating PNG review artifact.
7. `start_render` persists a queued job in SQLite WAL and returns immediately.
8. Blender renders a PNG frame sequence and an immutable sequence manifest.
9. Remotion consumes the sequence through a project-root public directory and
   produces the MP4.
10. The artifact manager verifies hashes and atomically publishes a final JSON
    manifest before the job becomes `succeeded`.

## Codex process boundary on macOS

The recommended deployment is:

`Codex -> authenticated 127.0.0.1 Streamable HTTP -> launchd worker -> Blender`

The HTTP boundary requires an exact bearer token, rejects duplicate or missing
authorization headers, enforces a bounded request body, and refuses non-loopback
bind hosts. It exposes the MCP endpoint and health check only; there is no
generic command or Python execution route.

This boundary is operationally necessary on the reference macOS environment:
Blender is signed, notarized, and runs normally outside Codex, but direct native
startup from the Codex application sandbox can fail before Blender initializes.
The external worker avoids treating that host restriction as a Blender or
adapter failure.

## Transaction operations

- `set_transform`
- `set_visibility`
- `configure_scene`
- `apply_action`

Every batch is completely validated before the first mutation. Unknown
operations, unknown fields, non-finite values, unapproved IDs, and batches over
100 operations are rejected.

## Recovery and cancellation

Job states are durable:

`queued -> starting -> running -> succeeded|failed`

Cancellation uses:

`queued|starting|running -> cancelling -> cancelled`

Nonterminal jobs found during startup recovery become `orphaned`. Blender and
Remotion launch in their own process groups so cancellation and timeouts can
terminate descendants without using a shell.

## Remaining release-candidate work

- Durable SQLite transaction idempotency and partial-render resume across
  server restarts.
- Granular render progress reporting and long-running cancellation/load soak
  tests.
- Broader `doctor` checks for the Node, Remotion/Chrome, SQLite, and output
  environment.
- Optional authenticated live Blender add-on and headless fallback.
- Windows x64 and Linux x64 certification.
- Fresh-machine installers and release CI.
- Expanded character asset packs, action libraries, audio, and captions.
