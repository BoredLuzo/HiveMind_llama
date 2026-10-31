# Release 1.5.0-preview

This is a preview of the 1.5 line: the MCP hardening and the settings/preset
work that followed 1.4.14. It is the build for the ThinkPad validation; Groot
stays experimental and Hermes stays the default coder.

## MCP: the two audit P1s are closed

- The exec gate now covers the whole execution class: `start_background` and
  `install_package` were missing from `_INTERNAL_EXEC_TOOLS`, and `run_tests`
  (project test scripts) plus `browser` (`page.evaluate` runs arbitrary JS)
  are execution-equivalent. All of them, together with the mutating tool class
  (`write_file`/`edit_file`/`write_file_append`/`edit_ast`/`replace_lines`/
  `patch_file`/`git_commit` and every `mcp_*` name), are refused by
  `/internal/tool/exec` without `HIVEMIND_INTERNAL_TOKEN`. Read-only tools stay
  available. The mutating set is imported from the runner's approval set so
  the two cannot drift.
- The action-approval gate keeps its contextless semantics; the IDE path is
  closed by the token gate instead of a second approval branch.

Phase-2 fixes from the same audit: an SSE response for another id raises
instead of being handed to the model as the tool result; `close_all()` takes
the lifecycle lock (shutdown no longer races the boot refresh); HTTP close
sends `DELETE` with the session header; the local `search_code` fallback is
argv-only (no shell quoting); the advertised `shell` forwards as `run_bash`
after the tier check; both MCP docs state the retry statuses and the auth flag
truthfully.

The remaining 34 findings are closed in the same release: the client streams
SSE with a body cap, recovers a 404 session once, checks ids on the JSON path,
redacts stdio args/URL userinfo, warns on config decays, joins multi-line SSE,
serializes call vs close/refresh, renders `structuredContent` and marks
truncation; the server seeds budgets on the contextless IDE path, writes the
audit line on timeouts, handles `notifications/cancelled` and stdio timeouts,
returns spec-correct error codes, narrows `isError`, sanitizes log names,
aligns the stdio/HTTP caps, bounds body reads and connections, and negotiates
the protocol version. Integration adds an `mcp_*` call budget and scans MCP
arguments in the destructive gate.

Phase 2 completes the tool surface: every tool now carries MCP annotations,
and the server exposes `git_diff`, `git_commit` (explicit paths only - no
`git add -A`), `run_tests` (stdio-only, output capped) and `model_status`
(loads nothing). It also speaks MCP resources (`resources/list`,
`resources/read`, `resources/templates/list`) and prompts (`review_diff`,
`explain_file`, `fix_tests`). Resources stay inside the configured workspace,
are text-only, cap at 256 KB and hide secrets such as `settings.json`,
`gateway.toml`, `.env*` and private keys. The advertised protocol remains
2024-11-05; 2025 sessions/GET-SSE stay deliberately unadvertised.

## Settings and presets

The code read several settings that were never shipped as defaults
(`duo_compress_tail_tool_max_chars`, `duo_planner_enabled`,
`duo_coder_always_vision`, the telegram mirror/approval/phone keys). They are
persisted now. `telegram_gateway_enabled` is deliberately absent from the
defaults: the gateway treats "absent" as "the toml master switch decides" and
"present and false" as a veto, so a shipped False would have shut down
toml-only gateways.

The save path fsyncs and cleans its temp file; the async call sites snapshot
the dict before the worker thread serializes it; POST /settings applies the
llama startup flags through the same helper as a preset load; presets migrate
the old `llama_ubatch_size` alias onto `duo_ubatch` and no longer inject keys
that no longer exist; the apply bookkeeping is stripped from loaded presets.

UI: the tool-round select can represent the shipped 50, the `.opt-name` rows
and the backend-only keys are searchable, the VRAM gate lands in the agents
panel, and the branch fields write the key the backend reads.

## Models and installer

- Groot is offered by the installer from
  `GrootTeam/Groot-19B-A3B-261023-GGUF` (Q4_K_M) with the shipped profile
  defaults: ctx 131072, ncmoe 32, KV q4_0, mmap+mlock, MTP on, native GGUF
  template; ub comes from `duo_ubatch` (shipped 256).
- LFM3 1.7B QAD replaced the retired LFM2.5-2.6B line in 1.4.14; the installer
  prose no longer mentions the 2.6B drafter.

## Language servers (LSP)

The harness now speaks LSP through one generic client instead of per-language
integrations. It auto-detects pyright-langserver (Python),
typescript-language-server (TS/JS), gopls, rust-analyzer, clangd and the
bash/lua/json/yaml/html/css servers; `lsp_servers` overrides a command, and
`lsp_enabled` is the only switch. The agent can ask for diagnostics,
definition, references, hover, document/workspace symbols and code actions,
and can request a project-wide rename plan (approval-gated, nothing is
written). A missing server returns `LSP_UNAVAILABLE` with the install command;
servers start lazily on first use, never at boot. Diagnostics are read-tier,
so the model can stop reading whole files to find type errors.

The agentic audit found two process leaks, both fixed: engine shutdown now
closes every cached language-server manager, and cache eviction closes the
evicted managers instead of orphaning their processes. A successful write or
edit also attaches a compact `[LSP auto-diagnostics …]` block when a server is
already warm (8-line cap; `lsp_auto_diagnostics=false` disables it); a write
never starts a cold server; diagnostics attach once one is warm. A cold
`lsp_diagnostics` waits up to 5s for the first publication
(`lsp_diagnostics_wait_s=5`) and only then answers "still analyzing" instead
of claiming the file is clean. Pyright's `file:///c%3A/...` URIs are canonicalized, so its real
diagnostics are actually visible.

The second LSP pass closes the remaining audit P2s: diagnostics now carry the
document version, so a delayed publication for the old text cannot satisfy a
freshness wait; `.tsx`/`.jsx` open as `typescriptreact`/`javascriptreact`
while still using the TypeScript server; `lsp_status`/`lsp_diagnostics` are
tier-1 in the schema fitter, and `lsp_enabled=false` removes the schemas
instead of offering tools that only answer `LSP_DISABLED`. Rename plans now
include the 1-based edit lines. A real-pyright smoke suite covers the whole
client path and skips on machines without pyright.

## Code panel, startup update check, compression

The code panel no longer rebuilds every line on every stream tick. Plain
streamed text appends to one text node, finished file/diff HTML is cached per
tab, paints coalesce per animation frame, very large files fall back to plain
text, and diff rendering is capped. Long agentic runs should stay responsive
while a 20k-line file is being written.

`start_hivemind.bat` now checks GitHub at startup and offers a newer release
with one `[Y/n]` prompt. The answer is cached for 24 hours, the check is silent
offline, pre-releases are offered to preview installs, and an accepted update
downloads the ZIP, verifies GitHub's sha256 digest and applies it through
`update.bat --zip`. Set `HIVEMIND_NO_UPDATE_CHECK=1` to skip it.

Compression now drops whole old tail turns when the summary plus the kept raw
tail still exceeds the threshold. It never splits an assistant tool call from
its tool results, keeps the newest four messages, adds a `[CONTEXT DROPPED: …]`
marker and releases the read guard for dropped read paths. The rule-based
fallback gets the same behavior and now accepts
`duo_compress_tail_tool_max_chars` (the caller had always passed it, but the
parameter was missing, so the fallback would have raised before compressing).

## Maintainability

The final pass removed the dead settings keys that old exports kept
round-tripping, single-sourced the runtime-flag key list (POST /settings
imports it from the helper), removed three silent swallows, renamed the token
gate to what it actually covers, and restored the comment blocks the docs
generator had attached to the wrong keys. The dev-only sync tool now keeps a
manifest and prunes files that left the repository.

## Tests

239/239 suites, ruff clean. New coverage: MCP phase 2 (137 checks:
annotations, the four new tools, resources containment/secret denylist,
prompts, backward compatibility), LSP client/tools (54 + 76 checks), the
startup update checker (36 checks), whole-turn compression (15 checks) and
the real-pyright LSP smoke suite (5 checks; skipped when pyright is absent).
The existing MCP suites still pass (28 + 175 + 73 + 23).

## Known limits

- A truncated `edit_file` is still dropped rather than applied half-way; the
  harness compresses first and asks for a smaller edit.
- The run deadline is soft: one +300 s grace extension per run, and the wedge
  handoff keeps its documented exemption.
- `mutate_settings` still covers one production path; the wider rollout and
  the two large functions (`run_code_duo`, `_start_process`) remain follow-ups.
- The live settings still carry ~20 keys with no reader; preset apply filters
  them, a load-time pop list is a follow-up. The live `settings.json` holds a
  plaintext GitHub token - rotate it and use the UI field.
