# Security Policy

## Supported versions

Only the current `main` branch is supported before the first stable release.

## Reporting

Do not open a public issue for a suspected vulnerability. Use GitHub's private
security-advisory reporting for `YellowFoxH4XOR/blender-mcp`.

Include the affected commit, operating system, Blender version, minimal
reproduction, expected boundary, and observed result. Do not include real API
keys, credentials, or private project assets.

## Security boundaries

- All MCP input is untrusted.
- Public paths must stay inside one configured project root.
- Blender executes only bundled adapter code and an allowlisted operation DSL.
- Arbitrary Python, shell commands, add-on loading, URLs, and network tools are
  not supported.
- Mutations use checkpoints and temporary `.blend` outputs before atomic
  replacement.
- Blender, Remotion, and their descendants run without shell interpolation.
- Asset catalogs approve local paths; they do not download content.
- The macOS worker binds only to loopback and requires an exact bearer token.
- Worker tokens and Codex configuration containing static authorization headers
  must be readable only by the owning user.
- The HTTP worker has no generic command endpoint and rejects oversized request
  bodies before MCP dispatch.

Reports that require arbitrary Python or network access are requests to remove
the security model, not missing features.
