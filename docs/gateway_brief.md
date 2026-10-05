# GATEWAY BRIEF: Telegram Gateway for HiveMind 1.3 (safe, maintainable, minimal)

File: `docs/gateway_brief.md` (UTF-8). This is the single valid assignment.
If it contradicts earlier chat messages, this file wins.

## GOAL

The owner uses HiveMind from their phone: text and photos in, status and
final result out, approvals via inline button. Optional (WP5): steering.
Safety and maintainability over feature count. When in doubt: leave it out
or fail-closed.

## RELEASE CUT

- The gateway ships in 1.3 as OPT-IN: off by default, no autostart, no
  entry in start scripts.
- WP0-WP4 and WP6 are mandatory for 1.3. WP5 (photos/steering) ships too
  only if everything else is proven with real runs by then; otherwise 1.3.1.
- A small proven gateway beats a large untested one.
- If a work package endangers the release date, say so in the report.
  Never cut security requirements to save time.

## KNOWLEDGE BASE

Everything about the HiveMind core in this brief comes from earlier
reports and is NOT verified. Line numbers are rough; search for symbols.
WP0 verifies every claim against code and experiments. If reality
diverges, report it and do not build on the assumption.

## DIVISION OF LABOR

- You have no Telegram access as a user. Only the owner can message the
  bot. No userbot (Telethon, MTProto user session, etc.).
- "Real run" means: the owner types on their phone following your test
  script (numbered steps: what to send, what to expect). Afterwards you
  evaluate the audit log and HiveMind log and quote the lines.
- Tests with unknown users and group chats need a second account / a test
  group from the owner. Until then: fake-tested only, marked as such.
- You never receive the bot token in chat. The owner sets it as an
  environment variable. Never in reports, logs, commits, or test output.

## ENVIRONMENT

- You work alone on the gateway. Base is main: get the base commit with
  `git rev-parse HEAD` when creating the worktree and name it in your
  first report. (It has not been fully accepted in the browser yet; write
  that into the report.)
- Branch `feature/telegram-gateway` in the worktree `repo_gateway`, so
  main stays release-ready. Gateway commits land on main only after the
  owner's go. Core changes only after go, as separate commits on main;
  afterwards rebase the gateway branch.
- The branch `s4a-archiv` is discarded and stays untouched.
- Live (127.0.0.1:8001, 8 GB VRAM) is shared with the owner. Before every
  start, restart, or run: say what you intend and wait for approval.
  Never while the owner is working live.
- Subagents/workflows: allowed for pure modules without GPU (auth, render,
  state, approvals, config) and for reviews. Never for runs against live.
  A review is a hint, not proof; the proof is the real run.
- One running gateway process per bot token. Before every start, verify no
  old instance is running.
- No changes to live/settings.json or live/presets.json without an
  explicit note. POST /settings persists immediately and is taboo for the
  gateway. Before every real run, ask the owner for a sessions/ backup.
- Never push without an explicit go.
- Windows: bat files ASCII + CRLF; no patch heredocs in bash (escapes
  break), edit files with the Edit tool; patch scripts abort loudly on
  every error, the next step only runs on success.
- Check the pre-commit hook in the worktree (core.hooksPath scripts/hooks,
  ruff as a module). A red commit is a process error.

## WORKING RULES

- One commit per WP. After each WP: STOP, report, wait for the go.
- "Done" only with: raw `git show --stat` (not retyped), grep lines of the
  changed spots, test status (python tests/run_regressions.py plus gateway
  suites) and, where the WP requires it, evidence lines from a real run.
- Every report ends with "NOT tested:" and "Open questions:".
- Core changes only where a WP names them. Need another one: STOP, written
  proposal (problem, change, risk), separate commit only after go. Never
  silently.
- No `except Exception`, no bare excepts. The lint gate
  no_new_silent_excepts MUST include hivemind_gateway/; prove that in WP1
  with an intentionally violating test case.
- pyright for every changed core file: 0 possibly-unbound.
- New tests as tests/test_<name>.py, registered in
  tests/run_regressions.py. No network access in the suite.
- If you find your own bug, say so in the report, with evidence. No silent
  fixes.

## ARCHITECTURE

- Own package hivemind_gateway/, own process, NOT inside the HiveMind
  server process. A gateway crash must not touch HiveMind.
- Modules: config, auth (pure), telegram_api (thin client), hive_client
  (SSE + endpoints), approvals (nonce store), render (splitting/escaping,
  pure), state (atomic JSON), commands, main.
- Write the Telegram client yourself (httpx; only getUpdates, sendMessage,
  editMessageText, answerCallbackQuery, getFile, sendDocument). No aiogram:
  fewer dependencies, smaller attack surface. Justify a different decision
  in the report. Check whether httpx is already a HiveMind dependency.
- Dependencies pinned (lockfile with hashes), pip-audit in the release
  check.
- gateway.toml contains no secrets and is validated at startup (fail
  fast). Only gateway.toml.example lives in the repo.
- Exactly ONE function send() through which everything goes to Telegram.
  It aborts when chat_id != owner_chat_id. Central invariant + test.
- At startup: read /health and check compatibility. The version string
  ("1.3.0-preview", git describe) is not a contract. Propose an
  api_version field in WP0 (core change only after go). Until then:
  refuse to start on unknown response shapes.

## CORE ASSUMPTIONS (unverified; WP0 verifies everything)

- POST /stream: payload {q, images: [base64], mode, chat_id}, response is
  SSE with run_id, status, planner_*, duo_coder, token, tool_call,
  tool_result, file_change, usage_meta, done (stop_reason), error.
- POST /api/run/{run_id}/steer with {text, images (max 4)}; injection at
  round boundaries. Assumed: duo-agentic and direct-with-tools yes;
  pipeline, direct-without-tools, planner phase no or delayed.
- POST /abort with {chat_id}.
- Chats: GET/POST /chats, GET/PUT /chats/{id} (rev, base_rev, 409 = server
  wins), search.
- Approvals: endpoints, SSE event of the request, decision route,
  default timeout (assumed 0 = unlimited).
- run_active and busy behavior on a second /stream.
- Server bind address.
- TRANSCRIPT (decided; WP0 verifies only): POST /chats creates the main
  json with 0 messages. A /stream run writes only the sidecar
  (.context.json) at completion. The gateway writes the transcript itself
  via PUT /chats/{id} with base_rev (409 = server wins):
  - User turn BEFORE the /stream start (the seed drops trailing user
    messages; the prompt arrives via q), assistant turn after done.
  - Message shape exactly as the UI writes it: read fields from a real
    UI chat, do not guess. The chat must remain loadable in the UI.
- SEED LABEL: history_seed falls back to the sidecar internally when the
  main json carries no turns. Since the provenance fix the label is
  honest (json | sidecar | none). The glob exclusion in _read_chat_json
  was a no-op and never the cause.

## SECURITY (mandatory, every item with a test)

### Authentication
- Whitelist on from.id, only chat.type == private, for ALL update types:
  message, edited_message, callback_query. Everything else silently
  dropped, rate-limited log only. No answer to strangers.
- Pairing: at first start the gateway generates a one-time code (>= 8
  chars, base32), ONLY on the local console. /pair <code> binds the owner
  id. Valid 5 minutes, single use, constant-time comparison. Max 5 failed
  attempts, then pairing locked until restart. Every attempt into the
  audit log. After successful pairing, pairing is disabled. The owner id
  lives in the gateway state file.

### Replay and backlog
- At startup, drop the backlog (offset to the newest).
- At runtime, drop updates older than 60 s (message.date).
- Dedupe by update_id. Persist the offset atomically (tmp + os.replace
  with retry, Windows). Runs are at-most-once: persist the offset BEFORE
  the run starts.
- Forwarded messages are never parsed as commands.

### Secrets
- Bot token only from environment variable or Windows Credential Manager
  (keyring), never from settings.json, presets, logs, chats, backups.
- Logging with a redaction filter (the token sits in the Telegram URLs!).
  Test: grep over all logs for the token pattern must be empty.
- State file, gateway.toml, audit log, gateway.disabled and the token
  source: in .gitignore, in the updater protect list (update.bat /XF resp.
  /XD) and in the leak check of deploy/package_release.bat.
- State and token paths live outside every workspace. Test in WP1 whether
  a HiveMind read tool can read the state path; rejected, or documented
  as residual risk.

### Network
- Outbound long-polling only, no webhook, no open port.
- The gateway talks to HiveMind via 127.0.0.1 only. At startup verify
  HiveMind does not listen on 0.0.0.0, otherwise refuse to start.

### Approvals (the riskiest feature)
- callback_data is a short random nonce, never a tool or command.
  Server-side map: nonce -> {approval_id, chat_id, tool_call_hash,
  expires, message_id}, single use.
- On callback verify: from.id, chat_id, nonce, expiry, hash.
- Display: full command including tool and working directory, as a
  document when too long, never truncated. Afterwards edit the message
  ("approved/denied at T"), buttons removed.
- Expiry is mandatory and owned by the gateway: own limit from
  gateway.toml (default 120 s). On expiry the gateway itself SENDS deny
  to HiveMind. Removing buttons is not enough: the run would hang and
  hold VRAM. The HiveMind timeout is only an upper bound.
- NO "always" / "remember this call" from the phone. The gateway never
  loosens a HiveMind gate rule.
- Until WP4 is standing, the gateway answers every approval request with
  deny and says so in the chat. WP0 verifies the gateway can send deny
  that way.

### Inputs
- Command whitelist: /start /pair /new /stop /status /verbose /lock /help,
  plus /steer from WP5, optional /get <path> (path allowlist inside the
  workspace, default OFF). Nothing touching a shell. Commands parsed only
  from own user messages, never from model or tool output.
- Limits: max text length, rate limit per user. Non-text messages (photo,
  document, voice, sticker) rejected with a note until WP5.
- One run at a time (8 GB VRAM system). If one is running (including from
  the UI), a normal message is rejected with a note. No queue. Prefer a
  server-side busy guard (409) over check-then-start in the gateway (core
  proposal in WP0).
- A normal message during a run is NOT a steer (from WP5).

### Output
- Model and tool output is untrusted: plain text as default. parse_mode
  only via a tested escape function. Link previews off. No workspace files
  sent except via /get with a path allowlist.
- Secret filter before sending (token/key patterns, .env contents),
  configurable.
- By default only status and final result. Thinking and planner only with
  /verbose. A final result longer than 3 messages goes out as a .txt
  document. Splitting at 4096 chars at line boundaries, code blocks kept
  balanced.
- Errors must arrive readable on the phone: VRAM preflight block,
  model_load_failed, server offline, run error. Silence is the worst
  behavior. Tests for that in WP2.

### Operations
- Audit log (JSONL, rotating, no secrets): every update (user id, type,
  hash instead of text), every approval decision, every run start/stop,
  every pairing attempt.
- Kill switch: file gateway.disabled or env variable. /lock from the
  phone locks; unlocking happens only at the PC.
- Fail-closed on ambiguity. HiveMind unreachable => "offline", no
  automatic retry that could duplicate runs.
- Restart with a running run: persist run_id and open approvals in state.
  At startup check run_active, set open approvals to deny, and ask the
  owner whether the orphaned run should be aborted. Never restart
  automatically.

## PERSISTENCE AND STREAMING

- One HiveMind chat per Telegram chat (title prefix "[TG]"), created once
  via POST /chats, mapping persisted atomically. Never share the same
  chat with an open UI tab.
- No token streaming. Status updates via editMessageText at most every
  3-5 s, respect 429/retry_after.
- /stop calls /abort. An orphaned run holds VRAM, so /stop must fail
  visibly on errors.

## RELEASE INTEGRATION (part of WP6, plan early)

- No autostart: start_gateway.bat (ASCII, CRLF, covered by
  test_bat_encoding) is started only by the owner.
- deploy/package_release.bat ships hivemind_gateway/, the lockfile and
  gateway.toml.example; NEVER gateway.toml, state, audit log,
  gateway.disabled or the token. The leak check refuses such zips.
- update.bat protects these files on apply (/XF) and prune.
- Dependencies installed only when the owner activates the gateway,
  unless httpx is a dependency anyway.
- README section: BotFather, token as env variable, pairing, rotation
  (/revoke), recovery, kill switch, Telegram account with 2FA, note that
  bot chats are not end-to-end encrypted and code and tool output travel
  over Telegram servers.
- CHANGELOG entry with "not included" (skills, cron, groups, voice,
  queue, possibly WP5).
- A fresh install without gateway configuration behaves as before. Test:
  server starts, regression green, no gateway process.

## TESTS

- Pure functions (auth, render, approvals, state, config) as unit tests.
- Fake Telegram server and fake HiveMind (SSE recorder).
- Security table as tests: unknown user (message AND callback), group
  chat, old update, repeated callback, expired callback (gateway sends
  deny), wrong chat, token in logs, flood, pairing with 6 failed
  attempts, restart during run (no double start, open approval becomes
  deny), 409 on chat write; from WP5: oversized photo, album with 5
  images.
- Invariant: no code path sends to a chat_id other than the owner's.
- At least one real run per WP where the WP names it.

## WORK PACKAGES (after each: stop, report, owner's go)

WP0  Read-only plus one experiment. Result: docs/gateway_contract.md with
     file:symbol:line for: /stream (payload, SSE events), /steer
     (including mode matrix), /abort, /chats (including rev/409),
     approvals (event, decide route, timeout, can the gateway send deny),
     run_active/busy, /health, bind address, preflight and
     model_load_failed error shape in SSE. Plus the transcript experiment
     (with live approval): POST -> /stream with chat_id -> GET shows the
     turn; first turn with N=0 and source=json, second turn with N=2;
     marker word only in the main json. Plus a packaging inventory (how
     package_release.bat, update.bat and install.bat work today). Core
     proposals (api_version, busy guard, delivery event, preflight
     fast-fail) as proposals, not code. No code change except the doc.
WP1  Skeleton: config, auth, pairing with attempt limit, state, logging
     redaction, send() choke point, lint gate coverage, protect lists,
     state path test. Still NO HiveMind call.
     Real run: unknown account ignored (second account), owner pairing
     works.
WP2  Text run bridge: /new, /stop, /status, chat mapping, transcript via
     PUT, busy rejection, status messages with throttle, splitting, .txt
     on overflow, readable errors, restart behavior, auto-deny of
     approvals. Real run: text run, /stop mid-run, gateway restart with a
     running run, run with deliberate VRAM block (readable message on the
     phone).
WP3  Remote profile in core (after go, separate commits on main):
     source:"telegram" in the /stream payload and stricter gate, plus the
     core proposals cleared in WP0 (busy guard, api_version). Proposal
     the owner confirms or changes before build: from the phone, read
     tools and workspace writes run; run_bash, git push, deletion and
     writes outside the workspace are NOT grantable from the phone (only
     via a PC confirmation within the timeout, otherwise deny). Tests,
     pyright, evidence from a real run.
WP4  Approvals: nonce store, buttons, full display, expiry with deny from
     the gateway. Real run on the phone: approve, deny, expiry, repeated
     tap, risky tool not grantable from the phone.
WP5  (optional for 1.3) Photos and steering. Only `photo`, max 10 MB,
     magic bytes; photo with caption starts a run (caption is q), photo
     without text rejected; albums (media_group_id) collected for ~2 s,
     max 4 images. /steer <text> or reply to the status message; the
     receipt means "handed over", not "delivered". The gateway says
     honestly when delivery happens or that the mode cannot (per the WP0
     mode matrix). Open, to be named in the report: whether a steer image
     is still in context after context compression.
WP6  Audit log, /lock, kill switch, optional /get, README, operations
     docs, pip-audit, release integration (above), CHANGELOG. Then the
     release check: fresh install from the zip, gateway off, regression
     green; then gateway activated, pairing, one run.

## OUT OF SCOPE

WhatsApp, cloud models, skills, cron, group chats, voice messages,
queue, token streaming.

Start with WP0. Report afterwards and wait.
