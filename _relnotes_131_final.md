**HiveMind 1.3.1 — Telegram gateway reliability, phone-run UX, deep-audit hardening**

HiveMind runs local LLM agents on your own GPU. 1.3.1 puts the full
agent on your phone, makes mid-run steering a first-class citizen,
hardens the image pipeline, and fixes 58 verified findings from two
full-harness audit rounds (all HIGH/MED/LOW closed; 93/93 suites).

## Telegram gateway (opt-in, off by default)

Control HiveMind from your phone: plain message in, the PC runs the
agent, status and result come back as Telegram messages. Nothing
starts or installs until you run it yourself — setup via
`start_gateway.bat setup` (token in the Windows Credential Manager;
full guide with security walkthrough: `docs/gateway_setup.md`).

- Owner-only pairing (one-time console code, 5 min), no webhook, no
  open port — outbound long-polling only. Kill-switch file on the PC
  overrides everything.
- Own runs execute as background tasks: /stop, approval taps and
  steering are reachable WHILE a run streams.
- A broken live stream no longer spams errors: the gateway checks the
  engine journal and hands the running job over to the mirror with
  one honest notice. SSE read timeout raised to 10 min (the 60 s
  timeout killed streams during cold-engine model loads).
- Approval cards show the REAL full command; expired/superseded cards
  answer honestly. Unanswered cards deny after the timeout
  (fail-closed).
- Every gateway restart starts a fresh [TG] chat; run results are
  persisted into the transcript so phone runs show up complete in the
  web UI.
- Commands: /new /stop /status /verbose /mode /models /setModel /ctx
  /preset /gate /workspace /cancel /help. Model choice applies to
  simple/direct runs too.
- Restrict phone runs (default ON): phone runs can search/read the web
  and chat — shell, writes, git and installs are blocked outright.
  Turn it off in the UI (or the panel below) to unlock approval-gated
  actions.
- Language mirror: answers come back in the language of your message.

## Steering you can see

- /steer (optional screenshots, max 4) injects at the next round
  boundary and shows up as a persistent card in the chat flow — in the
  UI, and reloaded too.
- The phone confirms every steer pickup; tool-call relay lines show
  what the agent is doing live.

## Web UI

- Feature search palette (/ or Ctrl+K) across every panel, toggle and
  setting; click jumps to it and reveals mode-hidden wrappers.
- Chat list shows per-chat disk size; chat continuity (transcript
  seeding, token budget, summary fix); hardened self-updater with
  sha256 digest verification.

## Vision & reliability

- Image runs are reliable on a cold engine: the projector (mmproj)
  loads even when a text-only prefetch is still in flight, and the
  image gate judges the model the run actually uses (/setModel), not a
  stale registry default.
- Optional toggle: coder always loads with the projector (no mid-run
  upgrade stall when you steer images often).
- Dates are anchored at the prompt tail, in the user turn and in
  search-result headers; every gateway restart starts a fresh [TG]
  chat, and agent models missing on disk are wiped to "unconfigured".
- 58 audit findings fixed across slot manager, runners, HTTP layer,
  tools and platform paths (approval preview integrity, containment
  re-checks before writes, destructive-gate coverage, loop-blocking
  removal, supply-chain digest checks, posix stop routes).
