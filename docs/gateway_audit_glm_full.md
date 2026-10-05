# HiveMind Telegram Gateway — Full Independent Audit (GLM-5.3)

- **Date:** 2026-10-05
- **Audit target:** worktree `feature/telegram-gateway` @ `073bef5` (`<repo>\repo_gateway` on the owner's machine, clean except two untracked docs)
- **Baseline:** three prior audits — `docs/gateway_security_audit.md` (F1–F10), `docs/wp3_proposals.md` (P1–P10), `docs/gateway_security_audit_deep.md` (N1–N9, fixes in `073bef5`)
- **Mandate:** verify and sharpen the existing record, then hunt for what three audits missed. Every claim carries `file:line`. No secrets, owner IDs, or session contents appear in this report.
- **Method:** full read of the gateway package (`hivemind_gateway/*`), the engine surfaces it touches (`server.py`, `routers/core.py`, `routers/config.py`, `routers/chats.py`, `infra/security.py`, `tools/runner.py`, `tools/websearch.py`, `tools/definitions.py`, `utils/file.py`, `core/chat_run.py`, `core/direct_runner.py`, `core/duo_runner.py`, `hive_functions/num_ctx_config.py`, `sse/events.py`), packaging (`update.bat`, `deploy/package_release.bat`, `.gitignore`, requirements), plus the live settings (boolean states only). Local tests: `tests/run_regressions.py` **83/83 PASS**, `node --check static/app.js` clean, encoding suite green.

---

## 0. Executive summary

The core transport invariants hold at `073bef5`: owner-only choke point (`hivemind_gateway/send.py:28-34`), fail-closed master switch (`hivemind_gateway/main.py:511-527`), triple off-switch, at-most-once offset handling (`main.py:639-642`), token hygiene (`hivemind_gateway/redaction.py:14-25`, `main.py:249-250`), and the N1–N6 fixes are genuinely in the tree with regression tests.

The audit's headline results are **one invariant that is weaker than documented (G1)** and **three new code-level findings (G2–G4)**:

1. **G1 (MEDIUM-HIGH):** The "8 approval tools are auto-denied from the phone" guarantee is conditional on the engine-side toggle `duo_action_approval_enabled`, which **defaults to False** (`settings.py:390`), is **currently False on the live system**, and is neither forced nor even mentioned as a prerequisite by the gateway or the setup guide (`docs/gateway_setup.md:143-144`). With the toggle off, a `/mode auto|pipeline` phone run executes `run_bash`, `write_file`, `git_commit` etc. with **no approval and no auto-deny** — the gateway's auto-deny (`hivemind_gateway/bridge.py:206-215`) only reacts to `approval_request` events the engine never emits in that state. Today's defaults (`mode=simple`, tier `readonly`) limit phone runs to web tools, which masks the gap.
2. **G2 (MEDIUM):** In the P10 approval relay, a *late second decision* (UI click, then phone tap) is stored as a pre-decision that **silently approves the next gated call** in that run — no card is shown for it (`routers/core.py:230-239`, `tools/runner.py:443-444, 519-542`). The gateway sends no `decision_id` (`hivemind_gateway/hive_client.py:119-122`), so the stale-nonce drop (`routers/core.py:226-227`) cannot catch it, and the tool-mismatch guard is inert because the endpoint stores no tool field (`core.py:237` vs `runner.py:512`).
3. **G3 (MEDIUM):** `web_fetch`'s SSRF guard never resolves DNS names and does not restrict ports (`tools/websearch.py:270-297`) — any hostname that resolves to loopback/private ranges (public wildcard DNS, rebinding) passes and can GET engine read surfaces (`/run/journal`, `/chats`, `/settings`), feeding run transcripts back into model context. Sharpened with a **negative result**: decimal/hex IPv4 forms (`http://2130706433/`) are *not* exploitable here — Windows `getaddrinfo` rejects them (verified locally).
4. **G4 (MEDIUM):** The CSRF guard compares `Origin` netloc to `Host` for non-GET requests that carry an Origin header (`infra/security.py:23-37`). DNS rebinding satisfies it trivially and makes the page same-origin for reads: a malicious website can `POST /settings` — arbitrary keys, incl. `git_token`, both telegram toggles, and `duo_action_approval_enabled` (`routers/config.py:384`) — and read every GET endpoint.

No release *blocker* beyond G1's documentation/invariant mismatch; G1 has a one-line honest fix (see §5).

---

## 1. Verification of the prior record

### 1.1 F1–F10 (`gateway_security_audit.md`)

| ID | Verdict at `073bef5` | Evidence |
|----|----------------------|----------|
| F1 | **CONFIRMED, sharpened by G1/G3** | Phone runs get the mode's full tool list; only `_APPROVAL_TOOLS` can produce approval events (`tools/runner.py:282-286`), and only when the gate is on (`runner.py:493-497`). Web tools remain the always-open exfil channel (`tools/definitions.py:359, 368-372, 378-380`). |
| F2 | **CONFIRMED** | Ungated: `undo_last`, `run_tests`, `browser`, `web_search`, `web_fetch`, `stop_background`, `get_background_output` — all inline handlers (`runner.py:926 ff.` dispatch map; mode allowlists `definitions.py:355-381`). `run_tests` still not added to `_APPROVAL_TOOLS`. |
| F3 | **CONFIRMED** | `HiveClient.health()` exists (`hive_client.py:59-63`) and no gateway module calls it (grep: only the definition). Startup remains blind to a wrong-port/wrong-process engine. |
| F4 | **FIXED (re-verified)** | `ensure_enabled` (`main.py:511-527`), UI veto absent-key semantics (`main.py:530-540`), kill-switch checks in both loops (`main.py:549, 604`), `gateway.toml.example` default `telegram_enabled = false`. |
| F5 | **CONFIRMED** | Global in-memory lockout, 5 attempts, until restart (`auth.py:167-170`; single `PairingManager` at `main.py:246`). Sharpened in G15. |
| F6 | **CONFIRMED** | Token from env/Credential Manager only (`main.py:56-71`); redaction attached with the env token (`main.py:249-250`); URL-pattern net (`redaction.py:14-15`). |
| F7 | **CONFIRMED** | No audit module; `gateway_audit.jsonl` written by nothing; `/lock` is a stub (`main.py:332-335`). |
| F8 | **CONFIRMED (scope)** | Same-user local malware reads token/state/kill switch; not contained by design. G4 sharpens the *remote-browser* part of this boundary. |
| F9 | **CONFIRMED** | Owner Telegram account = crown jewel; 2FA (two-step verification) is a hard setup prerequisite (`docs/gateway_setup.md:11-12`). P10 grows blast radius to steering/approving UI runs. |
| F10 | **CONFIRMED, sharpened by G7** | Busy heuristic is gateway-side only (`bridge.py:158-163, 271-286`); `/stream` still starts unconditionally; the journal heuristic itself can look at the wrong run (G7). |

### 1.2 P1–P10 (`wp3_proposals.md`) — status unchanged unless noted

P7 partially shipped (mode rides the body only, `bridge.py:346-373`, `hive_client.py:164-167`), P10 shipped incl. T (`server.py:1246-1251`, `core/direct_runner.py:301`) — both re-verified. P1–P6, P8, P9 remain proposals; this audit adds evidence for P2 (G3) and P6. **P9 note:** `/setModel` still does not affect direct/simple runs — confirmed, the direct runner reads only `direct_tools_enabled` from the override path (`direct_runner.py:301-312`).

### 1.3 N1–N9 (`gateway_security_audit_deep.md`) and commit `073bef5`

| ID | Verdict | Evidence |
|----|---------|----------|
| N1 | **FIXED** | Byte-encoded compare (`auth.py:160-165`); non-ASCII guess = safe deny + counted; test `tests/test_gateway_auth.py:147 ff.` |
| N2 | **FIXED** | `_full_command_from_frames` digs untruncated `extra.cmd/url/...` (`bridge.py:731-752`); engine keeps extras whole (`sse/events.py:54-56, 71`); tests `tests/test_gateway_bridge.py:541-557`. |
| N3 | **FIXED** | Mirror containment catch tuple extended (`bridge.py:609-613`). |
| N4 | **FIXED** | Cache buster `app.js?v=20261005-14` (`index.html:1944`); mirror-toggle init line present (`static/app.js:11408`). |
| N5 | **FIXED** | `isascii()+isdigit()` + arity (`bridge.py:513-514`); 131072 clamp on both ctx keys (`bridge.py:392-399`); tests `tests/test_gateway_bridge.py:560-576`. |
| N6 | **FIXED** | `HTTPError` caught in `stop()` (`bridge.py:304-308`) and `_owner_bridge_call` (`main.py:344`). |
| N7 | **STILL OPEN (confirmed)** | Mirror skips only `rid == _own_run_id()` (`bridge.py:654-656`); between `POST /stream` and the `run_id` SSE event the own run's `run_id` is `None` (`bridge.py:172-178` vs `198-201`) → own run announced as takeover. Amplified by G7. |
| N8 | **CLOSED** | Leak regex widened (`deploy/package_release.bat:45`: `gateway_state.tmp`, `gateway_audit.jsonl.\d+`); `gateway.lock` in prune protect (`update.bat:214`, commit `c569469`). Minor asymmetry noted in G14. |
| N9 | **STILL OPEN (confirmed)** | `/workspace` stores/PUTs any string (`bridge.py:790-820`); engine `PUT /chats/{id}` applies it unvalidated (`routers/chats.py:625-626`) — in contrast to `POST /settings`, which *does* existence-check `workspace` (`routers/config.py:294-304`). Sharpened in G11. |

**Numbering discrepancies to fix at merge time (documentation only):**
- The commit message of `073bef5` labels the leak-regex widening "N9", but the deep doc files it under **N8**; the doc's N9 (`/workspace` validation) has **no corresponding change**.
- `wp3_proposals.md:220-231` carries an *older, different* N1–N7 numbering than the deep audit's N1–N9 — same labels, different content. Cross-references should always name the doc.

---

## 2. New findings

Severity scale: HIGH / MEDIUM / LOW / INFO (matching the prior audits).

### G1 — The phone-run "auto-deny" invariant is conditional on an engine toggle that defaults OFF (MEDIUM-HIGH)

**Chain of evidence:**
- The engine gate fires only when `duo_action_approval_enabled` is true, read from the **global** engine settings (`tools/runner.py:493-497`; same in `_approval_active_for`, `runner.py:416-423`). Shipped default: **False** (`settings.py:390`; `docs/settings.md:30`). Live system: **False** (boolean state verified in `live/settings.json`).
- The gateway never sends that key in the run body — `_stream_overrides` builds only model/ctx/`direct_tools_enabled` (`bridge.py:379-403`; body assembly `hive_client.py:164-169`). It also *could not* today: the gate reads global settings, not the per-run snapshot (`_run_settings` merged at `core/chat_run.py:153-155` is not what `runner.py:494` consults).
- The gateway's auto-deny only reacts to `approval_request` SSE events (`bridge.py:206-215`) — events the engine emits exclusively through the gate (`runner.py:365-366, 455-457, 636-639`). Gate off ⇒ zero events ⇒ the auto-deny is dead code and every `_APPROVAL_TOOLS` call executes directly (`runner.py:1365-1374` falls through).
- The setup guide nevertheless promises: tools that "need an approval (shell commands, file writes, git commits) are denied automatically" (`docs/gateway_setup.md:143-144`) — with no mention of the engine-side prerequisite. The realrun plan knew ("`duo_action_approval_enabled` ANstellen… Default", `docs/gateway_realrun_wp2.md:125`), but that operational knowledge never reached the user-facing guide.

**Current exposure (why this is not HIGH outright):** live defaults are `mode=simple` (`settings.py:48`, live confirmed) with direct tier `readonly` = web tools only (`tools/definitions.py:378`; tier resolution `core/direct_runner.py:301-312`; live tier confirmed `readonly`). In that configuration the phone cannot reach `run_bash` regardless of the gate. **But** `/mode auto|pipeline` is a first-class gateway command (`bridge.py:55-56, 346-373`) that immediately selects full-tool modes (`duo_full`/`tool_agent`, `definitions.py:359, 371-372`) — with the gate off, a phone free-text run then executes shell/file/git tools ungated. The same is true for mirrored UI runs (no approvals to relay at all).

**Impact:** the injection backstop the docs advertise for phone runs does not exist in the default configuration; the owner believes commands are denied when they are not.

**Recommendation (pick one, ideally both):**
1. Docs/UI: state the prerequisite in `gateway_setup.md` and show the gate state in the gateway `/status` output (one `GET /settings` read the gateway already does).
2. Code: whitelist `duo_action_approval_enabled` (and only that key) into the `/stream` override intake (`server.py:1225-1251`) *and* make `_check_action_approval` consult the run-settings snapshot for it, then have the gateway force it to `true` for `source`-tagged runs — this is also the natural hook for the P1 allowlist work.

### G2 — Late second approval decision pre-approves the NEXT gated call (MEDIUM)

**Scenario:** an approval card is visible in the UI *and* on the phone (P10 mirror). The owner answers in the UI; within the mirror's ≤5 s tick window the phone tap also fires (`mirror_send` accepts 1/3 whenever `approval_sig` is set, `bridge.py:767-780`; the card is only un-latched on the next tick, `bridge.py:728-729`).

**Code path:**
1. UI decision resolves the waiting pause (`routers/core.py:218-229`).
2. The phone decision arrives: `is_pause_waiting` is now false → falls through to the pre-decision store (`core.py:230-239`), popping `_pending_approvals` and storing `{"answer": "1", "note": ""}` — **without a `tool` field** (`core.py:237`).
3. Next gated call in the same run: `stage_approval_card` **skips staging** because a pre-decision exists (`runner.py:443-444`), and the execution-time gate **consumes** the stored "1" → the call runs with no card ever shown (`runner.py:519-542`; write tools additionally earn the path free-pass, `runner.py:534-539`).
4. The two existing guards do not fire: the decision-nonce drop needs a `decision_id`, which the gateway never sends (`hive_client.py:119-122` vs `core.py:226-227`); the tool-mismatch guard reads `_pre.get("tool")`, which the HTTP route never sets (`runner.py:512` vs `core.py:237`). `_approval_expired` covers only the timeout-deny path (`runner.py:728`, `core.py:234-235`) — by design, per its own comment ("storing it as a pre-decision would silently approve the NEXT gated call", `runner.py:320-322`): the non-timeout late-decision case has exactly that hole.

**Impact:** exactly one gated call per race executes without user consent — the owner answered on two surfaces and the *second* answer silently authorizes a *different, later* call. Requires the approval gate to be ON (today: off, see G1 — latent until enabled).

**Fix:** send the `decision_id` (the gateway already has it in the pending card signature, `bridge.py:709`) in `decide_approval` (`hive_client.py:119-122`), and store the `tool` name in the pre-decision (`core.py:237`). Both are one-liners and make the existing engine-side guards work for the phone surface.

### G3 — `web_fetch` SSRF: hostnames are never resolved, ports unrestricted (MEDIUM)

`_guard_fetch_url` checks scheme, literal `localhost`/`.local`/`.internal` suffixes, and IP *literals* against private/loopback/CGNAT ranges (`tools/websearch.py:270-297`). The `except ValueError: pass` branch (`websearch.py:295-296`) sends every DNS name through unresolved:

- Any public hostname that resolves to `127.0.0.1` or a LAN IP passes (wildcard DNS services, attacker-controlled zones, rebinding) — the OS resolver does the rest.
- No port restriction: the loopback engine (`127.0.0.1:8001`) and SearXNG (`:8888`) are reachable by hostname.
- Redirect hops re-run the guard on each hop URL (`websearch.py:314-318`) — same hostname-level gap; the *resolved* IP is never pinned between hops.

`web_fetch` is GET-only, but the engine's GET surface is sensitive: `/run/journal` (full run transcripts incl. tool outputs, `server.py:1475-1494`), `/chats` and `/chats/{id}` (session content), `/settings` (`git_token` masked — the only masked field, `routers/config.py:144-145`), `/approval/pending/{run_id}`. **Exfil chain:** injected/steered URL → `web_fetch` → rebinding hostname → journal transcript → returned into model context → relayed to Telegram. This sharpens F1/P2 with a concrete vector for the still-open P2 items.

**Verified negative (important for P2 triage):** the classic decimal/hex/short-form IPv4 bypasses (`http://2130706433/`, `http://0x7f000001/`, `http://127.1/`, `http://0177.0.0.1/`) are **not exploitable on this stack** — Windows `getaddrinfo` rejects all of them (`Errno 11001`, verified locally on this host, no egress). The residual vectors are DNS names and IPv6 literal forms the `ipaddress` module does parse (`::ffff:127.0.0.1` is unwrapped and blocked, `websearch.py:287-288` — correct).

**Fix:** resolve the hostname before the request (and after every redirect hop) and apply the same IP-range check to the resolved address; optionally restrict ports to 80/443. This is P2's acceptance list, now with the engine-side gap precisely located.

### G4 — CSRF guard is not a DNS-rebinding defense; `POST /settings` writes arbitrary keys (MEDIUM)

`_csrf_origin_guard` runs only for non-GET/HEAD/OPTIONS requests **that carry an Origin header**, comparing `urlsplit(origin).netloc` to the `Host` header (`infra/security.py:23-37`, registered at `server.py:331-333`). Consequences:

- **No-Origin requests pass** (intended for CLI — but also every non-browser local caller and any SSRF primitive that can POST).
- **DNS rebinding defeats the check:** a page at `http://attacker.example/` whose DNS later resolves to `127.0.0.1` issues `fetch("http://attacker.example:8001/settings", {method:"POST", ...})` — `Host` and `Origin` are both `attacker.example:8001`, netlocs match, request passes, and because the origin is now same-origin the response is readable. Browser Private-Network-Access mitigations vary by engine and cannot be assumed.
- The `except Exception: pass` wrapper fails **open** (`security.py:35-36`).

Writable surface: `POST /settings` ends in `settings.update(data)` (`routers/config.py:384`) — arbitrary keys survive (only `active_preset` popped unconditionally, `ui_rev`/`settings_force` consumed as guards): **`git_token` is writable** (can be replaced — hijacks future pushes toward an attacker remote or breaks them), `telegram_gateway_enabled` (kills the phone link), `telegram_mirror_enabled` (silently disables/…— note absence already means off), `duo_action_approval_enabled` (toggle G1's gate off even where the owner enabled it), `duo_planner_ctx_cap` (raise the planner cap, `core/duo_runner.py:1054-1057`), `searxng_host`, `workspace` (this one *is* existence-checked, `config.py:294-304`). GET endpoints return everything except the masked `git_token` (`config.py:140-214`) — model registry, agent config, workspace path.

**Fix:** validate `Host` against an allowlist (`127.0.0.1:<port>`, `localhost:<port>`) instead of echoing it back — this kills rebinding in one line and keeps CLI working (no Origin). Optionally require `application/json` Content-Type for POSTs. This is the engine-side half of the trust boundary; the loopback-process half is G4-adjacent F8/WP6 territory (see §4 inventory).

### G5 — Telegram/engine failure during `_finish` latches `active_run` → permanent busy-lock (LOW-MEDIUM)

The run's `finally` block awaits `_finish(...)` **before** `_clear_run()` (`bridge.py:226-230`). `_finish` can raise exception types the outer catch in `start_text_run` (`bridge.py:144`, `HiveUnreachable/HTTPError/OSError`) does not cover — concretely `TelegramApiError` from the answer delivery (`bridge.py:255-259` via `send.py:46`) — and the assistant-turn write inside `_finish` catches only `(HiveUnreachable, OSError)` (`bridge.py:260-264`) while `_write_turn` can raise `HTTPStatusError` (`bridge.py:112`). If `_finish` raises, `_clear_run` is skipped, the exception escapes to the poll-loop guard (`main.py:645-651`, logged once), and the **persisted** `active_run` stays latched: every further phone text answers "Es läuft bereits ein Lauf" (`bridge.py:158-163`) and `/stop` never clears it either (`bridge.py:290-313`). Only a restart heals (`recover_orphan_run`, `main.py:427-450`).

**Fix:** wrap the `_finish` call (not `_clear_run`) in its own contained try/except, or clear before finishing — the busy invariant must not depend on Telegram's health.

### G6 — Poll-loop exception guard narrower than the mirror-loop guard (LOW)

`073bef5` hardened the mirror task against `TypeError/KeyError/AttributeError` (`bridge.py:609-613`, N3) but the **main poll loop** still catches only `TelegramApiError, HiveUnreachable, HTTPError, OSError, ValueError, PolicyViolation` (`main.py:645-646`). N1 was exactly the crash class a phone-controlled payload could trigger. Current parse/dispatch paths look type-safe (`auth.py:51-85` is defensive), so this is defense-in-depth — but the asymmetry invites the next N1.

### G7 — All gateway journal consumers use the "most recently active" journal (LOW)

`GET /run/journal` without `run_id` returns the journal with the newest `ts` (`server.py:1488-1494`). Every gateway consumer uses that form: the busy heuristic (`bridge.py:271-286`), the mirror tick (`bridge.py:640`), and restart recovery (`main.py:439`). With two runs alive (phone + UI), each consumer can act on the wrong run — the mirror may adopt or ignore the wrong one (this is also N7's amplifier: during the phone run's POST→`run_id` window, the *own* run is necessarily the most recent, `server.py:1362-1372`), and `_journal_busy` reports a foreign `run_id` to the owner. `mirror_tick` does pass the selected `rid` to approvals/steer consistently afterwards — the error is in *selection*, not in the later scoping.

**Fix:** fetch-by-`run_id` where one is known (the engine endpoint already accepts it, `server.py:1481-1487`), and for the own-run exclusion skip takeover whenever `active_run` exists with `run_id is None` (the deep audit's proposed N7 fix — still the right one).

### G8 — Steering/pending-setup ordering edges (LOW)

- `mirror_send` has no length cap: `mirror_intercept` runs **before** the `max_text_chars` check (`main.py:363-374`), so steering texts bypass the 4000-char limit into `POST /api/run/{id}/steer`, which itself caps images but not text (`routers/core.py:113`). Context pollution / minor DoS from the owner's own fat fingers.
- `consume_setup` runs before `mirror_intercept` (`main.py:355-366`): with a pending `/setModel` flow *and* an active mirror, an approval answer like "1" is answered with "Erwartet 3 Zahlen" instead of reaching the card (owner-only confusion; the card stays open, next tick re-prompts).

### G9 — Editing a recent phone text triggers a second run (INFO/LOW)

`parse_update` fills `date` from the *original* `message.date` for `edited_message` (`auth.py:70`); edits older than 60 s are dropped as stale (`main.py:421-424`) — good — but an edit **within 60 s** of the original arrives with a new `update_id` (dedupe passes, `state.py:160-168`), classifies as owner non-command text (`main.py:486-487`) and starts a second run. Owner-initiated only; interacts with F10 (no server-side single-run guard).

### G10 — `gateway.toml` (token-bearing risk) lives inside the worktree (INFO)

The live `gateway.toml` sits next to the code in `repo_gateway` (untracked; `.gitignore:121-134` covers `gateway.toml` and all state files; the release leak-regex and `update.bat` protect list cover it). Residual leak paths: `git add -f`, ad-hoc zips of the worktree, or `HIVEMIND_GATEWAY_HOME` pointing into the install dir (see G14's asymmetry). Consider relocating to `%LOCALAPPDATA%\HiveMindGateway\gateway.toml` — the loader already searches (`hivemind_gateway/config.py` path resolution), and the state dir is prune-safe by construction.

### G11 — `/workspace` moves the phone-run containment root, unvalidated (INFO; sharpens doc-N9)

The chat workspace **is** the tool-confinement root for phone runs. `/workspace <path>` PUTs any string without existence or sanity checks (`bridge.py:790-820`; engine side `routers/chats.py:625-626`), unlike `POST /settings` which rejects non-existent workspaces (`config.py:294-304`). Combined with G1 (gate off) and `/mode auto`, the paired phone can point the agent at `C:\` and get machine-wide read/write/exec — all owner-authorized, but the current UX sells it as a harmless convenience. Add the engine existence call (deep-audit proposal) and a warning line for roots outside the previous workspace.

### G12 — Preset load from `/setModel` is global (INFO, documented in-flow)

`POST /presets/{name}/load` changes engine-wide profile state for UI and phone alike (`hive_client.py:141-145`); the gateway warns in the question text (`bridge.py:496`). Re-verified; no action beyond keeping the warning.

### G13 — Engine-side ctx intake remains unclamped (INFO; sharpens deep-audit open question)

Server intake coerces ctx keys via `int()` with 0-fallback and no upper bound (`server.py:1235-1244`); the merge is a blind `dict.update` (`core/chat_run.py:153-155`); coder ctx has no cap anywhere (`hive_functions/num_ctx_config.py:166-169`), planner cap is `131072` but *overridable upward* via settings (`core/duo_runner.py:1054-1057`). The phone path is safe (gateway clamps, `bridge.py:392-399`, N5); the residual writers are loopback/rebinding callers (G4). VRAM-DoS severity stays within the loopback trust question.

### G14 — Supply chain / packaging consistency (INFO)

- Gateway pins `httpx==0.28.1` exactly (`hivemind_gateway/requirements-gateway.txt:16`); the engine floors at `httpx>=0.27` (`requirements.txt:5`, `pyproject.toml:12`). No hash pinning anywhere — acceptable for this threat model, noted for completeness.
- `update.bat`'s robocopy `/XF` and prune protect lists (`update.bat:214, 243`) include the core gateway files but **not** `gateway_state.tmp` or rotated `gateway_audit.jsonl.N` (the release leak-regex does, `deploy/package_release.bat:45`). Harmless while state lives in `%LOCALAPPDATA%`; becomes relevant exactly if G10's relocation is done in reverse (state into the install dir).
- `docs/gateway_security_audit_deep.md` and `docs/gateway_audit_installer_ui.md` are **untracked** (`git status`) — the deep audit that `073bef5`'s message references is not in the tree history. Commit them (they are already written secret-free) or the fix-commit's rationale is unreconstructable from the repo alone.
- Live deployment runs v1.2.4 without the gateway (CHANGELOG comparison) — consistent with "gateway ships in the next release"; the live booleans checked for this audit reflect a pre-gateway install plus a prepared `gateway.toml`.

### G15 — Pairing window notes (INFO; sharpens F5)

Pairing entropy is sound (8 random bytes → ~13 base32 chars, `auth.py:27-29`); brute force is infeasible, the lockout (5 global attempts until restart, `auth.py:167-170`) is an availability annoyance only, and since N1 non-ASCII guesses count as failures without crashing (verified in test). No change to the accepted risk — but note the lockout also silences the *legitimate* owner if a stranger burns the 5 attempts first (documented F5 behavior).

---

## 3. What was verified and held (no action)

- **Owner-only choke point:** every outbound byte goes through `send()`, which refuses any non-owner chat and everything while unpaired (`send.py:28-34`); the bridge's messenger wrappers all route through it (`main.py:256-273`).
- **Pairing hygiene:** one-time code, TTL 300 s, consumed on success, disabled afterwards (`auth.py:141-173`); forwarded `/pair` dropped pre-classification (`main.py:493`); non-private rejected (`main.py:386-388`); no answer to strangers (`main.py:389-407`).
- **At-most-once:** offset persisted before processing (`main.py:639-642`), backlog drop at startup (`main.py:587-596`), dedupe window bounded (`state.py:27, 160-168`), corrupt-state quarantine (`state.py:118-129`).
- **Token hygiene:** env/Credential-Manager only (`main.py:56-71`), token-like keys in `gateway.toml` are a startup error (`hivemind_gateway/config.py` `_check_no_secrets`), log redaction with generic URL net (`redaction.py:14-25`), answer-side secret filter (`render.py:29-37`), link previews disabled at transport (`telegram_api.py:92, 104`).
- **P10/T end-to-end (INJECTION cannot reach the body):** the override body keys originate exclusively from gateway state written only by owner commands — `run_overrides` solely via `consume_setup` with strict digit validation (`bridge.py:513-517, 539-560`), `tools` solely via `/tools` (`bridge.py:832-838`), `mode` solely via `/mode` (`bridge.py:362-371`); model output never writes gateway state; phone free text reaches the body only as `q`; steering goes to the separate `/steer` surface (`routers/core.py:101-135`). The `direct_tools_enabled` lift (`server.py:1246-1251`) is therefore reachable only by the gateway (or a loopback process — the WP6 boundary, not injection).
- **Gateway→engine transport:** loopback-only base URL enforced at config validation (`hivemind_gateway/config.py:38-44`), `trust_env=False` (no proxy detour, `hive_client.py:32-36`).
- **Windows path matrix (static re-test):** junction walk on every ancestor (`utils/file.py:136-151`), protected engine files incl. all `.py` under the engine root (`file.py:15-41`), containment via `resolve()+relative_to` (`file.py:152-166`). 8.3 names are normalized by `resolve()`; ADS names stay inside containment (impact-free); `C:foo` falls through the absolute-regex (`file.py:96-99`) but pathlib joins drive-relative paths onto the same drive and `resolve()` anchors them at the process CWD — contained or denied, no breakout. **One nuance worth knowing:** the junction walk `lstat`s every ancestor *before* containment denies, so a UNC path (`\\host\share\...`) triggers SMB name resolution (potential NTLM handshake to an attacker host) during the check itself — requires an injected UNC path in a file tool, exotic but real on Windows.
- **Engine bind:** `127.0.0.1` default, env-overridable (`run.py:168-169`); port resolution env > settings > 8001 (`run.py:171-176`).

**Loopback trust inventory (for the WP6 decision):** unauthenticated engine endpoints — `/stream` (`server.py:1178`), `/abort*` (`core.py:34, 83, 147, 158`), `/pause`, `/resume`, `/api/run/{id}/resume`, `/api/run/{id}/steer` (`core.py:92, 138, 54, 101`), `/approval/pending|decide` (`core.py:178, 190`), `/chats` CRUD (`routers/chats.py:386, 440, 610`), `/run/journal` (`server.py:1475`), `/settings` GET/POST (`config.py:140, 265`), `/presets*` (`config.py:499-563`), `/health`. `HIVEMIND_INTERNAL_TOKEN` guards only `/internal/tool/exec` (`core.py:270-275`), where `workspace_lock` is also derived server-side (`core.py:262-267`). Recommendation unchanged from the briefing's line of thinking: extend the token to gateway-originated traffic (both processes already share an env), prioritising the state-changing endpoints (`/stream`, `/chats` PUT, `/approval/decide`, `/abort*`, `/steer`, `/settings`, `/presets/load`); that simultaneously shrinks G4's blast radius to read-only.

---

## 4. NOT tested

- **Live engine behavior:** `GET http://127.0.0.1:8001/health` returned no response during the audit window (engine not running at the time) — no live endpoint behavior, no live run flows.
- **Live gateway flows** (pairing over real Telegram, real runs 1–7, mirror/approval relay against a live UI run) — per the briefing these are the owner's own next steps; this audit is static plus unit-level.
- **DNS rebinding / CSRF PoCs** (G4) and **SSRF via resolving hostname** (G3) — argued statically; no network activity beyond the failed `/health` (the numeric-IPv4 `getaddrinfo` checks were local resolver calls with no egress).
- **SMB/NTLM behavior of the UNC-`lstat` nuance** — no UNC paths were touched on this host.
- **Browser-specific PNA enforcement** for G4 (no browser automation used).
- **`tests/test_bat_encoding.py` as a standalone invocation** — it ran as part of the 83-suite regression run (line 2 of the log); not invoked separately.

## 5. Open questions

*(Resolved on 2026-10-05 — see the fix addendum below; Q3 remains open as the one deliberate follow-up.)*

---

## 6. Fix addendum (applied 2026-10-05, uncommitted)

All findings with a code or docs remedy were fixed in the working tree (17 files changed, 2 new test suites; regression run **85/85 PASS**, ruff clean, `node --check` clean, bat encoding green). Nothing is committed yet.

| Finding | Fix (file:line anchors approximate, see diff) |
|---------|-----------------------------------------------|
| **G1** | End-to-end gate force: `tools/runner.py` adds `_approval_gate_run_override` ContextVar + `_approval_gate_on()` (override wins, else global toggle; body can only raise to ON); `core/chat_run.py` publishes the merged run setting into the ContextVar; `server.py` lifts `duo_action_approval_enabled` from the `/stream` body; `hivemind_gateway/bridge.py` `_stream_overrides()` now always sends it `true` for phone runs; `/status` states the invariant; `docs/gateway_setup.md` rewritten to describe the actual mechanism. |
| **G2** | `hivemind_gateway/hive_client.py` `decide_approval()` carries `decision_id` + `tool`; the mirror stores both from the relayed card (`bridge.py` mirror state + `_mirror_reset` helper), `mirror_send` echoes them and reports a `duplicate` route honestly; `routers/core.py` records resolved decision_ids, drops duplicates (`routed: duplicate`), and stores `tool` in the pre-decision; `tools/runner.py` tool-match guard made effective (empty tool = legacy-compatible). |
| **G3** | `tools/websearch.py`: `_guard_ip_literal` refactor, new `_guard_fetch_port` (80/443 only) and `_guard_fetch_resolved` (judges EVERY resolved address; runs in a worker thread), both applied to the initial URL and every redirect hop via `_guard_fetch_target`. Numeric-IPv4 forms remain a non-issue on this stack (see §G3). |
| **G4** | `infra/security.py`: Host allowlist (`127.0.0.1`/`localhost`/`::1`, extendable via `HIVEMIND_TRUSTED_HOSTS`) enforced for ALL methods — kills DNS rebinding for reads and writes; fail-open `except: pass` wrapper removed. |
| **G5** | `bridge.py` `_start_text_run_inner` finally: `_finish` failures (Telegram/engine) are contained and logged, a visible failure note is attempted, and `_clear_run()` always runs — no more busy-latch. |
| **G6** | `main.py` poll-loop catch extended by `TypeError/KeyError/AttributeError` (parity with the N3-hardened mirror loop). |
| **G7/N7** | Mirror tick fetches the journal scoped by `run_id` once a session is active (`hive_client.journal(run_id)`), and skips the takeover decision while `active_run` exists with `run_id None` (own-run POST window). |
| **G8** | Mirror intercept now precedes the pending-setup consumption in `main.start_owner_run`; steering text is capped at `max_text_chars` in `mirror_send`. |
| **G9** | `edited_message` never re-runs: commands in edits still work, free-text edits are dropped with a log line (`main.py`). |
| **G10** | `hivemind_gateway/config.py` `resolve_config_path` now also finds `gateway.toml` in the state home (`%LOCALAPPDATA%\HiveMindGateway`) — a prune-safe home outside the code tree; existing installs keep precedence; documented in `gateway.toml.example`. |
| **G11** | `/workspace` refuses non-existent paths and drive roots before the PUT (gateway-side, same machine) — doc-N9 closed. |
| **G13** | `server.py` ctx intake clamps at 262144 (intake sanity; planner 131072 cap still applies downstream). |
| **G14** | `update.bat` /XF and prune-protect lists extended (`gateway_state.tmp`, `gateway_audit.jsonl.*`). Still open by design: commit the two untracked audit docs. |
| G12/G15, F5, F7 | No change — documented/accepted, or planned as WP6 (audit log). |

**New tests:** `tests/test_webfetch_guard.py` (14 checks: resolved-IP rebinding, v4-mapped, LAN, one-bad-among-many, port allowlist, literal negatives), `tests/test_csrf_host_guard.py` (15 checks: rebinding Host blocked on GET+POST, loopback passes, Origin checks intact, env extension), `tests/test_gateway_bridge.py` +8 (G1 force, G2 metadata/duplicate, G5 no-latch, G7 scoping, N7 skip, G8 cap, G11 refusals), `tests/test_gateway_commands2.py` +3 (G8 ordering, G9 edits), `tests/test_action_approval.py` +4 (decide endpoint stores tool, drops duplicates). Registrierte Suites: 85.

**Deliberately NOT done (open, Q3):** extending `HIVEMIND_INTERNAL_TOKEN` to gateway↔engine traffic — the Host allowlist + read-only GET surface is the interim boundary; the token rollout remains the engine-side hardening step for the state-changing loopback endpoints.

## 7. Follow-up round (2026-10-05, after external review)

Three additions from the review round, all tested (85/85 suites, ruff clean):

- **`/help` instruction list** (owner request): `hivemind_gateway/commands.py` now carries a structured German quick-start (`HELP_TEXT` — steering commands, configuration commands, and the two-line safety contract incl. the forced-gate guarantee and mirror-card 1/3 answers); `main.py` serves it verbatim.
- **Version-skew detection (G1 hardening):** `server.py` `/health` now reports `"gateway_overrides": true`; the gateway probes it at startup (`main.py` F3 check) — a missing marker means an OLD engine that would silently ignore the forced-gate body key: loud console warning, `engine_gate_support=False`, and `/status` downgrades from "Gate erzwungen" to an explicit "⚠️ Gate-Erzwigung INAKTIV" with the remedy. The version STRING was deliberately not used as the discriminator (live already reports 1.3.0-preview without the lift); refusing to start was also rejected — it would break the documented runs against an old engine, the visible downgrade is the honest middle. Bridge test asserts the warning path.
- **`setup-token` one-time setup (WP6 path):** `python -m hivemind_gateway.main setup-token` (and `start_gateway.bat setup`) prompts via getpass, validates the token against Telegram `getMe` (new `TelegramApi.get_me`, setup-only), stores it in the Windows Credential Manager via keyring, and prints the bot username — the token is never echoed, logged, or written to a file. Tested with an injectable store (commands2 suite). `keyring` (optional dependency, `requirements-gateway.txt` comment) was installed on this host; hash-pinning stays with the WP6 release check as documented.
- **Docs:** `gateway_setup.md` now states the honest limit of the Credential Manager (same-user processes — including this project's own `run_bash` — can read it; the full-command approval card is the actual safeguard) and documents rotation/unpair as before.
