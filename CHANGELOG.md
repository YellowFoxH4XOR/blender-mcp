# Changelog

All notable changes use semantic versioning.

## Unreleased

- Added TOML configuration, project initialization, and diagnostics.
- Added approved asset catalogs and template scene creation.
- Added scene revisions, constrained atomic transactions, checkpoints, and
  rollback-safe Blender outputs.
- Added scene validation and animation frame-sequence rendering.
- Added SQLite WAL render jobs, cancellation, restart recovery, and artifact
  manifests.
- Added a pinned Remotion composition that turns Blender sequences into MP4.
- Added a real template-to-transaction-to-preview-to-MP4 end-to-end test.
- Added an authenticated, loopback-only Streamable HTTP worker for Codex on
  macOS, including private-token and LaunchAgent primitives.
- Added a full Blender runtime doctor probe and an external-worker verification
  script.
- Added explicit Node, npm, and Remotion paths so background services do not
  depend on an interactive shell `PATH`.

## 0.1.0 - 2026-07-30

- Initial M0 status, inspection, preview, path policy, adapter contract, and
  security tests.
