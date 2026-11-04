# Release 1.5.0

1.5.0 lets HiveMind talk to more tools and be used from more places. It speaks
MCP in both directions, drives the language servers already installed on your
machine, and the Telegram bot now takes voice notes, photos and PDFs. Long
agentic runs got calmer: follow-up prompts survive the model reload after a
run, compression keeps the context bounded without breaking the tool history,
and a fresh install starts with settings that match the models it downloaded.

Hermes stays the default coder. Groot stays experimental.

Upgrade with `update.bat` (or the startup check offers it), or extract the zip
next to the old install. Settings, models, sessions and the venv are kept.
New dependency: `pypdf` (pure Python, for document text).

## MCP

HiveMind is an MCP server for your IDE (`start_mcp.bat`, HTTP 8090, or stdio)
and an MCP client for external servers.

- Protocol 2024-11-05, 2025-06-18 (sessions, `GET /mcp` SSE with
  `Last-Event-ID` replay) and a minimal 2026-07-28 (`server/discover`,
  per-request `_meta` version check, stateless initialize).
- 23 tools with annotations, plus `git_diff`, `git_commit` (explicit paths
  only), `run_tests` and `model_status`.
- Resources stay inside the workspace, text-only, 256 KB cap with a secret
  denylist. Prompts: `review_diff`, `explain_file`, `fix_tests`.
- Reads are free; the write tier needs `MCP_ALLOW_WRITE=1`, exec needs
  `MCP_ALLOW_EXEC=1` (stdio only) and the internal token.
- Client side: stdio or HTTP, per-server call budgets, annotation passthrough.
- `python tools/mcp_config_gen.py` prints configs for Claude Desktop, Cline,
  Zed and VS Code.

## Language servers

One generic client drives pyright, typescript-language-server, gopls,
rust-analyzer, clangd, bash/lua servers and the HTML/CSS/JSON/YAML servers.
LSP is on by default (`lsp_enabled`); no per-language switches.

- Ten tools: `lsp_status`, `lsp_diagnostics`, `lsp_definition`,
  `lsp_references`, `lsp_hover`, `lsp_symbols`, `lsp_workspace_symbols`,
  `lsp_code_actions`, `lsp_rename` (plan only) and `lsp_format`.
- A successful write attaches a compact `[LSP auto-diagnostics]` block when a
  server is already warm; a write never starts one.
- `.vscode/settings.json` is honoured, servers close after 10 minutes idle,
  TSX/JSX open as `typescriptreact`/`javascriptreact`.
- A new card in CONFIGS carries the switch, the auto-diagnostics toggle, the
  timeouts and the Python lint engine, and lists the install command for every
  supported language.

## Telegram

- Voice notes in both directions, and a spoken note keeps the selected mode.
- **Photos** (or an image sent as a file) become a run with the caption as the
  prompt; the vision projector is loaded on demand.
- **PDFs and text files** are extracted on the engine and run as prompt text.
  40 pages / 120k characters, 25 MB upload; a scanned PDF answers "send the
  pages as photos" instead of pretending.
- The phone follows the UI mode; a `/mode` override on the phone is dropped as
  soon as the mode changes in the browser.

## Long runs

- A follow-up prompt no longer dies because the coder was reloaded right after
  the previous run, and the coder-load retry honours `/stop` instead of
  churning model loads for minutes.
- Compression drops whole old turns and never splits a tool call from its
  results, never produces two user messages in a row.
- `[MEM]` lines show commit charge and per-process usage at
  startup/load/evict/run-end, so a slow run is diagnosable.
- Read aloud follows the route a run actually took, and the switches are
  re-read from the engine instead of relying on page state.

## Installer and updates

- **Release notes render properly**, with UTF-8, wrapping and a box that fits.
- The routing-decider question appears, the step no longer
  closes the window, and it logs to `logs\decider_install.log`.
- **Essential set** is the four models of the default route (gemma-4 E4B QAT,
  qwen3.5 4B MTP, Sharp-MiniCPM5 2B Q6, LFM3 1.7B). The menu prints it, and
  every finished step asks `[M] more models` / `[P] proceed`.
- Answering "no" to a model skips it completely, Ctrl+C exits cleanly.
- After every update the installer offers the optional pieces that are not on
  the machine yet (voice, decider, browser engine, POSIX tools).
- Core Utils are skipped when Git Bash already provides the tools, and an
  already installed package counts as success.
- The desktop shortcut is refreshed on every update, icon included.
- The startup update check offers new releases, verifies the sha256 and cleans
  up its download.

## Models

- Groot stays experimental.
- LFM3 is a tool-calling model; it needs llama.cpp b12001 or newer.
- The installer wires the agent roles to whatever `models.json` contains after
  a download — empty or uninstalled slots only, a model you picked stays.
- Defaults for a fresh install: mode `chat`, `code_duo` in agentic mode, the
  planner on and sharing the coder context.

## Known limits

- A truncated `edit_file` is dropped rather than applied half-way.
- The run deadline is soft: one +300 s grace extension per run.
- MCP 2026 support is limited to `server/discover` and `_meta` versioning.
- `undo_last`'s safety snapshot does not cover untracked files.
- Scanned PDFs are not rasterised (no PDF renderer is bundled).

## Tests

255 regression suites, ruff clean, 13 pre-push checks.
