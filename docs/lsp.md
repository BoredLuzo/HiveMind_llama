# Language servers (LSP)

HiveMind drives any installed language server through one generic client
(`infra/lsp_client.py`). There are **no per-language on/off flags**: a
language is available when its server command is found, and `lsp_enabled`
(default `true`) is the single switch.

- A server starts lazily on the first `lsp_*` call for its files, never at
  boot. A missing server answers `LSP_UNAVAILABLE` with an install hint.
- Servers are cached per `(workspace, event loop)`; engine shutdown closes
  them. They stay warm while the engine runs (no idle reaper yet).
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

Read-only modes (`duo_readonly`, `pre_explore`, `critic_verify`,
`direct_python`) get the eight read tools plus `lsp_code_actions`.
`lsp_rename` is only in the full modes. With `lsp_enabled=false` all `lsp_*`
schemas are removed.

## Automatic diagnostics

After a successful `write_file`, `edit_file`, `patch_file`,
`replace_lines` or `write_file_append`, the dispatcher appends a compact
`[LSP auto-diagnostics after writing …]` block (8-line cap) **when a server
for that language is already warm**. A write never starts a cold server.
`lsp_auto_diagnostics` (default `true`) disables this.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `lsp_enabled` | `true` | The only switch; `false` removes the schemas and answers `LSP_DISABLED`. |
| `lsp_servers` | `{}` | Per-language command override; replaces the default candidates and must resolve. |
| `lsp_client_timeout_s` | `30` | Per-request timeout; also caps initialize. |
| `lsp_diagnostics_wait_s` | `5.0` | Wait for the first/new diagnostics publication. |
| `lsp_auto_diagnostics` | `true` | Attach diagnostics to successful writes when warm. |
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

- No idle reaper: a started server stays warm until engine shutdown.
- No `workspace/configuration` values and no `.vscode/settings.json` reading;
  server-specific settings are not forwarded yet.
- Rust-analyzer publishes a native diagnostic and a version-less empty
  "clear" before the cargo-check result; the client keeps the fresh
  non-empty result instead of returning the clear. Cargo-check diagnostics
  that arrive later than the wait window can still be missed.
- No formatting/completion/signature help/semantic tokens/inlay hints/code
  lens/call hierarchy yet; the surface is diagnostics + navigation + code
  actions + rename planning.
- One workspace root per manager; no `workspace/didChangeWatchedFiles`.
