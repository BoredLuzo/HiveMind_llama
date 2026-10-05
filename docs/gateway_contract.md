# GATEWAY CONTRACT — HiveMind core API as verified for the Telegram gateway

WP0 deliverable. Verified against commit `33e6cb5` (branch
`feature/telegram-gateway` = main `01abe04` + this brief; base `a132ef6` per
brief, rebased onto main on the owner's instruction). Line numbers refer to
that commit; they drift — the symbols are the contract, the lines are the
map. Marked **PENDING** where the live experiment is still owed (the owner
was working live; per brief the experiment waits for an approved slot).

## POST /stream

- `server.py:1178 @app.post("/stream") / stream()` → `StreamingResponse`
  (`media_type="text/event-stream"`, server.py:1462).
- Payload (JSON body): `q` (str), `images` (list[base64-str]),
  `mode` (str, default from settings), `iterations`/`iters`, `chat_id`
  (server.py:1181-1184, :1273). Many optional duo_* toggles
  (server.py:1246-1326); the gateway sends none of them and relies on the
  settings snapshot. **No `source` field exists** (WP3 proposal below).
- `flush_ok=false` + `dom_messages` makes the server write the main chat
  json from the DOM (server.py:1278-1287 → `import_dom_messages`,
  routers/chats.py:365). The gateway never sends these; consequence: a
  gateway run does NOT write the main json (see Transcript).
- SSE framing: `data: {json}\n\n` per event.
  - First relevant event: `{"type": "run_id", "run_id": "<ts>-<hex8>"}`
    (emitted core/chat_run.py:478; parsed for the journal at
    server.py:1358-1364; run_id format core/chat_run.py:278).
  - Full type census (grep over core/ + routers/): `run_id`, `status`,
    `token`, `agent`, `planner_start|planner_thinking|planner_result|
    planner_done`, `pipeline_start`, `duo_coder|duo_coder_done|duo_critic|
    duo_critic_done|duo_round`, `tool_call`, `tool_result`, `file_change`,
    `usage_meta`, `ctx_meter`, `system_warning`, `image_description`,
    `files_summary`, `complexity`, `heartbeat`, `approval_request`,
    `agent_asking`, `agent_resumed`, `done`, `error` (error ONLY from the
    /stream wrapper, never from runners).
  - Terminal event: `done` built in core/chat_run.py:262-274:
    `{type, elapsed, stop_reason, stop_reason_bucket, tokens_generated,
    prompt_tokens_total, cached_tokens_total, requests_total}` (+extra).
  - Unhandled runner exception: server.py:1423-1433 emits
    `status` ("Internal stream error"), then
    `{"type": "error", "content": "<ExcType>: <msg[:160]>"}`, then
    `{"type": "done", "elapsed": 0, "stop_reason": "error"}`.
  - Client disconnect cancels only the SSE bridge; the run continues
    DETACHED (server.py:1436-1450) and is reattachable via
    `GET /run/journal` (server.py:1468). For the gateway this means:
    losing the SSE connection does not lose the run.

## POST /api/run/{run_id}/steer

- `routers/core.py:101 steer_run()`. Body `{text, images?}`; images: max 4
  (:117), each a base64/data-URL string ≥32 chars (:121), ≤~10 MB (:124);
  text or images required (:127). Response
  `{status:"queued", run_id, images, queue_len}`; 404 `"run not active or
  not found"` when nothing is queued (:129-130) — queue_steer returns False
  for unknown/finished run_ids (infra/run_control.py:304-317).
- Queue: per-run_id list (infra/run_control.py:301). Receipt means QUEUED,
  not injected — matching the brief's "handed over, not delivered".
- Injection points (all drains of `drain_steer_messages`,
  infra/run_control.py:320):
  - `core/tool_loop.py:472` — tool-round boundary, shared by chat/direct/
    agentic runs (steers become user messages before the next POST).
  - `core/duo_runner.py:2666` — duo chunk boundary (text into coder input;
    image steers as their own content-parts user message).
  - `core/duo_runner.py:3816` — duo-internal tool rounds.
- MODE MATRIX (verified by drain-site census; pipeline/direct runners have
  zero drain sites):
  - duo-agentic / duo with tools: YES (chunk + tool-round boundaries).
  - direct WITH tools: YES at tool-round boundaries only.
  - direct WITHOUT tools: NO injection (no tool loop rounds).
  - pipeline: NO injection (core/pipeline_runner.py imports no steer
    function).
  - planner phase (duo): NO drain before the first chunk/tool boundary →
    steers arrive late, not lost.
  - The gateway must therefore say honestly per mode whether/where a steer
    lands (WP5).

## POST /abort

- `routers/core.py:34 abort_stream()` — **DIVERGENCE from the brief**: the
  brief assumed `POST /abort` with body `{chat_id}`. Reality: chat_id is a
  QUERY parameter (`?chat_id=...`, optional `silent=true|false`); body is
  ignored; 400 when missing. It sets the chat-level abort event
  (`_get_abort_event(chat_id).set()`), which abort-aware waits wake on.
- Run-scoped alternative: `POST /abort/{run_id}`
  (routers/core.py:158, 404 if unknown) and `POST /abort/graceful/{run_id}`
  (:147). The gateway should hold run_id + chat_id and use `/abort/{run_id}`
  (precise) with `/abort?chat_id=` as fallback.

## Chats (`/chats`, routers/chats.py:27)

- `GET /chats` (:386): meta list incl. `msg_count, tokens, preview, bytes,
  interrupted`. `GET /chats/search?q=&limit=` (:529, substring over titles
  and messages, registered BEFORE `/{chat_id}`).
- `POST /chats` (:440): body `{title?, messages?, workspace?}`; id =
  `uuid4()[:8]` (:444); with no messages this is the main json with 0
  messages — brief assumption CONFIRMED. Returns `{ok, id, rev, chat}`.
- `GET /chats/{id}` (:588): 404 when unknown; restores blob refs before
  returning; adds `last_run`/`interrupted` from the sidecar context.
- `PUT /chats/{id}` (:610): body `{title?, workspace?, messages?,
  base_rev?}`. Atomic per-chat lock (:620-644). CAS: `base_rev` must equal
  the stored rev (routers/chats.py:286-289) or → **409**
  `{error:"stale_rev", rev, messages, hint:"server state wins..."}`
  (:649-656) — the caller adopts the returned messages. Success:
  `{ok, rev}`. Chats without a rev count as 0; every save bumps rev by 1
  (:291). `POST /chats/persist` (:467) is the create-or-update beacon path,
  same 409 semantics (:500-503).
- CAS semantics VERIFIED against live API 2026-10-05 (probe chat, deleted
  afterwards; no model run): POST /chats already returns **rev=1**
  (creation counts as a save — "no rev = 0" applies only to pre-existing
  files); PUT with base_rev=1 → `{ok, rev:2}`; a stale base_rev → 409
  `stale_rev` carrying the server's messages + hint, as documented.
- BLOB STORE (routers/chats.py:29-132): strings ≥400 chars in the keys
  `content|diffText|text|preview` are moved to
  `sessions/<chat_id>.blobs/<sha256-16>` on save and replaced by
  `{"_ref": hash}`; reads restore them. Backend-side: the gateway can PUT
  plain inline content and must never parse `_ref` itself.
- Message shape: `messages[]` entries with `role` ("user"/"assistant"),
  `content` (str or content-part list) — exact UI shape (code panels `cp`,
  image previews, ids) to be COPIED FROM A REAL UI CHAT in the live
  experiment, not guessed. **PENDING (live).**

## Approvals

- Gated tools `tools/runner.py:282 _APPROVAL_TOOLS`: run_bash, run_python,
  install_package, start_background, write_file, edit_file,
  write_file_append, git_commit. Gate setting:
  `duo_action_approval_enabled` (tools/runner.py:418, :492).
- SSE event `approval_request` (tools/runner.py:634-637):
  `{type, run_id, tool, preview, decision_id, auto_timeout_s}` — preview is
  TRUNCATED to 300 chars (tools/runner.py:462-470). The full command lives
  in the `tool_call` event's args (sse/events.py:9+ keeps `extra["cmd"]`
  untruncated for run_bash). Recovery: `GET /approval/pending/{run_id}`
  (routers/core.py:178) returns `{active, tool, preview, decision_id,
  auto_timeout_s}` while a card is open (fire-and-forget SSE, hence the
  poll).
- Decision routes (either works):
  - `POST /approval/decide/{run_id}` (routers/core.py:190): routes to the
    waiting pause (sets the user answer, :218-229) or stores a
    pre-decision while the call is still generating (:230-239).
    `auto_timeout_off` cancels the server countdown for the card (:204-209).
  - `POST /api/run/{run_id}/resume` (routers/core.py:54): only resolves a
    WAITING pause (404 otherwise, :67-68); decision_id mismatch →
    `{"routed":"stale"}` (:76-78).
- Answer format: `"1"|"2"|"3"|"0"` optionally `"1|user note"`
  (`_parse_approval_answer`, tools/runner.py:255-272): `0`=input-only,
  `1`=approve once, `2`=approve repo/"always" (remembered for the chat),
  `3`=deny. Empty/unclear = deny (fail-safe). **The gateway sends deny as
  answer `"3"` — CONFIRMED the gateway can deny.** It must never send `"2"`
  (brief: no "always" from the phone).
- Timeout semantics — **CORRECTED (2026-10-05)**: expiry IS deny
  (fail-closed), matching the brief. The WP0 draft read stale comments in
  tools/runner.py (:317 header and the ":687-688 auto-approve-once path"
  note) - both corrected on main in 2e938f3; the empty-answer path from
  countdown expiry falls into the fail-closed deny below it.
  `duo_action_approval_timeout_s` (read per gate call,
  tools/runner.py:575): `0` → no timer, abort-aware wait capped at 3600 s
  (:387, :578); `>0` → countdown expiry **DENIES the call once**
  (fail-closed, "[APPROVAL] timeout ... denied once", visible status
  line), and the card checkbox can switch the countdown off (:657,
  :680-685). A decision arriving after expiry is discarded once
  (routers/core.py:234-235). CONSEQUENCE for the gateway: unchanged -
  its own expiry (gateway.toml, default 120 s) sends deny `"3"` itself,
  which now matches the server behavior instead of racing an
  auto-approve. Gateway expiry shorter than the server timeout stays a
  configuration invariant to test in WP4. Until WP4 stands the gateway
  auto-denies everything with `"3"`.
- Deny result: the tool does not run; the model receives
  `ACTION_APPROVAL_DENIED` (tools/runner.py:544-549).

## run_active / busy

- **DIVERGENCE from the brief: there is NO server-side busy guard.** Every
  `POST /stream` starts a run (server.py:1298 creates the generator per
  request; run_id per run, core/chat_run.py:278). A second concurrent
  /stream is not rejected — it contends for VRAM. The brief's "run_active
  and busy behavior on a second /stream" does not exist as assumed. The
  gateway needs its own single-run gate, and the server-side 409 proposal
  below is MORE valuable than the brief assumed. (UI-only prevention; no
  API-level registry was found. `GET /run/journal` server.py:1468 lists
  recent run frames — a heuristic, not a guard.)

## GET /health

- `server.py:1760 health()`: `{status:"ok", version, llama_ok, model_count,
  test_omit_mmproj}`. `version` = `HIVEMIND_VERSION = "1.3.0-preview"`
  (server.py:103, hardcoded string, no git describe — BUILD_INFO.txt in the
  release zip carries the real build, deploy/package_release.bat). No
  compatibility/api_version field → proposal below.

## Bind address

- `run.py:168 bind_host = env HIVEMIND_HOST, default "127.0.0.1"`;
  `uvicorn.run(..., host=bind_host)` (run.py:196-198). Port:
  `settings.json server_port`, default 8001 (start_hivemind.bat:26-28).
  Gateway startup check per brief: refuse to start if HiveMind listens on
  anything but loopback — check via netstat/connect from a non-loopback
  perspective, and treat `HIVEMIND_HOST` in the environment as a red flag.

## Error shapes for the phone (WP2 tests)

- VRAM busy during vision preprocessing: readable `status` events, images
  marked unusable ("VRAM busy? stop other model loads and retry",
  core/chat_run.py:726, :735).
- `model_load_failed`: a duo stop reason (core/duo_runner.py:6624-6625, set
  at :1708/:3476) → arrives as `done.stop_reason="model_load_failed"`, not
  as an error event. The gateway must translate done.stop_reason into phone
  readable text: completed | error | model_load_failed | timeout |
  hard_stop | graceful_stop | loop_detected | wedge_escalated | halted |
  aborted (bucket logic `_stop_reason_bucket`, core/chat_run.py).
- Hard server errors: `{"type":"error","content"}` + `done(stop_reason:
  "error")` (server.py:1430-1433).
- Server offline: connection refused on /stream — gateway-owned "offline"
  message (brief: no auto retry).

## Transcript path (static verification; live experiment PENDING)

- POST /chats creates the main json with 0 messages (routers/chats.py:440).
  VERIFIED live 2026-10-05. The gateway write path is VERIFIED live too:
  PUT /chats/{id} with base_rev stores the turn, returns the new rev; a
  stale base_rev gets 409 + server messages (server wins, adopt); GET
  shows the turn; the chat appears in GET /chats; DELETE removes it (404
  afterwards). Probe: no model run, no VRAM touched.
- A /stream run writes ONLY the sidecar (`.context.json`: read-guard state,
  files_read, plan — core/duo_runner.py:6476-6480,
  core/duo/_pre_explore.py:877) at completion. The main json is written by
  clients (UI PUTs/persists; /stream only imports DOM on flush_ok=false).
  So the gateway writing turns via `PUT /chats/{id}` with base_rev matches
  the architecture; 409 = server wins, adopt returned messages.
- User turn BEFORE /stream start: required because the seed drops TRAILING
  user messages (normalization doc, context/chat.py — the trailing user
  message is treated as the new prompt `q`); the prompt travels via `q`,
  not via the transcript.
- Seed source: `history_seed_provenance` (context/chat.py:65) decides
  `json | sidecar | none`; the run re-seeds EVERY run from json/sidecar
  (core/chat_run.py:872-920, LABEL-LIE FIX 2026-10-05: label follows the
  provenance, and json/sidecar ALWAYS own — even empty). The brief's seed
  label claims are confirmed in code. The RUN-dependent part of the
  experiment (a /stream run writes only the sidecar; seed counts N=0
  first turn / N=2 second turn from the [HISTORY-SEED] log; marker word
  untouched by the run) still needs a live model run: **PENDING** (needs
  an approved GPU slot; the chat-write half is done, see above).
- Chat deletion removes json + sidecar + blobs (routers/chats.py:660-679).

## Packaging inventory (today)

- `deploy/package_release.bat`: zip from `git archive HEAD` (tracked files
  only); leak check rejects settings/presets/models/vision/routing/memory
  json and `sessions/`; embeds BUILD_INFO.txt (git describe + date). WP6
  must extend the leak check with gateway files (gateway.toml, state,
  audit log, gateway.disabled, token) — tracked-tree additions ship
  automatically.
- `update.bat`: robocopy apply excludes `/XF update.bat settings.json
  presets.json models.json vision_model.json routing_weights.json
  memory.json /XD sessions .hive_uploads learning_logs logs .venv llama`
  (update.bat:211), backup `/XD` list (:166), prune protect (:235). WP6
  adds the gateway user-state files to all three lists.
- `install.bat`: sets server_port, syncs deps. **httpx is already a core
  dependency** (requirements.txt:5, pyproject.toml:12: `httpx>=0.27`) —
  the brief's open question answered: no new dependency needed for the
  Telegram client; the gateway lockfile only pins.
- Version string: hardcoded `HIVEMIND_VERSION` (server.py:103); release
  zip carries BUILD_INFO.txt with the real git describe.

## Divergences from the brief's assumptions (report per brief)

1. `/abort` takes chat_id as QUERY param, not body (routers/core.py:34).
2. No run_active/busy guard exists; concurrent /stream is possible and
   unchecked.
3. Approval timeout expiry AUTO-APPROVES ONCE (tools/runner.py:688), it is
   not a deny bound; `0` = no timer (3600 s abort-aware cap). Gateway must
   out-race it with its own deny-expiry.
4. approval_request preview is truncated to 300 chars; the full command
   must be taken from the tool_call event (sse/events.py keeps cmd whole).
5. `error` SSE events exist only from the /stream wrapper (server.py:1432);
   runners express failure via done.stop_reason and status events.
6. Steer receipts: 404 only proves the run is gone/unknown at queue time;
   there is no delivery event (proposal below).

## Core proposals (proposals only — code only after the owner's go)

1. `api_version` in /health (or a gateway-visibility endpoint): monotonically
   bumped integer the gateway can gate on; `version` stays the display
   string. Small, additive.
2. Server-side busy guard: /stream returns 409 `{error:"run_active",
   run_id}` when a run for the same chat (or globally, config) is active.
   Removes the gateway's check-then-start race; UI unaffected.
3. Delivery event for steers: emit `{type:"steer_delivered", run_id,
   seq}` at the drain sites (3 sites) so a client can distinguish
   queued vs injected. Alternative: `steer_queue_view` exposure. Small.
4. Preflight fast-fail: /stream should emit an early, typed failure (e.g.
   `done.stop_reason="vram_preflight_block"` or an `error` event) when the
   model load cannot proceed, instead of long silent model-load waits.
   Today the phone would show a status trickle at best.

## NOT tested

- WP0 has no code and no test suite; nothing ran the regression suite.
- The run-dependent transcript half (sidecar-only writes, N=0/N=2 seed
  counts) is outstanding — needs an approved GPU slot; deliberately not
  run while the owner was working live.
- Real-run evidence for later WPs outstanding as planned (token/second
  account).

## Open questions

- Does the UI write any field the gateway must replicate in messages
  beyond role/content (ids? ts?) — answered by the live experiment.
- Whether `mode` values the gateway should use are exactly
  {auto, direct, duo, pipeline} or mode is derived from duo_* flags
  (settings default "auto") — confirm in WP1 against settings.json.
- Does the abort event wake a run between rounds reliably without SSE
  attached (detached mode) — live experiment when /stop is built (WP2).
