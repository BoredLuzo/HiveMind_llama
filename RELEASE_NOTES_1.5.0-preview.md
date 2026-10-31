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

## Maintainability

The final pass removed the dead settings keys that old exports kept
round-tripping, single-sourced the runtime-flag key list (POST /settings
imports it from the helper), removed three silent swallows, renamed the token
gate to what it actually covers, and restored the comment blocks the docs
generator had attached to the wrong keys. The dev-only sync tool now keeps a
manifest and prunes files that left the repository.

## Tests

233/233 suites, ruff clean. The new MCP suite covers the token gates, the
read-only exception, the SSE id mismatch and the session DELETE.

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
