# Blender MCP

A headless-first Model Context Protocol server for safe, deterministic Blender
automation.

## Status

The macOS headless workflow is implemented end to end:

`approved template -> inspect -> atomic transaction -> validate -> preview ->`
`durable render job -> Blender PNG sequence -> Remotion MP4 -> manifest`

The repository is not yet a cross-platform V1 release candidate. The optional
live Blender add-on and Windows/Linux certification remain separate milestones.

The project does **not** accept arbitrary Python or shell commands from MCP
clients.

## Design principles

- Headless Blender is the authoritative execution engine.
- MCP tools are thin wrappers around testable application services.
- Blender and the MCP server run in separate Python environments.
- Every Blender request uses a versioned, allowlisted JSON envelope.
- Files are restricted to one configured project root.
- Source scenes are not overwritten by inspection or preview operations.
- Failures return stable codes and request IDs.

## Development target

- macOS Apple Silicon
- Blender 5.2 LTS
- Python 3.12
- MCP Python SDK 2.x

Additional platforms and an interactive Blender add-on are intentionally
deferred until the headless path completes release hardening.

## Repository layout

```text
src/blender_mcp/    MCP server and application core
blender_adapter/    bundled scripts executed by Blender
schemas/            versioned JSON contracts
fixtures/           reproducible Blender fixtures
tests/              unit, security, integration, and end-to-end tests
docs/               contracts and architecture notes
remotion/            pinned frame-sequence-to-MP4 renderer
```

## Setup

```bash
cd /Users/akki/Desktop/github/blender-mcp
uv sync --python 3.12 --extra dev
cd remotion
npm ci
npx remotion browser ensure
cd ..
uv run blender-mcp init /absolute/path/to/blender-project
uv run blender-mcp doctor --config /absolute/path/to/blender-project/blender-mcp.toml
uv run blender-mcp worker install \
  --config /absolute/path/to/blender-project/blender-mcp.toml
```

Choose a project root containing the `.blend` files and preview outputs that
the server is allowed to access:

```bash
uv run blender-mcp serve \
  --config /absolute/path/to/blender-project/blender-mcp.toml
```

The project root is a security boundary. MCP paths must be relative to it;
absolute paths, traversal, symlink escapes, and unexpected file extensions are
rejected.

## Codex configuration

On macOS, the dependable Codex setup is an authenticated loopback worker
managed by `launchd`. Do not make Codex launch Blender as its own child process:
the Codex application sandbox can terminate an otherwise valid Blender
installation before Blender initializes. The worker keeps Blender outside that
sandbox while exposing only the constrained MCP tools on `127.0.0.1`.

The installed worker must use:

- a dedicated Python 3.12 virtual environment;
- an absolute Blender executable in `blender-mcp.toml`;
- an absolute Node executable and Remotion project;
- a private bearer-token file (`0600`);
- the `streamable-http` transport bound to loopback only.

`worker install` validates the complete offline runtime, creates an owner-only
bearer-token file, writes the user LaunchAgent, and starts it with `launchctl`.
It prints the token-file location and MCP URL. The command is macOS-only and
must be run from a Terminal checkout so Blender and Remotion remain outside the
Codex application sandbox.

The resulting Codex entry is:

```toml
[mcp_servers.blender]
url = "http://127.0.0.1:9876/mcp"
http_headers = { "Authorization" = "Bearer REPLACE_WITH_PRIVATE_TOKEN" }
startup_timeout_sec = 30
tool_timeout_sec = 21600
default_tools_approval_mode = "writes"
required = false
```

Keep `~/.codex/config.toml` private (`chmod 600`) because this form contains the
token. Restart Codex after changing MCP configuration. STDIO remains supported
for Terminal and clients that can safely spawn Blender:

```toml
[mcp_servers.blender]
command = "/absolute/path/to/venv/bin/blender-mcp"
args = [
  "serve",
  "--config",
  "/absolute/path/to/blender-project/blender-mcp.toml",
]
```

## MCP tools

- `get_blender_status`
- `inspect_scene`
- `list_project_assets`
- `validate_scene`
- `create_scene_from_template`
- `apply_scene_transaction`
- `render_preview`
- `start_render`
- `get_job`
- `cancel_job`
- `save_scene_as`
- `restore_checkpoint`

The server deliberately has no arbitrary Python, shell, add-on installation,
URL download, or external-asset tool.

## Approved assets

Character rigs, actions, materials, models, and templates must be declared in
`assets/catalog.toml` inside the configured project root:

```toml
schema_version = "1"

[[templates]]
id = "character-stage-v1"
path = "templates/character-stage.blend"
version = "1.0.0"
license = "CC0-1.0"

[[actions]]
id = "wave-v1"
path = "assets/actions/wave.blend"
version = "1.0.0"
license = "CC0-1.0"
```

The AI client never submits Python. It inspects the selected scene revision and
submits an allowlisted operation list such as `set_transform`,
`set_visibility`, `configure_scene`, or `apply_action`.

## Render pipeline

`start_render` returns immediately with a durable job ID. Poll with `get_job`
or stop with `cancel_job`. Blender renders a deterministic PNG sequence; the
pinned Remotion composition consumes the frame manifest and produces the final
MP4. Both the MP4 and its JSON artifact manifest contain verified SHA-256
metadata. Interrupted jobs are recovered as `orphaned`; automatic partial-frame
resume is still release-candidate work.

## Verification

Run tests that do not require Blender:

```bash
uv run pytest tests/unit tests/security tests/contract -q
```

Run the real Blender integration suite:

```bash
uv run pytest tests/integration/test_blender_adapter.py -q
```

Run the actual Blender-to-Remotion MP4 workflow:

```bash
uv run pytest tests/e2e/test_blender_to_remotion.py -q
```

On the reference macOS machine, Blender 5.2 and Remotion's Chrome process can
be blocked during native startup inside the Codex application sandbox. The
same signed and notarized Blender installation passes from the external
loopback worker or Terminal. Reinstalling Blender does not fix this sandbox
boundary.

Verify the external worker through the real MCP transport:

```bash
/absolute/path/to/worker-venv/bin/python \
  scripts/verify_external_worker.py \
  --token-file /absolute/path/to/private-token
```

This creates a catalog-approved scene, applies a revision-checked transaction,
validates it, renders a preview, runs the durable Blender-to-Remotion job, and
fails unless the final state is `succeeded`.

See [the frozen M0 contract](docs/M0_CONTRACT.md) for the exact boundaries,
and [the headless V1 workflow](docs/HEADLESS_V1.md) for the implemented
transaction and render architecture.
