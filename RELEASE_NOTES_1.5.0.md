# Release 1.5.0

1.5.0 builds on 1.4.14 with five workstreams: a hardened, protocol-current
MCP surface (server + client), a generic language-server client for the
coding agent, compression and context-reliability work for long agentic runs,
a phone surface that now takes pictures and documents, and a round of
installer/updater/git audits whose findings are all fixed in this build.
Hermes stays the default coder, Groot stays experimental.

Highlights, if you read nothing else:

- **MCP both directions** — 2024/2025/2026 protocols, resources, prompts,
  23 annotated tools, token-gated writes, per-server budgets, one-command
  IDE configs.
- **LSP for every installed language server** — ten `lsp_*` tools,
  automatic diagnostics after edits, workspace `.vscode/settings.json`
  honoured, on by default.
- **Reprompting is reliable** — the post-run reload race that hard-stopped
  follow-up prompts is gone; the coder-load ladder survives aborts.
- **Telegram takes photos and PDFs** — caption becomes the prompt, the
  projector loads on demand, scanned PDFs say so instead of pretending.
- **Installer fixes** — release notes render, the decider prompt appears,
  the essential set is the default route, declining a model skips it whole.

Upgrade: run `update.bat` (or the new startup check offers the update
directly), or extract the zip next to the old install. Settings, models,
sessions and the venv survive; new settings keys pick up their defaults
automatically. One new dependency: `pypdf` (pure Python, for document text).

## MCP: hardening, 2025 + 2026 protocols, budgets, IDE configs

- **Exec and mutating tools need the shared secret.** `run_bash`, `shell`,
  `run_python`, `start_background`, `install_package`, `run_tests`,
  `browser` and the whole mutating class (`write_file`, `edit_file`,
  `write_file_append`, `edit_ast`, `replace_lines`, `patch_file`,
  `git_commit`, every `mcp_*` name) are refused by `/internal/tool/exec`
  without `HIVEMIND_INTERNAL_TOKEN`; read-only tools stay available. The
  mutating set is imported from the runner's approval set so the two cannot
  drift.
- **The remaining 34 audit findings are closed** (client C2-C13, server
  S3-S14, integration I3-I5/I15): streaming SSE with a body cap, 404
  session recovery, id checks, off-loop taskkill, redaction, budget
  seeding, timeout audit lines, `notifications/cancelled`, spec-correct
  error codes, 2 MB caps, version negotiation.
- **Phase 2**: all 23 tools carry MCP annotations; new tools `git_diff`,
  `git_commit` (explicit paths only), `run_tests` (stdio-only, capped) and
  `model_status` (loads nothing); MCP resources
  (`list`/`read`/`templates`) stay workspace-contained, text-only, 256 KB
  capped, with a secret denylist; prompts `review_diff`, `explain_file`,
  `fix_tests`.
- **Protocol 2025-06-18**: HTTP sessions (`Mcp-Session-Id`, 1h idle TTL,
  64-session cap, idempotent DELETE) and the `GET /mcp` SSE stream with
  keep-alive, `Last-Event-ID` replay over a 500-event ring and live
  progress fan-out. stdio needs no session.
- **Protocol 2026-07-28 (minimal)**: `server/discover`, per-request
  `_meta` protocol-version validation (`-32022` for unsupported), and a
  stateless 2026 initialize (no session — the 2026 revision removed
  sessions). Batched initializes of any version answer on the 2024
  baseline. `resultType`, header enforcement, cache hints and MRTR stay
  unimplemented until clients appear.
- **Client**: per-server call budgets (`mcp_server_budgets`, rolling hour,
  `MCP_BUDGET_EXCEEDED` names server/limit/reset), remote tool annotations
  pass through, and `python tools/mcp_config_gen.py` prints ready-to-paste
  configs for Claude Desktop, Cline, Zed and VS Code — no real token in
  the output unless `--include-secret`.

Using it: `start_mcp.bat` serves `http://127.0.0.1:8090/mcp`; editors that
spawn a process use

```json
{"mcpServers": {"hivemind": {
  "command": "python",
  "args": ["infra/mcp_server.py"],
  "cwd": "C:/path/to/HiveMind",
  "env": {"MCP_ALLOW_WRITE": "1"}
}}}
```

`MCP_ALLOW_WRITE=1` enables the write tier, `MCP_ALLOW_EXEC=1` the exec tier
(**stdio only**), `MCP_ALLOW_QUERY=1` the chat query tool. Details and the
full tier table: [docs/mcp.md](docs/mcp.md).

## LSP: any installed language server

LSP is **on by default** — it activates through `lsp_enabled: true` in
`settings.json`: once a language server for the file type is installed,
the `lsp_*` tools appear in the coding modes. One generic client drives
whatever language servers are installed
(pyright, typescript-language-server, gopls, rust-analyzer, clangd, and
the html/css/json/yaml/bash/lua servers). `lsp_enabled` is the only
switch; `lsp_servers` overrides a command.

- Tools: `lsp_status`, `lsp_diagnostics`, `lsp_definition`,
  `lsp_references`, `lsp_hover`, `lsp_symbols`, `lsp_workspace_symbols`,
  `lsp_code_actions` (titles only), `lsp_rename` (plan with 1-based edit
  lines, writes nothing, approval-gated) and `lsp_format` (approval-gated,
  applies the server's edits atomically). Read tiers get the eight read
  tools; the two mutating tools are full-mode only.
- **Automatic diagnostics after writes**: a successful write/edit appends
  a compact `[LSP auto-diagnostics …]` block when a server is already warm
  (8-line cap; `lsp_auto_diagnostics` disables it). A write never starts a
  cold server. Diagnostics carry the document version — a delayed
  publication for the old text cannot satisfy a freshness wait — and LSP
  3.17 pull diagnostics are used when the server advertises them.
  rust-analyzer's native error survives its empty-clear follow-up.
- **Workspace settings**: `.vscode/settings.json` is honored
  (configuration pulls per section plus one `didChangeConfiguration`
  push). A cold `lsp_diagnostics` waits up to 5 s for the first
  publication instead of claiming a clean file. Servers close after 600 s
  idle (`lsp_idle_timeout_s`) and restart lazily. TSX/JSX open as
  `typescriptreact`/`javascriptreact`.
- **UI**: a new *Language Servers & Auto-Lint* card in CONFIGS carries the
  on/off switch, the auto-diagnostics toggle, the wait and idle timeouts and
  the Python auto-lint engine (auto / pyright only / syntax only). The card
  lists what to install for every supported language —
  `npm i -g pyright` (Python), `typescript-language-server` (TS/JS),
  `rustup component add rust-analyzer` (Rust),
  `go install golang.org/x/tools/gopls@latest` (Go), clangd (C/C++),
  `bash-language-server`, `lua-language-server` and
  `vscode-langservers-extracted` (JSON/YAML/HTML/CSS).
- **Lifecycle**: engine shutdown closes every cached server; cache
  eviction closes evicted managers; pyright's `file:///c%3A/…` URIs are
  canonicalized. Live-verified: pyright errors found → fixed → clean;
  rust-analyzer formatting with tabs preserved.
- Full tool table, tiers, errors and the server registry: `docs/lsp.md`.

## Reliability: reprompting, load races, compression

- **Follow-up prompts survive the post-run window.** The old sequence —
  run ends, coder evicted, a queued profile-ctx reload of the same model
  starves the next run's coder load, and the loader timeout hard-stopped
  on "attempt 1/3" — is fixed on both ends: `unpin_duo_models` skips
  models that are no longer loaded (the reload race is gone), and the
  coder-load timeout now evicts the stuck attempt and rides a ctx ladder
  (5 attempts) that honors `/stop` at every step. Live-tested: initial run
  `completed`, immediate follow-up `completed` in 181 s.
- **Whole-turn compression**: when the summary plus the kept raw tail
  exceeds the threshold, the oldest whole turns are dropped — never
  splitting an assistant `tool_calls` message from its tool results,
  never producing a `user,user` sequence (strict-alternation templates
  reject that), keeping the newest messages, adding a
  `[CONTEXT DROPPED: …]` marker and releasing the read guard. The
  rule-based fallback shares the behavior and now accepts
  `duo_compress_tail_tool_max_chars` (previously a missing parameter).
- **`[MEM]` observability**: one commit-charge line per
  startup/load/eviction/run-end —
  `commit=18.3/50.9GB engine=… llama[…]=… tts=… lsp[…]=…` — so a slow
  run is diagnosable per process instead of guesswork
  (`mem_watch_enabled=false` silences it).

## Git integration (audit fixes)

The git deep audit found no security hole (argv-list subprocesses, URL
vetting, masked token flows, no leak end to end) but four correctness
bugs, all fixed and probe-verified: auto-commit could not commit NEW
files since the 2026-10-14 scoping (`git commit -- paths` matches tracked
paths only — the scoped add is back), the checkpoint squash folded
foreign staged work via a bare commit (now pathspec-scoped, and a
`files=None` squash refuses instead of sweeping), the model-callable
`git_commit` tool requires explicit `paths` and rejects `.`/`..`, and
the ungated `undo_last` hard reset takes a recoverable
`git stash create` safety snapshot first.

## Installer & updates

- **Release notes render properly.** The old inline render double-mangled
  UTF-8 (ANSI read → OEM `type`), never wrapped 200-char lines,
  misaligned the box for every tag and erased code fences.
  `deploy/render_release_notes.ps1` draws the notes and the box itself:
  UTF-8 read, markdown-light conversion, code-block markers, 61-column
  wrapping, computed header width.
- **The decider opt-in prompt works** (`choice /d N` without `/t` errored
  on every install, so the question never appeared), and its "ready"
  message now gates on the torch/gliner2 import probe and the model
  download instead of only the snapshot exit.
- **Desktop shortcut auto-refresh**: `update.bat` re-creates the shortcut
  idempotently after every update (`create_shortcut.bat --quiet`) and
  refreshes the shell icon cache — no more installs whose icon went
  missing and stayed missing.
- **The updater asks for optional components.** After the dependency refresh
  it detects what this machine does not have yet — speech to text, text to
  speech, the routing decider, the Playwright browser engine, POSIX tools
  (skipped when Git Bash covers them) — lists it and asks once whether to
  install it. A fully equipped install gets one line and no prompt; every
  install is failure-tolerant. `deploy\check_optionals.bat --dry-run` shows
  the detection without touching anything.
- **Startup update check**: one `[Y/n]` offer at startup, cached for 24 h,
  silent offline, pre-releases only for preview installs, sha256 gate on
  the download, warnings digest with word-boundary matching ("Keine
  breaking changes" no longer triggers the scary prompt), and the
  downloaded zip is cleaned up after a successful install.
- There is no judge-specific model download: the Judge role can use any
  installed LLM. The optional routing decider (CPU classifier) remains the
  only routing extra.
- **The "essential" set is the default route, and it says so.** It used to
  be a skip-list of 15 models — including the 20.8 GB Qwen3.6 — which is not
  "essential" on a small machine. It is now a whitelist of four models
  (gemma-4:e4b-it-qat, qwen3.5:4b-mtp, minicpm5:2b-sharp, lfm3:1.7b-qad),
  the menu prints that list before asking, and every finished step ends with
  `[M] download more models` / `[P] proceed` instead of dropping out.
- **Declining a model skips it completely.** Answering "n" to a large GGUF
  used to still download its projector (~0.8 GB) and register the model in
  `models.json`. Ctrl+C during a download now exits with a plain message
  (the partial file stays and resumes next run) instead of an ssl traceback.
- **Core Utils: no false alarm, and usually not needed at all.** `run_bash`
  uses Git Bash when it is installed, and Git already ships
  grep/sed/awk/find — the installer now says so and skips the prompt instead
  of asking for a package the machine does not need. When there is no Git
  Bash, the winget id falls back to the Store product id (`9NBLGGH4R32N`),
  and success is verified by the **package** (`Get-AppxPackage
  Microsoft.Coreutils`) and the WindowsApps alias, not by `where grep` — a
  finished install no longer reports "did not complete" just because the
  running shell cannot see a fresh app alias yet.

## Prompt editor, search, LSP controls

The prompt editor now offers **exactly the roles a runner reads**, driven by
one server-side list (`/presets/prompt-roles`): analyst, refiner, critic,
synthesizer, duo_synthesizer, direct, vision_agent, duo_coder,
duo_critic_code and duo_critic_general. Before, the dropdown came from the
agent-card metadata: it offered `intent` (rule-based, no prompt) and a bare
`duo_critic` (both runners read `duo_critic_code`/`duo_critic_general`), so
those edits wrote files nothing loaded, while the two real critic keys and
`duo_synthesizer` were not editable at all. A legacy `duo_critic` file is
still read for both critic keys, so existing edits became effective instead
of staying dead; an unknown role now answers an empty prompt instead of
silently showing the Direct prompt. The vision prompt honours the preset
override now (it was passed as the built-in constant).

Feature search: a new **Language Servers & Auto-Lint** card in CONFIGS makes
`lsp_enabled`, `lsp_auto_diagnostics`, the diagnostics wait, the idle
timeout and the Python auto-lint engine configurable in the UI (they were
settings.json-only, so the search had nothing to find: "lsp", "pyright",
"linter" returned zero results). The indexer also covers bare
checkbox+span rows, so "mlock" finds "Lock in RAM"; the VRAM-TTL row is
searchable as "keep alive"/"ttl"; and the alias table gained the German and
acronym forms people actually type (kompression, zeit, speicher, lsp,
pyright, keep alive, …).

## Telegram: voice, images, documents

The phone surface ships voice, pictures and files — all local, all behind the
owner/private/rate gates.

**Voice.** A voice note is transcribed on the CPU (whisper.cpp/Parakeet) and
runs exactly like typed text; answers can come back as a voice note. A spoken
note now **keeps the selected conversational mode** (chat/simple/direct used to
be rerouted to `auto`, so speaking took a different route than typing);
`code_duo` and friends still hand the route to the engine, because forcing them
on "what time is it" was the old bug.

**Images.** A photo (or an image sent as a file) becomes a normal run with its
**caption as the prompt** — "what is wrong in this screenshot?" works from the
phone. The gateway used to answer "Text only in this build" while the engine's
`/stream` already accepted `images`. The largest photo size is used, image
documents are recognised by mime type, an empty caption falls back to
"Describe this image.", downloads are capped at 10 MB and the engine still
enforces its own 8-images / ~14 MB-per-url limits. Switch: **Images in** in the
Telegram card (or `telegram_image_in`), on by default; `image_in` in
`gateway.toml` is the fallback.

**The vision projector loads on demand.** A request with images asks for
`vision=True`; a warm text-only slot is reloaded with the projector instead of
answering "image input is not supported". New: the loader also honours a
`mmproj_filename` declared in the registry, so **user-registered** vision
models get their projector even when their base is not in the built-in list.

**PDFs and text files.** A PDF, `.txt`, `.md`, `.log` or `.csv` is extracted on
the engine (`POST /documents/extract`, pypdf in a worker thread) and run as
prompt text with the caption as the question — "summarize this" works. Caps:
40 pages / 120k characters (the answer says when it was cut), 25 MB upload. A
scanned PDF without a text layer answers "send the pages as photos" instead of
pretending, and a document larger than one phone run accepts is refused with a
hint to ask about a part. New dependency: `pypdf` (pure Python).

## Code panel & UI

The code panel no longer rebuilds every line on every stream tick: plain
streamed text appends to one text node, finished file/diff HTML is cached
per tab, paints coalesce per animation frame, very large files fall back
to plain text, and diff rendering is capped. The PRESETS tab shows which
preset is active, when it was applied, and whether settings were edited
since (the startup autoload then deliberately skips — the edited settings
win). The git-tools select and the Git Operations row explain what they
gate (model tool visibility vs. owner-only manual actions).

## Settings & presets

Settings the code read were never shipped as defaults
(`duo_compress_tail_tool_max_chars`, `duo_planner_enabled`,
`duo_coder_always_vision`, telegram keys, `lsp_*`, `mcp_server_budgets`,
`mem_watch_enabled`) — all persisted now. The save path fsyncs and cleans
its temp file; POST /settings applies llama runtime flags through the
same helper as a preset load; presets migrate the old
`llama_ubatch_size` alias and skip keys that no longer exist; 17 dead
keys are dropped on load. `docs/settings.md` is generated (264 keys).

## Models

- The installer catalog also carries an **experimental** 19B MoE
  agent-coder (off by default, not documented here — evaluate before
  relying on it).
- The Judge role can use any installed LLM (no dedicated download). The
  optional routing decider stays the only routing extra.

## Maintainability & audits

Five audits ran against this build (LSP agentic, MCP, git, installer/
updater, and four end-of-day slices over every change); all P0/P1
findings are fixed in this build, with probe-verified reports in the
repo docs. The dead settings keys, silent swallows and the divergent
runtime-flag lists are gone; `docs/settings.md` and `docs/lsp.md` are
generated and gate-checked.

## Tests

249/249 regression suites, ruff clean, 13/13 pre-push checks. New coverage:
MCP phase 2 (137 checks), MCP 2025 (47), MCP client budgets/config-gen (41),
MCP 2026 (15), LSP client/tools (55 + 76), the startup update checker (39),
whole-turn compression (15), `[MEM]` (16), the worker-pin suite (6),
preset prompt roles (26: the editor only offers keys a runner reads),
the installer download flow (25: declining a model skips the whole spec,
the essential set is a 4-model whitelist), Telegram images (22: photo sizes,
image documents, caption as prompt, caps), documents (23: PDF extraction,
page/char caps, DOC_* errors) and the real-pyright/real-rust LSP smoke suites
(5 + 4; skipped when the server is absent). The existing MCP suites still pass
(28 + 175 + 73 + 23), and the gateway bridge suite grew to 213 checks
(voice keeps the selected mode, images reach the engine).

## Known limits

- A truncated `edit_file` is still dropped rather than applied half-way;
  the harness compresses first and asks for a smaller edit.
- The run deadline is soft: one +300 s grace extension per run.
- `mutate_settings` covers one production path; the wider rollout and
  the two large functions (`run_code_duo`, `_start_process`) remain
  follow-ups.
- MCP 2026 items (`resultType`, header enforcement, cache hints,
  `subscriptions/listen`, MRTR) are implemented only as far as
  `server/discover` + `_meta` versioning; the rest waits for real
  clients.
- `undo_last`'s safety snapshot does not cover untracked files
  (`git stash create` cannot include them).
- PDF extraction needs a text layer: a scanned PDF is answered with "send
  the pages as photos" instead of being rasterised (no PDF renderer is
  bundled). Images through the phone need free VRAM for the projector —
  the same pre-flight rule as any text run.
- The plaintext GitHub token warning from the preview build still
  applies if your settings predate the fix: rotate the token and use
  the UI field.
