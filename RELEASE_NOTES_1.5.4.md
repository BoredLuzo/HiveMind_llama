# Release 1.5.4

Automations and reliability fixes on top of 1.5.3. Schedules now live in the
engine and are managed from the normal UI; chat auto-save no longer creates
duplicate history entries, and the installer detects freshly installed
language servers. No settings are reset and no model has to be re-downloaded.

Upgrade with `update.bat` (or the startup check offers it), or extract the zip
next to the old install. Settings, models, sessions and the venv are kept.

## Automations

- New sidebar tab **Automations**. Create a scheduled prompt with name,
  schedule (`every N minutes`, `once in N minutes`, `daily at HH:MM`), mode,
  tools tier, workspace and overlap policy.
- Jobs are owned by the engine, so they run while HiveMind is open even if the
  Telegram gateway is off.
- Every automation gets its own `⏰ <name>` chat. Results and run history stay
  visible in the normal chat list.
- Run-now, pause/enable and delete are available per job; recent runs show
  status and stop reason.
- Single scheduler lane: a scheduled run never starts while another run is
  active. `skip` is the default; `queue` holds at most one fire per job and
  runs it when the engine is idle.
- Automap is removed from the mode selector. Existing `mode=automap` presets
  load as `auto`; the engine support remains for old data.

## Chat history

- Auto-save is serialized, so two saves can no longer create two chats before
  the first returned an id.
- "Retry in new chat" is hidden after a first-turn failure; retry uses the
  same chat instead of adding a duplicate one-message entry.

## Installer and tools

- The Windows LSP installer refreshes the machine/user PATH before its
  self-test, so a freshly installed clangd is detected instead of reported as
  missing.
- Python tools use the engine or workspace interpreter instead of a bare
  `python` from PATH; `run_bash`, `run_tests` and background jobs get the venv
  Python on PATH.
- The destructive gate only blocks absolute-path opens in write/append mode;
  read-only file inspection no longer asks for confirmation.
- The Windows installer and updater add uv's `.local\bin` to the user PATH
  once.

## Update discovery and installer

- A cached "you are current" update-check result expires after 4 hours
  instead of 24, so a release published after your machine's daily check is
  offered the same day. An "update available" verdict keeps its 24-hour
  no-re-nag window.
- Offline is visible: the startup check prints one line ("check skipped
  (offline)" or a stale-cache notice) instead of silently skipping. HiveMind
  still starts normally without internet; `update.bat` also checks GitHub
  directly without a cache.
- The installer and updater print an `[OK]` line after every step, so you can
  see exactly where a step finished (or stalled).
- Coder prompts pin the platform on Windows (paths look like `C:/Users/...`;
  `/Users/...` is macOS), point at the preinstalled browser tool instead of
  Playwright installs, and tell the model to validate JS with `node --check`
  rather than Python's `compile()`. `install_package` refuses Playwright
  installs with the same pointer.

## Automations audit pass

- The editor now uses the same dark form controls as the rest of the UI
  (previous version rendered unstyled white inputs), job action buttons use
  the standard mini-button style, and the editor scrolls into view when
  opened.
- Scheduler hardening: the fire loop targets the actually bound port (an
  `HIVEMIND_PORT` override or busy-port fallback no longer sends jobs to the
  wrong instance), a job edited or deleted during a long run can no longer be
  corrupted by the run's end, zombie one-shot jobs are swept, a queued fire
  respects pause/disable, `PUT` validates like `POST` and cannot rewrite
  bookkeeping fields, and shutdown aborts an in-flight automation run.
- Dead knobs removed (unused approval/delivery fields, an unimplemented
  `replace` policy, a dead helper); job ids are unique; jobs are capped at
  50; `automations_enabled` is a settings kill switch.

## Telegram and scheduling follow-ups

- `/cron` on the phone is now a proxy to the engine's Automations: same
  commands (`add every|in|at`, `list`, `del`, `run`), one scheduler, one
  store. Existing gateway cron jobs migrate into the engine on the first
  gateway start (skipped jobs are reported; the retry happens on the next
  start).
- Daily schedules accept weekdays (Mo–Su chips in the editor, `days` in the
  API). No day selected means every day.
- The editor shows a live next-fire preview for the draft schedule.
- An automation that pauses on a question or approval card is marked
  `needs_input` (orange dot) instead of failing silently, and a desktop
  notification points at the paused run.

## Verification

- Full regression gate: 310 suites.
- Pyright baseline: no new errors.
- Linux push gate: gateway, document, image, send-file, planner and scheduler
  suites.
