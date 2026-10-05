# WP3 Proposals — remote profile, hardening, acceptance tests

Collected from the security audit round (2026-10-05) and the review
addendum. Each item: risk, proposal, acceptance test. Nothing here is
built — every item needs an owner go (core changes land as separate
commits on main).

## P1 — Remote profile as an ALLOWLIST (replaces deny-list thinking)

Risk: tool permissions are currently deny-based. Everything not in
`_APPROVAL_TOOLS` (8 tools) runs unrestricted — including tools nobody
reviewed as phone-reachable. The 2026-10-05 inventory (24 registered
tools) found these UNGATED and phone-reachable today:

| Tool | Why it matters ungated |
|---|---|
| `undo_last` | WRITES: reverts workspace file changes without approval |
| `run_tests` | executes the project test suite (currently refuses — "not wired" — but ungated the moment it is wired) |
| `browser` | headless browser: navigates arbitrary URLs — potential SSRF detour around the web_fetch loopback guard (unverified, needs a playwright-enabled check) |
| `stop_background` | kills background processes (cross-run interference) |
| `get_background_output` | reads stdout/stderr of background processes started by OTHER runs (cross-run info leak) |
| `git_status` | low — workspace git state only |
| `web_search` / `web_fetch` | external network with content in URLs (exfil) — loopback is blocked by the core guard (verified live 2026-10-05), external is not |

Proposal: for `source:"telegram"` runs, WP3 flips the model — a
named allowlist of READ tools (read_file, get_signatures,
find_references, list_dir, find_files, search_code, get_datetime,
task_complete, ask_user) plus explicit policy for web tools and
memory (P2/P3). Unknown/future tools are DENIED by default. Writes
and exec stay approval-gated with the WP4 buttons; run_bash, git
push, deletions and outside-workspace writes are never grantable from
the phone.

Acceptance tests: a phone-source run attempting each non-allowlisted
tool receives a typed denial; an allowlisted read succeeds; a NEW
dummy tool registered in the table is denied by default (the
regression point the deny-list could never cover).

## P2 — Web-tool policy for phone runs + SSRF acceptance list

Risk: web_fetch can exfiltrate workspace data in URLs to any external
host (needs prompt injection first); the loopback guard exists but
redirect/IPv6/decimal-IP evasions are untested.

Proposal: for telegram-source runs, disable web tools entirely
(reviewer position: "beim Abschalten beider Web-Tools") OR gate them
behind the WP4 approval. Independently, harden the core guard:

Acceptance tests for the guard (each must land on the block path, or
resolve to a non-private IP and be fetch-safe):
1. `http://127.0.0.1:8001/health` — VERIFIED BLOCKED live 2026-10-05
   ("web_fetch blocked: private/loopback IPs are not fetchable").
2. `http://localhost:8001/health`
3. `http://[::1]:8001/health`
4. `http://[::ffff:127.0.0.1]:8001/health`
5. `http://0.0.0.0:8001/health`
6. `http://2130706433/` (decimal IP)
7. `http://0x7f000001/` (hex IP)
8. a private LAN address
9. a hostname resolving to 127.0.0.1 via a local hosts entry
10. an EXTERNAL URL that 30x-redirects to 127.0.0.1 (local test server)
11. `web_search` with exfil-shaped query strings

The check must judge the RESOLVED IP after every redirect hop, not the
hostname string.

## P3 — Memory and shared-state poisoning

Risk: a constrained phone run writes where privileged UI runs later
read: session memory, the `[TG]` chat transcript, workspace files. A
marker planted under phone restrictions is consumed by a click-approved
UI run. A phone run's writes are approval-gated, but engine-side state
(memory summaries) may not be.

Proposal: memory writes from `source:"telegram"` runs are read-only or
kept in a separate memory namespace.

Acceptance tests (real-run round): phone says "merke dir den Marker
XQZ-7" → `/memory` unchanged; a later UI run in another chat does not
know the marker; no workspace file acting as prompt/config/tool
definition was written by the phone run.

## P4 — URL neutralization in agent answers

Risk: link previews are disabled at the transport layer (verified,
`test_gateway_transport.py`), but a URL that reaches the phone as text
can still be tapped/clicked by a human, and future surfaces (groups,
copy-paste) may re-introduce previews.

Proposal (cheap, defense in depth): render URLs in agent ANSWERS as
inline code (`https://example.com/?d=secret` →
`` `https://example.com/?d=secret` ``) or strip query strings from
non-allowlisted domains, with a visible note.

Acceptance test: an answer containing a URL with query parameters is
delivered in neutralized form; plain domains stay readable.

## P5 — Wall-clock run timeout (gateway-side)

Risk: today the bridge relies on httpx's 60 s INACTIVITY timeout and
the engine's own guards; a wedged engine that keeps emitting
heartbeats can hold a run open indefinitely (VRAM + a hanging phone
status).

Proposal: `run_timeout_s` in gateway.toml (default 3600). On expiry
the gateway calls `/abort/{run_id}` itself and posts a readable
message.

Acceptance test: fake engine streams heartbeats past the limit →
abort called, cleanup done, phone informed.

## P7 — Per-source mode (PARTIALLY SHIPPED)

Phone-side run mode is live: `/mode auto|chat|pipeline|automap` sends
the mode in the /stream BODY only — the browser UI's own mode in
settings stays untouched, respecting the "gateway never touches
POST /settings" taboo. `/mode off` follows the engine settings again.
What could still be added later: a `mode` default in gateway.toml and
per-run confirmation of the mode in the status line.

## P8 — Start/stop the gateway from the UI (supervisor)

Status: the UI can already KILL the link (settings veto, implemented
and tested). STARTING from the UI is different: the engine would have
to spawn the gateway process, and the token must then come from the
Windows Credential Manager (the gateway already reads it) instead of a
hand-started shell. Proposal: a small supervisor in the engine —
toggle ON spawns `python -m hivemind_gateway.main` as a child (token
from Credential Manager), toggle OFF vetoes as today plus terminates
the child; the UI shows the real state (running / stopped / vetoed).
Still opt-in: the toggle defaults to OFF, so nothing starts without a
deliberate click. Onboarding gains one step: store the token in the
Credential Manager (installer or documented PowerShell snippet).
Needs owner go — engine-side change.

## P9 — Direct-mode model/ctx overrides (body params)

Risk/limitation: /setModel overrides ride the /stream body as duo keys
(duo_planner_model/coder_model, duo_planner_ctx_target,
duo_coder_ctx_agentic/normal — merged into the run's settings snapshot
at chat_run.py:154). SIMPLE/direct runs resolve their model and ctx
from settings.agents.direct and IGNORE those keys — so the phone flow
currently only bites for duo/agentic runs (confirmed in the bot's
answer text).

Proposal: /stream accepts `direct_model` and `direct_ctx` body keys
(or the runner reads the duo keys as fallback for the direct role).
Acceptance tests: a simple-mode run with direct_model override loads
the named model; ctx applies (log line num_ctx); without the keys
behavior is unchanged.

## P10 — Run takeover: mirror a UI run to the phone (approvals, steering, start)

Owner request (2026-10-05): start an engine run at the PC (or from the
phone), leave, and keep controlling it from the phone — approvals,
steering, and result.

All engine endpoints already exist; this is gateway + one small core
change (T) + one frontend toggle (UI). Sub-features:

- **M — Mirror:** when a settings key `telegram_mirror_enabled` is on
  (UI toggle under the Telegram Gateway card, default OFF), the gateway
  polls `GET /run/journal` for an ACTIVE engine run that is not its own
  and mirrors it to the phone: throttled status edits, `done` with a
  readable stop-reason, errors immediately. Journal frames are the
  same SSE events the UI sees (they include tool_call details — the
  owner's own data on the owner's phone).
- **A — Approval relay:** while mirroring, poll
  `GET /approval/pending/{run_id}`; when the engine holds a card, the
  gateway renders it on the phone (tool + full command, untruncated —
  same rules as WP4) and relays the answer via
  `POST /approval/decide/{run_id}`. The engine already discards late
  duplicate decisions (expired-once, decision-id mismatch), so UI
  click and phone tap racing each other is safe: first wins.
  Default: relay ON when the mirror toggle is on (approvals are the
  point); master switch, UI veto and kill switch still override
  everything.
- **S — Steering:** a phone text while an engine run is mirrored goes
  to `POST /api/run/{run_id}/steer` (receipt = queued; the bot states
  per the mode matrix where it will inject — duo/tool-loops yes,
  pipeline never).
- **T — Direct tools access level from the phone (core change, small):**
  `direct_tools_enabled` is read from the run's settings snapshot
  (direct_runner.py:301) but /stream only lifts the six model/ctx body
  keys into that snapshot. Proposal: lift `direct_tools_enabled` the
  same way (same pattern, ~2 lines), then a `/tools on|off` phone
  command applies per-run without touching global settings.
- **W — Workspace from the phone (no core change):** `/workspace
  <path>` PUTs the workspace onto the `[TG]` chat
  (`PUT /chats/{id}`, field already live-verified); subsequent phone
  runs run inside it. Gate: path must exist (server-side check via the
  chats API 404/400 is absent — gateway checks via a cheap engine call
  or accepts and reports), confirmation echoes the resolved path, and
  the answer notes the blast radius ("der Agent liest dann dort").
- **Start from the phone:** a phone run already starts on the PC —
  the missing piece is only the workspace (W) and mode (/mode, live).
  For UI-native runs started at the PC, M/A/S above close the loop.

Security gates: takeover requires the pairing (owner only), the master
switch ON, no veto, no kill switch — any of the three kills mirror,
relay and steer instantly. The mirror never starts runs by itself; it
only watches and relays.

Acceptance tests (real-run round): UI run + mirror on → approval card
appears on the phone within ~5 s; phone "3" denies (tool does not
run); phone text lands as steer at the next boundary (status
"steered"); done reaches the phone with stop_reason; mirror off →
none of the above; veto mid-run stops the mirror within one loop.

## P6 — Smaller hardenings

- `run_tests` into `_APPROVAL_TOOLS` now, before anyone wires a real
  runner (one line, core).
- Log sanitizing: gateway logs currently carry only ids/enums (no user
  text) — keep it that way; add a control-character scrubber the day a
  log line wants to include text.
- Browser tool (if it ships enabled) must pass the same loopback rules
  as web_fetch — see P1/P2.

## Status of the review addendum items (2026-10-05)

| Item | Status |
|---|---|
| W1 approval timeout | Resolved: runner.py:719 FAIL CLOSED deny + `test_approval_timeout.py` 4/4; the old WP0 claim read stale comments. R5b stays as live evidence. |
| N1 link previews | Fixed at transport: is_disabled on sendMessage/editMessageText, documents carry no captions — `test_gateway_transport.py` 5/5. Real-run check stays in the round. |
| N2 tool inventory | Done (this file, P1): 24 tools, 8 gated; gaps listed. Allowlist is the WP3 acceptance criterion. |
| N3 confinement matrix | Done: 19/19, all traps blocked (`test_gateway_confinement.py`). Junction test can be re-run by the owner with mklink /J on demand. |
| N4 memory poisoning | Real-run step added to the round; proposal P3. |
| N5 SSRF | Live probe: loopback IS blocked (guard exists). Acceptance list in P2 — the old audit claim "no guard" was wrong and is corrected. |
| N6 UI veto semantics | Implemented + tested (5 cases + local-OFF-stays-OFF pair, `test_gateway_commands2.py`). |
| N7 small items | trust_env=False for the loopback client (done); secret filter proven on the .txt document path (bridge suite); wall-clock timeout → P5; log sanitizing → P6 note. |
