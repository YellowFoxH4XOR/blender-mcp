# Blender MCP

A headless-first Model Context Protocol server for safe, deterministic Blender
automation.

## Status

This repository is under active development. The first milestone is deliberately
small:

1. discover and verify Blender;
2. inspect a `.blend` scene through a versioned JSON contract;
3. render a preview still without mutating the source scene; and
4. expose those capabilities as typed MCP tools.

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
deferred until the headless path is reliable.

## Repository layout

```text
src/blender_mcp/    MCP server and application core
blender_adapter/    bundled scripts executed by Blender
schemas/            versioned JSON contracts
fixtures/           reproducible Blender fixtures
tests/              unit, security, integration, and end-to-end tests
docs/               contracts and architecture notes
```

## Setup

```bash
cd /Users/akki/Desktop/github/blender-mcp
uv sync --python 3.12 --extra dev
```

Choose a project root containing the `.blend` files and preview outputs that
the server is allowed to access:

```bash
export BLENDER_MCP_PROJECT_ROOT=/absolute/path/to/blender-project
export BLENDER_MCP_BLENDER_EXECUTABLE=/Applications/Blender.app/Contents/MacOS/Blender
uv run blender-mcp
```

The project root is a security boundary. MCP paths must be relative to it;
absolute paths, traversal, symlink escapes, and unexpected file extensions are
rejected.

## Codex configuration

```toml
[mcp_servers.blender]
command = "/opt/homebrew/bin/uv"
args = [
  "run",
  "--project",
  "/Users/akki/Desktop/github/blender-mcp",
  "blender-mcp",
]
cwd = "/Users/akki/Desktop/github/blender-mcp"
startup_timeout_sec = 20
tool_timeout_sec = 300
default_tools_approval_mode = "writes"
required = false

[mcp_servers.blender.env]
BLENDER_MCP_PROJECT_ROOT = "/absolute/path/to/blender-project"
BLENDER_MCP_BLENDER_EXECUTABLE = "/Applications/Blender.app/Contents/MacOS/Blender"
```

Restart Codex after changing MCP configuration.

## M0 tools

- `get_blender_status`
- `inspect_scene`
- `render_preview`

The server deliberately has no arbitrary Python, shell, add-on installation,
URL download, or external-asset tool.

## Verification

Run tests that do not require Blender:

```bash
uv run pytest tests/unit tests/security tests/contract -q
```

Run the real Blender integration suite:

```bash
uv run pytest tests/integration/test_blender_adapter.py -q
```

On the reference macOS machine, Blender 5.2 can crash during native startup
when launched inside the Codex application sandbox. The identical test passes
when run through an approved unsandboxed Blender process or directly from
Terminal. The launcher classifies an early segmentation fault as
`BLENDER_CRASHED`; it is not reported as an adapter validation failure.

See [the frozen M0 contract](docs/M0_CONTRACT.md) for the exact boundaries,
schemas, errors, and deferred capabilities.
