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

## Verification

- Full regression gate: 310 suites.
- Pyright baseline: no new errors.
- Linux push gate: gateway, document, image, send-file, planner and scheduler
  suites.
