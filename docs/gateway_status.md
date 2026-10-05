# Telegram gateway — status & change record

Date: 2026-10-05. Branch: `feature/telegram-gateway` (contains the
finished frontend main + 17 gateway commits; the merge back INTO main
awaits the owner's go). Test state: 83/83 regression suites green,
ruff clean. The brief (`docs/gateway_brief.md`) remains the single
valid assignment; this file records what is DONE.

## Shipped code

### WP0 — contract verification (docs only)
- `docs/gateway_contract.md`: every core endpoint the gateway leans on
  verified with file:symbol:line (stream payload + SSE census, steer +
  mode matrix, abort query-param divergence, chats CAS/409 + blob
  store, approvals event/decide/timeout, journal, health, bind
  address, error shapes), packaging inventory.
- Transcript write-path proven live: PUT user turn before the stream,
  PUT assistant turn after done, next run seeds N=2 (`[HISTORY-SEED]`
  log lines quoted in the contract).

### WP1 — gateway skeleton (`hivemind_gateway/`, 12 modules)
- Fail-closed master switch (`telegram_enabled`, default false; env
  override). Nothing polls Telegram unless deliberately switched on.
- `gateway.toml` validation at startup (fail fast; secret-like keys in
  the file are a startup ERROR — the token only ever comes from the
  environment or Credential Manager).
- Pairing anchored to the PC: one-time base32 code on the console
  (5 min, single use, constant-time compare), exactly one owner
  account, 5 global wrong tries lock until restart, failed attempts
  stay silent on the phone (console is the pairing interface).
- Atomic state in `%LOCALAPPDATA%`-equivalent (`%LOCALAPPDATA%`),
  outside every workspace: owner id, Telegram offset (persisted BEFORE
  processing — at-most-once), dedupe window, active run, verbose flag.
- Log redaction on every record (literal token + bot-URL patterns —
  verified in the live console).
- `send()` choke point: every outbound message; refuses any chat that
  is not the owner's before any network call.
- Pure modules: approval nonce store (single use, expiry, chat-bound),
  renderer (4096 split at line boundaries, balanced code fences,
  secret filter), command whitelist (forwarded messages never parse as
  commands).
- Telegram client hand-written on httpx: exactly the six allowed
  methods, no webhook support, outbound long-polling only.
- Poll loop: backlog drop at startup (offset to newest), 60 s replay
  window, update-id dedupe, per-user rate limit (/stop exempt),
  single-instance lock (pid + process-birth stamp), Telegram CONFLICT
  (second poller/webhook) exits loudly with code 3.

### WP2 — text-run bridge
- One `[TG]` HiveMind chat per Telegram chat, mapping persisted.
- Transcript via `PUT /chats/{id}` with base_rev: user turn BEFORE the
  stream, assistant turn after done; 409 = server wins, adopt and
  retry once (pattern verified live).
- SSE consumption: status updates throttled to one edit every few
  seconds, no token streaming; approvals auto-denied with answer "3"
  and a visible note on the phone.
- Readable failure mapping: model_load_failed / VRAM block / engine
  offline / run error — every failure answers the phone, silence is
  treated as a bug.
- Output: secret-filtered; >4096 chars split at line boundaries with
  balanced fences; longer than 3 chunks arrives as a .txt document
  (secret filter covers the document path too).
- Commands: /new, /stop (never rate-limited, fails visibly), /status,
  /verbose, /help.
- Restart behavior: orphaned run detected at startup via the engine
  journal, persisted approvals denied, owner asked; no auto-restart.
- Remote off switch: the gateway polls engine settings and shuts down
  cleanly when `telegram_gateway_enabled` is explicitly false (key
  absent = local switch decides; engine offline = keep running).
- Kill switch: `gateway.disabled` file or environment variable always
  wins.

## Real-run bugs found and fixed (each with a regression test)

1. `/pair` from an unpaired sender was dropped — the pair_window
   classification had no handler.
2. A wrong pairing code escaped `PolicyViolation` and would have
   killed the poll loop; failure paths now stay silent (no answer to
   strangers).
3. Instance lock: a recycled pid belonging to another process looked
   "still running" — the lock now stores pid + process birth stamp.
4. A raw httpx ConnectError (engine down during first contact)
   escaped every handler and crashed the gateway with a silent phone —
   all transport paths now surface as a readable offline message, and
   one broken update can no longer kill the poll loop.
5. A terminated process whose handle the parent shell still holds
   counted as "alive" — the liveness probe now requires
   GetExitCodeProcess == STILL_ACTIVE.

Live-verified end to end: bot created, token via environment only,
pairing bound the owner (no re-pairing across restarts), config
loaded, backlog dropped, settings veto poll green.

## Security audit + hardening (`docs/gateway_security_audit.md`)

- Threat model with five adversaries; findings F1–F10, corrected where
  a live probe proved the draft wrong (the engine DOES block
  loopback/private web_fetch targets — verified live).
- Triple off switch: kill-switch file, master switch, UI veto; the UI
  can only turn the link OFF, never override a locally disabled
  gateway.
- Link previews disabled on every send path (zero-click exfiltration
  class) — payload-level regression test.
- Workspace confinement matrix: 19/19 — all read/search/glob tools
  reject absolute outside paths, `../`, UNC, junctions, 8.3 short
  names, ADS syntax, case games, trailing dot/space and drive-relative
  paths (content-based criterion).
- Approval-timeout contradiction resolved with source + test: expiry
  is fail-closed DENY (runner FAIL CLOSED comment, 4/4 core suite).
- Known open surface until the remote profile (WP3) ships: web tools
  can reach EXTERNAL urls (exfil needs prompt injection first), and
  the deny-list leaves some tools ungated (undo_last, run_tests,
  browser, stop_background, get_background_output) — full inventory
  and the allowlist proposal in `docs/wp3_proposals.md` (P1–P7).

## Docs shipped

- `docs/gateway_setup.md` — user-facing setup guide (English),
  copy-paste-safe, with a plain-language security section.
- `CHANGELOG.md` — gateway entry under 1.3.0-preview (features only,
  explicit "not included" list).
- `docs/gateway_realrun_wp2.md` — the 7-step real-run plan including
  approval-against-core (R5b), concurrency, link-preview and memory
  checks.
- `docs/gateway_contract.md`, `docs/gateway_security_audit.md`,
  `docs/wp3_proposals.md`.

## Pending (owner actions only)

1. Real-run round 1–7 (gateway paired and running; engine models need
   a fresh load on first run).
2. Gos: merge branch → main; WP3 (allowlist P1, web-tool policy P2,
   memory separation P3); one-line `run_tests` gating (P6); frontend
   toggle `telegram_gateway_enabled` under Git credentials (spec in
   the audit, F4 — pure frontend, no core change); optional installer
   step that writes `gateway.toml` only on explicit opt-in.

## Deliberately NOT in 1.3

Photos and steering from the phone (WP5, may slip to 1.3.1), voice,
groups, queues, token streaming, skills, cron. Bot phone-texts are
currently German; a language switch is backlog.
