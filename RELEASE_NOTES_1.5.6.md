# Release 1.5.6

Maintainability, direct-run control, patch-native editing and 35B performance
on top of 1.5.5. No settings are reset and no model has to be re-downloaded.

Upgrade with `update.bat` (or the startup check offers it), or extract the zip
next to the old install. Settings, models, sessions and the venv are kept.

## Direct runs

- `/pause` now works on plain direct streams: the runner gates before a model
  round and at token chunks, `/pause-state/{chat_id}` becomes active, and
  `/resume` continues. An abort during the pause ends the run as `aborted`.
- Mid-run steering reaches pure direct runs: a steer queued before the answer
  rides into the prompt, one queued during the answer gets a bounded
  continuation round (max 2), and late steers deliver after the run.

## Tools and context

- `duo_core` stays the coder baseline (18 tools by default: 17
  read/edit/run/test tools + LSP diagnostics; `send_file` is request-gated).
  `duo_coder_tool_tier=full` opts into git/web/MCP/browser/LSP extras. Direct
  keeps the `readonly`/`research` tiers.
- Writes warm the language server in the background and attach diagnostics
  autonomously; the pyright/per-language lint block remains the fallback.
- New `apply_patch` tool applies a unified diff to one or more files in one
  call, with path-escape checks, all-files validation, atomic writes and
  rollback on a write failure. `old_text/new_text` and `edits=[...]` are
  unchanged.
- `duo_auto_test_on_edit` can run mapped tests in the same tool turn, and old
  edit payloads in history are compacted to `[applied diff: path (+X/-Y)]`
  stubs in long runs.
- Deep retrieval: when the tiny-workspace pre-seed is empty, a bounded nested
  index walk ranks files against the task text and adds path + size + top
  symbols (never full contents) to the first coder message.
- Frontend: all `localStorage` access goes through guarded wrappers, so
  Firefox's `SecurityError: The operation is insecure` no longer produces a
  frontend-error toast when storage is blocked.

## 35B performance

- A large MoE GGUF (>12 GB) no longer inherits a global `llama_mlock=true`
  as `mmap+mlock` (locked expert pages paged in from NVMe and measured up to
  4.7x slower decode). The profile and RAM guard still decide;
  `llama_mlock_skip_large_moe=false` restores the old behavior.
- MoE models can raise the ubatch via the profile `"ubatch"` key or
  `duo_ubatch_moe`, but only when the VRAM pre-flight fits the larger compute
  buffer, with a baseline fallback otherwise.
- Text-only loads skip the vision projector (`vision_mmproj_always=true`
  restores always-attach).
- Slot matching canonicalizes `:latest`/`#N` and casing, so role switches no
  longer reload a resident model.

## Benchmark harness

- HiveMind `duo`/`duo_full`, Aider, Hermes and Pi run against the same hidden
  tests, and all adapters share `_agent_timeout_s` for timeout parity.
- Raw/raw_files are no longer default conditions or report rows.
- The Colab autopilot runs 3 core rounds with the 4B model, then the 34-task
  Aider Polyglot suite, with optional 35B and DeepSWE cells, per-cell atomic
  Drive sync, `--resume` and intermediate status lines.

## Verification

- Full regression gate: 380/380 suites (Windows dev tree).
- Ruff clean; pyright reports 0 errors on the changed backend modules and no
  new errors against the baseline.
- Docker/Modal SWE-bench and DeepSWE harness checks stay green offline.
