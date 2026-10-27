# Release 1.4.11

Scope: v1.4.10..v1.4.11. Themes: the MCP client beta, speculative decoding back on for the coder, a leaner installer, and a repository that keeps main honest.

## MCP client (beta)

- HiveMind can use **external MCP servers** as tools, the other direction of
  the existing MCP server. Off by default: `mcp_client_enabled` plus a
  `mcp_servers` list of `{name, command, args, env, cwd, enabled}`.
- Tools appear as `mcp_<server>_<tool>` (capped at 64 characters) with the
  server's `inputSchema` passed through. A schema over 8 kB is replaced by an
  empty one instead of blowing up the request.
- `GET /mcp/servers` shows what is configured, running and discovered;
  `POST /mcp/reload` starts the servers and rebuilds the tool list. Servers
  start once at boot and every child is closed on shutdown.
- A server that hangs, crashes or prints garbage never breaks a run: the call
  returns a tool error and the connection is dead until the next reload. One
  child process per server, the whole tree is killed on a timeout, and an entry
  that leaves the config is closed. stdio only, 8 servers, 64 tools each.
- MCP tools follow the mode gate (write modes only), pass the approval gate,
  and stay blocked for phone runs.

## Coder speed: speculative decoding is back on

- The hermes 35B coder runs with draft-mtp again. Measured on b11889 with
  256-token answers: decode 20.0 -> 31.05 t/s (+55 %), prefill 122.1 -> 120.15
  (-1.6 %), acceptance 0.823. The 2026-10-24 decision was based on 220-token
  answers and a prefill penalty that does not reproduce.
- Reference measurement for `qwen3.5:4b-mtp`: decode 55.5 -> 85.35 t/s
  (+53.8 %), prefill 610 -> 568 (-6.9 %), acceptance 0.911.

## Installer

- `--no-drafters` skips the drafter sidecars; `--essential` implies it. With
  speculative decoding off for the shipped profiles those files (60 MB per
  Gemma, 620 MB LFM, 650 MB MiniCPM) were downloaded and never read.

## Diagnostics

- A failed write under Desktop, Documents, Pictures or a OneDrive folder now
  names the real cause: Controlled Folder Access and OneDrive locking, with the
  way out. Every other folder keeps the plain message.

## Repository

- `main` tracks the released state again and `deploy/promote_main.bat` keeps it
  there: fast-forward only, never a merge.
- `deploy/prepush_check.ps1` brings the 13 pre-push checks back into the repo.
- README no longer pins a release number or a zip name.
