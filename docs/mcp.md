# MCP / IDE integration

HiveMind is usable from an IDE in two directions. Both are local-only by default.

## 1. HiveMind → IDE (MCP server, port 8090)

`start_mcp.bat` starts an MCP server (`infra/mcp_server.py`) with stdio and an
HTTP transport. It exposes read tools always, write and exec tools only as an
opt-in.

| Transport | Read | Write | Exec (`shell`, `run_bash`, `run_python`) | `query` |
|---|---|---|---|---|
| HTTP (`127.0.0.1:8090/mcp`) | on | `MCP_ALLOW_WRITE=1` | **never** | `MCP_ALLOW_QUERY=1` |
| stdio | on | `MCP_ALLOW_WRITE=1` | `MCP_ALLOW_EXEC=1` | on |

Tools: `read_file`, `list_dir`, `find_files`, `search_code`, `get_signatures`,
`find_references`, `git_status`, `memory_get`, plus the write tier
(`write_file`, `write_file_append`, `patch_file`, `edit_ast`, `memory_set`) and
the exec tier (`shell`, `run_bash`, `run_python`).

### Hardening (2026-10-21)

- **Path containment is strict.** A path that resolves outside the workspace is
  rejected with `PATH_OUTSIDE_WORKSPACE`; it is no longer silently rewritten to
  `WORKSPACE/<name>`. `..`, absolute paths and NUL bytes are all rejected.
- **One governance funnel.** `tools/call` *and* the internal dispatch path check
  the transport tiers, so a write tool cannot be reached through routing.
- **Transport limits.** Header block ≤ `MCP_MAX_HEADER_BYTES` (32 KB, else 431),
  body ≤ `MCP_MAX_BODY_BYTES` (2 MB, else 413), at most `MCP_MAX_CONCURRENCY`
  (8) parallel requests, `MCP_MAX_BATCH` (16) per JSON-RPC batch, and
  `MCP_REQUEST_TIMEOUT_S` (300 s) per request. `Content-Length` is validated.
- **Governance is visible.** Every tool call writes one line to stderr:
  `[MCP-CALL] transport=http tool=read_file ok=true elapsed=0.02s bytes=123`
  (denials carry `denied_reason=...`).
- **Host and auth.** The HTTP transport rejects non-loopback `Host` headers
  (DNS rebinding) and requires `Authorization: Bearer <MCP_HTTP_TOKEN>` when the
  token is set. Binding to a non-loopback address without a token prints a
  warning at startup.

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
| `MCP_ALLOW_QUERY` | off | `query` tool (HiveMind run from the IDE) |
| `MCP_MAX_HEADER_BYTES` | `32768` | header block limit |
| `MCP_MAX_BODY_BYTES` | `2097152` | request body limit |
| `MCP_MAX_CONCURRENCY` | `8` | parallel HTTP requests |
| `MCP_MAX_BATCH` | `16` | requests per batch |
| `MCP_REQUEST_TIMEOUT_S` | `300` | per-request timeout, `0` disables |

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
- **Exec needs a shared secret.** `run_bash`, `shell` and `run_python` are
  refused over HTTP unless `HIVEMIND_INTERNAL_TOKEN` is set and sent as
  `X-Hivemind-Internal-Token`. Without the variable the answer is
  `403 exec tools are disabled over HTTP without HIVEMIND_INTERNAL_TOKEN`.
  Read and write tools stay available; their workspace lock is derived
  server-side and cannot be weakened by the request body.
- **Audit**: every call logs one line,
  `[INTERNAL-EXEC] tool=read_file ok=True elapsed=0.02s bytes=123`.

Consequence for exec over stdio: set the token in the environment of **both**
the engine and the MCP process (`MCP_HTTP_TOKEN` or `HIVEMIND_INTERNAL_TOKEN`)
- the MCP server forwards it automatically. Exec without that token is blocked by
design.
