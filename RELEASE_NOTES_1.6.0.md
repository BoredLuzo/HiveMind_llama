# Release 1.6.0

Models, Direct run control, patch-native Code Duo editing, MoE performance and
security hardening on top of 1.5.5 (`v1.5.6` was tagged only and never released
separately; its changes are included here).

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or the startup check offers it), or extract the release zip
next to your old install — `settings.json`, `models.json`, `sessions\`, `logs\`
and `.venv` are kept.

## Upgrade notes (behavior changes)

- **Updater is fail-closed:** Remote downloads refuse and keep backups if the
  release metadata lacks a matching SHA-256 digest. Extracting a local zip next
  to the old install skips this check and always works.
- **`POST /mcp` is stricter:** Requests with cross-site fetch headers, unknown
  origins (configure `MCP_HTTP_CORS_ORIGINS` if needed), or non-JSON bodies get
  `403`/`415`; standard CLI and desktop MCP clients are unaffected.
- **Text-only loads skip `mmproj`:** The vision projector only attaches once a
  vision request arrives (one-time reload). Set `vision_mmproj_always=true` to
  restore always-attach.
- **Large MoE GGUFs (>12 GB) skip `mlock`:** Global `llama_mlock=true` no longer
  forces locking, preventing slow NVMe expert paging (`llama_mlock_skip_large_moe=false`
  restores old locking).
- **Refreshed installer defaults:** The catalog switches defaults away from the
  older `hermes3.6` preset; already installed models remain untouched.

## Models & GGUF

- Drop-in discovery inspects GGUF metadata, multi-part shards and paired vision
  projectors without overwriting custom names, while explicit profiles always take
  precedence. Unknown MoE weights fall back to name detection (`30B+`).
- Updated model catalog presets (`twil:8b`, `air:26b-a4b`, `qwen3.6:35b-a3b-mtp`)
  with unified chat templates and KV defaults.
- Model registry merges scans cleanly so manual path registrations stay intact.

## Direct runs & routing

- `/pause`, `/resume` and mid-run user steering now work on plain Direct streams
  as well as tool loops; aborting a paused run ends cleanly as `aborted`.
- Auto mode no longer hijacks general questions into Code Duo when the encoder
  decider is off, and short read lookups stay on Direct tools.
- Added a read/web-only `research` tier for Direct mode, warm/direct slot
  reuse for memory extraction (capped context), automatic dead-MCP reconnect in
  scheduled automations, and improved Markdown chat typography in the UI.

## Code Duo & tools

- New `apply_patch` tool edits multiple files via unified diffs with atomic
  rollback on errors, alongside optional selective auto-tests after writes.
- Streamlined lean coder tier (`8` orthogonal tools; default `duo_core` stays
  `18` base tools + default Subagent = `19`) with tier-aware prompts and hints.
- Writes warm language servers in the background for automatic diagnostics, while
  the verify gate checks tests/smoke scripts (up to 3 vetoes before marking an
  unverified completion).
- Expanded multi-language symbol indexing, repo-map deltas on compression,
  tail-first KV eviction, and resumable chunk checkpoints across restarts.

## MoE performance

- Skipping global `mlock` on large MoE weights prevents NVMe thrash (measured up
  to 4.7x faster decode on 35B MoE vs. `mmap+mlock`).
- MoE models can raise compute `ubatch` when VRAM fits and split prefill/decode
  CPU threads cleanly.
- Canonical slot matching (`:latest`, `#N`, casing) avoids reloads on role
  switches, while active or pinned slots stay protected during concurrent requests.

## Security & isolation

- Native file and search tools enforce a shared denylist covering `.env`, SSH/TLS
  keys, credentials and session stores; test runner paths stay confined to the
  workspace (`Path.relative_to`).
- Subprocess tool runners strip secret and token environment variables before
  spawning child processes.
- Browser origin/content checks on `/mcp` and traversal validation on model
  configurations.

## Benchmarks

- Unified multi-agent harness (`bench/`) comparing HiveMind modes against Aider,
  Hermes and Pi with shared timeouts, hidden-test isolation and a resumable 3x3
  Colab runner.

## Verification

- **386/386** standalone regression suites passing on Windows, `ruff` clean,
  pyright baseline intact (`0` new errors), settings docs (`294` keys) and
  `uv.lock` verified.
- Linux CI portable subset, `13/13` LSP servers and `13/13` online `mmproj`
  specs verified.
