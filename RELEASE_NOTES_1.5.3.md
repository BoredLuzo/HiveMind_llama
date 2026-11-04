# Release 1.5.3

Reliability and delivery hardening on top of 1.5.2: Telegram files and approval
cards behave correctly during live runs, PDF and plain-text uploads both work,
resumed runs keep their queued files, and updates no longer remove optional
Python packages. No settings are reset and no model has to be re-downloaded.

Upgrade with `update.bat` (or the startup check offers it), or extract the zip
next to the old install. Settings, models, sessions and the venv are kept.

## Telegram delivery and approvals

- Binary documents (PNG, PDF, DOCX, ZIP) are sent byte-for-byte. Secret
  redaction only decodes known text types or valid UTF-8; it no longer rewrites
  binary payloads into replacement characters. UTF-16 and legacy single-byte
  text documents are still decoded and redacted instead of bypassing the
  filter.
- Approval cards for phone runs are polled from the engine, so they appear
  while the run is paused instead of after the gated call returns. A late
  staged card frame with the same tool and preview is ignored instead of
  producing a second, unanswerable card.
- If a paused approval expires engine-side, the phone latch is cleared instead
  of swallowing the owner's next message. A gateway restart leaves an
  `ask_user` question to its timeout instead of sending the literal answer
  "3".
- `ask_user` questions arrive as questions, not as approval cards, and the next
  text message is delivered as the agent's answer.
- `/stop` suppresses late approval cards from the aborted run. An abort the
  engine rejects is reported as a failure instead of a success.
- `/purge` reads the engine response correctly and reports the number of
  stopped model processes.
- A phone run adopted as a mirror after a gateway restart also delivers its
  queued `send_file` output to the phone.
- A gateway restart keeps the existing phone chat and its history. Set
  `HIVEMIND_GATEWAY_FRESH_CHAT_ON_RESTART=1` to start a new chat on restart
  instead.

## Documents and images

- PDFs and plain-text uploads (UTF-8, UTF-16, cp1252) are both extracted.
- Long documents use the document character limit instead of Telegram's
  4096-character message limit, so a normal report is no longer rejected after
  extraction.
- The last image in a chat is re-attached on text-only follow-ups in every
  mode, not only direct mode. Re-attached images are not duplicated into the
  workspace uploads folder.

## Runs and files

- `send_file` accepts files produced by `edit_file`, `patch_file`,
  `replace_lines`, `edit_ast`, and by scripts run through `run_python` or
  `run_bash` during the same run.
- Equivalent path spellings (forward/back slashes, drive-letter case, MSYS
  `/C:/...` style) resolve to the same workspace file for the write guard and
  the delivery queue.
- A resumed chunked run restores its explicit `send_file` queue, so the files
  selected before an abort or graceful stop are still delivered.
- The in-loop planner's output is distilled before it reaches the coder. Raw
  planner markdown and reasoning stay out of the coder context; the visible
  plan in the UI and on the phone is unchanged.
- Agentic runs that only add or change static assets (HTML, CSS, Markdown,
  SVG) no longer force the workspace test suite. Previously an
  unrelated pre-existing failing test could block `task_complete` and send the
  coder into an edit/test loop; the completion is now accepted without a test
  run when no testable code changed. A `run_tests` call in such a run is
  skipped before an approval card appears, and the agentic prompt no longer
  asks for tests on static assets. If the loop guard still stops such a run,
  the explicitly queued file is delivered instead of being dropped silently.
- The Linux push gate now installs `pypdf` and runs the gateway, document,
  image, send-file, planner-briefing and resume suites.

## Installer and update

- `install.bat` and `update.bat` use `uv sync --inexact`, so optional decider
  and voice packages installed earlier are not removed by an update. Windows
  update no longer pulls pytest/ruff into the user venv.
- `pypdf` is part of the core dependency set in `pyproject.toml` and
  `uv.lock`; document upload works on a fresh install without a manual step.
- The Linux installer now resolves the repo root from `deploy/` (the
  documented `sudo deploy/install_linux.sh` path works), installs uv where it
  looks for it, uses `--inexact`, keeps the uv-managed `.local` runtime across
  re-runs, and installs `python3-venv`/`python3-pip` for the fallback.
