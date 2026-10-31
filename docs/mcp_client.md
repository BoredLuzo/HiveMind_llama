# MCP client (beta)

HiveMind can also be an MCP **server** ([docs/mcp.md](mcp.md)). This page is the
other direction: HiveMind starts the MCP servers **you** configure and offers
their tools to the model.

Status: beta. Off by default. Two transports: stdio and HTTP.

## Two transports

- **stdio** (default): `command` + `args` start a child process. One process per
  server, killed with its whole tree on timeout or shutdown.
- **HTTP** (Streamable HTTP): an entry with `url` instead of `command` is called
  over POST JSON-RPC; answers may come back as JSON or as an SSE stream, and a
  `Mcp-Session-Id` the server sets is reused. The SSE answer is read as it
  streams: the call returns as soon as the answer event arrives and the stream
  is closed then - a server that keeps the event stream open afterwards does
  not cause a timeout, and a response body is capped at 8 MB in total. Nothing
  is started, so there is no process to kill.

```json
{"name": "remote", "url": "http://127.0.0.1:8931/mcp"}
```

### HTTP auth

Hosted MCP endpoints usually want a token. Two optional per-server fields,
both HTTP only (`stdio` servers ignore them):

- `headers`: an object of header name → value, sent with every request
  exactly as written, e.g. `{"X-Api-Version": "2022-11-28"}`.
- `token_env`: the name of an environment variable. When it is set and not
  empty, every request carries `Authorization: Bearer <value>`.

An explicit `Authorization` inside `headers` wins over `token_env`. A
configured `token_env` whose variable is missing or empty **fails the start**
with the variable name in the message - the server never starts
unauthenticated by accident. Tokens are never logged. `GET /mcp/servers`
reports the server list redacted (no env values, no header values,
secret-carrying `args` values such as `--api-key ...`, and `url` userinfo);
the `GET /mcp/servers` and the reload response both report `has_auth: true`
per HTTP server, never the value.
If the server answers 401 or 403, the tool call returns
`[TOOL_ERROR: MCP_AUTH_FAILED]` (token missing, wrong or expired) and the
connection stays alive - a wrong token is a configuration mistake, not a
broken transport. A changed `token_env` variable takes effect on the next
reload (`POST /mcp/reload` or the `mcp_reload` tool), which re-resolves it.
A hosted server via `token_env`, and one with an extra version header:

```json
[
  {"name": "github", "url": "https://api.githubcopilot.com/mcp",
   "token_env": "GITHUB_MCP_TOKEN"},
  {"name": "custom", "url": "https://mcp.example.com/mcp",
   "headers": {"X-Api-Version": "2022-11-28"},
   "token_env": "CUSTOM_MCP_TOKEN"}
]
```

Both share the same tool list, name prefix, approval gate and mode gate. What
the HTTP transport does not do: the optional GET stream for server-initiated
messages stays unused, so a server that pushes notifications is only noticed on
the next `POST /mcp/reload` or `mcp_reload` call.

## Enable it

Either use the **MCP client** card in the config page (toggle, server list as JSON, Save, Reload, live status) or edit `settings.json`:

```json
{
  "mcp_client_enabled": true,
  "mcp_client_timeout_s": 30,
  "mcp_servers": [
    {
      "name": "files",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\work"]
    },
    {
      "name": "sqlite",
      "command": "uvx",
      "args": ["mcp-server-sqlite", "--db-path", "C:\\work\\app.db"]
    }
  ]
}
```

| Key | Meaning |
|---|---|
| `mcp_client_enabled` | master switch, default `false` |
| `mcp_client_timeout_s` | per request, default 30 s (the handshake is additionally capped at 20 s) |
| `mcp_servers[].name` | short name, becomes part of the tool name |
| `mcp_servers[].command` | executable, resolved via `PATH` |
| `mcp_servers[].args` | argument list, no shell interpretation |
| `mcp_servers[].env` | extra environment variables for the child |
| `mcp_servers[].cwd` | working directory for the child |
| `mcp_servers[].headers` | HTTP only: extra headers sent with every request (auth, API version pins) |
| `mcp_servers[].token_env` | HTTP only: name of an environment variable; set and non-empty → `Authorization: Bearer <value>`, missing/empty → the server fails to start |
| `mcp_servers[].enabled` | set `false` to keep an entry but skip it; the string `"false"` counts as off |
| `mcp_server_budgets` | optional object `{server name: max tool calls}`; a missing key, `0` or a negative number means unlimited (default) |

After editing, `POST /mcp/reload` (or restart the engine). `GET /mcp/servers`
shows what is configured, what is running and which tools came back.

## How a tool looks

Every tool is named `mcp_<server>_<tool>`, sanitized to `[a-z0-9_]` and capped
at 64 characters, for example `mcp_files_read_file`. The description is
prefixed with `[mcp:files]` and the server's `inputSchema` is passed through, so
the model sees the real parameters. If the server sends an `annotations`
object for a tool (`readOnlyHint`, `destructiveHint`, `title`, ...), it is
passed through verbatim next to the parameters; a tool without annotations
gets no `annotations` key at all. Two caps keep a broken server from blowing
up a request: a name longer than 64 characters is truncated, and a schema over
8 kB is replaced by an empty one with a note.

## Per-server call budgets

A single server can eat an unbounded number of tool calls per run (a model
loop, a misbehaving IDE client). The optional settings key
`mcp_server_budgets` caps this per server:

```json
{"mcp_server_budgets": {"files": 100, "github": 20}}
```

- The limit counts **tool calls per rolling 1-hour window**, per server name
  (all tools of that server share the budget). The window rolls: a call
  counts until one hour has passed since it was made, then its slot frees up
  again.
- Absent key, `0`, a negative number or a non-integer value means
  **unlimited** - the default behaviour. A non-object value is logged as
  config decay.
- When the budget is exhausted, the call fails with
  `[TOOL_ERROR: MCP_BUDGET_EXCEEDED]` and a message naming the server, the
  limit and when the window resets. No request is sent to the server.
- Exhaustion is **not** a broken server: the connection stays alive, the
  status endpoint keeps reporting `ok`, and the next call inside the next
  window works again. A reload does not reset the counter either (editing
  the config must not be a budget loophole).
- The counter lives in memory only: an engine restart clears it. Limits are
  read on reload, like the server list itself.

## Connect an IDE to HiveMind itself

To use HiveMind's **own** MCP server from an IDE (Claude Desktop, Cline,
Zed, VS Code), `tools/mcp_config_gen.py` generates the ready-to-paste stdio
client config - the venv python and `infra/mcp_server.py` resolved to
absolute paths of the checkout the tool lives in, like `start_mcp.bat`
would start it:

```bat
python tools\mcp_config_gen.py --format vscode --out .vscode\mcp.json
python tools\mcp_config_gen.py                    :: summary + all four formats
python tools\mcp_config_gen.py --check            :: validate the JSON output
```

The generated `env` block carries `HIVEMIND_INTERNAL_TOKEN` as a
`<HIVEMIND_INTERNAL_TOKEN>` placeholder to fill in; `--include-secret`
embeds the value from the environment instead. No secret from
`settings.json` ever reaches the output.

## What the server process sees

`command`, `args`, `env` and `cwd` are handed to the child exactly as written.
Nothing is rewritten and no shell is involved (`.cmd`/`.bat` get `cmd /d /c`).
The **workspace is the server's own business**: an MCP server decides which
folder it exposes and which variable it reads for it. HiveMind does not inject
a workspace.

HiveMind's own server (`infra/mcp_server.py`) has two kinds of tools:

- **Proxy tools** (`read_file`, `write_file`, `run_bash`, `search_code`, ...)
  reach the running engine over HTTP and inherit **the engine's workspace**.
  `HIVEMIND_WORKSPACE` does not change where they look.
- **Local tools** of that server use for its own folder, which it takes from
  `HIVEMIND_WORKSPACE` (falling back to the folder it runs in).

A working entry:

```json
{"name": "self", "command": "python", "args": ["-m", "infra.mcp_server"],
 "cwd": "C:/HiveMind", "env": {"HIVEMIND_WORKSPACE": "C:/work/my-project"}}
```

So a proxy tool can answer from a folder you did not expect - that folder is
whatever the engine currently works in, not what the server config says. The
client passes the server's message through as a normal tool error; it is a
config question, not a client bug.

Tools are collected over every `tools/list` page (MCP paginates with
`nextCursor`), up to 64 per server and 10 pages. The tool list also contains
`mcp_reload`: calling it restarts the configured servers and rebuilds the list,
so a dead server does not end the run.

## Behaviour and limits

- One child process per server, started on the first refresh, killed on
  shutdown, on a timeout and when the client is disabled.
- A server that hangs, crashes or prints garbage never breaks a run: the call
  returns `[TOOL_ERROR: MCP_*]` and the connection is marked dead until the
  next reload. The exception is HTTP 401/403: the connection stays alive (see
  "HTTP auth" above). An HTTP 404 on an expired session is recovered instead
  of killing the connection (see below).
- HTTP requests are retried: up to 3 retries after the first attempt on
  connect errors (`ConnectError`), read timeouts (`ReadTimeout`), broken
  responses (`RemoteProtocolError`) and HTTP 502/503/504. Pauses
  0.5 s / 1.5 s / 3.0 s with ±20 % jitter, capped by
  the call timeout - when the remaining budget cannot afford the next pause,
  the last error wins and names the attempt count. **`tools/call` is the
  exception**: only connect errors are retried there, because a read timeout
  means the request was delivered and the server may already have executed
  the tool. The retry ladder covers the connect and header phase; once an
  answer streams in, a mid-stream failure is reported as a tool error instead
  of being replayed. A tool call that runs into its timeout sends
  `notifications/cancelled` (HTTP only) so the server can stop the work.
- If a server has forgotten the session (HTTP 404 while an
  `Mcp-Session-Id` was sent), the client drops the session, re-initializes
  once and retries the call. A 404 without a session still marks the
  connection dead.
- Config decay is logged, not silent: a non-list `mcp_servers`, a settings
  file that fails to load (the client then looks disabled) and an entry with
  both `url` and `command` (HTTP wins, `command`/`args` are ignored) each
  produce a warning in the log.
- Status codes are honored on the handshake confirmation too: only
  502/503/504 are retried with the same ladder; a 401/403 fails the start
  immediately with the server's auth-rejection text (the `MCP_AUTH_FAILED`
  code appears in **tool results**, not in the start error), and any other
  4xx/5xx fails the start instead of being waved through.
- Protocol versions are negotiated: the first offer is `2024-11-05`, or the
  version the server accepted on a previous connect (a rebuilt connection
  offers the stored version). Whatever version string the server answers
  with is accepted, including newer ones. A server that rejects the offer
  gets exactly one retry with the version its error advertised (else
  `2025-03-26`); a failed negotiation closes the connection.
- Output is capped at 200k characters per call; a cut is marked with
  `[truncated]`, and a result that carries only `structuredContent` is
  rendered as JSON. Image blocks are reported as `[image content omitted]`.
- At most 8 servers and 64 tools per server.
- While waiting for a stdio answer, notifications are skipped and answers for
  other request ids are capped (50); ids echoed as numeric strings (`"1"` vs
  `1`) still match.
- `command` and `args` are executed **without a shell**. Only `.cmd` and `.bat`
  are wrapped in `cmd /d /c` because Windows cannot exec them directly; quotes
  inside an argument are doubled (`""`), the way `cmd.exe` expects.
- Phone runs cannot use MCP tools: the phone restriction gate rejects anything
  outside its safe set, and MCP tools are not in it.
- Nothing starts while the client is disabled. A stored `mcp_servers` list
  without `mcp_client_enabled` costs nothing.

## Security notes

The client runs local programs with your user rights, exactly as configured.
Keep in mind:

- Only point it at servers you trust. The child inherits the engine's
  environment, including any token that lives there.
- Do not shell out through a wrapper script you would not run yourself; the
  client cannot sanely filter what a server does with its own arguments.
- A server's tools are exposed to the model. If a server can write files, the
  model can write files through it, outside the workspace guard of the built-in
  tools.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `MCP_UNKNOWN_TOOL` | the name is not in the current list; reload after an edit |
| `MCP_SERVER_DOWN` | the process exited or timed out; check `GET /mcp/servers` |
| `MCP_DISABLED` | `mcp_client_enabled` is false |
| `MCP_AUTH_FAILED` | the server rejected the token (HTTP 401/403): missing, wrong or expired; check `token_env` / `headers` |
| `MCP_BUDGET_EXCEEDED` | the server hit its `mcp_server_budgets` limit for the rolling 1-hour window; the message says when it resets - wait, or raise the limit |
| `token_env '...' is set but the environment variable is empty or missing` | set the variable before starting the engine, or remove `token_env` from the entry |
| `command not found` | the executable is not on `PATH`; use an absolute path |
| `is busy with another call` | a second call arrived while the first is still running; the server is fine |
| a server works in a terminal but not here | it probably needs a shell, an env var or a `cwd`; set `env` or `cwd` |
