# Language servers (LSP)

HiveMind drives any installed language server through one generic client
(`infra/lsp_client.py`). There are **no per-language on/off flags**: a
language is available when its server command is found, and `lsp_enabled`
(default `true`) is the single switch.

**Security:** language servers execute project toolchains (rust-analyzer runs
`cargo check`, which runs `build.rs` and proc macros). Only open workspaces you
trust. Servers get a scrubbed environment (no engine tokens/keys), and a repo's
`.vscode/settings.json` is **not** forwarded unless you opt in with
`lsp_forward_workspace_settings` (a repo could otherwise choose the command a
server runs). Phone runs exclude every `lsp_*` tool while the default
`telegram_phone_restricted` is on.

- A server starts lazily on the first `lsp_*` call for its files, never at
  boot. A missing server answers `LSP_UNAVAILABLE` with an install hint.
- Servers are cached per `(workspace, event loop)`; engine shutdown closes
  them. An idle reaper closes a server after `lsp_idle_timeout_s` (default
  600s, 0 = off) without a call; the next call restarts it lazily.
- `lsp_status` never starts a process; it only reports the registry.
- All tool positions are **1-based**; the handler converts to LSP's 0-based
  positions.

## Tools

| Tool | Arguments | What it does |
|---|---|---|
| `lsp_status` | none | Lists every known language, the resolved command, and whether it was found. Run this after `LSP_UNAVAILABLE`. Does not start a server. |
| `lsp_diagnostics` | `path` | Compiler/linter diagnostics for one file, formatted `[ERROR] relpath:line:col message (source)`. Uses push diagnostics and LSP 3.17 pull diagnostics when the server advertises `diagnosticProvider`. A cold document waits up to `lsp_diagnostics_wait_s` (default 5s) for the first publication. Auto-attached after writes when a server is warm. |
| `lsp_definition` | `path`, `line`, `character` | Definition location(s) for the symbol at a 1-based position. |
| `lsp_references` | `path`, `line`, `character` | Workspace-wide references to the symbol at a 1-based position. |
| `lsp_hover` | `path`, `line`, `character` | Type signature/docs at a 1-based position. |
| `lsp_symbols` | `path` | Document outline: classes, functions, methods, variables with kinds and line ranges. |
| `lsp_workspace_symbols` | `query` | Project-wide symbol search by name/prefix. |
| `lsp_code_actions` | `path`, `start_line`, `start_char`, `end_line`, `end_char` | Lists quickfixes/refactorings for a 1-based range, **titles and kinds only**; nothing is applied. |
| `lsp_rename` | `path`, `line`, `character`, `new_name` | Returns a project-wide rename plan with per-file edit counts and 1-based edit lines. **Writes nothing**; apply it with `edit_file`. Approval-gated (`_APPROVAL_TOOLS`). |
| `lsp_format` | `path` | Formats the file with `textDocument/formatting` and rewrites it atomically (server must advertise `documentFormattingProvider`). Approval-gated. |

Read-only modes (`duo_readonly`, `pre_explore`, `critic_verify`,
`direct_python`) get the eight read tools plus `lsp_code_actions`.
`lsp_rename` and `lsp_format` (the two mutating tools) are only in the full
modes. With `lsp_enabled=false` all `lsp_*` schemas are removed.

## Automatic diagnostics

After a successful `write_file`, `edit_file`, `patch_file`,
`replace_lines` or `write_file_append`, the dispatcher appends a compact
`[LSP auto-diagnostics after writing …]` block (8-line cap) and fires
`workspace/didChangeWatchedFiles` for the written path (an index hint for
gopls/rust-analyzer; external edits are still synced lazily on the next
call). A cold server is warmed in the background
(`lsp_auto_warm_on_write`, default `true`) with a bounded 3 s wait; when it
is not ready, the next write gets the diagnostics. If the LSP is disabled
or fails, the pyright/per-language lint block remains the fallback.
`lsp_auto_diagnostics` (default `true`) disables the block (the watched-file
hint stays on).

## Workspace settings

`<workspace>/.vscode/settings.json` is **not** forwarded by default (security:
a repo must not configure the language server that executes its toolchain).
With `lsp_forward_workspace_settings` enabled, the file is read (cached by
mtime) and answered to `workspace/configuration` pulls section by section,
plus one `workspace/didChangeConfiguration` push after the handshake. A
`{"rust-analyzer": {"checkOnSave": {"enable": true}}}` in the workspace
therefore reaches rust-analyzer; broken or missing files answer `{}`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `lsp_enabled` | `true` | The only switch; `false` removes the schemas and answers `LSP_DISABLED`. |
| `lsp_servers` | `{}` | Per-language command override; replaces the default candidates and must resolve. |
| `lsp_client_timeout_s` | `30` | Per-request timeout; also caps initialize. |
| `lsp_diagnostics_wait_s` | `5.0` | Wait for the first/new diagnostics publication. |
| `lsp_idle_timeout_s` | `600` | Close an idle language server after this many seconds; 0 = off. |
| `lsp_auto_diagnostics` | `true` | Attach diagnostics to successful writes. |
| `lsp_auto_warm_on_write` | `true` | Warm a cold server from the write path (bounded 3 s wait). |
| `lsp_max_diagnostics` | `50` | Diagnostics per call. |
| `lsp_max_results` | `50` | Locations/symbols/actions per call. |

## Server registry

| Language | Default candidates | Install |
|---|---|---|
| Python | `pyright-langserver --stdio`, `pylsp`, `jedi-language-server` | `npm install -g pyright` (or `pip install python-lsp-server`) |
| TypeScript / JavaScript | `typescript-language-server --stdio` | `npm install -g typescript typescript-language-server` |
| TSX / JSX | same server; `didOpen` uses `typescriptreact` / `javascriptreact` | same |
| Go | `gopls` | `go install golang.org/x/tools/gopls@latest` |
| Rust | `rust-analyzer` | `rustup component add rust-analyzer` |
| C / C++ | `clangd` | `winget install LLVM.LLVM` or distro package |
| Bash | `bash-language-server start` | `npm install -g bash-language-server` |
| Lua | `lua-language-server` | distro / `luarocks` |
| JSON / HTML / CSS | `vscode-json-language-server --stdio`, `vscode-html-language-server --stdio`, `vscode-css-language-server --stdio` | `npm install -g vscode-langservers-extracted` |
| YAML | `yaml-language-server --stdio` | `npm install -g yaml-language-server` |

`lsp_servers` example:

```json
{
  "python": "pyright-langserver --stdio",
  "rust": ["rust-analyzer"],
  "typescript": "typescript-language-server --stdio"
}
```

## Errors and limits

- `LSP_DISABLED` — `lsp_enabled=false`.
- `LSP_UNAVAILABLE` — no command found for the language (or an override did
  not resolve); the message contains the candidates and an install hint.
- `LSP_FAILED` — transport/protocol/timeout failure; a hung server is killed
  and restarted lazily on the next call.
- `INVALID_ARGUMENT` / `PATH_OUTSIDE_WORKSPACE` — bad or out-of-workspace path.
- Full-text sync cap: 4,000,000 chars per document; the request timeout is
  `lsp_client_timeout_s` (default 30s).

## Known limits

- Multi-root workspaces are not supported (one root per manager); external
  file edits are synced lazily on the next tool call (no real file watcher,
  only the tool-driven didChangeWatchedFiles hint).
- No completion/signature help/semantic tokens/inlay hints/code lens/call
  hierarchy; the surface is diagnostics + navigation + code actions +
  rename planning + formatting.
- Rust-analyzer publishes a native diagnostic and a version-less empty
  "clear" before the cargo-check result; the client keeps the fresh
  non-empty result instead of returning the clear. Cargo-check diagnostics
  that arrive later than the wait window can still be missed, and enabling
  checkOnSave in workspace settings makes the first answer wait for a real
  cargo check (seconds on a cold target dir).
