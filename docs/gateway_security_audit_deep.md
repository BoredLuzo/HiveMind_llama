# Telegram Gateway — DEEP Final Security Audit (release candidate)

Scope: the full `feature/telegram-gateway` branch (frontend main + gateway +
P10 takeover + frontend toggles + installer/updater/packaging), commit
`1a5ad55` (HEAD). Method: static review of every gateway module plus the
core paths it leans on (server.py P10/T lift, chat_run merge, direct_runner
snapshot, journal, approvals, settings), the three bat files, and the
frontend diff. Local runs: `tests/run_regressions.py` (83/83 PASS),
`ruff check hivemind_gateway/ server.py` (clean), `node --check app.js`
(OK), `tests/test_bat_encoding.py` (13/13), byte-level ASCII/CRLF checks,
one loopback GET /health (engine down — handled). Two findings were
confirmed with offline repro scripts (N1, N5). No live Telegram traffic.

## Executive verdict

**Release blocker: NO — with one pre-merge condition.** Nothing found
breaks the core security invariants (owner-only sends, fail-closed
switches, at-most-once runs, token hygiene, workspace confinement).
However **N1 (remote unauthenticated crash of an unpaired gateway via a
non-ASCII `/pair` message)** is a real remote DoS with a one-line fix and
should land BEFORE the branch merges to main; while the gateway sits
unpaired, anyone who finds the bot can kill it in a loop and permanently
prevent pairing. N4 (stale cache-buster on app.js) should ride along in
the same push. N2/N3 degrade the new P10 takeover layer (truncated
approval display, silent mirror-task death) and should be fixed with the
WP4/P10 polish round. Everything else is LOW/INFO or already tracked as
P1–P10 proposals.

## F1–F10 re-verdicts (all with fresh file:line evidence)

- **F1 — CONFIRMED, unchanged (HIGH design gap).** `_APPROVAL_TOOLS` is
  still exactly the 8 gated tools (`tools/runner.py:282-286`); `/stream`
  still carries no `source` field and phone runs are indistinguishable
  from UI runs (server.py:1180-1333 — no allowlist anywhere); web_search/
  web_fetch remain ungated. WP3/P1/P2 not shipped — known, not re-reported.
- **F2 — CONFIRMED, unchanged (MEDIUM).** Same frozenset: `undo_last`,
  `run_tests`, `browser`, `stop_background`, `get_background_output` stay
  outside the gate; `run_tests` was not added (P6 pending owner go).
- **F3 — CONFIRMED, unchanged (MEDIUM-LOW).** `HiveClient.health()`
  exists (`hivemind_gateway/hive_client.py:59-63`) but is never called;
  `run()` (main.py:555-598) starts polling with no engine identity/shape
  check.
- **F4 — CONFIRMED FIXED (verified deeper).** Master switch
  `config.py:28` + `ensure_enabled` main.py:511-527; UI veto
  main.py:530-540 polled at main.py:611-614; kill switch checked in BOTH
  loops (main.py:549 poll… 604, and 549 in `_mirror_loop` — actually
  main.py:549 mirror, 604 poll). Frontend toggle posts a real boolean
  (index.html:1674) and POST /settings stores values as sent
  (routers/config.py:375 `settings.update(data)`), so the veto's
  `not s["telegram_gateway_enabled"]` (main.py:540) sees a real bool.
  Triple off switch holds.
- **F5 — CONFIRMED (LOW), but superseded by N1.** Global 5-try lock
  (auth.py:163-165) unchanged; the same window now also carries the N1
  crash.
- **F6 — CONFIRMED (INFO).** Token from env/keyring only (main.py:56-71),
  redaction filter attached at Gateway init (main.py:249-250).
- **F7 — CONFIRMED open.** No audit module exists; `gateway_audit.jsonl`
  is written by nothing (only .gitignore:128-129, update.bat:214/243 and
  package_release.bat:45 mention it — defense built ahead of the code).
  `/lock` is a stub (main.py:332-335).
- **F8 — CONFIRMED (INFO).** Unchanged; same-user malware out of scope.
- **F9 — CONFIRMED (INFO), slightly larger blast radius.** With P10, a
  taken-over account can now also answer approval cards of PC-started UI
  runs and steer them (documented in the toggle hint, index.html:1684).
  Same trust anchor, more levers — 2FA remains the hard requirement.
- **F10 — CONFIRMED, unchanged (LOW).** Busy = `active_run` +
  journal heuristic (bridge.py:159-163, 271-286); server-side /stream
  still starts a run unconditionally (server.py:1305); no 409 guard.
  The mirror loop starts no runs, so it adds no new busy risk.

## NEW findings

### N1 — MEDIUM-HIGH: unpaired gateway crashes remotely on non-ASCII `/pair` (TypeError escapes the poll-loop guard)

- **Attack path:** gateway starts unpaired → pairing window open
  (main.py:575-582); `classify()` routes every sender to `pair_window`
  (auth.py:96); a stranger sends `/pair <emoji or any non-ASCII>` →
  `cmd_pair` (main.py:389-390) → `PairingManager.verify` →
  `hmac.compare_digest(code, guess)` raises
  `TypeError: comparing strings with non-ASCII characters is not
  supported` (auth.py:160). `cmd_pair` catches only `Pairing*`
  (main.py:391-407); the poll-loop guard list is
  `(TelegramApiError, HiveUnreachable, HTTPError, OSError, ValueError,
  PolicyViolation)` (main.py:645-646) — **TypeError is not in it** →
  propagates out of `run()` → process exits. REPRODUCED offline.
- **Impact:** unauthenticated remote DoS. Repeatable: every restart
  prints a fresh code, the attacker crashes it again — pairing can be
  blocked indefinitely. Paired gateways are safe (`PairingDisabled`
  raises before the compare, auth.py:152-155).
- **Fix (one line):** compare bytes —
  `hmac.compare_digest(self._code.encode(), str(guess or "").strip().encode())`
  — and/or add `TypeError` to the main.py:645 guard list as belt.
- **Acceptance test:** `/pair "😊"` during an open window → failed-attempt
  log line, gateway still polls; 5 wrong ASCII attempts still lock.

### N2 — MEDIUM: mirror approval cards show only the 300-char server-truncated preview (brief: full command, never truncated)

- The relay renders `body.preview` from `GET /approval/pending/{run_id}`
  (bridge.py:694-705). That preview was already truncated to 300 chars
  engine-side (`tools/runner.py:462-470`); the gateway's `[:1500]`
  (bridge.py:701) is dead code. The full command lives in the
  `tool_call` journal frame the mirror ALREADY downloads each tick
  (sse `extra["cmd"]`, contract §Approvals) but never parses.
- **Impact:** the owner taps 1/3 on a `run_bash` card without seeing the
  full command — weakens the "display fully, then decide" rule exactly
  where remote approval is newest. UI-click equivalence (P10) is
  unaffected at the PC, but the phone decides blind.
- **Fix:** enrich the card from the last `tool_call` delta frame
  (`extra.cmd`) before sending; fall back to the preview.
- **Acceptance test:** approval for a 400-char command → phone message
  contains the full command (test in test_gateway_bridge.py style).

### N3 — MEDIUM-LOW: `_mirror_loop` containment gap — one TypeError kills the mirror task silently

- `mirror_tick` catches `(HiveUnreachable, OSError)` and
  `(TelegramApiError, ValueError)` (bridge.py:591-601); `_mirror_loop`
  (main.py:543-552) has no guard of its own, yet its docstring claims
  "every error is contained" (main.py:546-547). A non-numeric/typed
  journal field — e.g. `{"n": {}}` makes `int(j.get("n") or 0)` raise
  TypeError (bridge.py:647/654) — escapes, the task dies, and asyncio
  only logs "Task exception was never retrieved" at GC. Consequence:
  approvals silently stop relaying while `mirror_intercept` keeps
  routing phone texts into `mirror_send` → steer 404s.
- **Fix:** widen mirror_tick's typed catch list (TypeError, KeyError,
  AttributeError) or wrap the loop body — note `except Exception` is
  banned by the brief, so extend the explicit tuple.
- **Acceptance test:** malformed journal payload → tick logs, next tick
  recovers, mirror session survives.

### N4 — LOW-MEDIUM: cache-buster NOT bumped for the HEAD app.js change

- `1a5ad55` (HEAD) added the mirror-toggle init to `static/app.js`
  (+3 lines) but `index.html:1944` still serves
  `app.js?v=20261005-13` — that buster was set by the PREVIOUS commit
  `5827d31` for the older content. Browsers holding the 5827d31-era
  app.js never re-fetch: the mirror toggle initializes from a stale
  script, showing unchecked while `telegram_mirror_enabled` is on
  (state mismatch on a safety-relevant toggle).
- **Fix:** bump to `?v=20261005-14` (and always bump in the same commit
  as the JS change). feature_search_core.js is consistent (-3 matches
  its last change).

### N5 — LOW: `consume_setup` integer validation gap + unclamped ctx magnitudes reach the run snapshot

- Validation `x.lstrip("-").isdigit()` (bridge.py:504-505) passes
  `"--5"` and `"²"` (superscript two — `isdigit()` True) but `int()`
  raises ValueError (REPRODUCED). The ValueError is caught by the poll
  guard, so no crash — but the owner gets SILENCE (update logged as
  failed, no reply), for exactly the interactive flow meant to be
  one-line-and-done.
- Magnitudes: whatever parses becomes `duo_planner_ctx_target` /
  `duo_coder_ctx_agentic|normal` in the run snapshot
  (chat_run.py:153-155 merge). Planner side clamps at
  `duo_planner_ctx_cap` (default 131072, duo_runner.py:1055-1070), but
  the CODER side returns explicit values unclamped
  (`resolve_ctx`, hive_functions/num_ctx_config.py:154-178 — explicit
  `> 0` wins as-is) → a fat-fingered `999999999999` asks llama.cpp for
  an absurd KV cache on the shared 8 GB engine (self-DoS / VRAM hold).
  Server-side lift filters only `_ci > 0` (server.py:1235-1244).
  Owner-only reachable (paired account; F9 class), no new privilege vs
  the UI's own body path — but the phone has no number picker.
- **Fix:** parse with per-part `try: int(x)` (reject on failure, answer
  with the usage line) and clamp ctx to e.g. 1024..131072 in
  `consume_setup` (or in resolve_ctx, engine-side, which also covers UI
  POSTs).
- **Acceptance test:** `"--5, 8, 16384"` → usage note (not silence);
  `"0, 999999999999, 16384"` → clamped/rejected with a readable line.

### N6 — LOW: `/stop`'s visible-failure invariant can be silently violated; `_owner_bridge_call` misses HTTPError

- `bridge.stop()`'s 404-fallback calls `abort_chat` which does
  `raise_for_status()` (hive_client.py:105-114) → `HTTPStatusError`
  escapes stop()'s `except (HiveUnreachable, OSError)`
  (bridge.py:304-307); the `/stop` branch (main.py:303-310) has no
  try, so the per-update guard logs it and the phone gets NOTHING —
  for the one command that "must fail visibly" (brief). Likewise
  `_owner_bridge_call` (main.py:341-347) catches only
  `(HiveUnreachable, OSError)`: engine 4xx/5xx on /new, /workspace,
  /models, /setmodel end silently. Contained (loop survives), but the
  answer is lost.
- **Fix:** catch `httpx.HTTPError` in both paths and answer visibly.
- **Acceptance test:** fake engine returns 500 on /abort → phone shows
  the "fehlgeschlagen, am PC prüfen" note.

### N7 — LOW: narrow own-run mirror race (run_id-None window)

- Between POST /stream and the run_id frame,
  `active_run.run_id` is None (bridge.py:172-201). The journal only
  registers on the run_id chunk (server.py:1361-1372), so the window is
  milliseconds — but a mirror tick landing inside it treats the
  gateway's OWN run as a takeover (`str(None or "")` != rid,
  bridge.py:642-655), announces it, and never clears the session (the
  own-check at :643-644 returns without reset) → phone texts become
  steering for the gateway's own run until it ends. The false "🏁
  Mirror-Lauf beendet" line on completion is the visible symptom.
- **Fix:** in the fresh-takeover branch, skip when
  `state.data.get("active_run")` exists with `run_id` None.
- **Acceptance test:** journal shows rid while own run pending → no
  takeover note.

### N8 — INFO: installer/updater/packaging consistency gaps

- **update.bat lists are inconsistent for gateway.lock:** present in the
  apply `/XF` list (update.bat:214) but MISSING from the prune-protect
  `$protFiles` (update.bat:243). A gateway.lock in BASE would be pruned
  to update_removed on the next update. All other gateway entries match
  across /XF ↔ prune ↔ .gitignore. `gateway.toml.example` is correctly
  in NEITHER protect list, so it updates.
- **Leak-check regex gaps (deploy/package_release.bat:45, anchored
  `$`):** matches gateway.toml, gateway_state.json(.corrupt),
  gateway.disabled, gateway.lock, gateway_audit.jsonl; does NOT match
  `gateway_state.tmp` (the actual tmp filename state.py produces via
  `with_suffix(".tmp")`, state.py:138), rotated `gateway_audit.jsonl.*`,
  or `gateway.pid` — all gitignored (.gitignore:127-134) and none
  written into the tree by default, so defense-in-depth only (the zip
  is `git archive HEAD` = tracked files). Verified programmatically:
  `gateway.toml.example` does NOT match the anchored regex and ships.
- **install.bat:** zero gateway references (only an httpx remark,
  install.bat:144) — fresh install unaffected; no autostart anywhere
  (start_hivemind.bat clean). Note: `start_gateway.bat` (brief's
  release-integration item) does not exist — the setup guide carries a
  copy-safe start command instead; still an open WP6 item, not a risk.
- **Encodings:** install.bat, update.bat, package_release.bat are
  ASCII + CRLF (byte-checked; test_bat_encoding 13/13 PASS).

### N9 — INFO/UX: `/workspace` performs no path validation

- `/workspace <path>` stores and PUTs any string
  (bridge.py:745-775, echo + warning included). Owner-only, so within
  the threat model — but the P10/W spec's "path must exist" gate is not
  implemented; a typo silently re-points the agent's read context.
  Suggest an existence check via a cheap engine call before the PUT.

## Sound and covered (verified clean)

- **send() choke point** (send.py:15-47): refuses unpaired and
  non-owner chats BEFORE any network call; every outbound path
  (Gateway.reply/send_message/edit_message/send_document, bridge
  messenger) routes through it.
- **telegram_api.py:** exactly the six allowed methods; no webhook;
  `link_preview_options.is_disabled` on sendMessage AND editMessageText
  (:89-105); documents carry no captions; defensive JSON parsing
  (non-JSON → TelegramApiError).
- **Token hygiene:** env/Credential Manager only (main.py:56-71);
  redaction filter rewrites msg and clears args before formatting
  (redaction.py:45-52) — %-lazy formatting cannot re-leak.
- **At-most-once:** offset persisted BEFORE processing (main.py:639-642);
  60 s replay window (main.py:421-424, 468); dedupe cap 512
  (state.py:160-168); backlog drop at startup (main.py:587-596).
- **Pairing:** base32 of 8 random bytes (~64 bit), 5 min TTL, single
  use, 5 global failures → lock until restart, disabled after success;
  pair_window accepts ONLY /pair, forwarded/stale silently dropped
  (main.py:489-504) — realrun fixes #1/#2 intact; PairingLocked/
  Disabled/Error all silent (realrun fix #2).
- **Instance lock:** pid + process-birth (FILETIME) with
  `GetExitCodeProcess == STILL_ACTIVE` (main.py:77-110) — realrun fixes
  #3/#5 correct; unknown birth conservatively refuses (main.py:163-170).
- **Triple off switch** semantics verified end-to-end incl. the
  "local OFF stays OFF" rule (veto only ADDS an off, never an on).
- **CSRF:** Origin-vs-Host middleware on all state-changing methods
  (server.py:324-333, infra/security.py:16-30) → a malicious web page
  CANNOT flip `telegram_gateway_enabled`/`telegram_mirror_enabled`
  (cross-origin POST → 403). GET stays open by design.
- **XSS via settings round-trip:** toggle states are assigned to
  `.checked` properties only (static/app.js:11405, 11408) — never
  interpolated into HTML; the two new onchange handlers pass booleans.
- **P10/T lift (core touch):** server.py:1249-1251 uses `_as_bool`
  (handles None/bool/int/float/str; dict/list fall to default False) —
  a non-boolean CANNOT crash the lift; the gateway always sends a real
  bool (bridge.py:392-394). Path verified end-to-end:
  body → `_model_overrides` → `_run_settings.update` (chat_run.py:153-155)
  → `ctx.settings.get("direct_tools_enabled")` (direct_runner.py:301).
- **Mirror machinery:** takeover requires the UI toggle AND master
  switch AND no veto AND no kill switch; never starts runs; own-run
  double-mirror guard present and tested; '2' rejected phone-side and
  never reaches the engine (bridge.py:723-726, tested); relay only
  while a card is actually open (`approval_sig`), else text = steer;
  steer 404 handled honestly; `/stop` covers mirrored runs
  (main.py:303-310) and the mirror loop checks the kill switch
  (main.py:549); veto shutdown cancels the mirror task via run()'s
  finally (main.py:654-657).
- **Modes:** gateway choices {auto, simple, pipeline, automap} are real
  engine modes (core/chat_run.py:1076-1091); aliases chat/direct→simple;
  duo (code_duo) deliberately not exposed.
- **Transcript/CAS:** GET-then-PUT with one 409 retry adopting server
  messages (bridge.py:96-114; workspace variant tested at
  test_gateway_bridge.py:557-573); `_ref` blob fields never parsed,
  only round-tripped.
- **Workspace confinement** (engine-side) and approval fail-closed deny
  unchanged from the first audit (test_gateway_confinement 19/19 in the
  83-suite run).
- **Tests/lint:** 83/83 suites PASS (incl. 13 gateway suites, P10
  takeover section in test_gateway_bridge.py:459-590); ruff clean;
  `except Exception` absent from hivemind_gateway/; lint gate covers the
  package (tests/test_gateway_lint_gate.py:35).
- **Static paths:** index.html references only static/app.js,
  static/feature_search_core.js, static/style.css (+ server-served
  favicon) — all exist; the `.ico` greps in app.js are JS property
  false-positives; no other image refs.

## NOT tested

- Any live Telegram or live engine path (engine down at audit time; the
  single permitted /health call returned connection-refused and was
  handled). The P10 real-run round (card on phone ≤5 s, deny stops the
  tool, steer lands, veto mid-run) remains owner-side evidence.
- N1 verified as the auth-layer repro, not end-to-end through a live
  Telegram update.
- P2 SSRF acceptance list items 2-11 (redirect/IPv6/decimal-IP evasions)
  and the browser-tool SSRF detour (P1) — still open engine-side.
- Junction/8.3 traps on a live filesystem (matrix is content-based).
- pip-audit / hash-pinned lockfile (requirements-gateway.txt pins
  httpx==0.28.1 without hashes; documented as a WP6 release-check task).

## Open questions

1. Should the mirror approval relay (phone can approve run_bash of UI
   runs) be re-scoped once the WP3 remote profile lands, or is
   "phone answer == owner UI click" the accepted permanent model?
2. gateway.lock: add to update.bat prune-protect for symmetry, or drop
   it from /XF (it never lives in BASE)? Either — but the lists should
   agree.
3. Should absurd ctx values be clamped engine-side (resolve_ctx /
   server lift) so the UI POST path is covered too, instead of only
   clamping the gateway's keyboard?
4. Ship start_gateway.bat in WP6 as the brief planned, or keep the
   documented copy-safe command as the supported start path?
