## [Unreleased]

Maintainability sprint on top of 1.5.5, plus targeted routing and runtime
fixes found by live tests and the 2026-11-07 deep audit.

Routing: a persisted agentic mode no longer hijacks short read/list prompts
into Code Duo (`is_simple_tool_prompt`, coding verbs veto); explicit
`agentic_force` and explicit modes still win. The legacy tool route shares
the direct-tools prompt rules, uses `direct_tools_max_rounds` (1..300) and
nudges one identical repeated tool call, so a read-only task ends with an
answer instead of `max_tool_rounds`.

Tools: the coder now defaults to `duo_core` (17 read/edit/run/test tools +
LSP diagnostics, ~2.8k schema instead of ~5k); `duo_coder_tool_tier=full`
opts into git/web/MCP/browser/LSP extras. Direct gains a `research` tier
(web + read/search, no write/exec) and defaults to `readonly`. Writes now
warm the language server in the background (`lsp_auto_warm_on_write`) and
attach diagnostics autonomously; when the LSP is unavailable, the existing
pyright/per-language lint block stays as the fallback, and when the LSP ran
it replaces that block. The 3 s wait is bounded and failures stay silent.

Runtime: slot eviction kills off the event loop (`_evict_lru_async`), the
Windows process-count probe runs in a thread, and `tzdata` moved from the
dev group into the shipped dependencies so named timezones work on a fresh
install. Tool-loop requests set `cache_prompt`, and the direct prompt tells
the model to answer without another tool call when it is done (fewer
re-sent rounds).

Ask-user: the card no longer runs its own 30 s auto-answer timer; the engine
governor is the single source, so one question can never receive two
autonomous answers. The card checkbox still persists the global setting.

Direct run control: `/pause` now works on plain direct streams (A1) — the
runner gates at every safe boundary (before a model round, at token chunks,
where the suspended upstream backpressures llama-server), `/pause-state/
{chat_id}` becomes active and `/resume` continues; an abort during the pause
ends the run as `aborted`. Mid-run steering also reaches pure direct runs
(A2): a steer queued before the answer rides into the prompt, one queued
during the answer gets a bounded continuation round (assistant answer +
`[OWNER STEER]` message, max 2 continuations) so the model sees and answers
it; steers arriving after the last boundary still deliver after the run.
The UI shows plain "Run paused/resumed" dividers for direct runs (no chunk
counts).

Frontend: all `localStorage` access goes through `_hmStorageGet`/
`_hmStorageSet`. Browsers that block cookies/storage (Firefox throws
`SecurityError: The operation is insecure`) no longer get a frontend-error
toast on every load; the panel-width and perf-bar preferences simply fall
back to their defaults.

Benchmark: new `bench/` D1 harness (raw / HiveMind direct / HiveMind duo /
aider) with a 21-task starter corpus and hidden, tamper-proof tests. It is
developer-only and export-ignored. First full core-tier comparison
(2026-11-09, one run/cell): core coder and Aider both pass 19/21; coder
54.3 s / 66,089 tokens per cell vs. Aider 22.1 s / 970. The core tier costs
~39 % fewer tokens than the 2026-11-08 full-tool run (108,378/cell) with the
same 19-20/21 quality band. The detailed run notes stay local, outside the
repository. A later three-way run added
`duo_lean` (10 tools): it tied at 19/21 but was slower (52.7 s vs. 35.8 s
median) and saved only 3.1 % net tokens, so `duo_core` stays the coder
baseline. The clean rerun after the GPU/VRAM reload: **HiveMind 20/21
(95.2 %), Aider 18/21 (85.7 %), median 27.7 s vs. 20.9 s, net 76,652 vs.
20,350 tokens** (Aider-only 0, HiveMind-only 2, McNemar p=0.5).

External anchors: `bench/tasks_polyglot` imports a fixed 10-task Python
subset of the Aider/Exercism polyglot benchmark with hidden tests (all
validated red-before/green-after). `bench/external/deepswe_qwen36.sh` and
`deepswe_qwen36_colab.ipynb` run a 5-task DeepSWE subset against the
official Qwen3.6-35B-A3B UD-Q4_K_XL with HiveMind's sharp template; the
profile now uses that template too.

Live verification: `tests/live_smoke.py` now passes Direct and Duo end to end
(Duo writes `hello.py`, runs the check, calls `task_complete`; 36.8 s). The
8k-context guard was fixed (no duplicated system prompt, calibrated 1.05
factor, context-aware minimum budget); `--ctx 8192` now passes in 29.2 s
without compression storms. The smoke default remains 16k.

Coder efficiency: tiny top-level workspaces (≤4 text files, context-scaled
cap) are pre-seeded into the first coder message, so round 0 can edit instead
of `read_file`. When the pre-seed produces nothing (deep/multi-file repos),
a bounded repo-index walk ranks nested files against the task text and the
first coder message gains a compact signature-hint block (path + size + top
symbols, never full contents; empty/no-match queries add nothing and the
≤4-file pre-seed bytes are unchanged) — `duo_deep_retrieval` disables it;
`WOULD_DROP_SYMBOLS` now includes the original definition line
(`Keep definition: MODE = "prod"`), which targets the `multi_config` retry
loop. MoE prefill keeps the full `--threads-batch` while decode keeps the
physical-core `--threads` split. Covered by 368/368 regression suites.

Audit batch: B3/B8 behavior coverage was added for `pipeline_runner`,
`chat_run`, `llama_start_phases`, `_pre_explore` and `manager_load` (the
pipeline suite found and fixed an abort-before-round-1 `UnboundLocalError`).
C4 adds `deploy/bump_version.py` (version.py + server anchor + pyproject,
`--dry-run`/`--check`); C5 adds an opt-in integration matrix for Telegram,
STT/TTS, vision, SearXNG, MCP and send_file. D1 adds relative-gain/net-token
reporting; D2 adds the incremental repo index + query-selected preseed plus
deep retrieval (bounded nested walk ranking signature hints into the coder
prompt; `duo_deep_retrieval*` / `duo_index_max_*` knobs); D3 adds batch
multi-edit (`edits=[...]`, one atomic write); D4 scopes `run_tests` through
the testmap (`duo_selective_tests`); D5's Job-Object sandbox was already
wired; D6 adds chat-scoped outcome hints (status only); D7 adds
`/git/diff`, checkpoint listing/restore and the panel; D8 adds the Claude
Code/Codex adapter scaffold. Residual: the D2 eval A/B backing
"fewer tokens at same or better success", D3 multi-file dependency
ordering, hosted C3 cron and real C5 credentials.

Dead code and single sources of truth: `_plan_state` slot transport,
`_dump_for_test`, 11 unused preflight re-exports, the `core/duo` package
re-exports and the `_pipeline_chat_stream` server shim are gone; the version
regex lives in `core/version.py` (updater + update_check import it); the
`server.py` compatibility anchor keeps the ≤1.5.5 updater able to read the
version for the next upgrade.

Structure: `duo_runner` shed the pre-loop model/VRAM/evict block
(`core/duo_coder_preloop.py`, -571 lines) and the task-complete verify gate
(`core/duo_task_complete_gate.py`, -190); `chat_run` replaced 13 nonlocals
with `RunTokenLedger`/`PrefetchCoordinator` and moved the inline tool route
to `core/chat_tools_route.py`; the `dir()`/`locals()` probe class is gone.
A post-split audit (2026-11-08) restored Guard-C telemetry
(`slot_ctx_floor_stop` now crosses the loop-guard seam) and the AUTO-STOP
drop diagnostics in the epilogue carry, removed dead round-input carry
reads, and extended `test_duo_split_integrity` to reject any `ps.carry` key
a helper reads without its call site passing it. Both deferred back-import
cycles are gone: `_history_budget_tokens` lives in `core/llm_stream.py`
and `duo_finalize`/`duo_finalgate` import resume/project-state helpers from
their canonical modules instead of `duo_runner`. Eleven verified dead
top-level names and two dead split-state fields were removed. The same
audit removed AST-verified dead stores from `run_code_duo`, extracted its
55-line TOOL-CALL-MISS diagnostic into a behavior-tested pure logging
helper, folded the byte-identical emergency-evict block into one bounded
two-pass loop, and merged the duplicated empty-answer branches — 3,604 →
3,492 lines without a new state object. A second pass removed the
remaining dead local unpacks in the split helpers and replaced the
0.90/0.70/0.62 eviction magic numbers with named `ctx_guard` constants.
The malformed/truncated tool-call policy now lives in
`_plan_dropped_tool_retry` (small result object, 4 behavior checks) instead
of 50 inline lines in the no-tool-call branch. The text-only/empty-answer
ladder moved to `_plan_text_only_round` (soft check, summary acceptance,
zero-activity nudge, empty retry; 6 behavior checks). Both helpers and
their small result objects now live in `core/duo_policies.py`; `duo_runner`
shrinks by another ~130 lines while `run_code_duo` keeps only the calls and
loop control. The grace-round gate and the shared post-chunk auto-test
policy moved to `core/duo_round_gate.py` (22 behavior checks); the runner
retains only `_di`/mark/break bookkeeping. The round-finish rollback/override
status policy moved to `core/duo_finish_round.py` (20 behavior checks), and
the repeated advance trio is one runner-local closure. The 111-line
PlanTracker round block moved to `core/duo_plan_round.py` (12 behavior
checks, including a fixed stall-counter f-string). The CTX-FULL tool-budget
ladder (4x stop / storm-guarded force compression / tiny-budget fallback)
moved into the same gate module. Truncated-write salvage moved to
`core/truncated_write_salvage.py` (6 behavior checks). The ctx-meter/SWA
round moved to `core/duo_ctx_meter.py` (8 checks). Explore-only detection + write reset
moved to `core/duo_explore_gate.py` (6 checks). The `[WRITE-TRUNCATION]`
diagnostic moved into `core/duo_policies.py` (behavior-tested). Seven state
dataclasses moved next to their helper modules (epilogue, round-input,
inloop-planner, dtool-setup, compress-cycle, finalgate, finalize). The
coder-POST wait/result seam moved to `_duo_coder_post_wait`; the evict/mask
decision moved to `core/duo_ctx_meter.py`; the DEBUG message-signature
diagnostics moved to `core/duo_helpers.py`. `duo_runner.py` is now 5,001
lines and `run_code_duo` 2,936 (from 5,934 at 1.5.5).

Runtime hardening: AgenticToolLoop now reuses the F3 5xx policy (500/502/504
max. 3 attempts, context-overflow bodies never retried; 408/429/503 keep the
long retry), and `ToolLoop` intersects phase tools with the mode tier when
`tool_mode` is set (phase-only behavior unchanged). LSP auto-diagnostics and
warm-on-write now verifiably honor the per-run settings snapshot, with the
missing-snapshot fallback test fixed. The UI sends `direct_tools_tier`,
`duo_coder_tool_tier` and `lsp_auto_diagnostics` with the request, so a
just-changed dropdown cannot race the async settings write.

Live smoke: `tests/live_smoke.py` runs a Direct answer and a Duo tool-layer
file write against a running engine, with the tool-round/budget pins needed
for small contexts. Direct passes; Duo is blocked on this box by ComfyUI
holding ~7 GB VRAM (manager correctly refuses the coder load).

The same audit caught two split regressions: the C2 preloop constructor no
longer received `duo_coder_sys`, so the helper rebuilt the coder prompt
from `""` and overwrote the base/dynamic/image/follow-up hints; and the
final-cleanup seam never synced `_project_state_run_counted`, so
`ProjectState.total_runs` was incremented twice per duo run. Both are fixed
and pinned by `test_duo_split_integrity`. Dead `_plan_tracker` and
`session_out` state fields were removed.

Efficiency: tool-call sanitizer scans only new messages (two reset points),
the prefill meter reads a 64 KB tail instead of the whole slot log, the
long-run diagnostics cache the newest slot-log path for 30 s, `/vram/log`
runs off the event loop, and the critic reads at most 2000 chars per written
file.

CI and quality: Python 3.14 in both workflows, `windows-full` runs
`--ci-subset --jobs 2` with an exclusive lane for socket/LSP suites,
checkout-safe guards in the five environment-dependent suites, ruff target
3.14, behavior suites for updater/vision-pre/start-phases, and a pyright
baseline lowered from 191 to 1 (0 NEW).

## [1.5.5] - 2026-11-05

Maintainability release on top of 1.5.4. The model registry audit fixes are
in: models.json is merged instead of rewritten (manual registrations survive
a scan), a single `invalidate_caches()` call drops every model cache,
`force_refresh` really rebuilds the index, the picker dedup prefers the
manifest/profile name over the longest parsed alias, ghost override keys
whose file is gone are no longer listed, quant-alias collisions fall back to
numbered aliases instead of dropping a file, `save_model_mapping` is atomic
and refuses llama.cpp slot ids, and `add_model` stores absolute paths.

Repo maintainability: `DEFAULT_SETTINGS` moved to `settings_defaults.py`
(pure data, no I/O; settings.py re-exports it), the regression runner runs
suites in parallel (6 workers — the full gate dropped from ~18 minutes to
under 2 minutes; `--jobs 1` restores the old behavior), and the settings-docs
generator is split-aware. Dead code removed from `fetch_models.py`
(`_sha256_of_file`, `ESSENTIAL_SKIP_KEYS`) and the stray `.orig` files are
gone.

## [1.5.4] - 2026-11-05

Automations: scheduled agent runs are now owned by the engine, not the
Telegram gateway. Each automation stores a prompt, a schedule (`every`, `once`
or `daily`), mode, tools tier, workspace and an overlap policy. It fires as a
normal engine run into its own `⏰ <name>` chat, so the transcript, run journal
and UI behave like an interactive run. A single scheduler lane never starts a
scheduled run while another run is live: `skip` is the default, `queue` holds
at most one pending fire per job, and a queued fire runs when the lane frees.
The UI has an Automations tab with create/edit/run-now/pause/delete and recent
run history. Automap is removed from the mode selector (old `mode=automap`
presets map to `auto`; engine support stays for compatibility).
An audit pass hardened the scheduler: fires target the actually bound
port, a job edited during a long run is not corrupted, zombie one-shot
jobs are swept, `PUT` validates like `POST`, shutdown aborts an
in-flight automation run, and the editor uses the standard dark form
controls.

Chat auto-save is serialized, so two saves can no longer create two chats
before the first one has an id. "Retry in new chat" is hidden after a
first-turn failure, where it only produced duplicate one-message history
entries. The Windows LSP install step refreshes the machine/user PATH before
its self-test, so a freshly installed clangd is detected instead of reported
as missing.

This release also includes the re-published 1.5.3 fixes: Python tools use the
engine or workspace interpreter instead of a bare `python` from PATH,
`run_bash`/`run_tests`/background jobs get the venv Python on PATH, the
destructive gate only treats absolute-path writes as destructive (read-only
file inspection no longer asks), and the Windows installer/updater persist
uv's `.local\bin` on the user PATH.

Update discovery and installer visibility: a cached "you are current" result
now expires after 4 hours instead of 24, so a release published after a
machine's daily check is offered on the same day (an "update available"
verdict keeps its 24-hour no-re-nag window). An offline machine says so in
one line — either "check skipped (offline)" or a stale-cache notice — instead
of silently skipping. The installer and updater print an `[OK]` line after
every step, so a stalled step is visible.

Coder prompts pin the platform on Windows (paths look like
`C:/Users/...`, `/Users/...` is macOS), point at the preinstalled browser
tool instead of Playwright installs, and tell the model to validate JS with
`node --check` rather than Python's `compile()`. `install_package` refuses
Playwright installs with the same pointer instead of running a doomed
download.

## [1.5.3] - 2026-11-04

Hardening release on top of 1.5.2. Telegram documents are binary-safe again:
secret redaction decodes known text types (UTF-8, UTF-16 and legacy
single-byte) and leaves binary payloads untouched, so PNG, PDF, DOCX and ZIP
bytes reach the phone unchanged. Own phone runs poll the pending
approval source, so approval cards appear while the engine is paused;
ask_user questions are relayed as questions with free-text answers; `/stop`
suppresses late cards and a late duplicate staged approval frame is ignored;
an expired engine-side pause clears the phone latch instead of swallowing the
next message, and a gateway restart leaves an ask question to its timeout
instead of answering the literal "3". A non-2xx abort is reported as a failure
instead of "Abort sent"; and `/purge` parses the engine body instead of
formatting an httpx.Response. Phone runs adopted as a mirror after a gateway
restart can deliver `send_file` to the phone too, and
a gateway restart keeps the phone chat unless
`HIVEMIND_GATEWAY_FRESH_CHAT_ON_RESTART=1` is set.

Documents cover plain text (UTF-8, UTF-16 and cp1252) alongside PDFs and use
`document_limit_chars` instead of the 4096-character message cap. Images sent
in a chat re-attach in every mode, and reattached context images are no longer
duplicated into `.hive_uploads`. `send_file` accepts files written by
`edit_file`, `patch_file`, `replace_lines`, `edit_ast`, `run_python` and
`run_bash`, maps equivalent path spellings to one canonical workspace path,
and a resumed chunked run restores its explicit send queue. The in-loop
planner's output is distilled before it reaches the coder, so planner markdown
and reasoning stay in the planner and UI. Agentic runs that only change static
assets (HTML, CSS, Markdown, SVG) no longer force the workspace test suite;
unrelated pre-existing test failures used to derail `task_complete` into an
edit/test loop. The `run_tests` tool itself is skipped for those runs before
an approval card appears, and the agentic prompt no longer asks for a test
run on static assets. If the loop guard still stops such a run, the explicitly
queued file is delivered instead of being dropped silently.

`install.bat` and `update.bat` sync with `uv sync --inexact`, keeping optional
decider and voice packages across updates, and update no longer installs the
dev group into the user venv. `pypdf` is a declared core dependency in
`pyproject.toml` and `uv.lock`. The Linux installer now resolves the repo root
from `deploy/`, installs uv into the directory it checks, passes `--inexact`,
protects the uv-managed `.local` runtime from re-sync, and installs
`python3-venv` for the pip fallback. The Linux push gate installs `pypdf` and
runs the gateway, document, image, send-file, planner-briefing and resume
suites.

Python tools now use the engine (or workspace) interpreter instead of a bare
`python` from PATH: on uv-only installs `run_python` no longer reports a
missing Python, and `run_bash`/`run_tests`/background jobs get the venv
Python on PATH. The Windows installer and updater add uv's `.local\bin` to
the user PATH once. The destructive gate only treats absolute-path opens in
write/append mode as destructive, so read-only file inspection no longer asks
for confirmation.

## [1.5.2] - 2026-11-04

Telegram file delivery and agent polish on top of 1.5.1. `/send <path>`
delivers a workspace file immediately; agentic phone runs queue `send_file`
and deliver only after final verification, with workspace confinement,
secret-name blocking and the 50 MB Telegram limit. A completed run with one
deliverable file sends it automatically even if the model forgot the tool.
Direct mode with full tools has the same file path: it writes, queues with
`send_file`, and delivers after the direct tool loop. Direct phone runs select
the UI tiers with `/tools off|websearch|python|full`, per phone run, without
changing the desktop setting; `full` stays workspace-locked and still needs
`/restrict off` for write/shell access. Phone runs now pass the chat workspace
in the run body, so `/workspace` actually wins over the desktop workspace.
Images sent in a direct chat stay in context: the next text-only turn
re-attaches the last image set. The image context is wiped when HiveMind
shuts down unless the persistent-image-uploads toggle is on; deleting the
chat always removes it.

The coder no longer receives planner markdown or planner reasoning, only a
distilled numbered execution list; with the planner off it receives only the
user prompt. The planner prompt is less absolute. The LSP panel lists
installed and missing language servers, Python has exactly one linter
(pyright, with a `py_compile` environment fallback), and the near-duplicate
web-search guard allows eight rephrased searches instead of three.

Installer and updater both install optional Python packages through uv
instead of assuming the venv has pip (uv-created venvs often do not); when uv
is missing, install.bat provisions it. The decider entry names its real
footprint (~2.5 GB) and prints the exact retry command on failure. A
model-only decider install is detected as missing the Python package.

The Linux `types` CI job no longer reports the optional Windows-only
`keyring` import as missing.

## [1.5.1] - 2026-11-03

Security and reliability release on top of 1.5.0. MCP file/search tools now
enforce the secret denylist on every path (including the forwarded engine
path), `/settings` no longer persists GET-only keys, preset exports are
scrubbed of tokens, and the approval gate fails closed without a run id.
The `/v1/chat/completions` agent mode requires `HIVEMIND_INTERNAL_TOKEN`,
read-tier MCP git/search tools cannot write files or execute option-injected
commands, presets can no longer flip the approval/phone posture or add MCP
servers, and model-chosen commands get a scrubbed environment (no engine or
API tokens). The served UI (`index.html`, `static/`), `sessions/` and engine
scripts are write-protected for file tools, the markdown renderer escapes
quotes, and the saved-chat sanitizer rejects entity-encoded script URLs.
The gateway restores its full state on restart (cron jobs, `/gate off`, chat
pins), adopts an orphaned live run as a mirror instead of wedging the phone,
and the idle monitor now runs. Chat ids up to 64 chars load correctly,
oversize saves report 413 instead of ok, SSE errors terminate the response,
and git/chat/VRAM/gateway-stop probes no longer block the event loop.
`run_tests` and dynamic MCP tools are covered by the approval set, and the
user's approval toggle is authoritative: tainted runs are marked, but no card
is forced while approvals are off. `/abort` during an approval pause actually
stops the run instead of reporting success. The repo-map delta survives the
compression it is appended for, the tail/decode guards keep counting across
rounds, and a queue-full drop at the end of a stream still delivers a gap
marker plus the done frame. Follow-up runs keep their last task/plan in the
chat sidecar even when the planner was skipped or the run was aborted, so
reopening an old chat and asking about it starts from the previous run
instead of blind. The
taint from web/MCP content survives prefetched tools, chat-id and gateway
stop/start races are fixed, and dropped SSE frames are marked as an incomplete
answer instead of being persisted silently. `write_file` preserves CRLF,
downloads verify the Hugging Face LFS SHA256, and git undo/consolidate no
longer lose untracked files or deletions.

New MoE expert cache (`moe_cache_mib`, `--moe-cache-mib`, opt-in) and a VRAM
fit toggle (`llama_fit`, `--fit`/`--fit-target`, on by default), both
capability-checked and part of the VRAM pre-flight; a failed cache load retries once without the flag.
The expert-cache surcharge now also survives the grace and post-reclaim
rechecks (it could previously be evicted into an overflow crash), an explicit
layer override wins over the model registry (the CPU fallback stays on the
CPU), the CPU backend no longer evicts peers for a VRAM budget that does not
apply, and ROCm devices are probed by their own device/library names instead
of being rejected as a missing Vulkan build.
MoE loads use half the logical CPUs. LSP now verifies 13 languages end to end
(the settings card shows installed vs missing servers on PATH; installer packs
for Python, TS/JS, web/YAML, Bash, Rust and C/C++; Go and Lua
are manual installs), runs servers with a scrubbed
environment and no repo-controlled settings by default.

Memory retrieval now uses SQLite FTS5 (bm25, porter stemming, prefix match)
instead of the 96-bin hash vectors, with the old cosine path as the fallback
when a sqlite build lacks FTS5. Soul skill distillation is off by default. The
README lists what leaves the machine and when, and the gateway guide states
that bot traffic is not end-to-end encrypted. `force_kill_all` kills only this
install's own servers (name-wide sweep behind `?all=true`), Linux VRAM is read
from sysfs for AMD/Intel, and the voice setup can log in to Hugging Face
itself.

## [1.5.0] - 2026-11-01

1.5.0 scope: MCP hardening, settings/preset improvements, the generic LSP
client, the code-panel/compression/update-check improvements, and
installer/updater rendering, git commit scoping and reprompt reliability
fixes on top of 1.4.14. Groot stays **experimental**. The
startup update check offers new releases directly (including pre-releases
for preview installs).

### MCP (both directions)
- **Exec and mutating tools over HTTP need the shared secret.**
  `run_bash`, `shell`, `run_python`, `start_background`, `install_package`,
  `run_tests`, `browser` and the mutating class (`write_file`, `edit_file`,
  `write_file_append`, `edit_ast`, `replace_lines`, `patch_file`,
  `git_commit`, all `mcp_*` names) are refused by `/internal/tool/exec`
  without `HIVEMIND_INTERNAL_TOKEN`; read-only tools stay available. The
  mutating set is imported from the runner's approval set, not copied.
- The SSE client raises on an id mismatch instead of returning a foreign
  message; `close_all()` takes the lifecycle lock (the refresh error path
  calls the unlocked body); HTTP close sends `DELETE` with `Mcp-Session-Id`.
- The local `search_code` fallback is argv-only (a quote in the pattern could
  inject commands); the advertised `shell` forwards as `run_bash` in the
  shared path only, after the tier check.
- `docs/mcp.md` / `docs/mcp_client.md` state the token-gated set, the real
  retry statuses (502/503/504) and that `has_auth` appears on both endpoints.
- **34 MCP fixes.** Client: streaming SSE with a
  body cap, 404 session recovery, JSON-id check, off-loop taskkill, stdio/URL
  redaction, config-decay warnings, string-id match, multi-line SSE,
  call/refresh guard, structuredContent + truncation marker, cmd quoting.
  Server: budget seeding for the IDE path, timeout log line,
  `notifications/cancelled` + stdio timeout, correct error codes, id-less
  notifications without a response, non-dict batches, narrow `isError`,
  log-name sanitizer, 2 MB stdio cap, body-read timeout + connection cap,
  version negotiation. Integration: `mcp_*` call budget, documented mode-less
  exception, destructive gate scans MCP args.
- **Protocol 2025-06-18**: `initialize` negotiates
  2024-11-05 / 2025-03-26 / 2025-06-18 (anything else falls back to the
  2024 baseline). 2025 HTTP sessions get a `Mcp-Session-Id`
  (`secrets.token_urlsafe(32)`), required on later POSTs (unknown/missing →
  404 with a JSON-RPC error body), 1h idle TTL (`MCP_SESSION_TTL_S`),
  64-session cap (`MCP_SESSION_MAX`), idempotent `DELETE /mcp`. `GET /mcp`
  opens the per-session SSE stream with a keep-alive heartbeat
  (`MCP_SSE_KEEPALIVE_S`), `Last-Event-ID` resumability over a 500-event
  ring and live fan-out of progress notifications. stdio needs no session.
  Sampling/elicitation/completions are unsupported (-32601).
- **Per-server client budgets + IDE configs**:
  `mcp_server_budgets` {server: calls per rolling hour} is enforced in the
  client's call path (`MCP_BUDGET_EXCEEDED` names server, limit and reset;
  reloads keep counters); remote tool `annotations` pass through into the
  host schema. `python tools/mcp_config_gen.py` generates ready-to-paste
  stdio configs for Claude Desktop / Cline / Zed / VS Code with a
  `<HIVEMIND_INTERNAL_TOKEN>` placeholder unless `--include-secret`.
- **Protocol 2026-07-28 minimal**: `server/discover` answers
  identity + versions + capabilities on both transports; per-request
  `_meta` protocol version is validated (unsupported → `-32022`); a 2026
  initialize echoes stateless with no session; batched initializes of any
  version are answered on the 2024 baseline. The 2026 revision removed
  sessions; support stays minimal (`resultType`, header enforcement,
  `ttlMs`/`cacheScope`, MRTR are not implemented yet).
- **Tool annotations and new tools**: all 23 tools carry MCP annotations
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`).
  New tools: `git_diff` (read, bounded), `git_commit` (write tier, explicit
  paths only, no `git add -A`), `run_tests` (exec tier, stdio-only, bounded
  output) and `model_status` (read, loads nothing). The server also speaks
  `resources/list`/`read`/`templates/list` (workspace-contained, text-only,
  secret denylist, 256 KB cap) and `prompts/list`/`get`
  (`review_diff`, `explain_file`, `fix_tests`); protocol stays 2024-11-05.

### Language servers (LSP)
- One generic LSP client (`infra/lsp_client.py`) with a broad auto-detected
  registry (pyright, typescript-language-server, gopls, rust-analyzer, clangd,
  bash/lua/json/yaml/html/css servers). No per-language on/off flags:
  `lsp_enabled` is the only switch, `lsp_servers` an optional command override.
- Tools: `lsp_status`, `lsp_diagnostics`, `lsp_definition`, `lsp_references`,
  `lsp_hover`, `lsp_symbols`, `lsp_workspace_symbols`, `lsp_code_actions`
  (titles only) and approval-gated `lsp_rename` (returns a summarized plan,
  writes nothing). A missing server answers `LSP_UNAVAILABLE` with an install
  hint; a server starts lazily on the first tool call.
- Diagnostics are read-tier; rename is mutating and gated. `lsp_status` and
  `lsp_diagnostics` are tier-1 in the schema fitter (small-ctx runs keep them);
  with `lsp_enabled=false` every `lsp_*` schema is removed instead of being
  offered and then answering `LSP_DISABLED`.
- **Agentic runs get diagnostics after writes**: when a language server is
  already warm, a successful write/edit appends a compact
  `[LSP auto-diagnostics …]` block (8-line cap); a cold server is never started
  by a write (only an explicit LSP call starts one). `lsp_auto_diagnostics` turns
  it off. A cold explicit `lsp_diagnostics` now waits up to 5s for the first
  publication (`lsp_diagnostics_wait_s=5`) and only then says "still
  analyzing" instead of falsely reporting a clean file.
- **Lifecycle fixes**: engine shutdown closes every
  cached LSP manager (previously pyright/gopls/rust-analyzer survived the
  engine), and manager-cache eviction closes the evicted servers instead of
  dropping them. Pyright's percent-encoded, lower-case drive URIs
  (`file:///c%3A/...`) are canonicalized before diagnostics are stored, so
  real pyright results reach the tool layer.
- **LSP fixes**: diagnostics carry the document version; a delayed
  publication for the pre-change text is discarded instead of satisfying the
  wait. `.tsx`/`.jsx` send `typescriptreact`/`javascriptreact` in `didOpen`
  while still using the TypeScript server. `lsp_rename` now includes the
  1-based edit lines in its plan. A real-pyright smoke suite checks the
  client end to end and skips cleanly when pyright is not installed.
- **Pull diagnostics + rust-analyzer**: `textDocument/diagnostic` is used when
  the server advertises `diagnosticProvider`. rust-analyzer sends the native
  type diagnostic, a version-less empty clear, and later a cargo-check result;
  the client keeps the fresh non-empty finding instead of returning the clear.
  A real-rust smoke suite covers it (skips without rust-analyzer).
- `docs/lsp.md` documents all ten tools, arguments, tiers, errors, the server
  registry and the known limits; the README now carries the tool table.
- **Idle reaper + workspace settings + lsp_format**: a
  language server is closed after 600s idle (`lsp_idle_timeout_s`, 0 = off)
  and restarted lazily. `.vscode/settings.json` is answered to
  `workspace/configuration` (per section) and pushed once via
  `workspace/didChangeConfiguration` — a workspace can finally configure
  rust-analyzer/pyright. The new approval-gated `lsp_format` applies the
  server's `textDocument/formatting` edits atomically, and every write fires
  a `didChangeWatchedFiles` hint for workspace indexes.
- **[MEM] commit-charge snapshots**: one line per startup, model load,
  eviction and run end — `commit=x/yGB engine=…MB llama[…]=…MB tts=…MB
  lsp[…]=…MB` (PrivateUsage via GetProcessMemoryInfo, PIDs from HiveMind's
  own managers; `mem_watch_enabled` switches it off). Commit growth across
  loads is now visible per process.

### Smart Preload: keep-warm pins
- **Keep workers warm after run now pins**: the reloaded pre-explore/planner
  workers get `keep_alive=-1` (the same primitive as "Pin direct") so
  prefetch and unrelated loads cannot evict them between runs. The next run
  start runs `unpin_all()`, so a stale pin never blocks a fresh load.
- The SMART PRELOAD rows carry visible explanations now (what prefetch
  does, why it is off by default, what keep-warm+pinned means).

### Duo core, Git, MCP, UI and installer fixes
- **Duo core**: the coder-load timeout retry ladder skipped the abort
  check (an aborted run burned ~10 min of load/kill churn) — abort is now
  checked at entry and after each retry wait. Whole-turn compression could
  join two user messages (`user,user` — strict-alternation templates reject
  that); the remainder is appended one-by-one with a guard now.
- **Git**: the checkpoint-squash fold commit was still BARE (foreign staged
  work folded) — now pathspec-scoped; `files=None` no longer keeps
  `add -A` (a squash without a list is refused); the empty-fold
  fallback is a safe mixed `git reset` instead of `--hard`; the
  `git_commit` tool rejects `.`/`..` paths.
- **MCP**: two spec-consistency notes are documented: a `_meta`-carried
  2025 version without a session header is served stateless;
  `Mcp-Protocol-Version` header values are not validated.
- **UI/installer**: the release-notes box is drawn inside the renderer (the
  update.bat pipe discarded the body — only the header printed);
  `warnings_digest` survives a second call and flags the plural
  "Warnings" now (word-boundary + German "keine" suppression);
  `style.css` cache buster bumped (the preset-badge CSS was stale).

### Installer / Updater
- **Release notes render properly**: the old inline render double-
  mangled UTF-8 (ANSI read → OEM `type` — every umlaut became `├ñ`),
  never wrapped 200-char lines (box 63 chars), misaligned the header box
  for every tag and erased code fences. `deploy/render_release_notes.ps1`
  now does UTF-8 read, markdown-light conversion, code-block markers,
  61-col wrapping and prints the box with a computed width.
- **Decider opt-in prompt never showed**: `choice /d N` without `/t`
  errored (errorlevel 255) on every install — the question never appeared.
  `/t 30 /d N` now, and the "ready" message gates on the torch/gliner2
  import probe AND the model download instead of only the snapshot exit.
- The startup update check prints a warnings digest (word-boundary regex,
  "Keine breaking changes" no longer triggers the scary prompt) and deletes
  the downloaded zip after a successful install.
- Startup check: 24h cache, pre-release policy, sha256 gate, silent when
  offline.

### Git
- **Auto-commit commits new files again**: the scoped
  commit (`git commit -- paths`) matches tracked paths only, so a run's
  brand-new files failed with "pathspec did not match". A scoped
  `git add -- <run files>` now precedes the scoped commit — foreign
  already-staged work is still never swept.
- **Checkpoint squash folds only run files**: the soft reset folds
  everything since the base into the index; the following `git add -A`
  swept foreign uncommitted work into the folded commit. The squash takes
  the run's file list now.
- **`git_commit` tool requires explicit paths**: the model-callable
  tool did `git add -A` + a bare commit, sweeping the whole index (the
  approval gate is off by default). `paths` is now required and the commit
  is scoped to them.
- **`undo_last` is recoverable**: the ungated `git reset --hard`
  destroyed all uncommitted work since the checkpoint. A `git stash create`
  safety snapshot is taken first and its recover sha is in the result.
- No security regression: argv-list subprocesses, URL vetting, masked
  token flows, no leak end to end.

### Code panel, startup update check, compression
- **Code panel performance**: streamed plain text now appends to one text node
  instead of rebuilding every line every 120 ms; finished file/diff HTML is
  cached per tab; paints coalesce per animation frame; files over 200k chars
  (or 6000 lines) fall back to plain text; diff rendering caps at 3000 lines
  and 400 chars per line.
- **Startup update check**: `start_hivemind.bat` asks GitHub for newer
  releases (pre-releases only for preview installs by default), caches the
  answer for 24h, prints one `[Y/n]` notice and installs an accepted ZIP
  through `update.bat --zip` after verifying GitHub's sha256 digest.
  `HIVEMIND_NO_UPDATE_CHECK=1` disables it; a failed check never blocks
  startup.
- **Whole-message compression**: when the summary plus the kept raw tail (or
  the rule-based result) is still above the compression threshold, the
  compressor drops the oldest whole tail turns, never splitting an assistant
  `tool_calls` message from its tool results, keeps the newest four messages,
  marks the drop and releases the read guard for dropped read paths. The
  rule-based fallback also accepts `duo_compress_tail_tool_max_chars` now
  (the caller already passed it; the parameter was missing).

### Settings, presets, UI, docs
- Missing defaults are persisted: `duo_compress_tail_tool_max_chars`,
  `duo_planner_enabled`, `duo_coder_always_vision`, the telegram mirror/
  approval/phone keys. `telegram_gateway_enabled` is deliberately not
  defaulted (absent = the gateway.toml switch decides; present and false is a
  veto).
- Save path: fsync (tolerated failure), temp cleanup, deepcopy at the async
  call sites, POST /settings applies the llama startup flags through the same
  helper as a preset load, presets migrate `llama_ubatch_size` to
  `duo_ubatch` and skip keys that no longer exist, and the bookkeeping keys
  are stripped on load.
- UI: the tool-round select can show the shipped 50; `.opt-name` rows and
  key-name entries are searchable; the VRAM gate jumps to the agents panel;
  the branch fields write `git_default_branch`.
- `docs/settings.md` regenerated (254 keys); the generator lifts block
  comments and skips banners. Groot's profile loses the dead `ubatch` and
  `side_call_sampling` keys and uses its native GGUF template.

### Maintainability
- 17 settings keys with no production reader are dropped on load (old UI and
  preset exports no longer round-trip them); preset apply skips unknown keys.
- `RUNTIME_FLAG_KEYS` is exported by `backend/runtime_flags` and imported by
  POST /settings, so the key list cannot drift from the helper.
- The three silent swallows are gone: POST /settings logs an ImportError, the
  VRAM profile merge logs, and the deadline cancel abandons the tool task
  without awaiting it (an outer cancellation is no longer swallowed).
- `_INTERNAL_TOKEN_GATED_TOOLS` names what it gates (exec + mutating); the dead
  `shell` clause in the shared dispatch and the stale `batch` note are gone.
- `docs/architecture.md` lists the new log markers; the settings generator's
  notes for the voice/telegram keys are restored.
- `tools/sync_to_installs.py` (dev-only, gitignored) keeps a manifest and
  prunes files it synced before that are no longer tracked.

Tests: 233/233 suites, ruff clean.

## [1.4.14] - 2026-10-31

### Tool loop
- `duo_tool_rounds` ships **50** (was 0). A fresh install could not write a
  file: `_tool_round_base` requires `> 0`, so agentic chunk runs attached no
  tools and produced text-only "implementations" (7/7 rounds, empty
  workspace). An explicit 0 still disables the loop.
- **Truncated `edit_file` no longer retries itself.** `write_file` already
  salvaged partial content with a marker; an `edit_file` cut at the output
  limit is dropped (a half edit would corrupt the file) and now forces a
  compression instead of retrying with the same clamped budget (a
  2404-token budget against a 20 455-token prompt, deterministic truncation).
- `min_viable_tool_budget`: a tool round must keep `max(1024, min(4096,
  request//2))` output tokens; the fixed 1024 floor let the 2404 budget ship.
- `write_file`/`edit_file` shrink guards are proportional: >=30 content lines
  and <=25 % now blocks without `confirm_shrink` (11 616 -> 1 716 bytes,
  ~40 -> 9 lines passed the old absolute limits).
- `[TOOL-CALL-MISS]` WARNING per failed tool round: thinking flag +
  chat_template_kwargs, tools count/schema/hash, tool_choice, slot/requested
  ctx, prompt/cached tokens, finish_reason, completion/max_tokens, all sampler
  values, and 500 chars of content, reasoning and raw SSE.

### Deadline
- The run deadline is enforced **inside** a tool round: the heartbeat cancels
  the executor (and the server-side generation). A cancelled round reports
  `timeout` in its own stop-reason bucket.
- A successful write grants at most **one** +300 s grace extension per run
  (auto-verify shares the ledger; the wedge handoff keeps its documented
  exemption); the unbounded extension let a 900 s run's coder phase reach
  1064 s.

### MoE sizing
- `vram_of()` uses the stored MoE split (`active_gpu_gb` + `n_cpu_moe`) for
  every table row and profile: hermes/qwen3.6 21.0 -> 4.66 GiB, minicpm
  21.0 -> 4.64, lfm2.5:24b 14.4 -> 5.91, mellum 7.2 -> 4.0.
  `_enforce_vram_budget` classifies by `moe_cfg_for` instead of name
  substrings.
- `vram_of_with_ctx` delegates all MoE models to `vram_of_moe`; the Groot
  profile carries `vram_gb_override 4.6` and `kv_bytes_per_token 6336`.
- `duo_ubatch` ships 256 (measured opt-in 512/1024); preset loads use the same
  resolver through one `apply_runtime_flags()` helper, so an explicit
  `duo_ubatch` survives a preset load and garbage ints cannot 500 the apply.

### Models and installer
- LFM3 1.7B QAD replaces the retired LFM2.5-2.6B line (direct/subagent roles,
  fallback ladders, `safe_profile_matrix`, docs).
  `BINARY_MIN_BUILD` gates `lfm3`/`lfm3-omni` at b12001.
- The installer no longer offers a dedicated judge-model download. The Judge
  role accepts any installed LLM; the optional CPU decider stays as the only
  routing extra. `setup_models.bat`/`fetch_models.py` now describe the
  essential set as core models plus the optional omni vision worker.
- Groot uses its native GGUF chat template (the Sharp v22.5 override is
  gone); b12001 parses the native XML tool calls with thinking off
  and on.
- Downloader: HTTP 416 with fewer bytes on disk than expected is no longer
  "Already complete" - the partial file is removed and the download retried.

### Hygiene
- Log-once for profile MoE resolutions; `_binary_help` no longer blocks the
  event loop (thread) and does not cache failures; slot-log telemetry reads a
  40 KB tail from a `__file__`-anchored path; `save_presets` uses a PID temp
  name + retry; the settings lock is an `RLock`; the Telegram start note shows
  the pinned chat mode; `/vram/table` includes profile-sized MoEs.

Tests: 232/232 suites, ruff clean.

## [1.4.13] - 2026-10-30

## Stability

- Preflight is **fail-closed**: an unknown model (alias or path that does
  not resolve) and a failing `tasklist` now BLOCK the guard instead of
  passing it. `gguf_size_gb() == 0` and `llama_process_count() == -1` are
  the two block signals; both were fail-open before (a bench with an
  unknown model could start with only the bare 8 GB headroom check, and a
  broken tasklist reported "0 processes").
- `langrun_load.py` aborts on `llama_process_count() <= 0` (was `== 0`, so
  an unknown count -1 passed and the load run started against an unknown
  engine state).
- **Context-compression storm guard**: the CTX-FULL path fired a force
  compression on every round when the tool-output inflow kept `est` above
  the SWA zone (40 compressions in 56 s, `after` stuck at
  4411). `context.ctx_guard.allow_force_compress` throttles to one force
  compression per 3 rounds; the rounds in between fall back to the
  existing tiny-output-budget clamp. Regression test replays the storm
  (3 FORCE in 8 rounds instead of 8).
  **The brake never blocks a hard limit**: when the guard is above 90 % of
  ctx or two rounds in a row got no viable output budget, the compression
  fires regardless (`emergency=True`). Without that, a throttled round would
  send `max_tokens 512` against `head <= 0`, the server would truncate or
  answer 400 and the tool-call history would be poisoned - exactly the case
  the MIN-VIABLE-GUARD exists for. Four regression checks cover it.
- Log retention: slot logs beyond the newest 10 per port are MOVED to
  `logs/archive/` - nothing is deleted before an archive copy exists, and
  an archive collision gets a numeric suffix instead of overwriting.

## Side calls

- **Empty-content policy**:
  models that ignore `chat_template_kwargs.enable_thinking` (measured:
  LFM2.5 2.6B, 800/800 tokens in the think block, `content = ""`) made every
  side call receive an empty string. `LlamaClient.chat()` and
  `core/fix_agent.py` now **log `[SIDE-SKIP]` and return ""** - reasoning is
  never substituted, because titles, memory entries and insights persist what
  they receive.
- Groot profile: `ncmoe 32`, `ub 1024`, `mtp true` (measured, see Models).

## Pre-flight and side calls

- The VRAM pre-flight now adds a **compute-buffer surcharge for the ubatch**
  (0.28 MiB per ubatch unit above 256, measured on the live server: Groot
  dedicated 5188 MiB at ub 512 vs 5333 at 1024). The load-time warning for a
  large ubatch fires above **1024** instead of 512.
- **Empty side-call content is skipped, not substituted**: a model that
  ignores `enable_thinking` (LFM2.5 2.6B) returns `content = ""` with only a
  think block. `LlamaClient.chat()` and `core/fix_agent.py` log
  `[SIDE-SKIP]` and return "" - reasoning text is never persisted as a
  title, memory entry or insight. The compression path has its own local
  fallback summary for an empty answer (previously it only fired on an
  exception, so an empty summary block reached the coder).
- Final Groot profile measured end to end (live server, b12001, ncmoe 32,
  ub 1024, MTP nmax2, ctx 131072): 10 tool-call rounds at **31.65 t/s mean**
  (min 25.00, max 33.91), 10/10 tool calls parsed, dedicated peak 6079 MiB,
  shared peak 1874 MiB, no crash.

## Engine build

- Default llama.cpp build is now **b12001** (auto-discovery picks the highest
  build number under `llama/`; b11889 stays installed). `--help` diff
  b11889 -> b12001: none of the flags used by the loader was removed or
  renamed, the `--spec-type` value list is identical, and b12001 is the first
  installed build that knows arch `lfm3` (PR #31252).
  Measured A/B (warm-up discarded, `-r 5`, ub 512, `-p 2048 -n 0` and
  `-p 0 -n 128`, medians with stddev):

  | model | build | tg128 | pp2048 |
  |---|---|---|---|
  | Hermes 35B (ncmoe 35) | b11889 | 17.30 +- 0.12 | 134.1 +- 2.2 |
  | Hermes 35B (ncmoe 35) | b12001 | 17.29 +- 0.13 | 133.8 +- 2.7 |
  | Groot 19B (ncmoe 28) | b11889 | 15.17 +- 0.03 | 168.6 +- 2.1 |
  | Groot 19B (ncmoe 28) | b12001 | 15.16 +- 0.06 | 169.6 +- 1.3 |

  Both stay inside the 3 % rule, so b12001 became the default. A 10-round
  mini run on Groot with b12001 held 14.84 t/s (min 14.28, max 15.20).

## Models

- **Groot 19B-A3B-261023** (`groot:19b-a3b-261023`, Q4_K_M, 11,657,618,560 B)
  added through the normal mechanism - profile
  `model_configs/models/groot_19b-a3b-261023.json` + `models.json` alias.
  Header: `qwen35moe`, 41 blocks, **128 experts (8 used)**, ctx 262144,
  `nextn_predict_layers=1` (MTP **on**: tools +60.7 %, prose -4.9 %), KV
  `2 x 256` per layer (576 B/token with q4_0). Expert stacks 9.33 GiB,
  216-249 MiB per block; `ncmoe 32` (real-server sweep 24/28/32, shared judged as delta to the 470 MiB idle baseline: 32 is the fastest AND has the smallest delta).
  Profile: ctx 131072, KV q4_0, ub 1024, mmap+mlock, jinja, thinking on.
  Sampler per model card (thinking temp 1.0/presence 1.5, code temp 0.6/
  presence 0.0). Hermes stays the default and is unchanged.
- **Downloader**: LFM2.5 2.6B is replaced by **LFM3-Omni 2B** (with mmproj)
  and the text-only **LFM3 1.7B QAD**. This works from b12001 on - b11889
  answered "unknown model architecture: 'lfm3'". Measured on b12001:
  LFM3-1.7B-QAD tg128 152.15 +- 0.27 t/s, pp2048 1477 +- 1.2 t/s (1 GB,
  fully on the GPU).
- **Side calls get an explicit sampler**: summaries, titles and memory calls
  now send temp 0.7, top_p 0.8, top_k 20, min_p 0, presence_penalty 1.5 and
  `chat_template_kwargs.enable_thinking=false` instead of inheriting the
  server defaults (top_k 40 / min_p 0.05). Every such call logs
  `[SAMPLER] kind=side ...` with the values that actually go on the wire.
## Prefill throughput

- New setting **`duo_ubatch`** (default **1024**; `llama_ubatch_size` stays as
  the older alias). `--batch-size` follows the ubatch and never drops below
  1024, so `ubatch <= batch` always holds. Prefill is 89-94 % of wall time
  on an 80k run, so this is the knob with the largest effect on
  total run time. Measured on the 35B MoE (b11889, pp 8192, fresh process
  per variant): ub 256 -> 77.64 t/s, **ub 512 -> 119.96 t/s** (shared 206
  MiB), ub 1024 -> aborted (shared 80 -> 340 MiB), ub 2048 -> 193.59 t/s
  but shared peak 2348 MiB (spills to system RAM). 512 is the fastest value
  that stays off the shared-memory path. **Against the idle baseline
  (~470 MiB shared):** on the real server
  at 18 011 prompt tokens, ub 512 -> 1024 gives 91.7 -> 113.6 t/s
  (+23.9 %) for +52 MiB of
  extra shared memory, so 1024 is the default. 2048 stays out.

## Compression cost

- New setting `duo_compress_tail_tool_max_chars` (default 8000) caps each
  tool output kept in the compression tail. Offline measurement on a
  61.7k-token list (local-only mode, no model): uncapped after-size
  ~22.2k tokens, at 8000 chars ~12.6k, at 16000 ~18.6k. The kept tail was
  the after-size driver (12-18 raw messages, single read_file dumps reach
  20k+ chars).
  **It keeps the END of a tool output, not the head**: test/build errors,
  tracebacks and diffs stand at the end, so the head is dropped with a
  visible marker (`[... first N of M chars dropped ...]`). `tool_call_id`
  pairing and message order are verified offline.

## Diagnostics (passive, opt-in)

- One `[PERF] kind=main|side ... p_tok= cache_n= prompt_n= prompt_ms=
  pred_n= tps=` line per llama request (`utils/perf_log.py`), wired into
  the coder main loop and into `backend/llama_client` (side calls:
  summaries, titles, memory). Missing fields print `-`, never `0`.
- `[DECODE-DROP]` warning when a round decodes below 50 % of the median of
  the previous rounds after >= 5 rounds - **warning only, no restart**
  (a restart would destroy the evidence).
- The memory/GPU sampler (`HiveMind_dev/memwatch.py`) is opt-in via
  `HiveMind_system_sampler=1`.

## Documentation

- README: the Vulkan speed note now states the measured numbers -
  Spark-X2.6 ~5.6 t/s vs Qwen3.5-4B-MTP ~55 t/s.

## Speed diagnosis and load safety

- The 35B coder loads with `load_mode: none` now: llama-bench tg128 measured
  2.92 t/s with mmap+mlock vs 13.77 t/s with none (15-16 t/s in real runs),
  because
  the expert pages page in from NVMe under RAM pressure. The profile-level
  `load_mode` override bypasses the SPEC+NONE guard - measured
  at 14.6-17.7 t/s without OOM when the slot is exclusive.
- A RAM-GUARD in the loader rejects `load_mode: none` when free RAM is
  unknown or below GGUF size + 8 GB (three stacked 18.3 GB hermes
  commits froze a machine at 56 GB commit) and falls back to plain mmap.
- The load-mode decision lives in one pure function
  (`backend/manager_load._resolve_load_mode`), unit-tested. It is
  evaluated **before** any profile override: a speculative model
  (`mtp: true`) resolves to `mmap+mlock` even if its profile says
  `none`, so the 35B profiles run on mmap+mlock unless
  `HIVEMIND_MTP_SPEC=0` is set.
- MiniCPM-V 4.7 is vision-capable again (projector wired, bench and
  real-run verified).
- Spark-X2.6 on Vulkan runs ~6 t/s - prefer Qwen3.5-4B-MTP for coding.

## More fixes in 1.4.13

- Presets: a load applies the llama startup flags immediately, warns about
  models missing locally, re-saving keeps custom prompts, the autoload
  guard survives daylight-saving stamp compares.
- MCP client: servers that never started no longer show as running,
  handshake error statuses fail loudly, `GET /mcp/servers` redacts
  env/header values.
- A trailing `-ud` in a model name reaches the KV family table again.
- One context resolver (chat, load, duo) instead of three copies.

## [1.4.12] - 2026-10-28

## New coder model in the installer

- The Hermes entry is now **Hermes3.6 Genesis APEX-Mini** from the author's
  Genesis-Hermes repository: 20.2 GB weights, the same projector, and the chat
  template the author ships with it is downloaded next to the weights and used
  for the model. Same architecture as before (MoE 35B with 3B active, MTP head,
  vision, 262k trained context), so MTP, CPU expert offloading and the VRAM
  estimate carry over unchanged.
- The old tag keeps its profile, so presets and installs that already have the
  larger file keep working. New installs get the Mini.

## MCP client: auth, version negotiation, retries

- HTTP server entries can authenticate: `headers` are sent verbatim, and
  `token_env` names an environment variable whose value goes out as
  `Authorization: Bearer ...`. A `token_env` whose variable is missing or
  empty fails the start with the variable name in the message - the server
  never starts unauthenticated by accident. An explicit `Authorization`
  header wins over `token_env`. Tokens never appear in logs; the reload
  response reports `has_auth`, never the value.
- HTTP 401/403 answer as `[TOOL_ERROR: MCP_AUTH_FAILED]` and keep the
  connection alive: a wrong token is a config mistake, not a dead server,
  so a fixed token works on the next call.
- Protocol versions are negotiated. The version a server accepted is stored
  and re-offered after a reload; a server that rejects the first offer gets
  exactly one retry with the version its error advertised. Newer version
  strings are accepted.
- HTTP requests retry connect errors, read timeouts and 502/503/504 with
  0.5/1.5/3.0 s backoff (±20 % jitter, capped by the call timeout).
  `tools/call` is the exception: only connect errors are retried there,
  because a read timeout means the tool may already have executed. A timed
  out tool call sends `notifications/cancelled`.

## Model profiles and the load log

- A model profile's `kv_cache_type` applies to dense models now (profile >
  family table > global), mirroring the MoE path - a profiled q8_0 no longer
  silently ran the global q4_0.
- `[VRAM-LOAD]` logs where the context came from (`ctx_source`:
  request/settings/profile/table), so a wrong context is traceable instead
  of guessed.

## UI

- `until_finished` defaults to on: a coding run finishes the job instead of
  stopping at the round limit.
- The context stats explain themselves: every perf field carries a tooltip.
- The model filter accepts several search terms: "qwen mtp" finds
  `qwen3.5:4b-mtp` now, before it only matched the whole query string.

## Long runs stay diagnosable

- Every coder round logs its own numbers now: generation time, decode rate,
  prompt tokens actually processed and the free VRAM afterwards. A run that
  slows down shows whether the model was busy or something else was.
- The read timeout takes the context into account. On an 8 GB card prefill runs
  at roughly 90-130 tokens per second, so a 38k prompt costs several minutes;
  that used to end as "timeout" while the server was still working.
- `llama_ctx_checkpoints` (default 4) and `llama_cache_ram_mib` (default 1024)
  are settings now instead of fixed flags. Both bound what llama.cpp keeps per
  slot; `0` disables them.

## Fixed

- The coder slot's in-use guard never ran: an async lookup was called without
  awaiting it, the error was swallowed, and the guard stayed off since it was
  added. A slot could therefore be evicted in the middle of a round.
- A load that only just fits now reports how much VRAM is left afterwards and
  warns that decoding will slow down as the context grows.
- The installer's judge download never ran: the path carried a 0x0C byte where
  the `\f` of `deploy\fetch_models.py` should have been. Fixed.
- The judge model and the routing decider are optional steps inside installer
  step 6 now, both off by default, both saying how to install them later;
  previously no installer step called the decider fetch at all.
- The release zip no longer ships a personal path inside
  `deploy/prepush_check.ps1` (the 1.4.10 and 1.4.11 releases carried it and
  were removed from GitHub).
- MiniCPM-V 4.7 is vision-capable again: the projector needs llama.cpp
  b11574+ and the shipped binary is newer, so the model profile carries
  `mmproj_filename` + vision again and the status line names the model that
  actually receives the image (it warns instead of lying when the direct
  model has no vision profile).
- A preset load now applies the llama startup flags it carries
  (`llama_cache_reuse`, `llama_cache_ram_mib`, `llama_ctx_checkpoints`,
  `llama_ubatch_size`, `llama_mlock`) instead of waiting for a restart, and
  warns about models the preset references that do not exist locally.
- One context resolver: the chat path (server.py) and the duo path
  (`num_ctx_config`) delegate to `core.state` instead of keeping their own
  copies of the settings > profile > table chain.
- MCP client fixes: a server whose start fails no longer shows as
  "ok" in `GET /mcp/servers` and every reload retries it; error statuses on
  the handshake confirmation fail the start instead of being waved through;
  a changed `token_env` applies after a reload; `close_all` forgets dead
  servers; `GET /mcp/servers` no longer ships env values or literal
  Authorization headers to the browser.
- `moe_kv_cache_types`/`dense_kv_cache_type`: a trailing `-ud` suffix is
  stripped once and reaches the family table - before, names containing
  `-ud` mid-string pulled a foreign profile and `-ud` names never hit the
  table.
- `update.bat` ignored `server_port` in settings.json (read without JSON
  parsing), so its running-instance guard always checked port 8001.
- Re-saving a preset destroyed its saved custom prompts; a re-save keeps
  them now and only a new preset starts from the base prompts.
- Applying a preset live-syncs `vram_preflight_gate`, bumps the settings
  revision against stale tabs, and no longer freezes bookkeeping keys into
  the snapshot. The preset auto-load guard parses ISO stamps, so a
  daylight-saving boundary can no longer revert newer user edits.

## [1.4.11] - 2026-10-27

## MCP client (beta) grows up

- A server entry can use `url` instead of `command` now: Streamable HTTP next to
  stdio. Answers may come as JSON or as an SSE stream, a `Mcp-Session-Id` from
  the server is carried on every call, and nothing is started, so there is no
  process to kill. Both transports share the tool prefix, the name cap, the mode
  gate and the approval gate.
- New card in the config page: enable, the server list as JSON, Save, Reload and
  a live status line. Until now the beta could only be configured by editing
  `settings.json`.
- The tool list follows `nextCursor`, so a paginating server no longer loses
  every tool after the first page.
- `mcp_reload` is a real tool: the model can restart the servers itself when a
  call reports a dead one. It uses the configuration the client was started
  with.
- Documentation says what the server process actually sees: proxy tools work in
  the engine's workspace, local ones in `HIVEMIND_WORKSPACE`.

## Settings changes no longer get lost

- Two writers that each changed a key used to lose one of them: `save_settings`
  is atomic per write, but the read-modify-write around it was not (measured:
  `a=111` and `b=222` ended as `a=0`, `b=222`). All changes go through
  `mutate_settings()` now, which holds the lock across read, change and write.
- The temp file carries the process id and one retry, so a vanished temp file
  cannot fail a save.

## Bash without leftovers

- The operator list was incomplete: a single `&` still created files named `&`,
  `echo` and `done`, and a newline silently dropped the second command. The
  guard covers `&`, `|`, `;`, `>`, `<`, backtick, `$(`, newline, CR and `^` now,
  and the shims report the tool name like their siblings.

## Model sizing without table edits

- A MoE model that is not in the VRAM table is estimated from its own profile:
  `moe_cpu_experts` plus `vram_gb_override`, with an optional
  `kv_bytes_per_token` for the KV cost. A local model with a profile needs no
  repo edit; before, a missing table line fell back to the dense estimate and
  the pre-flight refused the load.
- Two bugs on the way: an entry had been merged into another model's dict
  (valid Python, so nothing complained and that model was unreachable), and the
  MoE path raised `KeyError` for any entry without KV fields.

## Installer

- `--no-drafters` is an explicit opt-out that names its price; `--essential`
  does not skip drafters any more (they are in use since speculative decoding is
  back on).
- Model downloads resume with a Range request and check the final size instead
  of starting an 18 GB download from zero.

## Reliability

- 21 `asyncio.create_task` calls without a strong reference go through
  `utils/bg_tasks.spawn`: the task cannot be garbage-collected mid-flight and a
  failure is logged instead of lost.
- `_atomic_write` keeps the target file's POSIX mode (the 0o600 temp file
  stripped the executable bit from every rewritten script).
- The git helpers, the two PowerShell calls in the settings endpoint and the
  model downloads have timeouts; a held `.git/index.lock` can no longer hang a
  run step.
- `sync_to_installs` takes its file list from `git ls-files`, so a scratch file
  in the working tree no longer lands in the install.
- The regression runner fails when a registered suite is missing instead of
  skipping it silently; a UI test no longer asserts on the live install's
  presets.

## [1.4.10] - 2026-10-27

## Chunking plans what you asked for

- A request like "create greet.py and main.py" no longer becomes a project
  scaffold. Steps that only set up a shell (README, src/, requirements.txt,
  .gitignore, Dockerfile, ...) are dropped unless the request mentions them or
  asks for a project. When nothing but scaffolding was planned and the request
  names files, the plan is built from those files instead.
- The plan ends where "Completed Tasks" begins: finished work used to come back
  as extra chunks, so the coder re-ran what it had just done.
- The "Chunking is off" notice reads the run's own flag now. It used to read
  the saved setting, so a run started with chunking on announced that its plan
  was skipped while the chunks ran.
- A chunk is marked done after its test result, not before: a failing test
  re-processes the chunk instead of recording it as complete.
- The last chunk needs the critic's approval like every other chunk. It used to
  pass unreviewed, and it is the one carrying the integration work.
- `duo_chunking` and `duo_pre_explore` ship off: both are opt-in, and both work
  when switched on.

## Bash without junk files

- `run_bash` no longer creates files named after shell operators. The Windows
  shims for `touch`, `rm` and `mkdir` split their arguments on whitespace, so
  `touch a.py && echo done` also "touched" `&&`, `echo` and `done`. A command
  that contains a shell operator is refused with an explanation now: one
  command per call, `write_file` for files.

## MCP client (beta)

- HiveMind can use **external MCP servers** as tools, the other direction of the
  existing MCP server. Off by default: `mcp_client_enabled` plus a
  `mcp_servers` list of `{name, command, args, env, cwd, enabled}`.
- Tools appear as `mcp_<server>_<tool>` (capped at 64 characters) with the
  server's `inputSchema` passed through. A schema over 8 kB is replaced by an
  empty one instead of blowing up the request.
- `GET /mcp/servers` shows what is configured, running and discovered;
  `POST /mcp/reload` starts the servers and rebuilds the tool list. Servers
  start once at boot and every child is closed on shutdown.
- A server that hangs, crashes or prints garbage never breaks a run: the call
  returns a tool error and the connection is dead until the next reload. One
  child process per server, the whole tree is killed on a timeout, and an entry
  that leaves the config is closed. stdio only, 8 servers, 64 tools each.
- MCP tools follow the mode gate (write modes only), pass the approval gate,
  and stay blocked for phone runs.

## Coder speed: speculative decoding is back on

- The hermes 35B coder runs with draft-mtp again. Measured on b11889 with
  256-token answers: decode 20.0 -> 31.05 t/s (+55 %), prefill 122.1 -> 120.15
  (-1.6 %), acceptance 0.823.
- Reference measurement for `qwen3.5:4b-mtp`: decode 55.5 -> 85.35 t/s
  (+53.8 %), prefill 610 -> 568 (-6.9 %), acceptance 0.911.
- The trade is named: with speculative decoding the greedy output is not
  bit-identical to plain decoding (near-tie tokens flip). That is the price
  for the speed. `HIVEMIND_MTP_SPEC=0`
  restores byte-identical runs.

## Installer

- The judge/decider model is an optional step, asked like Voice: skipping it
  leaves the decider on its keyword fallback until you install it later.
- Playwright installs through the venv interpreter with the real error text and
  the manual retry spelled out; HiveMind runs without it, only the browser tool
  needs it.
- Setup offers an essential model set that leaves out the judge/decider helper.
- `--no-drafters` skips the drafter sidecars, explicitly only, and warns that
  it costs roughly half the decode speed for the models that need one (Gemma-4
  MTP, the DSpark pair). `--essential` does NOT skip them: the drafters are in
  use.

## Load and context

- `POST /vram/load` accepts `num_ctx` (or `ctx`) and reports the value it used
  plus where it came from: request, profile or the server default.
- The context card states the measured speed picture where you set it.

## Diagnostics

- A failed write under Desktop, Documents, Pictures or a OneDrive folder now
  names the real cause: Controlled Folder Access and OneDrive locking, with the
  way out. Every other folder keeps the plain message.

## Repository

- `deploy/promote_main.bat` moves `main` to the released state:
  fast-forward only, never a merge.
- `deploy/prepush_check.ps1` runs the 13 pre-push checks from the repo.
- README no longer pins a release number or a zip name.

## [1.4.9] - 2026-10-26

### Git integration

- **Test connection names the problem**: `missing_or_invisible`,
  `token_invalid`, `no_access`, `unreachable` or `not_github`, each with the
  matching next step. A repo that does not exist and a token that cannot see a
  private repo used to collapse into the same 404, and a rate-limited check
  read as a permission problem.
- **Create repo creates the remote first**: an empty workspace with a remote
  that does not exist yet no longer dead-ends in the clone step. New repos are
  private by default.
- **Existing-repo heal**: a local repo without a remote gets one created and
  `origin` attached instead of "already exists".
- **The panel acts on the project it shows**: validate, init, stash, reset,
  branches and config follow the workspace field (the loaded chat's project).
  Before, the panel showed one project and the buttons touched the default one.
- **Agent tab line**: branch and remote state with a Configure link; a
  successful Create repo refreshes it.
- **Clone into an empty workspace answered 500** after a successful clone.
  Fixed, and a failed clone no longer leaves a half-written `.git` behind.

### Scheduled jobs

- **A daily job fired once and never again**: `/cron at 09:00` recomputed its
  next slot from the wall clock, so it stayed on the same day whenever the
  slot had already passed. It now advances from the slot that fired.

### Speech

- **Cold start no longer fails**: a request that arrived during the first
  dependency install saw "process alive", skipped the health check and errored
  after about 8 s while the install takes about 40 s. The fast path requires
  confirmed health now.
- **Honest idle hint**: the Voice line states the real cold start (up to about
  a minute, later requests are instant).
- **Three failed starts no longer wedge TTS** until a restart; the failure
  counter decays after 10 minutes without a new failure.

### Models and context

- **`POST /vram/load` accepts `num_ctx` (or `ctx`)** and reports the value it
  used plus where it came from (request, profile, default). Before, a start was
  sized only from the table and settings, so a profile's context never reached
  llama-server.
- **Settings that the UI derived on a page render are no longer written back**,
  so a preset saved from the page keeps the values you set.
- **A `settings.json` changed on disk** since the engine read it now logs a
  warning with both states instead of silently overwriting.

### Hardening

- **Every `/git` endpoint rejects a workspace** that is not a directory on this
  machine, so reset, checkout and stash cannot fall back to the default repo.
- **`/chats` and `/skills` drop a dead workspace** instead of storing it, and
  chat autosave keeps the chat's own project.
- **`git init` rejects clone URLs** that git would read as an option or an
  `ext::` transport, and refuses to initialise inside the install directory
  when no workspace is set (that would put `settings.json` with the git token
  under version control).
- **Tool counter cap**: the per-run counter no longer grows without bound.

## [1.4.8] - 2026-10-25

### The EDIT→TEST→FIX loop, densified

- **Per-chunk test feedback ships ON** (`duo_test_feedback_chunk=True`): the
  chunk auto-test + self-fix loop was fully built but opt-in — most agentic
  runs finished without any test feedback. Both this and the final gate are
  on by default now.
- **Edit→test mapping** (`hive_functions/testmap.py`, pure stdlib): the auto
  test runs the test files that cover what was written instead of always the
  full suite. Name conventions (pytest/jest layouts, tests/ placement) plus a
  bounded python import probe; no match → full suite. The completion veto's
  auto-verify uses the same mapping.
- **Auto-Verify on veto**: the completion veto no longer asks the model to
  run tests (small models often ignored that, and the retry slipped through
  unverified). The harness runs the suite itself: green → completion accepted
  as verified and the gate re-arms; red → concrete failures into the coder
  context; no test suite → the smoke-test nudge (nothing ran is not a
  failure).
- **Scoped test runs**: `run_tests(paths=...)` with `test_cmd_scoped`
  templates (python/js/ts) and a no-match fallback to the full command — a
  jest "no tests found" must not read as red. `python -m pytest` replaces the
  bare `pytest` call, which is not on PATH everywhere (every auto-test read
  as a false red on such machines).
- Verify-gate suite grew 9 → 16 checks; new testmap suite (15 checks).

### Frontend

- **Streaming performance**: four hotspots fixed — the speaker-button sync
  deep-cloned and laid out every chat bubble 4×/s (now: active bubble only,
  detached clone, no forced layout), live markdown re-parsed the whole answer
  every frame (now ~8 renders/s with a trailing paint), think blocks rewrote
  their full text per frame/event (now delta appends on a persistent text
  node), and the perf bar painted on every token event (now 4 Hz).
- **Run stats line**: a muted DeepSeek-style footer under the final answer —
  elapsed, tokens, cached, t/s, model on hover. The `done` event now names
  the model, and the direct path reports real usage (include_usage) instead
  of a chars/3 guess. The stats persist on the assistant message (structured
  stash on the footer — every autosave re-attaches them, not only the first),
  so a reloaded chat shows the same line.
- **Context-fit warnings**: every context input (all four duo ctx fields, the
  per-agent sliders, on load and on model change) shows the real VRAM
  estimate and marks it red over budget — a warning only, never a block; the
  run-start preflight still decides. Per-agent slots render one compact line,
  full detail on hover.
- **Models chip**: the header shows which model(s) the selected mode calls
  (coder/planner in duo, analyst+synthesizer in pipeline, direct otherwise) —
  one muted chip, full names on hover.

### Preset autoload guard

- The startup auto-load re-applied the active preset snapshot at EVERY
  engine boot and persisted it — silently reverting everything the user
  saved since the last preset load. A preset application now stamps
  `preset_applied_at == settings_saved_at` in one write; the boot skips
  re-applying when settings are newer. Old files without stamps keep the
  historical apply-always behavior.

### Engine/watchdog and measured fixes

- **Watchdog handshake**: the headless watchdog had no marker — it raced a
  manual `start_hivemind.bat` for port 8001 (winerror 10048). It now writes
  a heartbeat marker, supports `stop`, and `start_hivemind.bat` stops it in
  the kill path only (answering "N" keeps the watchdog protecting the kept
  instance).
- **GET /chats off the event loop**: per-chat disk-bytes glob+walk measured
  a 1.77 s loop stall on 480 chats — one `to_thread` for the whole map.
- **Gateway journal cursor**: the mirror re-downloaded the FULL frame window
  twice per 5 s tick; it now sends the cursor it always tracked, and the
  second-run peek fetches meta-only.
- **Blob store binary-safe + html blobified**: text-mode writes corrupted
  any payload containing literal CRLF (`\r\n` → `\n\n`, lossy for chats
  written since early October; found by the real-chat roundtrip suite);
  blobs are bytes now
  with a magic prefix, legacy files repaired on read (Windows). The rendered
  `html` — the biggest inline payload (1.1–2.7 MB per chat) — is blobified
  like cp/images.
- **Monotonic run/slot clocks**: run deadlines, `last_used` and `pinned_at`
  ran on wall clock (NTP jump = spurious run expiry / mass eviction). The
  idle monitor kills outside the manager lock (the kill held it ~10 s) with
  a freshness re-check, excludes mid-load slots, and stamps last_used at
  claim time. The pre-explore LLM client retries 5× instead of 30 and never
  retries a ReadTimeout (was: up to 30 × the read window).
- **Queue-aware AMD commit wait**: the 2–6 s Vulkan commit sleep ran after
  every load; it now fires only when a successor is actually pending
  (load-sequence bump / queued prefetch). The collision retry remains the
  net.
- **MoE KV cache profile override** (`kv_cache_type` profile key; default
  stays q4_0) and **`llama_gpu_layers_auto`** (omit `--n-gpu-layers` so
  llama.cpp fits layers to free VRAM itself; default off).
- **Whisper warm server wired**: `preload()` spawned `whisper-server` since
  1.3.11 but transcription always paid the CLI's model load. The warm path
  is used first now; any transport error falls back to the CLI.
- **Planner-handoff ctx pin**: planner==coder loads the planner slot at
  `max(planner_ctx, coder_ctx)` so the coder reuses the slot instead of a
  kill+reload (15–60 s + KV loss). The pin is announced; the graceful
  cascade keeps the worst case identical.
- **run_tests parallel with reads**: one run_tests in a read-only round runs
  concurrently with the reads (up to 90 s no longer serialized behind it);
  any stateful/write tool keeps the fully serial path.
- **duo e2e smoke**: `run_code_duo` is driven end-to-end on a scripted
  llama-server (regression coverage for the duo core).
- Completion-veto auto-verify hardening: the status
  events actually reach the stream, the inline verify run emits heartbeats
  and grants a deadline grace, and "no tests ran" (manifest but zero tests)
  is a smoke-test nudge, not a red suite.

### Hardening, simplification and measured tuning

- **Simplification of the grown subsystems** (behavior-preserving,
  180 suites green): dead retry branches in the llama client removed,
  the VRAM budget guard extracted from ensure_loaded, the
  duplicated /props adoption unified, six atomic-write closures collapsed
  into one.
- **Windows blocklist bypass closed**: the POSIX drive rewrite
  (`/c/users` → `C:/users`) could complete a dangerous pattern after the
  entry check — the rewritten command is re-checked now.
- **UX quick wins**: `lang="en"` + the last German failure strings, the
  auto-mode description names what actually routes (keyword rules vs
  decider), every non-green run-end reason carries a one-line actionable
  hint, opt-in browser autostart (env-gated, headless restarts never
  spawn windows).
- **Measured flag tuning** (see above): hermes coder without draft-mtp
  and with eager loading; `llama_mlock` actually works now; the
  registry-MTP env bypass closed; speculative models keep mmap (Vulkan
  OOM guard).
- MiniCPM-V-4_7: the DSpark drafter moved to the Q6_K build; the retired
  AD variant and old drafters removed from the shared model folder.

### Voice over Telegram

- A voice note now runs the full chain: transcription -> decider ->
  run. The from_voice flag was dead code since 1.4.6 (frozen-dataclass
  assignment silently failed) - spoken notes always ran as
  settings[mode] instead of letting the decider route them.
- to_wav16k defaulted to tmp_dir=None: every voice note 500ed at
  Path(None) before the audio was decoded. Defaults to %TEMP% now.

### Decider

- /decider <on|off|shadow> over Telegram (bare = status incl. model).
  OFF unloads the encoder model (~1.8 GB RAM) immediately via the new
  settings-react hook; ON loads it lazily at the next decision.
- DecisionCache survives engine restarts (context/decider_cache.json,
  7-day TTL, best-effort IO).
- German input: routing matchers carry German stems.

### Security

- External-bind guard: a non-loopback HIVEMIND_HOST bind requires
  HIVEMIND_INTERNAL_TOKEN from non-local clients (the API can write
  files and execute commands). Loopback unaffected.
- The risk of `duo_action_approval_enabled=False` is documented next to
  the setting.

### Updater / Installer

- Prune logic extracted to deploy/update_prune.ps1 (the inline
  PowerShell broke in cmd quoting - prune and manifest rewrite were
  silently skipped).
- Installer: Models step skippable; optional decider environment
  (gliner2 + torch CPU + transformers + model weights).
- frontend: parallel write_file streams get per-card pending tabs (the
  "stuck in generation" hang); dead voiceEnableDefaults /
  updateComposerToolStatus calls removed.


### Telegram

- `/status` shows the engine's active run, so a run started in the browser is
  visible on the phone.
- The phone sees WHAT the routing decider decided, not only that it ran.

## [1.4.7] - 2026-10-21

### Installer

- `install.bat` offers Microsoft Core Utils as a new optional step [4/9]. It
  detects an existing install (`where grep`), asks before installing and runs
  `winget install --id Microsoft.CoreUtils`. Every outcome (already there,
  winget missing, install failed, skipped) continues the install; nothing
  aborts. Without them the coder prompt points at the built-in tools, with them
  the model can use `grep`, `sed`, `awk`, `find` as it was trained.
- The remaining steps are renumbered to 9.

### Windows shell awareness

- The coder prompt now states whether this Windows box has POSIX tools. Models
  are trained on GNU coreutils and write `grep`, `sed`, `awk`, `head`, `find`;
  without them those calls fail. With Microsoft Core Utils installed
  (`winget install Microsoft.CoreUtils`) the prompt says so and the model can
  use them as it was trained. Without them it names the built-in tools
  (`search_code`, `read_file`, `find_files`) and the PowerShell cmdlets instead
  of guessing.
- `tools/env_check.py` reports the coreutils state and the install command.

### Telegram

- The browser owns the run mode. A phone `/mode` override only survives while
  the engine mode is unchanged; switching modes in the UI makes Telegram follow
  the frontend again instead of running the old override forever.
- New `/follow on|off` (default on): a phone message continues the chat the
  browser used last while it is fresh (one hour). A run started in the UI can
  be continued with a plain "go" from the phone. Off, the phone keeps its own
  `[TG]` chat.

### Fixes

- The post-run hooks (insight extractor, skill distiller) no longer pick
  `agents.judge.model` as their worker. They used to load the judge at ctx 4096
  after every run and evicted the still-warm coder to make room, so the next
  run paid a full coder load. They reuse whatever model is already loaded and
  only fall back to refiner, analyst, judge in that order.
- Text to speech: when the sidecar answers 200 without audio, the engine now
  reports `TTS_ENGINE_BROKEN` with the sidecar log hint, retries once with the
  shipped default voice and then holds a five minute cooldown instead of paying
  a synthesis round trip for every sentence. The read-aloud prefetch keeps a
  no-op catch, so a superseded fetch can no longer surface as
  `Uncaught (in promise)`, and a broken engine clears the queue.

## [1.4.6] - 2026-10-21

### Routing decider (no GPU judge needed)

- One decision engine (`decide_from_signals`) for both the sync and the async
  path - the precedence logic used to exist twice.
- The encoder now classifies **concrete actions** (`asks_a_question`,
  `write_new_file`, `edit_existing_code`, `debug_a_problem`, ...) instead of
  abstract `direct|agentic`: 14/14 on the probe set vs 9-10/14, ~630 ms per
  decision (one forward pass for route + planner).
- The plan decision is a local heuristic (no model, 11/12 on the probe set);
  `routing_planner` gained `heuristic` as a value and `auto` now means it.
- Threshold calibrated to the new score range (0.55, was 0.85 - that escalated
  almost everything); `latency_ms` reports inference time, not model load.
- Fixed: `shadow` mode no longer silences the judge; the short-prompt shortcut
  no longer bypasses the decider (a 9-word code task used to run as chat).

### IDE integration

- New `POST/GET/DELETE /context/editor`: an IDE reports the open file,
  selection and diagnostics (per workspace, 300 s TTL, 4k char selection cap).
- The block reaches the prompts (direct/pipeline via `_make_messages`, agentic
  via the duo coder prompt) and the run record only keeps a summary
  (`file`, `selection_chars`, `diagnostics`) - never the code.
- MCP server hardened: paths outside the workspace are rejected
  (`PATH_OUTSIDE_WORKSPACE`) instead of silently rewritten; the governance tiers
  are enforced on every dispatch path; header (32 KB), body (2 MB), concurrency
  (8), batch (16) and request-timeout (300 s) limits; stdio answers an oversized
  line instead of dropping it; per-call log line (`[MCP-CALL] ...`).
- `/internal/tool/exec` (the endpoint MCP and IDEs proxy into) got a loopback
  host guard and requires `HIVEMIND_INTERNAL_TOKEN` for exec tools; read/write
  stay available, the workspace lock is server-side.
- Docs: `docs/mcp.md` (both directions, tiers, limits, env reference).

### Telegram

- `/help` is generated from the command spec, so every command is listed
  (22 instead of the curated subset) and ships an inline keyboard: tapping a
  command runs it - it never starts a run.
- A command that needs a value asks first; the next message is the value,
  `stop` cancels.
- Voice notes now go through the decider: `mode=auto` plus an explicit
  `duo_agentic_mode=false`, so a spoken note is not forced into a coder run.

### UI

- The agentic context block is a card now: header, label column, compact
  fields with a `tok` unit and a muted VRAM estimate instead of a stack of
  orange number inputs. The planner row only shows when "Planner = Coder" is
  off.
- The plan step offers three choices only: always plan, decider decides, never
  plan. Older values (`auto`, `heuristic`, `judge`) still resolve and stay
  visible in the status line instead of being rewritten silently.
- The routing card sits under MODE in the Agents tab and only appears while
  the mode is `auto`: decider, judge and the plan step. The plan row is called
  "Plan step" and says that the planning MODEL lives in Agents, because the
  old "Planner" label read like a model choice. Live line: `Auto -> decider (encoder, CPU) · planner:
  heuristic (free) · threshold 0.55`, plus a warning when a fixed mode
  bypasses the routing entirely.

### Fixes

- In `auto` mode the persisted `duo_agentic_mode` no longer hijacks the route:
  a stored agentic flag (a preset sets it) used to send every auto run straight
  to code_duo, before the decider was asked. A simple question ended up in a
  coder run. Now the flag only forces agentic when the decider is off; an
  explicit request travels as `agentic_force` (the Telegram `/mode agentic`
  sends it), so an intentional agentic run still works.
- The feature search finds voice and routing: "tts", "stt", "routing", "auto"
  and their German twins expand to the real labels, and explicit index entries
  cover the Voice card (TTS/STT toggles, voice, speed, volume) plus the mode
  row, the routing selects and the plan step. Multi-word aliases now expand to
  a list of terms instead of one phrase that could never match.

### Fixes

- Text to speech no longer fails silently per sentence: when the sidecar
  answers 200 without audio (pocket-tts raised `string keys in translate table
  must be of length 1` for the fetched voice config), the engine classifies it
  as `TTS_ENGINE_BROKEN`, retries once with the shipped default voice, and then
  holds a five minute cooldown instead of paying a synthesis round trip for
  every sentence. The error carries the sidecar log hint.
- The read-aloud prefetch keeps a no-op catch, so a superseded fetch can no
  longer surface as `Uncaught (in promise)`, and a broken engine clears the
  queue instead of retrying the rest of the answer.

### Telegram follows the frontend

- The mode you pick in the browser is now the default for phone runs. A
  `/mode` override from the phone only survives while the engine mode is
  unchanged; as soon as the UI switches modes, Telegram follows it again.
- New `/follow on|off` (default on): a phone message continues the chat the
  browser used last while it is fresh (one hour), so a run started in the UI
  can be continued with a plain "go" from the phone. Off, the phone keeps its
  own `[TG]` chat as before.

### Cleanup

- Removed dead code (`_maybe_add_until_finished_block`,
  `reset_read_guard_after_compression`, `_fallback_explore_prompt`,
  `wait_approval_decision`, `reset_to_base_configs`) and the never-read
  `routing_pipeline_allowed` setting; `mcp_servers` is marked reserved.
- `deploy/recommend_models.py`: suggests a start config from the detected
  VRAM/RAM plus the measured tok/s per model.
- Model catalog: `spark-x2.6:4b` pinned to `XHToken/Spark-X2.6-4B-GGUF`
  (replaces the Spark-X2.5 entries) and `lfm2.5:24b-a2b` pinned to
  `LiquidAI/LFM2.5-24B-A2B-GGUF` with its DSpark drafter sidecar
  (`LFM2.5-24B-A2B-DSpark-F16.gguf`, 0.6 GB). The profile carries the
  calibrated `moe_cpu_experts` (24, measured 6051 MiB @c4096) so the VRAM
  pre-flight and the loader agree: 5.91 GB @4k, 6.54 GB @32k on an 8 GB card. Both have profiles and automap
  entries; `tests/test_model_catalog.py` pins repo, filename and drafter so a
  renamed asset cannot silently download nothing again.
- `model_configs/models/spark-x2.6_4b.json` for Spark-X2.6 (4B, Q4_K_M): sampling
  per model card, thinking on by default (`spark-x2.6` joined the reasoning
  bases), tool calls work through the embedded template
  (`tool_calls` + `finish_reason=tool_calls` on b11456) - no external
  `spark25` parser needed on llama.cpp.
- Docs: profile-naming example clarified; the docs no longer reference
  `deploy/fetch_tts.py`, `core/sidecar.py` or `RESULTS.md` (none ship).

## [1.4.5] - 2026-10-20

### Fixed

- **LLM pre-explore could never run: three module globals were missing.**
  `hive_functions/pre_explore/llm.py` used `global _llm_client, _llm_client_lock,
  _pre_explore_usage` but never defined them at module level, so the first read
  raised `NameError: name '_llm_client_lock' is not defined`. Every pre-explore
  LLM call burned 30 retries and ended in `LLM FAILED after 30 attempts` - the
  whole exploration silently never happened. The names are defined now, and a
  test walks the AST of every pre-explore module to make sure a `global`
  statement always has a matching module-level definition.
- **Parallel pre-explore crashed on `asyncio.gather`.** `runner.py` wrapped the
  gather in `asyncio.create_task()`, which requires a coroutine - `gather()`
  returns a future, so the step failed with `a coroutine was expected, got
  <_GatheringFuture pending>`. It uses `ensure_future` now.
- **A pre-explore failure can no longer take the run down.** `_xparallel_results`
  was only bound on some paths, so an early error (for example a failed VRAM
  pre-flight check for the explorer model) ended in `UnboundLocalError` at
  `state.update`. The variable is pre-initialised, and the pre-explore phase is
  wrapped: it logs the full traceback, tells the user, and continues with the
  static repo map instead of aborting.

### Added

- **Opt-in downloader for the routing decider.** The release zip still ships no
  weights; `deploy/fetch_decider.py` fetches `fastino/GLiNER2.5-Decide` into
  `models/decider/` and prints the two settings to use. The Python dependency
  (`gliner2` + torch) stays out of the normal install and lives in
  `requirements-decider.txt`. Without both, `routing_decider` stays off and the
  LLM judge decides as before.

## [1.4.4] - 2026-10-20

### Added

- **Token statistics now answer "which model, how fast, how stable" instead of
  only "how many tokens".** Each run records the phase durations
  (`phases_ms`) and both the requested and the resolved mode, and
  `/soul/token-stats` returns three aggregations: `by_model` (per model *and*
  role: coder, planner, critic, pre-explore, direct — plus the pipeline stages
  analyst, refiner, synthesizer, vision), `by_model_by_day`, and `by_mode`
  (which route actually ran: `code_duo`, `direct`, `pipeline`).
  The Soul panel renders a compact model list (speed · crash-free) and a mode
  list; clicking a model filters the run list below to the runs that model
  touched.
- **Real decode-speed fallback from the llama-server logs.**
  `deploy/backfill_speeds.py` parses `eval time … tokens per second` out of
  `logs/llama_server_*.log`, maps the GGUF file name to a model tag via
  `models.json` and stores a median per model in `token_stats.json`. Records
  without phase timing show that measured rate (marked `~`) instead of nothing.
- **Pipeline runs are measurable now.** The pipeline runner reported no usage at
  all: `requests 0`, `prompt_tokens 0`, no phase data, and its token count was a
  characters/3 guess. `chat_stream` gained an opt-in `usage_out` (default off, so
  direct and duo paths are untouched) and the run wrapper around
  `pipeline_chat_stream` reports every stage as `usage_meta` + `run_meta`, with
  its own PhaseTimer bucket. A pipeline run now records e.g.
  `requests 4`, `prompt_tokens 4205`, `phases {analyst 1100, refiner 133,
  critic 600, synthesizer 172}` and the matching `phases_ms`.

### Fixed

- **`/static/*` no longer hides a deploy behind the browser cache.** The
  `?v=` token only changed when someone remembered to bump it; a middleware now
  sends `Cache-Control: no-cache, must-revalidate`, so a normal reload is
  enough.
- **User stops no longer count as failures.** `crash-free` is
  `completed / (runs − abort − graceful_stop)`: stopping a run yourself says
  nothing about the model.
- **Roles that were configured but never ran are not counted.** The critic
  appeared in 70 duo records without a single critic round, which made the model
  list report runs that never happened.

- **The stream handler no longer swallows error tracebacks.** It logged
  `logger.exception` from a different block than the `except`, so every
  `/stream` failure printed `NoneType: None` and the cause was unreadable. The
  traceback is now captured where the exception happens and logged with it.
- **Routing groundwork (opt-in, inert by default).** `routing/decider.py` adds a
  resolver with three independent switches - `routing_decider` (a CPU encoder,
  GLiNER2.5, no VRAM), `routing_judge` (the existing LLM judge) and
  `routing_planner` - each `off | shadow | on`. Defaults reproduce today's
  behaviour exactly (`decider=off, judge=on, planner=auto`); an existing
  `settings.json` without the keys is resolved through `DEFAULT_SETTINGS`, so a
  missing key never silently disables the judge. The encoder is an optional
  dependency: without `gliner2` the resolver logs one warning and falls back to
  the judge. The decider can only pick `direct` or `agentic` - it never selects
  the pipeline route. A missing dependency, a timeout or a broken judge falls
  back instead of failing the run.

## [1.4.3] - 2026-10-19

### Fixed

- **write_file could hang forever on a folder the engine may not write into.**
  The atomic write used `tempfile.mkstemp(dir=<target>)`. CPython treats
  `PermissionError` there as a name collision while `os.access(dir, W_OK)` says
  the folder is writable - and on Windows that check reads the read-only
  *attribute*, not the ACL, so it is nearly always true. In a sandboxed/ACL-locked
  workspace the call looped up to `TMP_MAX` (2**31-1) at 100 % CPU: the coder
  called `write_file`, the UI sat on "Processing context..." for eleven minutes,
  and the file was never created. All seven atomic write paths now create the temp
  file through `_mkstemp_bounded()` (`O_CREAT|O_EXCL`, at most 16 names, a real
  permission error propagates on the first try), and `write_file` answers the model
  with `WRITE_FILE_PERMISSION` naming the sandbox/ACL cause instead of spinning.
- **Same trap in the STT temp WAV** (`core/stt_audio.py`): a locked-down `%TEMP%`
  could hang a transcription. It now creates the file the same bounded way.
- Settings panel nesting (regressed in 1.4.1, fixed in 1.4.2) and the gated
  `/prefetch/stats` route stay as they are; nothing else changed.

### Note

Start HiveMind by double-clicking `start_hivemind.bat`, not from inside an
agent's restricted terminal - otherwise the engine inherits a write sandbox that
only covers the HiveMind folder.

## [1.4.2] - 2026-10-19

Hotfix for 1.4.1: the settings sidebar came up broken and the Prefetch card had
lost its endpoint. It also includes additional fixes that followed 1.4.1.

### Fixed

- **Broken sidebar in 1.4.1**: an extra `</div>` (left over from removing the
  per-role preload rows) closed the Configs panel right after the "Startup
  Preload" card. Everything below it — Keep-Alive, Voice, Smart Preload, Model
  Config, Energy, Git, Telegram — rendered outside any panel, so those cards
  stayed visible under *every* tab and could not be scrolled. Memory, Soul,
  Models and Chats fell out of `#sidebar` into `<body>` and their tabs went
  blank. One line removed; the panel nesting matches 1.4.0 again.
- **`/prefetch/stats` returned 404**: the endpoint lived in the debug router,
  which is gated behind `debug_api_enabled` since 1.4.1 — and that defaults to
  off. The Prefetch card loads the stats on every panel open, so the route now
  lives in the always-mounted core router. No user-facing setting to change.

### DSpark drafter (Sharp-MiniCPM)

- **Ships with the installer**: the two Sharp-MiniCPM5-2B specs download
  `openbmb/MiniCPM5-2B-DSpark-GGUF` (`MiniCPM5-2.6B-DSpark.gguf`, ~650 MB) as a
  sidecar — never registered as a model. Sidecars are now checked *before* the
  `--only-missing` skip, so an existing install gets the drafter on a re-run.
- **Enabled for the Sharp-MiniCPM profiles**: `model_configs/models/`
  `minicpm5_2b-sharp.json` carries `dspark_draft_filename` + `dspark_draft_n_max: 2`.
  Measured with the app's own llama flags (AMD Radeon iGPU, Vulkan0, 16 threads):
  target alone 14.1 tok/s, DSpark n-max 2 10.6 (49% acceptance), n-max 4 7.7
  (34%) — slower on this laptop class. `HIVEMIND_DSPARK_SPEC=0` turns the
  drafter off machine-wide without editing a profile.
- **`dspark_draft_n_max` is honoured**: the profile field existed but nothing
  read it; every DSpark model ran with the global default (10).

### Under the hood

- `core/duo_runner.py`: the module-level helpers and the stable callbacks
  moved into `core/duo/preflight.py` (no behavior change, re-exported).
- Stuck handler: `loop_detected` is initialized before the extracted handler
  runs — the `UnboundLocalError` aborted every run after the first tool call.
- Verify gate: a completion accepted after one veto is now marked unverified
  and reported instead of silently passing.

## [1.4.1] - 2026-10-18

Reproducible installs, no dead switches, less information disclosure.

### Added

- **`deploy/gen_settings_docs.py`** - the generator behind `docs/settings.md`.
  The table had drifted (204 of the then-236 keys); it is now regenerated
  (237 keys) and `--check` fails the suite when the doc is stale.

### Fixed

- **Reproducible installs**: `uv.lock` ships (64 pinned packages) and both
  installers sync from it (`uv sync --frozen`), so a release installs exactly the
  versions it was tested with instead of whatever is newest that day.
- **Dead switches removed**: `automap_code_duo_enabled`,
  `automap_duo_pre_explore`, `automap_duo_parallel_preexplore`,
  `automap_pipeline_websearch_enabled`, `duo_distilled_executor` and the per-role
  `startup_preload_{analyst,coder,judge_in_agentic}` were read by nothing. The
  Automap card now drives the real settings (`duo_pre_explore`,
  `duo_parallel_preexplore`, `pipeline_websearch_enabled`), startup preload has
  one honest switch, and the three per-role preload rows are gone.
- **Debug API closed by default**: `/debug/tasks` (asyncio task stacks with file
  paths), `/debug/models`, `/thinking/debug` and `/budget/debug` were mounted on an
  unauthenticated loopback API. New `debug_api_enabled` (default off) gates the
  whole router.
- **Write-truncation log no longer stores user code**: `logs/write_truncations.jsonl`
  kept the full raw `write_file` call (up to 64 KB of generated file content per
  entry, never rotated). It now records `path`, `raw_len`, `finish_reason` and
  `json_complete` only - still enough to see that a call was cut off mid-JSON.

## [1.4.0] - 2026-10-17

Voice, end to end: speak answers here, dictate runs from the phone, and hear the
answer back. Everything is opt-in and nothing starts until you turn it on.

### Added

- **Text to speech (Pocket TTS, CPU only, off by default).** A Voice card block
  with a master switch, seven shipped English voices (default `anna` - `cosette`
  is a non-commercial recording, see `docs/voice_licenses.md`) a speed control
  (`1.08` - Pocket TTS has no speed parameter, so HiveMind time-stretches the audio
  with WSOLA and keeps the pitch), a "Play sample" button and a clear engine
  status line. The speaker button on every answer reads that answer - it is
  hidden unless TTS is on, reads the bubble at click time (works on a still
  streaming answer) and never autoplays.
- **The TTS engine is a supervised sidecar, not a dependency.** It starts through
  `uvx` on the first request (isolated environment, PyTorch never enters
  HiveMind's venv), answers `/health` when ready, is stopped at exit, and shuts
  itself down after `tts_idle_stop_s` (default 900 s). Without `uvx` it fails with
  a clear code instead of downloading 200 MB.
- **Read aloud** (four switches in the Voice card): direct mode reads the whole
  answer; agentic runs read the plan first and then the coder's answer; ask-user
  questions and the defensive gate / approval cards can be spoken too. One item at
  a time, stopped by Stop, long text cut at a sentence boundary.
- **Telegram voice notes in** (`voice_in` / the switch in the Telegram card): the
  note is downloaded (20 MB, 300 s caps), transcribed by the local engine and the
  transcript runs exactly like typed text - same owner/private gates, same
  approvals. Every `STT_*` code is translated into a sentence.
- **Telegram answers as voice notes** (`voice_out` / the switch in the Telegram
  card): the final answer is additionally sent as a voice note (`sendVoice`,
  Ogg/Opus) through the same owner-only `send()` boundary. A TTS failure is a log
  note, never a broken run.
- **Installer voice step** (opt-in, four choices): speech to text only, both, text
  to speech only, or skip - plus `deploy\setup_voice.bat [model] [--tts|--both]`
  for later and `install_linux.sh --voice|--tts|--both`. Whatever you skip stays
  one click away in the Voice card, and all three paths call the same fetch code.
- **`GET /tts/status`, `GET /tts/voices`, `POST /tts`** (`format=wav|opus`,
  `X-HiveMind-Duration/Voice/Speed` headers). The voice list is served from one
  place; the UI builds its dropdown from it.
- **Settings search** finds the new features by their everyday names (English and
  German: `tts`, `vorlesen`, `read aloud`, `diktieren`, `microphone`, `voice note`,
  `telegram voice`).

### Fixed

- **Every `/stt` request returned `STT_EMPTY`.** The temporary WAV was created
  with `tempfile.mkstemp(...)` and the fd was never closed; on Windows the file
  stayed open for writing, the parakeet/whisper child could not read it
  ("failed to read audio data") and the endpoint reported "no speech". The
  regression
  test now requires the decoded WAV to be readable *and* deletable by a second
  process.
- **Speech blocked the whole UI.** `/tts` ran the sidecar start (20-30 s on the
  first request) directly on the event loop: SSE, status polling and every other
  request stalled. Synthesis, Opus encoding, audio decode and transcription now
  run in a worker thread; `/tts/status` answers in ~20 ms during a synthesis.
- **The TTS restart budget counted successful starts:** after three normal idle
  shutdowns the fourth start refused to run ("failed to start three times"). Only
  consecutive failures count now, and a healthy start clears the budget.
- **The mic button rendered as a small white box** (the stylesheet still carried
  an old cache-buster) and the "speech to text" sub-block stayed visible after
  switching it off (the toggle did not update the in-memory setting). The button
  now has the send button's footprint (40x40, 7 px radius, red, pulsing while
  recording) and both master switches collapse their blocks immediately.
- **Installer step numbering** said `[x/7]` for the first five steps and `[x/8]`
  for the last three after the voice step was inserted.

### Changed (tools, sandbox, Linux)

- **Write tools that were never gated now ask**: `edit_ast`, `replace_lines` and
  `patch_file` write files and were missing from the approval list - with phone
  approvals on, the model could rewrite a file without a card. The policy (what is
  gated and, more importantly, what is deliberately not) is now written next to
  the list.
- **The dispatcher refuses what the run does not offer**: a name outside every
  mode's tool list (`patch_file`, a resurrected legacy handler) and a
  machine-changing tool outside the current mode (a chat or read-only run calling
  `write_file`/`run_bash`) are rejected before a handler runs.
- **Malformed tool arguments are an error again** in the agentic loop: broken JSON
  used to become "no arguments" and the tool ran with defaults (a `list_dir` on
  the workspace root instead of the intended folder). The duo path already did
  this; the agentic loop now matches.
- **`start_background` goes through the destructive gate** like `run_bash` (it
  runs the same kind of shell command and was only protected by the approval
  switch).
- **`search_code` runs without a shell**: the backends were shell strings built
  from the model's pattern and path (`rg … "pattern" 2>nul`, `grep … | head -50`).
  A read-only tool that runs without an approval prompt must not hand cmd a
  string; every backend is an argument list now and the 50-line cap is applied in
  Python.
- **Linux gets per-tool limits**: Windows job objects have no twin on POSIX, so
  the only limit was the service-wide `MemoryMax` - a runaway `run_python` could
  take the whole server down. `RLIMIT_AS` / `RLIMIT_NPROC` / `RLIMIT_CPU` are
  applied per child (same settings keys, `0` = off), and the unit now carries
  `HOME`, `UV_CACHE_DIR` and `HF_HOME` for the speech sidecar.
- **Linux installer**: no longer copies `settings.json`/`sessions`/`logs` from a
  developer checkout into `/opt/hivemind`, runs its voice step as its own step
  after llama.cpp, installs `requirements-voice.txt` when the extras are missing,
  and pre-warms the TTS cache for the service user instead of root.

### Fixed (voice polish)

- **License documentation for the audio stack** (`docs/voice_licenses.md`): the Pocket TTS
  model is CC BY 4.0, the code MIT, and the shipped voices come from four sources
  with different terms. **`cosette` is Expresso = CC BY-NC 4.0 (non-commercial
  only)**, so the shipped default is `anna` (CC BY 4.0) now; `cosette` stays
  selectable and is labelled non-commercial. The Voice card prints the license per
  voice and the attribution line, the README links the table.
- The "Play sample" button wrapped onto two lines (it borrowed the 40x40 icon
  button); it has its own text-button style now.
- The per-answer speaker button moved from the header into a footer under the text
  and only appears once the answer has content - in the header it floated over an
  empty bubble and looked attached to the user's message.
- **Read aloud starts ~7x sooner**: the text is queued in chunks (first one ~60
  characters, then up to 240) instead of one 600-character request, the next chunk
  is fetched while the current one plays, and the engine reuses one keep-alive HTTP
  client. Measured warm: 49 characters -> 0.9 s, 98 -> 1.6 s, 320 -> 6.5 s before
  the first sound (the sidecar is pre-warmed on page load when TTS is on).
- Turning the TTS master on now enables the two answer switches (direct + agentic)
  when none was set, so "voice on" actually speaks without a second visit.

- **Volume control and a louder default**: the sidecar returns speech at about
  -15.5 dBFS peak (measured), i.e. 12-15 dB quieter than a normal speech file. The
  engine now levels every chunk to -1.5 dBFS (`tts_normalize`) and the Voice card
  has a volume slider (`tts_volume`) that applies to read-aloud, the per-answer
  speaker and the sample.

- **Read aloud no longer narrates the tool output**: the extractor strips tool
  chips, websearch result blocks, tool-call rows, think blocks and code panels
  before speaking, so a direct-mode answer is read as the answer - previously the
  whole bubble (every search result included) went to the engine.

- **Voice notes for mirrored runs**: a run started in the browser now sends the
  voice note to the phone as well. The mirror path relayed text only, so "answers
  as voice notes" never fired for anything but a phone-started run.
- **A gateway restart no longer looks like a dead bot**: the replay protection
  discards what arrived while it was offline; the owner now gets one short note
  telling them to resend.

- **Presets are complete snapshots and loads are non-destructive.** Loading a
  preset now MERGES the per-agent config instead of replacing the whole dict, so a
  preset saved before a role or field existed can no longer wipe its
  `max_tokens`/`thinking_budget` (the reported "output budget resets and answers
  get cut"). The live pipeline is refreshed right after a load (it used to keep the
  pre-load budgets, which the next save then stored back), the save carries the
  agent cards that are on screen, and the load no longer blocks the UI with an
  alert. A regression test asserts that a snapshot contains every persisted
  setting (secrets and machine paths stay out).
- **Download catalog fixed**: the Hermes entry pinned file names the upstream repo
  had renamed, so a fresh install fetched neither weights nor projector. The
  README table now lists the real repo and the measured download sizes.

### Telegram output contract

- **Choose what a phone run sends back**: `telegram_result_mode` = `both` (text
  plus a voice note), `text`, or `voice` - the last one is a results-only channel
  where the model-generated answer arrives as a voice note and nothing else. A
  failed synthesis falls back to text; an error always stays text.
- **`telegram_progress_msgs`** silences the status line and the tool-call lines.
  Approval cards and the final result are unaffected - they are safety and result,
  not progress.
- **`tts_preload_run`** (Voice card) warms the speech engine when a run starts, so
  a phone-only flow speaks in about a second instead of paying the cold start. The
  gateway calls the new `POST /tts/preload` right after the user turn is written.

- **A broken Python environment is diagnosed instead of guessed.** A fresh
  1.3.8 install on Python 3.14 reported `cannot import name 'ArgsKwargs' from
  'pydantic_core._pydantic_core'`. The
  launcher's env check now prints the interpreter, the pydantic/core versions and
  the compiled module's size, names a shadowing copy, reinstalls **wheels only**
  (`--only-binary :all:` - a source build is how a half-written extension
  happens), and then walks the real causes in order: antivirus quarantine, a
  mismatched pair, a fresh venv, a fresh folder. `pydantic>=2.11` is now an
  explicit dependency, because an old `pydantic_core` next to a new `pydantic` is
  exactly what raises that error.

- **Installer: the venv is pinned to Python 3.14.** `uv python install
  3.14` failed when an unmanaged `python3.14.exe` existed in `~/.local\bin`; the
  installer noted it and continued, uv then chose **3.15**, where `tree-sitter`
  has no wheel - the source build needs MSVC and killed the install. The
  interpreter is now pinned (`uv venv --python 3.14`, `uv sync --python 3.14`), a
  `--force` retry replaces the foreign executable, a missing 3.14 stops with
  instructions instead of falling back, and the finished venv is asserted to be
  3.14. A leftover `.venv` without `python.exe` is cleared first (that used to
  fail with "not a valid Python environment" on the second run).
- **A stray line in `install.bat`** printed `Der Befehl "ERROR-every-second-run"
  ...` at the start: an unterminated quote in a REM made the next line a command.
- **Telegram card: the voice controls grey out with voice off.** The
  result-format select and the hint lines are now greyed out and disabled together
  with the voice-note toggles.
- **Complete licence table for the audio stack** in the README: Pocket TTS model
  (CC BY 4.0 + gate terms) and code (MIT), the voice recordings per dataset
  (VCTK/Alba CC BY 4.0, Voice-Zero CC0, Expresso **CC BY-NC 4.0**), whisper models
  (MIT), whisper.cpp/parakeet binaries and the parakeet GGUF (MIT, upstream
  CC BY 4.0). Also states that no model is vendored - everything is downloaded at
  first use.

- **Env check without imports**: it reports the resolved paths, shadowing copies
  (PYTHONPATH / user site), the compiled module's size and a missing __init__.py
  (namespace package -> "unknown location"), and escalates the repair to
  uninstall + install so orphaned files are actually removed.

### Notes

- English only. Speech to text models: `parakeet-q4k` 396 MB (3.6 % micro-WER),
  `base` 141 MB, `small` 465 MB, `tiny` 74 MB - pick by RAM, the installer prompts
  once.
- Warm TTS runs at 1.25-1.85x realtime (320 chars -> 19 s of audio in 10 s). The
  first request after idle pays the sidecar start once. Text is capped at 600
  characters per request.
- Speech to text + text to speech cost ~1.4 GB of models and two uv caches, all
  outside the release zip and the repository.
- Chat push-to-talk (V2) is not in this release; the mic button appends a
  transcript to the message box, and the read-aloud switches read answers,
  the plan, the coder answer, ask-user questions and approval cards.

## [1.3.11] - 2026-10-17

Web search that actually answers, a planner-briefing cap, and the voice work
from 1.4 (all off by default).

### Fixed

- **Web search returned the same junk for every query.** Four separate causes:
  the shipped SearXNG config replaced every upstream default instead of merging
  (so no outgoing User-Agent - Wikimedia answered 403 and the wikipedia engine
  silently returned nothing), google was bot-blocked and poisoned general
  queries, `bing` answered English queries with another region's pages, and the
  tool itself pinned `engines="bing,duckduckgo"` + `language="all"`, overriding
  whatever the instance had enabled.
- **Rebuilding the SearXNG image changed nothing** in the running container: the
  image declares `/etc/searxng` as a volume, so an old anonymous volume kept
  shadowing the baked config. Every `compose up` in `searxng.bat` now passes
  `--renew-anon-volumes`.
- **A query that names Wikipedia is answered by the MediaWiki API directly**
  (descriptive User-Agent, shorter-prefix retries, ranked by title overlap) when
  the search engines return nothing useful - the case that used to return shop
  homepages. An empty engine result falls back there too.
- **A web-search budget wall no longer kills the run.** It used to set
  `loop_detected` after two exhausted calls; a run ended after 24 rounds
  with zero files written. Now the first exhaustion disables `web_search` for the
  rest of the run (the coder keeps working with its other tools) and only a model
  that keeps calling the disabled tool four more times ends the run.
- **Near-identical searches are recognised.** The dedup compared exact strings,
  so "RDP processor RAM memory" and "RDP processor graphics memory" were
  different searches. Token overlap (or three shared words) now counts, and the
  third such attempt stops the topic with a clear instruction instead of burning
  the budget.
- The result header printed the literal `{_ws_today}` instead of the date.

### Added

- **Coder briefing limit** (`duo_coder_briefing_think_tokens`, default 1500): the
  planner's thinking is injected into the coder as a briefing only up to that many
  estimated tokens; above it the coder gets the pure plan - a planner that thinks
  8-16k tokens before writing a plan otherwise poisons it. `0` = plan only,
  `-1` = always inject (old behaviour). The decision is logged as
  `[CODER-BRIEFING] mode=... think_tokens=... limit=...`.
- **Speech to text (English only, off by default):** a Voice card in the settings
  with a status line showing engine / model / ready, a Download button with
  progress for the whisper.cpp + Parakeet binaries and models, a 3 s microphone
  test, model/language/preload/thread controls, and a red mic button left of send
  that appends the transcript to the message box. Nothing runs until the switch
  is turned on; the optional audio packages live in `requirements-voice.txt`.
- `deploy/fetch_whisper.py` fetches the whisper.cpp release (whisper-cli,
  whisper-server, parakeet-cli) and the model tiers.

### Notes

- Search engines enabled in the shipped config: duckduckgo, mojeek (general) plus
  github/stackoverflow/pypi/npm/lib.rs/arxiv. google, bing, brave, wikidata are
  disabled with the reason in the file.
- Voice is English only in this release and has no chat push-to-talk beyond the
  transcript button described above.

## [1.3.10] - 2026-10-17

Workspace regression fix, plus the first, disabled piece of the voice work.

### Fixed

- **Workspace regression (introduced in 1.3.9).** The web UI sends the global
  *Workspace* field with every run, and 1.3.9 let that value beat every other
  source. Consequences on a machine whose settings came from somewhere else: a
  chat's own workspace was overridden (files landed in the wrong project), and a
  path that only exists on another machine left the run with **no** workspace at
  all - `write_file` had no root and the model looked like it had forgotten the
  project. The UI value is now treated as the default it is; only a value that
  *differs* from the setting counts as an explicit request.
- A stale workspace value no longer ends a run empty-handed: the normal chain
  (task path, chat workspace, UI setting, last used) decides, auto-commit stays
  off, and the run says which directory it used.
- A workspace fallback to the engine directory is no longer stored as "last
  used" - one accidental run used to pin the install folder for every later run.
- `searxng.bat` no longer closes silently: it probes the Docker **daemon**
  (not just the CLI on PATH), explains a stopped Docker Desktop, keeps the
  window open when double-clicked, and reports a stopped container instead of
  "[OK] SearXNG is running".
- A CPU-only install with a large context now says so (`[PERF-WARN]` with the
  real number and the practical 8k-16k range) instead of looking like a hang.
  This is the "one write_file took 25 minutes on a laptop" class of problem.

### Added (off by default, no UI yet)

- Speech-to-text groundwork, English only: `POST /stt` and `GET /stt/status`,
  `deploy/fetch_whisper.py` to fetch the whisper.cpp / Parakeet binaries and
  models, and the `stt_*` settings. Nothing runs until `stt_enabled` is turned
  on, and the optional audio packages are only pulled in by
  `requirements-voice.txt`. First step of the 1.4 voice work.

## [1.3.9] - 2026-10-15

Chunk-resume verification run + resume durability:

- chunk checkpoints now cover EVERY done path: the persist call existed
  only on the skip branch (the "after every done chunk" comment
  overstated it) - all seven normal completion paths (critic-skipped,
  auto-approve, auto-test skipped/passed/exhausted, normal completion)
  write the resume block after advancing
- _halt_with_resume does NOT halt anything (name misleading) - it only
  writes the resume block; the chunk loop continues on its own
- RESUME DURABILITY: resume-block
  writes for API-driven runs (no chat session file) silently no-opped -
  the context path resolved to None while the log claimed "Block
  saved"; a hard kill then lost the checkpoint. Every chat_id now gets
  a standalone context file under sessions/, and _write_resume_block
  VERIFIES the write and logs honestly when it did not persist
- end-to-end: planner reads index.html in one call, chunk
  1 committed then chunk 2 starts without pause, the plan is injected
  into the chunk-2 coder context, exactly ONE final test message, and
  after a hard engine kill + restart the resume intake reports
  "Resuming: 2/3 chunks already done" and continues at chunk 3
- ui: short tool results rendered the same text twice (summary carried
  the full text AND the force-opened pre repeated it - every green
  run_tests row); the pre is skipped when the summary IS the content
- the smoke-test nudge now INSTRUCTS the model to include at least one real
  assertion or explicit output check ("a file that cannot fail does not
  count as a test"). This is prompt text only: the auto-test gate checks the
  command string of the last run_bash (it looks like a test run), not the
  content of the written test file - a test that cannot fail still passes
  the gate

Budgets redesign: no model-class staffels

- core/budgets.py: Budgets.resolve(n_ctx, cpt, max_out, settings) derives
  every cap from measured sizes ONCE per run and freezes them (schema-
  visible values must not change mid-run — prefix cache). Settings keys
  "budget_<field>" override single values; None/absent = auto. tools/
  limits.py now holds ONLY fractions and named floors/ceilings — the
  only hard numbers left in the codebase
- the fixed staffels are gone: tool-result cap = 0.12 × n_ctx × cpt
  (clamp 2k-24k; ~7.7k at 16k, practically yesterday's 8k), read refusal
  by estimated SIZE against the read budget instead of a 1200-line
  count, anchor window and outline derived from the injection budget,
  compression trigger headroom-based (reserve = clamped max_out + p90
  growth; free < 1.5 × reserve triggers, reproducing ~0.70 at 16k/8k)
  — write-heavy runs trigger earlier without a special case
- tool schema is a CONTINUUM, not compact/full: every tool has a
  priority (tier-0 core always kept), three description levels, and the
  fitter upgrades by priority while the schema budget (0.25 × n_ctx ×
  cpt) holds; what does not fit minimally is dropped. At 16k MiniCPM
  keeps the full schema; an 8k model shortens automatically — no model
  names anywhere (grep test enforces it)
- chars/token is a per-model EMA now (update from every real response:
  sent chars vs usage.prompt_tokens), start value 3.92 measured via
  /tokenize — Budgets use it instead of the 3.0 constant
- resolved budgets are logged ([BUDGETS] line) and written as a
  phase="budgets" telemetry row so every run shows what was derived
- new tests: test_budgets (monotonicity in n_ctx, the 16k base case
  reproduces the tuned values, override rule, floors/ceilings at
  extremes, cpt EMA convergence, schema continuum, and a grep test that
  keeps model names/size staffels out of budget and tool-selection
  code); test_schema_truth now pins the schema text to the RESOLVED
  run budgets

Compression, injections, tool profiles and web search:

- compression without slot eviction: deterministic observation masking
  (stale tool outputs -> recall markers, the same machinery as the old
  >90% emergency valve) now runs BEFORE the LLM summary and can resolve
  the threshold crossing without it - no LLM call, no coder evict
  (settings: duo_mask_before_compress, default on)
- the light compression model (duo_compress_model) is only loaded when
  it fits NEXT TO the coder (can_fit excludes the coder's slot) - the
  documented live case (light model evicted the coder: ~20s reload +
  reuse=0%) can no longer happen; otherwise the coder compresses itself
- edit_file failure anchor: instead of a fixed 60-line file head (up to
  4.3k chars per failed attempt) the error now shows +-5 lines around
  the CLOSEST match to the failed old_text (best_match_location in
  patch_2_fuzzy_edit), capped at 1200 chars - the retry loop edits from
  the right region on the first retry
- bash-loop reinject menu is names-only (~300 instead of ~5.2k chars -
  the model already carries the full descriptions in its schema); the
  too-large-read signature outline dropped 200 -> 60 items
- tool descriptions are fitted to the schema budget instead of a
  profile switch (see the budgets section): every tool has a priority,
  core tools are always kept, non-core tools that do not fit even at the
  minimal description level are dropped
- web_fetch has its OWN call budget (webfetch_max_calls, default 20) -
  pure doc-fetching no longer drains the web_search budget
- trafilatura extraction runs in a worker thread - a heavy page no
  longer freezes all streaming on the event loop
- browser click returns only the CHANGED content vs the previous
  snapshot (capped 2000, explicit snapshot for the full page); evaluate
  is capped at 8000 like every other browser action

Tool-pipeline ordering and budgets:
cache hygiene and measurement first):

- measurement basis first: one phase="tools" telemetry row per round
  (tool-result chars before/after the 8k cap, user-role injection
  chars, re-read count, round ms) plus cache_miss flag and shipped
  tools_schema_chars on the coder row - baselines are measurable
  before/after each lever
- real chars/token measured via /tokenize on the running server: 3.92 for a
  code+prose mix, not the 3.0 constant (schema ~4.3k tokens, not 5.6k;
  the chars/4 estimator overestimates by ~30%)
- schema truth from constants: tools/limits.py is the single source,
  read_file's schema text is generated from it and now states the real
  limits (REFUSED above 1200 lines / capped 32000 chars) - the stale
  "capped at 200 lines" text sent small models into 200-line ritual
  reads the handler no longer needs; the write-result BIG_FILE_NOTE
  400-line lie and the handler's stale comments are gone; a new test
  (test_schema_truth) pins handler, constants and schema text together
- run_bash schema slimmed 1006 -> ~590 chars: the PowerShell traps
  (backticks, -e one-liners, stderr, curl -m) moved into the coder
  system prompt's OS hint (once per run instead of once per round in
  the schema; the routing + UTF-16/no-shell-writes core stays in the
  schema)
- compression trigger gap closed: the guard already prefers the
  server's real prompt_tokens (GUARD-REAL-BASIS), but that value is
  one round stale - the chars appended since that POST are now added
  with the measured real/estimator ratio (DELTA-ON-REAL), so a full
  big-write round can no longer slip past the trigger
- prefix-cache hygiene: the reactive-think activation used to rewrite
  EVERY system message mid-run ("<|think_on|>" prefix), which forced a
  full re-prefill on 8 GB Vulkan. Thinking is now re-enabled
  payload-side (chat_template_kwargs keys off think_runtime, overriding
  no-think) and the only in-context change is one appended note; new
  test (test_prefix_stability) pins the append-only behavior

/vram/estimate fix:

- /vram/estimate now resolves _MOE_TABLE with the same exact/-ud/
  prefix logic as vram_of_moe (new moe_cfg_for helper) - the UI
  estimate previously fell through to the dense heuristic and showed
  the dense size for a large MoE where the pre-flight gate used the
  MoE row

Host-RAM caps (the "compression frees RAM, then swap fills" report):

- b11456-era context features defaulted to host-RAM-heavy values: the
  prompt cache could grow to 8192 MiB (idle-slot snapshots are NOT
  cleared with per-slot KV, which is how HiveMind slots run) and each
  slot kept 32 context checkpoints in host RAM. On long installs this
  filled RAM and then swap; a compression rewrites the prefix, so the
  cached old state was dead weight anyway
- slots now start with --cache-ram 1024 and --ctx-checkpoints 4. The
  effective override path is the environment: HIVEMIND_CACHE_RAM_MIB /
  HIVEMIND_CTX_CHECKPOINTS (read in backend/llama_config.py). The settings
  keys llama_cache_ram_mib / llama_ctx_checkpoints are read by server.py,
  but they have no entry in DEFAULT_SETTINGS, no UI field and no
  documentation - they only take effect if written into settings.json by
  hand, and a settings.json entry then also overrides the env variables

task_complete badge honesty ("untested" despite green tests):

- the badge showed the model-supplied build_status default ("untested")
  even when the run itself had green tests on record. The run's own
  test outcome (model-invoked run_tests, auto-test, or a bash run that
  looks like a test invocation) is now tracked per run and upgrades ONLY
  the default: untested -> passing/failing; an explicit model status like
  "blocked" stays

MoE is now a question, not a name guess:

- the 30B+ heuristic silently decided which models get expert offload.
  Model profiles accept "moe": true/false (config > heuristic), the
  config UI asks per model (Auto / MoE / Dense, POST /config/moe-pin)
  and the hint shows whether the default is pinned, table-calibrated
  or name-autodetected

Tool use and context:

- the tool schema is fitted to the context window on EVERY tool path, not
  only in the duo coder: the direct tiers and the tool agent shipped the
  full set (~14.6k chars) against an 8k window before. At 8k they now send
  the tools that fit with the descriptions that fit; at 16k nothing changes
- git_status and git_commit only appear when git is configured for this
  install (git_tools_enabled: auto | true | false, auto = a git credential
  is set). Tools that need no service stay where they are
- a mistyped tool name used to be corrected with the first eight tool names
  alphabetically - a list that contained none of read_file, write_file,
  run_bash, run_tests or task_complete. The correction now names the most
  important tools first and says how many exist
- content masked or deduplicated out of the context is re-readable again:
  the read guard was not released, so a fresh read answered "already read,
  the content is in your context above" while it was gone
- the "image left the window" note is appended once per run instead of on
  every round
- the read ladder ("exploring but not implementing") now fires for a model
  that only reads - 8 files without a single write - and stays silent in
  read-only modes and in the middle of normal multi-file exploration
- chars-per-token is measured on the running server (/tokenize on the exact
  tool schema) instead of assuming the 3.92 default; every cap in the run
  derives from that measurement. Falls back to the per-model EMA
- the injection budget is enforced instead of only logged, so hint text
  cannot stack without a cap
- wedge-handoff repair: the fix agent inherits the coder's context size
  (it used to reload the warm slot) and its output budget is configurable
  (duo_wedge_handoff_max_tokens)
- **Unload coder after run** (Configs tab toggle, `duo_coder_after_run`): OFF leaves
  the coder warm for the next prompt (the Coder TTL decides when it is released),
  ON releases the slot at the end of the run - also on abort - so the next prompt
  can load any model or mode without evicting something first
- `install_package` reports its own failure (`INSTALL_FAILED`, not a `run_bash`
  label) and, when pip has no wheel for the venv's Python, says so with the
  three ways out - the same hint now also fires when pip is run through
  `run_bash`, which is the only path left once the tool list is trimmed

## [1.3.8] - 2026-10-11

Chunking and long-term-memory reliability:

- chunk progress is persisted (resume block) after every completed
  chunk - a hard kill previously lost all in-memory chunk progress
  and a resumed run repeated chunks from zero
- the auto-test anti-spoof check now recognizes bare python test-file
  runs (python3 test_smoke.py counted as "no test run", which stacked
  auto-test + nudge + unknown-runner on top of a passing test)
- read_file limits are char-based instead of the 200/400-line double
  gate - large files no longer force the model to assemble content
  from many small range reads
- memory discipline prompt: the coder must call the remember tool for
  durable facts; plain-text storage claims no longer count as saved
- telegram: planner thinking tail is relayed (💭 Planning: …) when the
  plan lands

## [1.3.7] - 2026-10-11

Telegram mirror visibility + coder vision honesty:

- browser screenshots for coders running without mmproj now state that
  the model cannot view the image and point at snapshot/evaluate text
  output (source of truth: slot.vision_active from /props)
- telegram mirror relays tool calls made before the mirror attached
  (last 6 ride the adoption note, newest last)
- pre-explore partition_done events are relayed (one line per
  partition - early read/search work is no longer invisible)
- per-tick tool-line cap raised from 12 to 20

## [1.3.6] - 2026-10-10

Follow-up fixes:

- write_file calls cut at the output limit are no longer salvaged and
  executed - a half-written file that looks complete caused rewrite
  loops; cut writes are dropped with a clear notice so the model
  re-sends in smaller pieces
- read_file's too-large message now states the file on disk is
  complete, and write results on big files carry a note that full
  reads above 400 lines need line ranges - stops rewrite loops caused
  by partial views of fresh writes
- /stream model overrides now patch the agent card dict as well as the
  flat key; overrides used to silently lose to the card
- ui: finished run journals no longer replay frames over the chat
  transcript, so the end block renders once
- log hygiene: gateway poll lines no longer flood the supervisor log
  (~1.8 MB per two days), the engine httpx log, or the uvicorn access
  log on the console
- VRAM pre-flight gate toggle (settings, on by default): turning it off
  attempts loads the estimate does not trust - real OOM failures drive
  the ctx cascade instead of the estimate
- coder load ctx cascade raised from 3 to 5 attempts (120832 now
  reaches the 8192 floor), and models registered for the new
  K2-Horizon-7B/3.7B and Mellum2.1-12B-A2.5B-Thinking GGUFs

## [1.3.5] - 2026-10-10

Reliability and observability fixes for local runs:

- tool loop requests usage via stream_options.include_usage; llama.cpp
  b11456 ships usage/timings only with this flag, so cache-reuse and
  per-call metrics now arrive on every coder round
- speculative decoding for MTP models: fixed the flag name to
  --spec-draft-n-max and validate it against the binary's --help
  (unsupported binaries start undrafted instead of failing the load)
- VRAM pre-flight: qwen3.5 hybrid KV correction (only 8 of 33 layers
  carry KV, 9.08 KB/token measured) - the old estimate overshot ~2x
  and could block loads that fit
- slot logs: timestamped filenames per server start (no more overwrite)
  and automatic cleanup keeping the newest 10 per port
- telemetry: one JSONL row per coder call under logs/telemetry/
  (prompt/decode timings, cache reuse, time-to-first-byte, compression
  id, prefix divergence index)
- round labels no longer print 'chunk 1/1: None' when a subtask is
  missing
- lint guardrail now fails on unparseable files and scan-count
  mismatches
- Linux install path: windows-only packages are
  platform-gated in the dependency lists, all windows imports are
  guarded, the pre-flight falls back to estimates where live GPU
  counters are unavailable, and the uv-based installer needs nothing
  but curl

## [1.3.4] - 2026-10-08

Follow-up fixes:

- planner parse diagnosis redacts token-like strings in logged heads
- model list collapses same-file aliases (minicpm was listed three
  times for two files)
- git token field: empty field plus saved placeholder instead of the
  cryptic mask; empty and mask-echo saves can never wipe the stored
  token; browser autofill suppressed on the field
- steering divider note placed inside the run block like the steer card
- steer frame: mid-run user messages packaged as the highest
  priority instruction at both injection sites
- tool-round steer drain drops image parts under images_off/preprocess
  like the chunk-boundary drain

- full compression as the shipped default (partial off everywhere)
- re-anchor: current file excerpt on old_text errors + project
  inventory after compression
- loop-detected runs commit and push their written files like
  completed runs
- speculative decoding (draft-mtp) flags for MTP models behind a
  registry guard

## [1.3.3] - 2026-10-07

Telegram gateway hardening:

- takeover relay hardening: fazit on every end path, late-result relay
  for never-adopted runs, silent catch-up on adoption (answer + tool
  dedupe), no more bare "finished" lines
- /gate: G1 force rides body overrides only (mid-run /gate off reaches
  running UI runs); /gate off lifts the toml force floor instead of
  silently degrading phone runs to deny; honest /gate, /help, /status
  texts
- visibility: duo_start model label, ctx meter milestones + compression
  note, file-change lines, coder output deltas, own-path parity, 429
  retry, journal cap resync (logical n + window base), adoption
  state-sync, reattach wording
- ask cards: Yes / No / Wait-later buttons on UI and Telegram (free
  text stays for own commentary); ask cards state they are not the
  approval gate
- coder "takes no images" toggle (duo_coder_images_off); mmproj
  toggles visible in the duo image block
- banner byline back (by: Luzo), /stream 400 on empty body
- git: credentials gate needs Username AND Email - empty git_email
  silently blocked every auto-commit/push

## [1.3.1] - 2026-10-06

Hardening release: 34 fixes across the whole harness, plus the
takeover/steering/vision work that landed during 1.3.0's lifecycle.

### Highlights
- Approval integrity: the approval card now shows the REAL command
  (run_bash's cmd key) instead of a 60-char fallback; expired and
  superseded cards answer honestly on every phone path.
- Slot manager: the steer-image vision upgrade is lock-protected with a
  loading guard and a zombie-slot kill on any start failure; a stale
  ready-event can no longer hand out a dead port; the planner slot is
  truly evicted when the coder needs more context; vision loads of
  unlisted models no longer crash; prefetched/unpinned models age out.
- Containment: browser screenshots are workspace-gated, install_package
  rejects shell-redirect characters, task-path workspaces refuse drive
  roots and files, protected-path matching survives Win32 trailing-dot
  and case tricks, write_file_append checks containment before probing.
- Visibility: takeover runs now deliver the run's RESULT to the phone
  (not just "completed"); ask_user pauses actually pause in direct mode
  (run_id was never propagated); agent-mode OpenAI streaming works
  (frames carried literal backslash-n).
- Hygiene: steer queues / step-skip / pause maps swept at registration
  time, models cache mutates in place, git_token mask can no longer be
  persisted by a stale tab, /vram and first-chat scans off the event
  loop, agent models not on disk wipe to unconfigured at startup.

## [1.2.4] - 2026-10-03

One-click self-updates, a real image pipeline for the agentic duo, and
reliability work on writes, approvals and model loading.

### Highlights
- `update.bat` updates an installation in place from the latest GitHub
  release: automatic backup, release notes with warning surfacing,
  a dependency refresh for the existing .venv, and rollback
  instructions. Downgrades are refused and the download is verified
  against the release tag.
- Images work end to end: an image plan decides per role whether the
  planner/coder run with a vision projector or receive the raw image,
  projectors resolve per model size, and uploads can optionally be kept
  in the workspace. Previews and chat thumbnails open in a zoom
  lightbox.
- Runs survive a page reload or a dropped connection: the UI reattaches
  and replays what it missed, and mid-run steering is wired in.
- Writes: looping output is caught early, truncations are diagnosed and
  salvaged instead of producing broken files.
- Approvals: an unanswered card auto-approves once after a configurable
  timeout instead of blocking the run forever.
- VRAM: a slot that was just killed no longer tricks the pre-flight
  gate into blocking the next load.
- Git: auto-commits and auto-push only run when credentials are
  configured in the Git panel, and the model installer ships a
  projector for every vision-capable model it offers.

### Also
- Code panel tabs with a remembered view mode; reloading a chat
  restores the panel state and tool-call chips.
- Per-instance llama ports, so two HiveMinds can run on one machine.
- New in the installer catalog: Sharp-MiniCPM5-2B. Duo compression
  defaults to Auto (Coder).
- Chat continuity: continuing a loaded chat feeds the model the saved
  conversation (last 40 turns, up to 8k characters each) instead of a
  small sidecar window; complex runs persist their history too; the
  per-chat workspace is stored with the chat and survives reloads; and
  conversation summaries reach the prompt instead of being filtered out.
- Developers: a pre-commit hook ships under `scripts/hooks` (see
  README for the one-time activation).

## [1.3.0] - 2026-10-05

The installation baseline. Everything below is included:
image upload paths (collision-free, real formats), chat continuity
(transcript seeding, token-budget window, summary fix), the hardened
self-updater (including
preview-aware version ordering), rev compare-and-set with server-wins
409, fail-closed approval timeout, the edit shrink guard, planner/
thinking/checklist persistence (small variant, out of the model seed),
one-file-per-chat fork guard with rev-based adoption, a 200 MB save
brake, concurrent-writer coverage.

Also added:
- feature search palette (/ or Ctrl+K in the web UI): indexes every
  panel's sections, toggles, agent cards, settings fields (alias ctx ->
  context) and the seven tabs themselves (soul, tokens -> token budgets,
  chats...); results are mode-independent, a click jumps to the setting
  and reveals mode-hidden wrappers - the startup banner shows a TIP
  line for it
- chat list shows each chat's disk size (json + blobs) in the meta line
- steering with screenshots: /steer accepts images (max 4), injected as
  content parts at the next round boundary - the workaround for images
  falling out of the compressed window
- image path hardening: the direct-chat send gate uses the model
  registry (legacy name matching silently discarded images for every
  shipped vision model), GIF/WebP are re-encoded to JPEG in the browser,
  the flow card refreshes on mode changes
- qwen3.5 MTP models are vision-capable (projector pinned per model)

Not in this release:
- message edit / regenerate controls in the chat UI do not exist yet
  (history edits are only possible via the chats API; the seed path
  handles them, but there is no button for them)
- steering notes, status lines and approval/ask cards are not persisted
  (approval cards recover server-side only while the run lives)
- chat cleanup UI (the sizes are now displayed, but there is no button
  to shrink a chat yet)
- no live warning at the ctx slider when the set ctx cannot fit the
  VRAM budget (the guard only fires when a run starts; the observed
  failure class - 35B @ ctx 71680 with a pinned planner, 9B @ 40960 -
  is only caught there); /vram/estimate ignores pinned slots too, and
  the preflight waits ~18 s on VRAM that will never free instead of
  failing immediately with the concrete numbers
- an orphaned run (browser/tab died) keeps its VRAM until it finishes
  or /abort arrives; server-side continuation works, but UI
  completeness after reconnect (planner parts, thinking foldout) is
  incomplete
- the per-round image trace is observe-only: when an image falls out
  of the compressed window (round 1 carries it, later
  rounds do not), nothing re-attaches it - the coder now gets an
  explicit "[VISION UPDATE]: image no longer in context, do not invent
  details" replacement note plus one visible status line instead
- session compression never runs in agentic chat
  - the summary path is only exercised with agentic_mode off
- the planner's visual details survive the compression summary via
  the anchor (full plan re-injected every compression);
  what the summary does to surrounding context is untested

Partially addressed (known):
- blob versions are add-only until a chat is deleted (no per-chat GC)
- message html/content stay inline in the chat json (the bulk of large
  chats); images double-store (chat + .hive_uploads) when persistence
  is on
- restored tool chips stay silent when their path spelling differs from
  the panel key
- the judge-duo uncompressed re-read gate never triggers (unreachable
  threshold) - judge-routed duo runs keep the compressed window
- the history budget estimator under-counts short messages by ~1.5x
  (measured with the real /tokenize endpoint: 340 chars filler = 131
  real tokens vs 85 estimated); calibration open
- killing llama-server mid-run aborts cleanly (hard_stop /
  runtime_load_error, no loop_detected); what a SUCCESSFUL reload does
  to partial results is untested
- chat continuity: the chat json is re-seeded on every run (edits and
  regenerations included); sidecars written before that fix may contain
  foreign history and still serve as fallback for chats without json
- a future Telegram gateway sharing one server with an open UI would
  multiplex the (global) in-process session memory - needs a per-chat
  session store before that lands

Vision:
- the server's real vision capability is read back from /props after
  every load and stored on the slot; when an image plan says "raw" but
  the loaded model runs without a projector, the run downgrades that
  role to the image description (or skips the image) with a visible
  warning instead of prompting a blind model to describe what it
  cannot see
- the coder's [VISION] prompt note is only added when the plan really
  routes the image to it, and is retracted when the post-load check
  says the projector dropped; the planner note distinguishes whether
  the coder will see the image too
- every model call that carries images logs images_in_request=N (and
  the planner POST logs it too), so "did the model see the image" is
  checkable in the log
- GIF/WebP uploads are re-encoded to JPEG in the browser (first frame)
  before upload - the formats the server-side decoder chokes on no
  longer reach the pipeline; a failed conversion is dropped with a
  visible notice
- release zips embed BUILD_INFO.txt (git describe + commit date) and
  the start banner prints the exact build a zip-installed copy runs
- repo syncs write the same BUILD_INFO.txt into the install, so a
  synced live/test copy can also say which commit it runs
- direct chat mode strips images (with a loud log line) when the
  loaded server reports vision:false instead of sending a request the
  server would reject
- image panel: the duo block carries its own multimodal info line (the
  generic one used to vanish permanently in duo mode); the chat hint
  names the planner first and follows planner/coder model changes live
  without re-attaching the image
- qwen3.5 MTP specs are vision-capable now (projector pinned per
  model); if a projector fails to load, the /props gate downgrades the
  run with a warning instead of pretending

### Telegram gateway (opt-in, new)

Control HiveMind from your phone: plain message in, the PC runs the
agent, status and result come back as Telegram messages. Off by
default — nothing starts, nothing is installed until you run it
yourself. Setup guide with a security walkthrough:
`docs/gateway_setup.md`.

- Pairing is anchored to the PC: a one-time code is printed on the
  gateway console (5 min, single use), `/pair` binds exactly one
  Telegram account, then pairing closes. Wrong tries count on the
  console, five lock it until restart. Nobody pairs from a distance.
- Text runs end to end: the message starts a run in a dedicated `[TG]`
  chat, the conversation is written back into the HiveMind transcript
  (both turns, so the UI shows it and the next run seeds from it),
  status edits update one message instead of streaming, long results
  arrive as a .txt document.
- Commands from the phone: /new, /stop (abort a running job), /status,
  /verbose, /mode (phone-side run mode via the /stream body — the
  browser UI keeps its own mode), /models + /setModel (numbered model
  pick, then one-line preset/ctx answers; ctx overrides ride the run
  body, preset load is global), /cancel, /help. One run at a time — if the browser already runs
  something, the phone gets a busy note instead of a queue.
- Failure is readable: model load blocked by VRAM, engine offline or a
  run error all produce a plain-language message instead of silence.
- Restart behavior: the gateway survives its own crashes without
  double-running anything (Telegram queue position saved before a run
  starts), recognizes an orphaned run at startup and asks the phone
  whether to abort it.
- Security posture: allowlist-only access, no webhook and no open port
  (outbound long-polling only), the bot token is read from the
  environment or the Windows Credential Manager and scrubbed from every
  log line, model/tool output is filtered for secrets before sending,
  a kill-switch file on the PC overrides everything the phone sends,
  and phone paths/usernames are collapsed before they reach Telegram.
- Approval policy for phone runs (/gate in the chat, select in the UI):
  deny = gated calls (shell/write/git) are auto-denied (default);
  ask = they pause the run and arrive as tappable cards on the phone
  (1 once / 2 always-this-chat / 3 deny); off = no gate force.
- Restrict phone runs (default ON): phone runs may only use web and
  read-only tools plus text — the single biggest safety switch.
- Mirror/takeover: a run started in the browser UI appears on the phone
  with tappable approval cards; phone texts steer the run.
- Supervisor: Start/Stop/status for the gateway process live in the UI
  card; one-time token setup via `start_gateway.bat setup` (Credential
  Manager), `start_gateway.bat stop` for detached instances.
- Model selection now also applies to simple/direct runs.

Not included in 1.3: photos and voice messages from the
phone, group chats, queues, token streaming, skills, cron. The bot's
phone-side texts are English.

## [1.2.3] - 2026-09-26

Hotfix: fresh installs crashed on startup before the server ever bound
its port.

### Startup
- The v1.2.2 duo_coder settings preset ships `temperature: null` (the
  coder temperature stays pipeline-controlled by design). On the first
  start, `apply_settings_to_pipeline` hit that null with `float()` during
  the server import: an uncaught `TypeError: float() argument must be a
  string or a real number, not 'NoneType'` killed the process right after
  the start banner printed its localhost link, and the console closed
  before any error could be read. The server never bound its port.
- Agent temperatures of `null` are now skipped instead of cast, keeping
  the pipeline-controlled semantics. Every other agent field is applied
  unchanged and no settings edit is needed: existing settings.json files
  with the shipped null start cleanly.

## [1.2.2] - 2026-09-20

Agentic coder pipeline repair: events, context, approvals, code panel.

### Coder core
- Coder events (tokens, tool chips, results) reach the browser reliably
  again: the SSE queue insert of the event bridge was dead code, runs
  worked server-side while the UI sat on an empty bubble.
- The coder's exec context follows the agentic context setting again
  instead of silently running truncated 8k prompts; empty instant-answer
  rounds from that are gone.
- Hermes Final sampling matches the model card again (seed 42, top_p
  disabled in the agent profile).
- Zero-activity guard: a text-only summary with zero reads and zero
  writes is no longer accepted as run completion. The model could echo
  the plan briefing for three rounds and finish as "completed" without
  touching a file. One targeted start-working nudge is sent first; if it
  keeps talking, the run aborts as a loop instead of faking completion.

### Approvals
- Approval and ask_user scope keyed on the persisted chat id while the
  UI polls with the registered run id. In saved chats the poll missed
  the pending entry, wiped the staged card, and the run waited forever
  behind a stale "Processing context" counter. Scope is run id first
  now; the card handler also clears the stale heartbeat line.

### UI
- Run journal: every SSE frame is journaled per run and replayable. A
  page reload or a silently dropped stream no longer detaches the UI
  from a running run. The frontend reattaches on load and tails the
  journal when the live stream ends without a done event.
- Prefill meter: the coder heartbeat shows real llama-server prefill
  progress (percent, tokens, tok/s), interpolated between the log's
  batch lines and hidden below 3k tokens. Long post-compression
  re-prefills stop looking like a hang.
- Code panel: `file_change` ships again for `replaced` edit results
  (the v1.2.1 result phrasing matched none of the tracker patterns, so
  the File view stayed empty for whole runs), the File view shows the
  streamed text while an edit generates, and every finished chip carries
  its diff id so a click pins that exact call instead of the first one.
- Code panel follows streams strictly on click: a newly streaming write
  updates its tab silently and never switches the view on its own —
  clicking the generating tool call (or its tab) follows the live text,
  opening the panel mid-stream jumps to it.

### Tools and models
- run_bash description warns off shell-written file contents on
  Windows: no heredocs, `>` redirects write UTF-16, backticks are
  PowerShell escapes. File content belongs to write_file/edit_file.
- min_p fix: a declared min_p 0.0 now ships in coder and critic
  payloads. The old guards dropped it, re-enabling the server's 0.05
  default that model cards explicitly warn against.
- The explore meter no longer logs a LOOP-DETECT warning on every
  healthy read round (it reported a streak of 0 at warning level).

## [1.2.1] - 2026-09-19

Pause lifecycle and tool metrics fixes from the first full Linux/Docker E2E pass.

- A resolved pause now leaves the pause registry (`wait_for_resume` cleans
  up after a successful resume, not only on timeout). Until now an
  answered ask_user left a dead entry in `_pause_events`/`_decision_ids`,
  so `/approval/decide` kept routing staged-card decisions into the dead
  pause: the pre-decision was silently lost and the gate paused a second
  time after generation finished.
- Decision endpoints (`/api/run/{id}/resume`, `/approval/decide/{id}`)
  route on a *waiting* pause only (`is_pause_waiting`) instead of bare
  registry membership, so stale entries can no longer swallow fresh
  decisions.
- The destructive gate publishes its pause like the approval gate does
  (`/approval/pending`, kind "ask"). Previously the gate paused without a
  card: the UI poll reported nothing, nobody could answer, and the run sat
  silent for the full pause timeout before declining. Button answers
  ("1", "approve", "once") now count as confirmation.
- Tool chips no longer show a line count next to the path. What a call
  changed lives in the result metrics below (added, removed, changed
  blocks), now including the call's estimated token size.
- Auto-split continuation calls no longer report the marker token's line
  count as if it were content.

## [1.2.0] - 2026-09-19

Action approval gate, websearch reliability, live code panel, Linux fixes.

### Action Approval
- Write file tool calls are reworked for efficiency on local models: a
  call generates exactly once. Approving during generation runs the write
  with zero extra decoding; denying aborts the stream mid-generation. The
  earlier hold design regenerated the entire call after the decision,
  doubling decode time on every approved write_file (a 3k-token write is
  over a minute of extra decoding at 40 tok/s) and a denied hold wasted
  the full generation. This makes large writes viable on small models,
  alongside the existing auto-split and truncation salvage paths.
- Opt in gate (`duo_action_approval_enabled`, toggle in the agentic settings,
  effective mid run): the coder pauses before run_bash, run_python,
  install_package, start_background, file writes and git_commit.
- Buttons: Approve once, Approve and remember (writes per file, commands per
  exact arguments), Deny, Input only, plus an optional note to the agent.
  Chat scoped memory, nothing survives a restart.
- The card appears the moment the tool name streams in and stays pinned at
  the chat bottom while the call generates. Approving during generation
  runs the call without a pause and without regenerating; deny aborts the
  generation immediately. A decision racing the pause setup is consumed
  instead of deadlocking the run.
- Card state survives page reloads (pending approval poll) and clears on
  decision, abort and run end. No silent bypass in until_finished runs;
  pauses and decisions are logged ([APPROVAL] lines).
- Decisions carry the pause's id (decision nonce): a stale tab or a late
  answer for an already resolved pause is dropped instead of silently
  overwriting the next question.
- `ask_user` goes through the same card: question, answer input, buttons for
  sending the answer or proceeding autonomously. In until_finished runs it
  now really pauses instead of silently throttling; the governor (max
  questions per 10 min, timeout auto-answer) stays as the runaway guard.
  A card toggle with a 30s timer auto answers questions while it is on;
  the toggle is a setting (`ask_user_auto_proceed`) and part of presets.

### Websearch
- SearXNG default engines `bing,duckduckgo` (google CAPTCHA suspended,
  wikipedia dead under most tags, github flooded); safesearch on; per
  hostname result cap.
- Budget exhaustion stops the run after 2 consecutive hits instead of
  burning rounds. No result answers carry a reformulation hint.

### UI
- Generated code streams into the right code panel (tab per file, plain
  render, throttled, drag resizable) instead of inline under the tool call.
- Sticky follow for chat and panel: pinned while you sit at the bottom,
  scrolling up pauses, returning resumes. Panel stream survives reloads.
- DIFFSTAT blocks start collapsed and show `@@ line N @@`.
- Tool gen streaming card keyed by tool call index, parallel writes no
  longer mix arguments.

### Fixes
- Error cap `duo_max_tool_errors` (12) decays on successful calls.
- run_python child IO forced to UTF 8, so check and cross marks no longer
  crash on cp1252.
- Browser: repeated navigate attempts to a blocked target escalate after
  the second try.
- start_hivemind.bat restart: stale PID no longer aborts a successful kill.
- run_state refs survive round re creation.
- edit diffs on repeat writes compare against the pre edit content
  (workspace snapshots re capture when the file changed between rounds).
- Tool chips report real line counts; a trailing newline no longer adds a
  phantom line. Pending approval and ask cards pin back to back at the
  chat end when both are pending.

### Linux
- Live VRAM via nvidia-smi (was Windows PDH only). The reclaim wait no
  longer spins its full timeout when no live source exists.
- `HIVEMIND_GPU_BACKEND` beats a `gpu_backend` key in settings.json.
- mlock warns once on Linux; Docker needs `--ulimit memlock=-1` because the
  default 64 MiB limit breaks the model load.
- Port cleanup no longer falls back to a name based pkill that would kill
  every llama-server on the host; the /proc scan stays port scoped.
- run_tests kills the whole test tree on POSIX (`start_new_session` plus
  `killpg`) instead of orphaning pytest and npm children.
- start_background commands run under /bin/bash on POSIX; plain `chmod +x`
  no longer trips the destructive gate there, recursive chmod still does.
- DLL and device probes are Windows only; Linux skips them, which removes
  the false `cudaGetDeviceCount=0` abort in CUDA containers.
- Desktop notifications are a silent no op off Windows.
- run.py error output no longer dies with EOFError without a TTY.

## [1.1.5] - 2026-09-17

UI polish and streaming fix.

### UI

- Reasoning blocks now render ```-fenced code as styled code blocks (was
  plain text) — formatting applies when the block closes.
- Fixed: whitespace-only stream chunks were dropped, gluing words together
  around numbers ("September2026", "um20 Uhr"). Chat history was always
  correct server-side; this was display only.
- vram_guard stop reason now shows as an error with retry buttons instead
  of a green "completed".

## [1.1.4] - 2026-09-13

Tools: 14 fixes across the tool loop, file ops and external tools.

### Verify chain (was effectively dead)

- `verify_last_ok_serial` was bumped by every tool type — the duo verify
  gate, critic veto and final hard-stop could never fire. Only a passing
  run_bash bumps now.
- Verification spoofing closed: echoing `[TEST-RESULT] ✅`/`pytest` into
  run_bash output no longer satisfies the auto-test gate (only real run_tests
  results or test-command evidence count); the exit-code scan takes the LAST
  `[exit code: N]` match; a failing run_tests (❌) counts as a failure.

### Correctness

- Four hint-escalation branches replaced the tool result without appending
  the tool message → dangling tool_call in history (API 400 risk).
- CRLF files are no longer silently rewritten as LF by edit_file /
  patch_file / replace_lines / edit_ast (universal-newline reads fixed).
- write_file_append: a >250k-char remainder was silently truncated with a
  success message; empty content destroyed a pending auto-split remainder.
  Both are honest errors now.
- run_python: non-empty output no longer swallows a non-zero exit (same
  `[exit code: N]` marker as run_bash).
- task_complete "blocked" detection actually matches the handler output.

### Safety

- Browser tool: loopback/private IPs and "localhost" are no longer navigable
  (llama-server, SearXNG, the UI were reachable) — only the tool's own file
  server origin.
- critic_verify now runs with the workspace lock (was unconstrained).
- install_package pip requires a workspace venv (can no longer install into
  HiveMind's own venv).
- test_runner timeout kills the whole process tree (taskkill /T), not just
  the shell; post-eviction read-guard SKIP trap removed; patch_file-branded
  hint no longer fires on unrelated tool failures.

## [1.1.3] - 2026-09-13

Linux native-lib check fix.

### Fixes

- The installer hung forever in "DLL check pending (ggml-vulkan.so)" on
  Linux: the check globbed for a FLAT ggml-vulkan.so next to the binary,
  but the ubuntu tarballs ship lib/libggml-vulkan.so (lib prefix + lib/
  subdir). Both the installer check and the backend runtime probe now
  accept both layouts (rglob); Windows .dll behavior unchanged. Backend
  error message names the correct file per platform.

## [1.1.2] - 2026-09-13

Tree-scout workspace fix.

### Fixes

- The repo map could be built from the WRONG folder: a Windows path inside
  the task text (e.g. copied from another machine) won over the resolved
  UI workspace, and non-existent paths were accepted without an existence
  check. The resolved workspace now always wins when valid; text-extracted
  paths are only a fallback for workspace-less chats and must exist.
  Ignored text paths are logged.

## [1.1.1] - 2026-09-11

Agentic-run fixes, run-state persistence and installer model-setup rework.

### Fixes — agentic runs

- Coder load with a context mismatch (planner slot at a smaller ctx than the
  agentic coder target, e.g. 10240 vs 32768) now reloads the model instead of
  instantly stopping with `loop_detected` after 0 tool rounds.
- Runs without planner and without chunking no longer hit an
  `UnboundLocalError` (`_planner_model`) in the post-explore worker evict.
- The smoke-test nudge (`[AUTO-TEST] No test suite`) fired every tool round
  instead of once per run; TC-DE-NAG and the READ-LADDER counters suffered
  the same per-round reset — all run-state is run-persistent now.

### Model setup

- Curated 7-model download catalog, every entry pinned to an exact Hugging
  Face repo (no fuzzy search — it could pull wrong lookalike repos):
  gemma-4 E4B/E2B it-QAT (UD-Q4_K_XL, mmproj, MTP drafter),
  qwen3.6:35b-a3b-ud, Hermes3.6 Genesis V13 MTP-APEX-Compact,
  qwen3.5:4b-mtp, qwen3.5:2b-mtp, lfm2.5:2.6b (+DSpark drafter).
- A custom models folder (e.g. another drive) is asked for ONCE and kept:
  one pass downloads missing models AND registers the whole folder (the
  installer previously asked twice and could not be used with an own
  folder at all).
- setup_models.bat renders its menu from the catalog (single source of
  truth).
- The desktop-shortcut step now runs BEFORE the long download steps, so a
  first install gets its icon even if the user aborts during the model
  downloads.

### Docs

- architecture.md mermaid fix + freshness pass; README download-catalog
  section rewritten.

### Launcher, settings, thinking toggle

- Windows launcher: the console-kill job object is created with
  `CreateJobObject()` (the previous `CreateJob()` call does not exist in
  pywin32 and crashed the launcher on startup).
- `POST /settings` no longer returns 500 for request bodies without an
  `agents` block (`UnboundLocalError` on the model-sync role list).

### Thinking toggle (planner vs. tool rounds)

- The `--reasoning on/off` server flag is now scoped per model: the duo
  tool-thinking override previously landed on whichever model loaded first
  (typically the planner) and left the coder server at template default —
  so tool rounds thought even with the toggle off, and the planner's
  thinking was suppressed despite being enabled. Now the planner thinks
  and tool rounds don't, exactly as configured.

### Model choice & recommendations (user over policy)

- Explicit model choices from the UI are recorded persistently
  (`agents_user_model_choice`) and always win: the safe-profile policy
  matrix no longer reverts a card to its own model — including when the
  user re-picks the shipped default (the old CARDS-WIN heuristic could not
  express that and silently swapped e.g. duo_coder back to lfm2.5:2.6b).
- Explicit card temperatures are recorded too (`agents_user_sampling_choice`)
  and beat both the policy matrix and the model recommendation.
- On a pure model change the card adopts the model's recommended
  temperature from `model_configs/models/*.json` — the model itself, ctx
  and thinking are never touched by recommendations.
- The safe-profile matrix no longer pushes fixed ctx defaults
  (`duo_coder_ctx_agentic/normal`) or `thinking`/`thinking_budget` onto
  agent cards; ctx is a per-model/per-user decision only.
- New regression suite `tests/test_safe_profile_choice.py` (12 checks)
  registered in `tests/run_regressions.py`.

### Models & defaults

- `qwen3.5:4b-mtp` is the default coder/planner/subagent model (MTP
  speculative decoding) and was added to the model downloader.

### Tool loop & stability

- `patch_file` removed from the model-facing tool list (`edit_file`
  fuzzy-matches SEARCH blocks internally).
- Final-summary accept, escalate-now for <10% partial compressions, adaptive
  no-think retries, per-chunk tool budgets, planner plan re-injection.
- Coder VRAM fallback slot runs at >=20k context; an incompressible round-0
  payload runs with a minimal budget instead of tripping the 3-strike stop.
- Grace round actually runs now; a failing chunk ends itself instead of
  halting the run.
- Browser tool serves workspace `file://` URLs over a loopback HTTP server
  (ES modules/fetch work; everything else stays rejected).

### Launcher & installer

- `start_hivemind.bat` can kill + restart an already-running instance.
- Windows: the whole process tree (python + llama-servers) is bound to a
  kill-on-close job object — closing the console ends the stack.
- Installer: `uv sync` falls back to plain venv + `requirements.txt`,
  parenthesized install paths no longer break the dependency step.

### Language & cleanup

- English-only across MCP tool descriptions, JSON-RPC errors, user-visible
  strings and LLM-facing scaffolding; memory extraction understands English
  phrasing (data keys unchanged).
- Dead settings removed; `hivemind.service` ExecStart points at `.venv`.


## [1.1.0] - 2026-09-09

Context flow and run stability release: compression, planner→coder handoff
and the agentic tool loop got a round of fixes, plus new model support and
installer hardening.

### Context flow & stability

- Compression works now where it silently did nothing before: the cut index
  no longer counts the pinned system prompt, no-ops are logged loudly and
  escalate instead of tripping the fail guard invisibly.
- `duo_partial_compression` is on by default: summary + raw tail keeps the
  llama.cpp prefix cache alive and the raw tail readable for the coder.
- Grace tool rounds, per-chunk budget resets and chunk containment in the
  agentic loop: a chunk that runs out of budget no longer eats the loop's
  detection logic or leaks rounds into the next chunk.
- Deadline grace: a successful write/edit extends the run deadline (+300s),
  so long productive rounds on slow backends are no longer killed mid-task
  while loops still abort on schedule.
- Planner deadline abort is clean: cancelling the planner task at the run
  deadline no longer produces an "InvalidStateError: Result is not set"
  traceback, and falls back to best-effort coding as intended.
- Read ladder path-reset: only repeated reads of the SAME file escalate the
  "exploring but not implementing" hint — reading different files resets the
  counter instead of falsely flagging normal multi-file exploration.
- Real token numbers in the UI context meter (estimated vs. real, per-round
  cache-reuse display).
- Stale-tab protection: settings posts carry a revision; outdated tabs get
  rejected on protected compression keys instead of silently overwriting.
- Default duo coder budget lowered to 8000 max tokens (was 12000): a single
  huge completion round could eat half the post-compression headroom on
  32-60k contexts; big files are written in multiple appends instead.

### Tool calling

- ARG-COMPACT stub now nudges read-before-write; a new STUB-ECHO-GUARD
  rejects write/edit calls that try to write the internal stub back into a
  file (observed with small models).
- Memory/forget keyword routing requires short command-like messages, so a
  long task spec containing "store"/"note"/"delete" is no longer swallowed
  by the memory early-return.

### Models

- Spark-X2.5 (4B/1.7B) profiles and per-model configs, tested against
  llama.cpp b10872+ (thinking + tool calling, ~60 tok/s on Vulkan).
- Ling-3.0-tiny sampling profile per model card.

### UI

- Three compression toggles surfaced (cache-friendly / partial / local-only);
  the absolute "Compression Limit" field was removed — the auto floor (70% of
  ctx) is the baseline, `duo_compress_threshold` stays a settings.json
  override.
- Preset loading is reliable (UTF-8 BOM tolerant) and reports success/failure
  instead of failing silently.

### Installer

- uv-based install on Windows AND Linux (`uv python install 3.14` +
  `uv sync --all-extras`; requirements.txt stays as legacy fallback), with a
  fresh machine needing no preinstalled Python at all.
- Paste-safe interactive answers (pasted multi-line replies no longer crash
  the dependency step with a syntax error).
- `clean_release.bat` strips `.venv` and verifies installer inputs before
  packaging (a zip without `pyproject.toml` used to crash on first run).
- English-only keyword matchers and system-facing strings throughout;
  `AGENTS.md` documents repo conventions.

## [1.0.13] - 2026-09-08

### Added

- `duo_compress_local_only` (default off): skips the compression LLM summary call
  entirely and builds the local fallback summary instead. For setups where that call
  always runs into the read timeout (MoE with CPU experts, slow prefill). Also skips
  the light compressor load and the mini-shrink retry. Combines with
  `duo_partial_compression`.
- Linux/POSIX support for the llama.cpp backend: `gpu_backend` accepts `cpu` (env
  `HIVEMIND_GPU_BACKEND`), platform-aware binary discovery, RAM via /proc/meminfo,
  port-kill chain fuser -> /proc inode scan -> pkill, `.so` probe on POSIX.
- CPU backend load path: `--n-gpu-layers 0`, no `--device`, VRAM pre-flight
  short-circuited, CPU-count thread defaults on POSIX (Windows keeps 16/8).
- `deploy/fetch_llamacpp.py` downloads linux assets (ubuntu vulkan/cpu/rocm, zip and
  tar.gz, chmod +x); rocm regex accepts versioned names (`rocm-10.0-x64`).
- POSIX tool-command ladder (`hive_functions/language_config.py`): `python` -> `python3`,
  PowerShell pipe cmdlets -> `head`/`tail`.
- `deploy/install_linux.sh` (system deps, venv, llama.cpp fetch, systemd) +
  `deploy/hivemind.service`; README linux sections.
- `tests/test_linux_paths.py`.
- Model config `hermes3.6_35b-a3b-uncensored-genesis-v13-mtp-apex-compact.json`
  (mirror of the v12 config, `mtp: true`).
- `llama_ubatch_size` setting (default 256), wired through to `--ubatch-size`. Larger
  values (512/1024) speed up prefill for MoE models with CPU expert offload at the
  cost of a bigger compute buffer.

### Fixed

- The installer never accepted downloaded DLLs (release blocker):
  `_verify_backend_dlls` checked `ggml-vulkan` without the `.dll` extension on Windows,
  so every vulkan/cuda install failed at step 4 and deleted the intact build. Now:
  correct extension via directory enumeration (survives antivirus scanning fresh
  files), ~90s retry window, archive cross-check (a quarantined DLL keeps the build
  and prints restore instructions instead of deleting), and no more HTTP 416 when the
  temp file is already fully downloaded.
- The documented per-model `sampling` block in `model_configs/models/*.json` is now
  applied at runtime (`models_registry.get_sampling`, fallback in
  `core/model_sampling.py`). It previously had no effect; `_model_sampling_overrides`
  still wins when set.
- Documentation brought in line with the code: README no longer claims `Auto` is the default
  mode, that AutoMap is judge-driven, that the custom wizard collects `sampling`, that
  `GITHUB_TOKEN` or `SEARXNG_SETTINGS_PATH` exist, or a `--no-vulkan` flag. Tool table
  completed, tool-tier values documented.
- `direct_tools_tier` accepts the `websearch` alias (a literal `websearch` value used
  to disable all tools).
- `docs/settings.md` regenerated (197 keys, was stale at 189).
- Four reachable NameErrors (found by the ruff gate): missing `import re` in
  `core/duo/_pre_explore.py`, undefined `RE_THINK_CLEANUP` in `core/duo_helpers.py`,
  missing `save_learned_config` import in `learning/peer_ratings.py`, and
  `llm_read_timeout` never passed to the pre-explore workers.
- Lint guardrails: `ruff.toml` (E9, F821, F601, F811, F841, W605) enforced by
  `tests/test_lint.py`, plus `tests/test_no_new_silent_excepts.py` with a frozen AST
  baseline that fails on new silent except handlers.
- User-visible strings (SSE status, model-load errors, RuntimeErrors) translated to
  english across core/, backend/, hive_functions/, tools/ and app.js. Functional
  german intent keywords stay on purpose.
- `duo_compress_auto_floor` default 0.78 -> 0.70: the request overflow zone starts at
  ~72-73% of ctx (prompt + clamped max_tokens + template overhead), so the old floor
  forced `trigger=force` compressions through it, each costing a full cache-losing
  re-prefill.
- Silent CPU fallback no longer applies to coder-class models: the VRAM threshold for
  the one-time `--n-gpu-layers 0` retry dropped from 5.0 to 3.0 GB. The coder sat just
  under 5 GB and could reload fully on CPU (~1-2 tok/s) instead of failing loudly.

### Removed

- Dead setting `automap_mode` (defined, documented, never read).

### Tests

- Registered the unregistered suites `auto_split_pending`, `ctx_guard`,
  `planner_model_persist`, `read_only_detect`, `tool_arg_compact` plus the new
  `lint` and `no_new_silent_excepts`; sampling-override coverage in
  `test_models_registry.py`.

## [1.0.12] - 2026-09-06

### Added

- Token estimator calibrated to 3.0 chars/token (`utils/token.py CHARS_PER_TOKEN`),
  shared by ctx guard, UI meters and compression notices; the UI shows integers now.
- `clamp_request_max_tokens` (`context/ctx_guard.py`): each tool round clamps
  `max_tokens` so prompt + output fits the ctx. The compression threshold is
  floor-driven again (`duo_compress_auto_floor`) instead of binding to the full output
  reserve.
- `duo_compress_model` (default `lfm2.5:2.6b`): compressions run on a small model when
  it fits without evicting the coder, otherwise on the coder. Read timeout configurable
  via `duo_compress_llm_timeout_s` (default 180).
- `ling-3.0-tiny` as recommended low-resource coder (registry config, downloader entry,
  setup menu, README).

### Changed

- Recommended coder/hermes model switched to `qwen3.6:...-genesis-final-apex-compact`
  (~17 GB, MTP, 35 CPU experts) — replaces hermes3.6 v12 in the downloader, setup menu,
  README and per-model config.
- New default `duo_compress_auto_floor = 0.78`.

### Fixed

- Compression notices show integer estimates instead of raw float values.

## [1.0.11] - 2026-09-04

### Added

- Cache-friendly context management for long runs (`context/ctx_guard.py`,
  `core/duo_runner.py`): the llama.cpp prefix cache stays alive between compressions.
  Auto threshold is `min(auto_floor*ctx, ctx - reserve)`; in-place recall-marker
  eviction only as emergency (>90%, no compression left). New settings:
  `duo_cache_friendly_ctx`, `duo_partial_compression`, `duo_compress_auto_floor`,
  `duo_compress_overflow_reserve`, `duo_min_free_ctx_tokens`, `duo_max_compressions`.
- `duo_partial_compression` (default off): only the older part of the history is
  condensed, the recent tail stays byte-identical so KV-shift reuse can salvage the
  suffix after a rebuild.
- `[CACHE] prompt/cached/reuse%` telemetry per coder round plus compression and
  eviction counters.
- `deploy/analyze_cache_log.py` (A/B analyzer over hivemind.log) and
  `deploy/ab_phase1.ps1` (live A/B toggle + log snapshot).

### Changed

- Compression is the primary shrink, in-place eviction the exception. The tool history
  is append-only between compressions, so `[CACHE] reuse` stays ~100%.
- Rule-based compression fallback: if the LLM summary fails validation, a deterministic
  pass replaces old tool outputs with recall markers instead of keeping full context.
- AUTO-SPLIT continuation marker detection is robust: quotes/backticks, short replies,
  literal marker lines at the file end get healed, pending keys canonicalised.
- Presets save and load the coder context deterministically (overlay body + UI flush
  before save, no more race with the debounced settings POST).
- `models_dir` is never stored into or restored from presets; the safe-profile matrix
  no longer overwrites explicitly set `duo_coder_ctx_*`.
- Compression HTTP timeouts raised (connect 10s, read 120s).
- UI: compression label/help in english, app.js cache-busted.
- Context compaction of executed write calls (`core/tool_executor.py`): after a
  successful oversized write, the unsent tool-call arguments are replaced with a stub
  (path + arg chars + sha1 prefix). The prefix cache stays alive and ~20-30k tokens no
  longer sit in the context until the next compression.

### Fixed

- Oversized `write_file`/`write_file_append` with a quoted AUTO-SPLIT marker no longer
  truncates files by writing the marker as literal content.
- Preset coder context no longer resets to the safe-profile default after reload.
- Long runs no longer degrade into per-round full re-prefills in cache-friendly mode.
- Post-run insight extraction no longer stalls up to 240s loading a small model while
  the evicted big model still holds RAM; skipped entirely when no model is loaded.
- `read_file` aborts oversized full reads early (binary sniff + streaming newline
  count) instead of reading the whole file just to answer FILE_TOO_LARGE.
- Transport-level stream drops show a neutral "run stopped / possibly parked" notice
  instead of a red model error.
- Skip during the planner aborts the planner and starts the coder (previously a long
  planner finished and the run ended with zero coder output). Skip on a single
  non-chunk round is ignored.

## [1.0.10] - 2026-09-04

### Changed

- Chat template updated to qwen3.8-froggeric v22.5 (`model_configs/chat_template22.5.jinja`,
  versioned filename so you can tell the template version at a glance). 22.4 removed;
  the qwen3.5/3.6 and hermes3.6 configs plus the code fallback point at it.

## [1.0.9] - 2026-09-03

### Added

- Server-side AUTO-SPLIT for oversized writes (`tools/handlers/file_ops.py`): when a
  write exceeds the per-call char limit, the leading chunk is written immediately and
  the remainder is cached server-side. The model finishes with one tiny
  `write_file_append(path, content="<AUTO_SPLIT_CONTINUE>")` call instead of
  regenerating everything. Pending entries expire after 5 minutes.
- The last explicitly loaded preset is applied again on startup.
- Read-only detection understands content constraints (`core/duo_helpers.py`):
  `is_read_only_request()` needs a read-only phrase AND no non-negated implementation
  intent, so "DO NOT MODIFY" inside a build task no longer disables the tool round.

### Changed

- Coder WRITE RULES + the dynamic char cap are injected into the system prompt; tool
  schemas advertise `maxLength: 20000` and the AUTO-SPLIT marker; `write_file_append`
  appends received content in one go.

### Removed

- Tiel-Coder model recommendations and their default configs (downloader, setup menu,
  README table, per-model configs, launch defaults).

### Fixed

- Context compression crash: the empty-history early return handed back a 2-tuple
  where the caller unpacks 3, mis-stamped as loop_detected and hard-stopped healthy
  runs.
- The planner model was wiped to None on every settings reload, silently falling back
  to the coder model. Stored value persists now.

## [1.0.7] - 2026-09-02

### Added

- Tool-loop bash reinjection: repeated `run_bash` loops (3x identical, ABAB, ABCABC)
  no longer abort the run. The available toolset is re-injected as a menu message and
  the loop signature resets so the model can pick a different tool. Non-bash loops
  keep the abort; the round budget still bounds everything.
- Tiel-Coder 35B-A3B MTP models (Compact ~18 GB, APEX ~26 GB) as recommended downloads
  with per-model configs: `--jinja` + embedded template, full offload, temp 1.0 /
  top_p 0.95 / top_k 20 (0.6 agentic), vision via the shared mmproj.

### Fixed

- Safe-profile policy no longer overwrites the user's saved `vram_budget_gb` on every
  reload (user-preference key, policy value is informational).

## [1.0.6] - 2026-09-02

### Changed

- MoE CPU experts dropdown lists only installed models; `-ud` variants stored/read via
  the backend-compatible key so overrides actually apply.
- Hermes3.6 Genesis V12 MTP-APEX-Compact is a recommended downloader model again
  (+mmproj, per-model config ships with the release).
- Optional desktop shortcut (`create_shortcut.bat`, installer step 7/7), custom name
  via argument.

## [1.0.5] - 2026-09-02

### Added

- Presets are back: save/load/delete named configurations incl. per-agent prompts.
  Load replaces the whole configuration, only on an explicit click. No auto-load at
  startup, no preset override in the agentic coder, `git_token` never stored.

### Changed

- Agentic/duo context is user-controlled: the per-agent Context setting is honoured in
  the duo/agentic paths, the 16k agentic floor is a minimum only, slider goes to 128k.
- `pip` installs use `python -m pip` so a stale pip.exe cannot fail silently.
- Tool-thinking off by default (`duo_coder_tool_thinking_auto_mode` = off).

### Fixed

- Duo/agentic context honours a user-set override even when a persisted duo_* default
  exists.
- The llama.cpp updater fetches the newest nightly straight from the releases list
  (the old nightly-tag.txt pointer lagged ~100 builds); transient failures print a
  clear message instead of a traceback.
- Duo context inputs persist while typing; the effective context is logged
  (`[CTX-EFFECTIVE]`).
- The context chosen in the frontend is sent with the /stream request and applied to
  that run, so display and reality can't drift apart.
- `NameError: workspace_lock` in `_inject_tool_error_hints` crashed every tool round
  that hit an `EDIT_FILE_NO_BLOCKS_APPLIED` hint.
- MoE CPU experts dropdown shows the full model key (no ambiguous duplicates).
- CUDA nightlies (b10760+) split the runtime into
  `cudart-llama-bin-win-cuda-<ver>-x64.zip`; the updater downloads and places it next
  to llama-server.exe.

## [1.0.4] - 2026-09-02

### Added

- Direct-chat tool tiers redesigned: off -> Websearch (`web_search`/`web_fetch` only)
  -> read + python -> full.
- `web_fetch` sends a real browser User-Agent to reduce 403s; strict sites may still
  block, prefer `web_search` there.
- Semantic LRU reclaims more context: stale `read_file` outputs are evicted after a
  successful write to the same path, repeated full reads dedupe to the newest copy,
  path-less outputs age out via half-rate TTL decay (errors keep double TTL).

### Fixed

- run.py missing-file guard blocked fresh installs after the pre_explore module became
  a package ("Missing files: hive_functions/pre_explore.py").
- Agentic coder crashed on every code_duo run: the extracted pre-explore phases lost
  several locals, now read from state.
- 8 GB setup works out of the box: direct/duo_coder default to `lfm2.5:2.6b`,
  `default_8gb_v1` no longer forces the 9B model, agent-card choice always wins.
- VRAM pre-flight suggests fitting models instead of a generic hint.
- llama.cpp download is resumable + CRC-verified; installer survives parenthesized
  install folders.

## [1.0.3] - 2026-09-01

### Fixed

- Installer crashed silently when the install folder contained parentheses (cmd
  for-block parsing); the recursive checks now run via PowerShell with the path from
  an env var.
- SearXNG stale-container bind-mount error survived `docker rm`; the compose fallback
  runs `docker compose down --remove-orphans` before retrying.
- The installer window could close without a message: steps are logged to
  install.log, downloads are wrapped in try/except, all installer scripts have a
  top-level crash guard.
- Safe-profile policy reset `vram_budget_gb` 8.0 -> 7.5 on every start; the policy
  value is a safety ceiling now, an explicitly set higher budget wins.
- PRE-FLIGHT VRAM block on high context: before failing, the loader auto-downgrades
  the context (16384 -> 8192 -> 4096) and rewrites --ctx-size.
- The updater failed on freshly-published nightlies; it now only picks a build that
  already exposes a matching asset.
- Agent-card temperature was overwritten by the model sampling profile; an explicit
  agent temperature wins now.
- "Run completed" divider is a subtle line in simple chat, full marker elsewhere.
- Agent-card context slider cap raised 32k -> 65536.

### Changed

- Hermes3.6 V12 compact config aligned to the qwen3.5:9b-ud launch parameters
  (num_ctx 4096, duo_coder 8192) so the 35B MoE runs on 8 GB.
- Sampling profiles aligned to the official Qwen model cards.

## [1.0.2] - 2026-09-01

### Added

- `tests/test_release_integrity.py`: missing-file check accepts the pre_explore
  package layout, searxng engine lists are consistent across settings/websearch/yml,
  the SearXNG config is valid YAML with a secret placeholder.

### Fixed

- "models folder missing" note suppressed when `models_dir` is configured.
- VRAM budget display syncs with the server response after save.
- MoE dropdown deduplicated by base name.
- SearXNG install on a stale container: `--force-recreate`, remove + retry on failure.
- run.py guard accepts `hive_functions/pre_explore/` as a package.

### Changed

- Default engine list: brave, bing, github, wikipedia, mojeek, stackoverflow, pypi
  (google removed for captcha/rate-limit reasons, mojeek added).
- Websearch toggle checks status immediately; host input read live.
- Preload defaults off (`startup_preload_enabled`, `startup_preload_judge_in_agentic`,
  `smart_preload_enabled`, `judge_keepalive_enabled`).
- Configs tab regrouped into Model Preload & VRAM / Learning & Model Config / System;
  the duplicated git toggle in the Agents tab is gone.
- "Use Presets" panel removed from the agentic coder; `duo_runtime_profile` stays
  "balanced" as the backend default.
- Composer tool-status line removed; the header Chat-Tools badge is the indicator.

## [1.0.1] - 2026-09-01

### Added

- `deploy/backup_data.py`: backs up sessions/, memory.json, soul.json, presets.json,
  settings.json, learned configs, learning_logs to backups/<timestamp>/.
- LICENSE (BUSL 1.1) and this changelog.
- Sandbox hardening: the Windows Job Object also caps job memory and process count
  (`duo_tool_sandbox_max_mem_mb`, `duo_tool_sandbox_max_procs`); POSIX subprocesses
  run in their own process group and die via killpg.
- Hermes3.6 Genesis V12 MTP-APEX-Compact in the downloader + per-model config; strict
  producer sampling (temperature + top_k only, everything else disabled).
- searxng_repair.bat: auto-starts Docker, recreates the container, verifies /healthz,
  rebuilds on a stale baked-in config.
- SearXNG settings installed to /usr/local/searxng/settings-hivemind.yml and selected
  via SEARXNG_SETTINGS_PATH — the /etc/searxng volume shadowed the baked config and
  caused HTTP 500 / KeyError: default_doi_resolver.
- Per-model sampling stats: `sampling` block in model_configs/models/*.json keyed by
  the runtime mode keys, highest priority over the built-in family profiles,
  collected by the custom-model wizard.
- `seed` support in the OpenAI payload builders.
- `GET /health`, requirements-dev.txt, generated docs/settings.md, 8 new regression
  suites (17 total).

### Changed

- Memory and registry writes are atomic now, persistence errors logged not swallowed.
- Dead env var `HIVEMIND_AUTO_RESUME` removed.
- server<->state consolidation: 7 duplicated helpers removed, canonical versions live
  in core/state.py; backend config sync lives in `server._sync_backend_runtime_config`.
- Pre-explore fallback defaults aligned with settings.py DEFAULT_SETTINGS.
- 7 previously hidden settings keys are explicit in DEFAULT_SETTINGS.

### Removed

- ~40 dead imports from server.py and core/duo_runner.py (AST-verified unused).

### Fixed

- parse_tool_error leaked the [TOOL_ERROR_META] block in format-B messages.
- UTF-8 BOM removed from 5 files (breaks AST tooling).
- Mojibake fix in the 3 functional regex lines of server.py (double-encoded UTF-8) —
  german greetings/questions match again.
- `_model_profile` consolidated into core/model_sampling (incl. user-config merge).
- README staleness (fallback model 4b-ud, planner thinking budget 8000).

### Refactored

- server.py 3000 -> 1573 lines in stages: tools/handlers package, pre_explore package,
  chat_util/security/log_noise extractions, run_stream -> core/chat_run.py.
- backend/llama_server_manager.py (2406 lines) split into llama_manager_utils,
  llama_slots and 5 mixins (load/evict/process/prefetch/health).
- execute_tool_round: helpers moved to core/tool_exec_helpers.py, mutable slots boxed
  into a ToolRoundState dataclass (32 -> 16 parameters).
- pre_explore sub-phases extracted as functions.
- Fixed along the way: git_commit referenced workspace_lock instead of _workspace_lock
  and crashed with a NameError whenever git was available.
