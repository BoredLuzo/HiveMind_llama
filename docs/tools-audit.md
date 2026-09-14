# Tools Deep Audit — 2026-09-13

Three audit streams (file ops/guards, execution/external tools, loop
integration) over the `tools/` package and its wiring in `core/`. This
document records ALL findings and their status. Fixes shipped in v1.1.4 are
marked **FIXED**; deliberately accepted risks are marked **ACCEPTED**; the
rest are **OPEN** (low priority, no behavior bug in the default path).

## Verify chain (loop integration)

| # | Finding | Status |
|---|---------|--------|
| 1 | `verify_last_ok_serial` bumped by every tool type — verify gates permanently disarmed (tool_exec_helpers.py:703) | **FIXED** — run_bash-only guard |
| 2 | `run_bash("echo '[TEST-RESULT] ✅'")` spoofed the auto-test gate; `echo pytest` counted as a test run | **FIXED** — gate counts only real run_tests results or run_bash with test-command evidence in the tool_call args |
| 3 | Exit-code scan took the FIRST `[exit code: N]` match — echoed `[exit code: 0]` masked real failures | **FIXED** — last match wins (tool_executor + utils/tool.py) |
| 4 | Failing `run_tests` (`[TEST-RESULT] ❌`, no exit-code marker) counted as verified | **FIXED** — ❌ counts as failure in `run_bash_failed` |
| 5 | After a full compression the executor's message-scan anchors are gone (last edit/bash forgotten) → task_complete accepted blindly | **FIXED** (via #1: the run-persistent serials are the intended compression-proof backstop and work again) |
| 6 | 4 hint-escalation branches replaced `_dresult` without appending the tool message → dangling tool_call (API 400 risk) | **FIXED** |
| 7 | `_tc_blocked` never True ("build_status" never in handler output) → ladder's blocked-aware acceptance dead | **FIXED** — matches "Build: blocked" |
| 8 | TC-DE-NAG short-circuits the 3-attempt ladder; ladder counter warm across chunks | OPEN (documented; behavior intentional after #7 fix — re-evaluate if blocks feel unfair) |
| 9 | call_sigs loop detection blind to period ≥ 4 cycles (ABCD…) — bounded by round budget except `until_finished` (budget 999999) | OPEN |
| 10 | STUB-ECHO false positive: the guard phrase occurs in this repo's own source | OPEN |
| 11 | 6-failure stop: counter only resets on successful write/patch; message says "across different error types" (inaccurate); model never gets the suggested round | OPEN |
| 12 | `[auto-diff]` tool messages appended without tool_call_id/name | OPEN |
| 13 | AUTO-TEST result appended uncapped (est-token inflation before real usage) | OPEN |
| 14 | LRU-evict + read-guard SKIP trap: re-read after eviction said "content is in your context" (false) | **FIXED** — eviction drops the path from the read-guard set |
| 15 | patch_file fallback hint fired for every failing tool (attempts_per_file[""]) | **FIXED** — patch_file/edit_file only |
| 16 | dead fuzzy-resolve branch in write guard (`_resolved_path != raw_path` always false) | OPEN |

## Execution / external tools

| # | Finding | Status |
|---|---------|--------|
| 17 | run_python swallowed exit codes when stdout non-empty | **FIXED** — appends `[exit code: N]` as last line |
| 18 | run_python timeout: no post-kill wait (zombie transport noise) | OPEN |
| 19 | Timeout discards captured partial output | OPEN |
| 20 | Child env = full server env incl. HIVEMIND_* (accepted for a local coding agent) | ACCEPTED |
| 21 | Windows `/c/`→`C:\` rewrite corrupts single-letter-host URLs (`https://e/x`) | OPEN |
| 22 | touch/rm intercepts lack the semicolon guard mkdir has | OPEN |
| 23 | rm reports success even when removal failed (`ignore_errors=True`) | OPEN |
| 24 | Output caps fight: exec caps at 6000 (tail), executor at 8000 (head) | OPEN |
| 25 | keep-alive touches manager `_slots` privately in run_bash builds | OPEN |
| 26 | background tools: start in server CWD (not workspace); orphans on restart without sandbox; dead entries never reaped; no per-run handle ownership | OPEN (documented; sandbox path is safe on Windows) |
| 27 | test_runner timeout killed only the shell (orphaned pytest/npm children) | **FIXED** — tree kill (taskkill /T on Windows, killpg fallback posix) |
| 28 | test_runner "unknown language" failure parsing false-positive prone | OPEN |
| 29 | browser: loopback/private IPs navigable (llama-server, SearXNG, UI) | **FIXED** — only the tool's own file-server origin allowed; "localhost" hostname blocked |
| 30 | browser route-guard registration swallowed by bare except (also a lint-baseline delta) | OPEN |
| 31 | fileserve: no auth, serves whole workspace incl. dotfiles (loopback-only, ephemeral port) | ACCEPTED |
| 32 | browser screenshots land in server CWD | OPEN |
| 33 | install_package pip could install into HiveMind's own venv | **FIXED** — requires workspace .venv/venv, else clear error |
| 34 | `pkg@^1.2.3` rejected (`^` not in _PKG_RE); `dev` arg truthiness ("false" → --save-dev) | OPEN |
| 35 | web_fetch SSRF via DNS name resolving to loopback/private (guard checks the literal hostname only) | OPEN (web tool has read-only fetch; browser is hardened) |
| 36 | web_fetch: no download size cap before truncation | OPEN |
| 37 | websearch budget under-count in parallel prefetch batches | OPEN |
| 38 | parallel readonly prefetch has no per-tool timeout (sequential path has 30s) | OPEN |
| 39 | 3 files exceeded their silent-except lint baseline (browser route-guard, websearch check_status, misc task_complete parse) | OPEN (narrow or baseline-update at next lint pass) |

## File ops / guards

| # | Finding | Status |
|---|---------|--------|
| 40 | workspace lock is a no-op when `workspace_lock=None` — critic_verify dispatched without it | **FIXED** — critic passes `_ws_str` |
| 41 | CRLF preservation dead code: `read_text()` universal newlines made `_has_crlf` always False → CRLF files silently rewritten as LF (edit_file/patch_file/replace_lines/edit_ast) | **FIXED** — `newline=""` reads on the 3 write paths + ast_tools |
| 42 | write_file_append: remainder >250k chars silently truncated, drain reported "full content written" | **FIXED** — capped flag + honest `AUTO_SPLIT_REMAINDER_CAPPED` error |
| 43 | empty content destroyed a pending AUTO-SPLIT remainder (pop before empty-check) | **FIXED** — remainder survives; error suggests the marker |
| 44a | write_file_append glued the first appended line onto a non-newline-terminated file end (looked like "append added only one line") | **FIXED** — newline boundary inserted + honest line count (2026-09-15) |
| 44b | a self-made model continuation chunk silently destroyed a pending AUTO-SPLIT remainder → truncated file, only the chunk landed | **FIXED** — AUTO_SPLIT_PENDING rejection keeps the remainder; marker drain unaffected (2026-09-15) |
| 44 | duo_full write-guard covers `write_file` only — write_file_append bypasses READ_REQUIRED on existing files | OPEN (append semantics make blind-guard less harmful; documented) |
| 45 | undo_last (no path) = git reset to last checkpoint — wipes ALL chunks' work, can revert to a PREVIOUS run's checkpoint; schema text doesn't say so | OPEN (documented risk; git_autocommit users unaffected) |
| 46 | undo snapshots via read_text/write_text corrupt line endings / non-UTF8 files on restore | OPEN |
| 47 | undo_last git checkpoint/reset is repo-wide, not workspace-scoped | OPEN |
| 48 | edit_ast invisible to undo/diff/read-guard (not in capture_before, guard sets, written-set) | OPEN |
| 49 | `.env` not in `_PROTECTED_PATHS`; git hooks writable in-workspace | OPEN |
| 50 | find_files: `..` pattern lists outside workspace (read-only disclosure) | OPEN |
| 51 | read_file: huge single-line file fully read before 32k cap; inverted ranges return empty silently | OPEN |
| 52 | replace_lines: `int()` parse errors swallowed into generic failure; bounds message off-by-one (len+1 accepted) | OPEN |
| 53 | `write_json_atomic` without fsync (torn write on power loss) | OPEN |
| 54 | junction check false-positives on pnpm-style node_modules junctions | OPEN |
| 55 | `patch_file` dispatched but never advertised (dead hint ladder); `get_file_summary` dead reference; critic-inject prompt text still says "patch_file" (duo_runner) | OPEN |
| 56 | `allow_overwrite` arg: read by guard but not in schema; `__allow_overwrite__` mapped to nothing | OPEN |
| 57 | HALLUCINATION_GUARD advice contradicts FILE_TOO_LARGE guard for big files | OPEN |

## Verified OK (audited, no bug)

- Run-persistent refs (`tc_consecutive`, `at_nosuite_nudged`, ladder trio): all consumers index `[0]`; no shadowing.
- `_cap_tool_result` preserves head; read-set dedupe runs pre-cap.
- `_PARALLEL_SAFE` set is genuinely read-only.
- edit_file multi-occurrence policy: reject, not first-match; fuzzy ambiguity guarded.
- replace_lines bounds math correct (incl. append-at-EOF via len+1).
- write_json_atomic rename is same-volume atomic.
- Browser path traversal neutralized by stdlib `translate_path`; file:// confined to workspace.
- Background tool Windows tree-kill chain solid.
