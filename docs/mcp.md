# MCP / IDE integration

HiveMind is usable from an IDE in two directions. Both are local-only by default.

> The opposite direction, using **external** MCP servers as tools, is the
> MCP client beta: [mcp_client.md](mcp_client.md).

## 1. HiveMind → IDE (MCP server, port 8090)

`start_mcp.bat` starts an MCP server (`infra/mcp_server.py`) with stdio and an
HTTP transport. It exposes read tools always, write and exec tools only as an
opt-in.

| Transport | Read | Write | Exec (`shell`, `run_bash`, `run_python`, `run_tests`) | `query` |
|---|---|---|---|---|
| HTTP (`127.0.0.1:8090/mcp`) | on | `MCP_ALLOW_WRITE=1` | **never** | `MCP_ALLOW_QUERY=1` |
| stdio | on | `MCP_ALLOW_WRITE=1` | `MCP_ALLOW_EXEC=1` | `MCP_ALLOW_QUERY=1` |

The `query` tool is opt-in (`MCP_ALLOW_QUERY=1`) on **both** transports.

Tools: `read_file`, `list_dir`, `find_files`, `search_code`, `get_signatures`,
`find_references`, `git_status`, `git_diff`, `model_status`, `memory_get`,
plus the write tier (`write_file`, `write_file_append`, `patch_file`,
`edit_file`, `edit_ast`, `replace_lines`, `memory_set`, `git_commit`) and the
exec tier (`shell`, `run_bash`, `run_python`, `run_tests`).

Phase 2 additions (2026-10-31):

- **`git_diff`** (read): bounded `git diff` with optional `ref` (commit-ish or
  `A..B` range) and `path` filter. The ref is validated (no leading dash, no
  shell metacharacters) and the call runs argv-only, never through a shell.
- **`git_commit`** (write tier, mutating): commits **explicitly listed paths
  only** — `git add -- <paths>` + `git commit -m <message> -- <paths>`. The
  whole index is never staged (`git add -A` is impossible by construction),
  the workspace root itself is refused, and the paths must resolve inside the
  workspace. Gated exactly like the other mutating tools (`MCP_ALLOW_WRITE=1`).
- **`run_tests`** (exec tier, stdio only): auto-detects pytest / `npm test` /
  `cargo test` / `go test` or runs an explicit `cmd` (prefix allowlist, no
  shell chaining/substitution — `;|&<>\`$%` are refused). Bounded timeout
  (10–300 s) and bounded output (tail kept, `MCP_TEST_OUTPUT_MAX_CHARS`).
- **`model_status`** (read): configured model roles (`runtime_models.json`),
  currently loaded models with VRAM (Ollama `GET /api/ps`) and the ports.
  Read-only: it loads nothing.

### Hardening (2026-10-21)

- **Path containment is strict.** A path that resolves outside the workspace is
  rejected with `PATH_OUTSIDE_WORKSPACE`; it is no longer silently rewritten to
  `WORKSPACE/<name>`. `..` escapes and NUL bytes are rejected; absolute paths
  that resolve inside the workspace are allowed.
- **One governance funnel.** `tools/call` *and* the internal dispatch path check
  the transport tiers, so a write tool cannot be reached through routing.
- **Transport limits.** Header block ≤ `MCP_MAX_HEADER_BYTES` (32 KB, else 431),
  body ≤ `MCP_MAX_BODY_BYTES` (2 MB, else 413), at most `MCP_MAX_CONCURRENCY`
  (8) parallel requests, at most `MCP_MAX_CONNECTIONS` (32) open connections
  (else 503), a body-read timeout of `MCP_BODY_TIMEOUT_S` (30 s, else 408),
  `MCP_MAX_BATCH` (16) per JSON-RPC batch, and `MCP_REQUEST_TIMEOUT_S` (300 s)
  per request on **both** transports — stdio included, so a hung call is
  aborted with a `-32000` error instead of blocking the loop forever.
  `Content-Length` is validated. The stdio line cap equals
  `MCP_MAX_BODY_BYTES`, so a payload that fits over HTTP also fits over
  stdio.
- **Governance is visible.** Every tool call writes one line to stderr:
  `[MCP-CALL] transport=http tool=read_file ok=true elapsed=0.02s bytes=123`
  (denials carry `denied_reason=...`).
- **Host and auth.** The HTTP transport rejects non-loopback `Host` headers
  (DNS rebinding) and requires `Authorization: Bearer <MCP_HTTP_TOKEN>` when the
  token is set. Binding to a non-loopback address without a token prints a
  warning at startup.

### Annotations, resources, prompts (phase 2, 2026-10-31)

**Tool annotations.** Every `tools/list` entry now carries `annotations`
(`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). Read
tools are `readOnlyHint=true` + `idempotentHint=true`; the write/edit class
adds `destructiveHint=true`; shell/exec and `query` add `openWorldHint=true`;
`git_commit`, `run_tests`, `write_file_append` and `memory_set` are mutating
but not destructive. They are hints — clients may ignore them, and the tier
gates above are unchanged, so existing clients keep working.

**Resources** (server direction). The `initialize` capabilities now include
`resources` and `prompts` alongside `tools`.

- `resources/list` — workspace files only (the workspace root is the allowlist,
  never the disk). Hidden dirs and junk/privacy dirs (`.git`, `__pycache__`,
  `node_modules`, `.venv`, `chats`, `sessions`, `logs`, …) are pruned; capped
  at `MCP_RESOURCE_MAX_ENTRIES` (200) entries and `MCP_RESOURCE_MAX_DEPTH`
  (8) levels. Entries are standard `file://` URIs with `name`, `mimeType`,
  `size`.
- `resources/read` — bounded text only: at most `MCP_RESOURCE_MAX_BYTES`
  (256 KB), UTF-8, NUL-byte detection rejects binary content. Path containment
  is strict (a URI resolving outside the workspace is `-32602`), and a small
  denylist (`settings.json`, `gateway.toml`, `.env*`, `*.key`, `*.pem`,
  `id_rsa*`, credentials/secrets files) is blocked in **both** the listing and
  the read with a clear `-32002` error — `settings.json` can carry preset API
  keys, so auto-discovery surfaces never see them.
- `resources/templates/list` — one template, `hivemind://workspace/{path}`, to
  read a file by its workspace-relative path. (This method is a 2025-revision
  addition; serving it on the 2024-11-05 baseline is harmless for old clients
  and useful for new ones.)
- Not found / too large / binary / secret-denied answer `-32002` with the
  reason; a missing, non-string or wrong-scheme URI answers `-32602`.

**Prompts.** `prompts/list` + `prompts/get` with three static templates:
`review_diff` (review the uncommitted changes), `explain_file` (argument:
`path`), `fix_tests` (optional `scope`). Arguments are flattened and capped
(300 chars) plain-text substitutions — a prompt never executes anything by
itself. Unknown prompts and missing required arguments answer `-32602`.

### Env reference

| Variable | Default | Purpose |
|---|---|---|
| `MCP_HTTP_PORT` | `8090` | HTTP port |
| `MCP_HTTP_BIND` | `127.0.0.1` | bind address |
| `MCP_HTTP_TOKEN` | *(empty)* | Bearer token, empty = no auth (loopback only) |
| `MCP_HTTP_EXTRA_HOSTS` | *(empty)* | extra allowed `Host` values |
| `MCP_HTTP_CORS_ORIGINS` | *(empty)* | origins that get CORS headers |
| `MCP_ALLOW_WRITE` | off | write tier |
| `MCP_ALLOW_EXEC` | off | exec tier (stdio only) |
| `MCP_ALLOW_QUERY` | off | `query` tool on both transports (HiveMind run from the IDE) |
| `MCP_MAX_HEADER_BYTES` | `32768` | header block limit |
| `MCP_MAX_BODY_BYTES` | `2097152` | request body limit (also the stdio line cap) |
| `MCP_MAX_CONCURRENCY` | `8` | parallel HTTP requests |
| `MCP_MAX_CONNECTIONS` | `32` | open HTTP connections, beyond that 503 |
| `MCP_BODY_TIMEOUT_S` | `30` | per-read body timeout, else 408 |
| `MCP_MAX_BATCH` | `16` | requests per batch |
| `MCP_REQUEST_TIMEOUT_S` | `300` | per-request timeout on both transports, `0` disables |
| `MCP_SHARED_TOOL_ENABLED` | `1` | forward engine tools to `/internal/tool/exec` (`0` = local handlers only) |
| `MCP_SHARED_TOOL_URL` | *(derived)* | engine exec URL (defaults to `HIVEMIND_URL` + `/internal/tool/exec`) |
| `MCP_SHARED_TOOL_TIMEOUT_S` | `120` | read timeout for the engine forward (min 10) |
| `MCP_SHARED_TOOL_MODE` | `mcp_agent` | `tool_mode` sent with the forward |
| `MCP_SHARED_TOOL_MODEL` | `qwen3:8b` | `model_for_limits` sent with the forward |
| `MCP_SHARED_INCLUDE_WEBSEARCH` | `1` | `include_websearch` flag sent with the forward |
| `MCP_RESOURCE_MAX_BYTES` | `262144` | max bytes per `resources/read` |
| `MCP_RESOURCE_MAX_ENTRIES` | `200` | max entries per `resources/list` |
| `MCP_RESOURCE_MAX_DEPTH` | `8` | max directory depth for the listing walk |
| `MCP_TEST_OUTPUT_MAX_CHARS` | `8000` | `run_tests` output cap (tail kept) |

## 2. IDE → HiveMind (editor context, port 8001)

The MCP server lets an IDE *call* HiveMind. The opposite direction — HiveMind
knowing what the editor currently sees — is the editor-context endpoint:

```
POST   /context/editor   {"workspace": "...", "file": "...", "cursor_line": 42,
                          "selection": "...", "open_files": ["..."],
                          "diagnostics": [{"file": "src/main.rs", "line": 42,
                                           "severity": "error", "message": "..."}]}
GET    /context/editor?workspace=...
DELETE /context/editor?workspace=...
```

Rules: one slot per workspace, 300 s TTL, selection capped at 4 000 characters,
at most 20 diagnostics, 256 KB body. Unparseable values are ignored rather than
rejected (`cursor_line: "abc"` becomes `0`).

`infra/editor_context.py::format_block()` renders it as a compact prompt block:

```
[EDITOR CONTEXT — from the IDE, current state]
Open file: src/main.rs, cursor line 42
Selected code:
```...```
Editor diagnostics:
- [error] src/main.rs:42 borrow of moved value
[/EDITOR CONTEXT]
```

A client only needs to POST on save/selection change. JetBrains, VS Code and
Neovim all have a trivial path to that (an HTTP call on a document listener).

## 3. The engine endpoint behind it (`POST /internal/tool/exec`)

Both directions above end up in the engine when the MCP server proxies a tool
call. That endpoint is hardened as well (2026-10-21):

- **Host guard**: only loopback `Host` headers (plus `MCP_HTTP_EXTRA_HOSTS`) are
  accepted, which blocks DNS rebinding.
- **Exec and mutating tools need a shared secret.** `run_bash`, `shell`,
  `run_python`, `start_background`, `install_package`, `run_tests`, `browser`
  and the mutating class (`write_file`, `edit_file`, `write_file_append`,
  `edit_ast`, `replace_lines`, `patch_file`, `git_commit`, all `mcp_*` names)
  are refused over HTTP unless `HIVEMIND_INTERNAL_TOKEN` is set and sent as
  `X-Hivemind-Internal-Token`. Without the variable the answer is
  `403 exec/mutating tools are disabled over HTTP without HIVEMIND_INTERNAL_TOKEN`.
  Read-only tools stay available; their workspace lock is derived server-side
  and cannot be weakened by the request body.
- **Audit**: every call logs one line,
  `[INTERNAL-EXEC] tool=read_file ok=True elapsed=0.02s bytes=123` (the tool
  name is logged flattened to one line, so a newline inside it cannot forge
  log entries).
- **Every call runs in a real mode.** The endpoint defaults `tool_mode` to
  `mcp_agent`; a mode-less call has no mode gate to violate and is reserved
  for unit tests and direct in-process API calls, never for the HTTP surface.
  Budgets (websearch, web fetch, package installs, `mcp_*` calls) also apply
  on this path: without a run context the engine keeps shared counters per
  hour window instead of a fresh budget per call.

Consequence for exec over stdio: the **engine** requires
`HIVEMIND_INTERNAL_TOKEN` — `MCP_HTTP_TOKEN` alone only guards the MCP HTTP
transport and does not unlock engine-side exec. Set the token in the
environment of **both** the engine and the MCP process under the same value
(the MCP process forwards `HIVEMIND_INTERNAL_TOKEN`, or falls back to
`MCP_HTTP_TOKEN`, as `X-Hivemind-Internal-Token`). Exec without that token is
blocked by design.
