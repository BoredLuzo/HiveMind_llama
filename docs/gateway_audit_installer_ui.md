# Gateway release audit — installer, updater, UI, static paths

Auditor: release-engineering audit, 2026-10-05.
Scope: branch `feature/telegram-gateway` (worktree), release candidate.
Method: static walkthrough + local test runs only. No code changed; this file is the only artifact created.

Tracked gateway surface (verified via `git ls-files`): `hivemind_gateway/` (14 files incl. `requirements-gateway.txt`), `gateway.toml.example`, `docs/gateway_*.md`, `tests/test_gateway_*.py` (12 suites), plus edits to `index.html`, `static/app.js`, `static/feature_search_core.js` (new), `server.py` (7 lines), `update.bat`, `deploy/package_release.bat`, `.gitignore`, `tests/run_regressions.py`. The real `gateway.toml` in this dev worktree is untracked and gitignored (`.gitignore:127`), so `git archive` (package_release.bat:35) can never ship it.

---

## 1. INSTALLER — PASS (clean)

**Fresh-install walkthrough (static).** `install.bat` contains zero gateway references (grep `gateway|telegram` over `install.bat`, `deploy/install_windows.bat`, `deploy/install_linux.sh`, `create_shortcut.bat`, `start_hivemind.bat`, `setup_models.bat`, `searxng.bat`: no matches). It copies/tracks nothing gateway-related and creates nothing gateway-related. Fresh install leaves:
- `gateway.toml` absent (only `gateway.toml.example` ships, tracked), and
- no autostart: `start_hivemind.bat` starts the server only; the gateway is started manually per `docs/gateway_setup.md` §3 (`python -m hivemind_gateway.main`).

Correct end state achieved: a fresh install cannot start the gateway, by two independent fail-closed layers:
- `hivemind_gateway/config.py:28` — `telegram_enabled: bool = False` (master switch default off; `main.py` raises `StartupError "gateway is DISABLED (fail-closed master switch)"` when neither `gateway.toml` nor `HIVEMIND_GATEWAY_ENABLED=1` turns it on).
- `resolve_config_path()` (`config.py:87-97`) returns `None` without `gateway.toml`, and nothing invokes the module anyway.

**Dependency sync cannot break from `requirements-gateway.txt`.** The primary path `uv sync --all-extras` (install.bat:109) reads `pyproject.toml` only; pyproject has no gateway/telegram references and no extra pointing at the file. The release fallback `uv pip install -r requirements.txt` (install.bat:104) likewise never touches it. `requirements.txt` contains no gateway section (its `httpx>=0.27` is a core dep the gateway reuses — `requirements-gateway.txt:3-7` states the gateway adds NO new dependency). The file is simply ignored. Verified.

**Encoding/style.** `python tests/test_bat_encoding.py` → `13 passed, 0 failed` (all shipped .bat ASCII+CRLF, including `install.bat`, `update.bat`, `deploy/install_windows.bat`, `deploy/package_release.bat`, `deploy/release_checks.bat`). Re-run after inspection: same result.

**deploy installers.** `deploy/install_windows.bat` (NSSM service) and `deploy/install_linux.sh` have no gateway references; `deploy/hivemind.service:11` `ExecStart=.../python run.py` — the systemd unit starts the server only, never the gateway.

**Gap check (planned optional installer step).** Confirmed NOT built — and `install.bat` works cleanly without it. Natural attach point: a new optional step `[7/7]` after the SearXNG block (install.bat:309-327), gated on an explicit `choice`: copy `gateway.toml.example` → `gateway.toml` (real activation still needs the token in the shell env per docs). No dependency step would be needed (gateway installs nothing — `requirements-gateway.txt:14-15` documents the manual `pip install -r` for activators only).

---

## 2. UPDATER — PASS with minor findings (works correctly with the gateway present)

**Protection lists.**
- Apply `/XF` line (`update.bat:214`) contains all six candidates: `gateway.toml`, `gateway_state.json`, `gateway_state.json.corrupt`, `gateway.disabled`, `gateway.lock`, `gateway_audit.jsonl`. NOT protected (correct): `gateway.toml.example` — it updates like any tracked file.
- Prune `$protFiles` (`update.bat:243`) contains: `gateway.toml`, `gateway_state.json`, `gateway_state.json.corrupt`, `gateway.disabled`, `gateway_audit.jsonl` — **`gateway.lock` is missing** (Finding F-2). `gateway.pid` and `gateway_audit.jsonl.*` (gitignore:132-136) are in neither list (F-3).

**BASE vs LOCALAPPDATA separation (confirmed).** Gateway user-state lives at `%LOCALAPPDATA%\HiveMindGateway` — `hivemind_gateway/state.py:32-39` (`state_home()`), used for `gateway_state.json`, `.corrupt`, `gateway.disabled`, `gateway.lock` (`state.py:22-24,42-51`); the audit log is planned for the same dir (`state.py:5-6`). Both robocopy invocations operate purely on `%BASE%` (backup line 166, apply line 214), so that state is outside the update tree by construction: never backed up, never overwritten, never pruned. The `/XF` names are belt-and-suspenders against a wrongly built zip, exactly as the comment at `update.bat:210-212` and `.gitignore:128-136` state. The tmp leftover from the atomic state write (`state.py:138`, `with_suffix(".tmp")`) also lands in LOCALAPPDATA — invisible to robocopy.

**Flow walk (gateway installed):** backup → download → extract → stage new update.bat → apply → verify → prune → dep refresh.
- Backup `/XD` (line 166): does not exclude `gateway.toml`, so the owner config is COPIED into `update_backup_<ver>\`. Harmless for correctness (it is a copy, not a clobber) and contains no secrets by construction — `config.py:19,59-66` rejects any `token/bot_token/secret/api_key/password` key at startup; the bot token lives in the shell env / Credential Manager (`docs/gateway_setup.md` §5). Same rest-at-rest class as the already-backed-up `settings.json`. Informational (F-5).
- Apply: `/E` overlays the zip; `hivemind_gateway/`, `gateway.toml.example`, `requirements-gateway.txt` arrive as tracked content; nothing gateway-owned in BASE is clobbered.
- Verify: version check on server.py — gateway-neutral.
- Prune (9a): only files listed in the previous update's `update_manifest_installed.txt` (i.e. previous-zip release files) that the new zip dropped get moved to `update_removed_<ver>\`. Gateway tracked files are in every future zip → never pruned. The `gateway.lock` gap in `$protFiles` cannot bite today because the file is not in BASE (see separation above); it only matters if a future zip ever shipped a file of that name.
- Dep refresh (9b): `uv pip install -r requirements.txt` only. Gateway deps are never refreshed by the updater — fine today (httpx is a core dep), but if a future release bumps `requirements-gateway.txt` (it pins `httpx==0.28.1` vs core `>=0.27`), a gateway user must re-run the manual install line from the file header. Open question OQ-2.

No step can delete or clobber gateway user-state. **`python tests/test_bat_encoding.py` re-run after inspection: 13 passed, 0 failed.**

---

## 3. RELEASE PACKAGING — PASS with one minor note

`deploy/package_release.bat:45` leak-check regex `(^|/)(...|gateway\.toml|gateway_state\.json|gateway_state\.json\.corrupt|gateway\.disabled|gateway\.lock|gateway_audit\.jsonl)$` or `(^|/)sessions/`:
- All six gateway user-state names covered; `$`-anchored, and .NET alternation backtracks through the alternatives, so `gateway_state.json.corrupt` matches while plain `gateway.toml` does NOT match `gateway.toml.example` — the example file ships, as intended and as documented in the comment at lines 42-44.
- `hivemind_gateway/` ships (tracked; nothing in the check blocks the directory).
- `requirements-gateway.txt` ships and contains no secrets — content is comments + `httpx==0.28.1` + an optional `keyring` comment (file read in full; no token, no path leaks).
- `deploy/release_checks.bat` runs `tests/run_regressions.py` + the mmproj network check; nothing contradicts gateway shipping — the regression suite explicitly includes 12 gateway suites (`tests/run_regressions.py:93-100+`), which pass (spot-ran `test_gateway_config.py` 18/18, `test_gateway_state_path.py` 8/8, lint gate 6/6; `ruff check hivemind_gateway/` clean).
- Note (F-4): `gateway.pid` is not in the leak-check list. Nothing writes that filename today (no writer in `hivemind_gateway/*.py`); it is a gitignore belt-and-suspenders entry only.

---

## 4. UI TELEGRAM INTEGRATION — PASS (clean and findable)

**index.html** (block lines 1664-1687, added in 0587f94):
- `#tg-enabled-toggle` (1674) → `postSettings({telegram_gateway_enabled:this.checked})`; `#tg-mirror-toggle` (1682) → `postSettings({telegram_mirror_enabled:this.checked})`. Keys correct (consumed by `hivemind_gateway/main.py:532-540` veto and `bridge.py:587-589` mirror gate).
- Markup matches existing patterns exactly: `.cfg-card` > `.cfg-card-title` / `.tgl-row` > `.tgl-text` + `label.cfl-sw` > `input` + `span.sl`, hints as `.fld-hint` — same shape as the Git card above it.
- Div balance of the inserted block: 6 open / 6 close — balanced. Whole-file count is 537 `<div` vs 536 `</div>`, but the merge-base file already was 531/530 (pre-existing, elsewhere; browsers auto-close). Net change of this branch: +6/+6. Not a regression.

**static/app.js**:
- Init from GET /settings (11403-11408): `tgTog.checked = s.telegram_gateway_enabled || false`, `tgMirrorTog.checked = s.telegram_mirror_enabled || false` — plain boolean assignment to `.checked`, no innerHTML, no string interpolation of settings values anywhere for these keys. Toggle writes go through `this.checked` (boolean) into `postSettings` (1370-1381) → debounced JSON `POST /settings` (1353-1356) — the same channel as every other setting; server merges non-protected keys (`routers/config.py:266` post_settings). No markup-injection surface.
- `node --check static/app.js` → OK.

**static/feature_search_core.js + search index**:
- ALIASES includes `bot: 'telegram'` (feature_search_core.js:25). Loaded before app.js (index.html:1943 before 1944); the query matcher uses it: `_render` → `window.FSCore.expandTerms(q)` (app.js:11759), any-term substring match against `i.kw` (11760-11764).
- The DOM index picks the new card up automatically with zero extra code: `.panel` crawl indexes every `.cfg-card` title (app.js:11621-11624 → Card "Telegram Gateway") and every `.cfg-card .tgl-row .tgl-text` (11625-11628 → "Telegram Gateway — Enabled", "Telegram Gateway — Mirror engine runs to phone"), keywords include the lowercased labels. Searching "telegram" hits directly; searching "bot" expands to `['bot','telegram']` and hits via the alias. Panel id `p-config` maps to PANEL_LABELS `configs` (11552-11553).
- Tests: `node tests/js/test_feature_search_core.mjs` → all pass (exit 0); `python -m pytest tests/test_feature_search_core.py` → 2 passed.

---

## 5. IMAGE / STATIC PATHS — PASS

All static references in `index.html` (complete set — only three exist):
| Ref | Line | Exists |
|---|---|---|
| `/static/style.css?v=20261005-2` | 8 | yes |
| `/static/feature_search_core.js?v=20261005-3` | 1943 | yes |
| `/static/app.js?v=20261005-13` | 1944 | yes |

- No `src=` image refs, no `url(...)`, no `.png/.jpg/.svg/.gif/.ico` file references anywhere in `index.html`. The two `.ico` hits in `app.js` (5671, 7258) are substring matches of the `.icon` CSS property (`_stCfg.icon`), not files; `<img>` elements in app.js are fed data/blob preview URLs, not repo paths.
- `favicon.ico` exists (`static/favicon.ico`, 15406 B) and is served by the server route `server.py:1510-1512` (`/favicon.ico` → `static/favicon.ico`).
- Cache busters bumped as required for this round's changed files: `app.js?v=20261005-13`, `feature_search_core.js?v=20261005-3`. `style.css` was not changed on this branch, so its buster staying at `20261005-2` is correct.
- Informational: `static/favicon-16x16.png`, `favicon-32x32.png`, `site.webmanifest` exist but are referenced by nothing (no `<link rel="icon">`/manifest tags in the head). Harmless dead assets, pre-existing. `style.css`'s only `url()` is the Google Fonts import (network, pre-existing, degrades to fallback fonts offline).

---

## Findings

| # | Severity | Evidence | Suggested fix |
|---|---|---|---|
| F-1 | INFO (by design) | Optional "Telegram gateway" installer step not built. `install.bat` works cleanly without it (verified above). | When built, attach as optional `[7/7]` after the SearXNG block (install.bat:309-327): explicit `choice`, copy `gateway.toml.example` → `gateway.toml`; no dependency step needed. |
| F-2 | LOW | `gateway.lock` is in the apply `/XF` list (update.bat:214) but missing from the prune `$protFiles` list (update.bat:243). Unreachable today (lock lives in LOCALAPPDATA, prune only considers BASE), pure belt-and-suspenders gap. | Add `'gateway.lock'` to `$protFiles` in update.bat:243. |
| F-3 | LOW | `gateway.pid` and rotated `gateway_audit.jsonl.*` (gitignore:132-134) are absent from update.bat `/XF`, `$protFiles`, and the package_release leak check. No code writes these names today (audit log planned for WP6, state.py:6). | When WP6 lands, extend all three lists; or add now as belt-and-suspenders. |
| F-4 | LOW | Same as F-3 for `deploy/package_release.bat:45` — `gateway.pid` not in the zip leak regex. | Append `gateway\.pid` to the alternation when the pid file becomes real. |
| F-5 | INFO | Backup step copies `gateway.toml` into `update_backup_<ver>\` (update.bat:166 has no /XF for it). No secret possible (config.py:19,59-66 forbids token keys at startup; token lives in env/Credential Manager). | Optional: add `gateway.toml` to the backup `/XD`... note robocopy /XD takes dirs; would need `/XF gateway.toml`. Cosmetic only. |
| F-6 | INFO (pre-existing) | index.html whole-file div count 537/536 — already 531/530 at merge-base ac5d5bf; the telegram block itself is balanced (+6/+6). | Out of scope; harmless (browser auto-close). Could be located and fixed separately. |
| F-7 | INFO | tests/js/test_feature_search_core.mjs asserts ctx/temp aliases but has no explicit case for the new `bot→telegram` alias. | Add one `ok(...)` line: `FSCore.expandTerms('bot')` includes `'telegram'`. |

## NOT tested

- Live runs of install.bat / update.bat / package_release.bat (destructive/network); everything above is a static walkthrough plus repo-local evidence.
- Real Telegram API, real pairing, gateway process lifecycle (covered by the gateway suites another auditor ran: 12 suites green via run_regressions wiring; spot-ran config/state-path/lint-gate here).
- POST /settings origin/CSRF hardening (explicitly out of scope — another auditor).
- Whether `httpx==0.28.1` (gateway pin) vs `>=0.27` (core) drifts after `uv pip install -r requirements.txt` picks a newer httpx — import-compat only, no pin enforcement anywhere.
- pytest cannot execute the script-style gateway suites directly (module-level `sys.exit` → INTERNALERROR); they are run by `tests/run_regressions.py` / direct invocation, which passes. Pre-existing repo convention, not a branch defect.

## Open questions

- OQ-1: Should the future installer step also offer Windows Credential Manager token storage (`keyring` is commented-optional in requirements-gateway.txt:17-18), or keep the env-var-only flow from docs/gateway_setup.md §2?
- OQ-2: After a future release bumps `requirements-gateway.txt`, what tells an existing gateway user to re-run `pip install -r hivemind_gateway/requirements-gateway.txt`? update.bat's dep refresh intentionally reads requirements.txt only. A release-notes warning line (update.bat already surfaces "warning/migrat" lines from notes, line 138) would fit.
- OQ-3: Where should `gateway.pid` eventually live — LOCALAPPDATA next to the lock (recommended, keeps it out of every robocopy path) or BASE (would require the F-3/F-4 list additions)?
