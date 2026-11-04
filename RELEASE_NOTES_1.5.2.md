# Release 1.5.2

Agent and Telegram polish on top of 1.5.1: cleaner planner/coder hand-off,
file delivery from phone runs, clearer language-server status, and a more
forgiving web-search guard. No settings are reset and no model has to be
re-downloaded.

Upgrade with `update.bat` (or the startup check offers it), or extract the zip
next to the old install. Settings, models, sessions and the venv are kept.

## Telegram file delivery

- `/send <path>` delivers a workspace file immediately.
- In agentic phone runs the model can call `send_file` for a file it wrote in
  the same run. The file is **queued** and delivered only after the final
  verification passes, as the last action of the run. If the model wrote
  exactly one deliverable and forgot to queue it, the run sends that file
  automatically on a completed run.
- Delivery is workspace-confined; symlink escapes, secret-looking names
  (`.env`, keys, credentials, token/password names) and files above the 50 MB
  Telegram document limit are refused.
- Phone runs now pass the chat workspace in the run body, so `/workspace`
  takes effect even while the desktop workspace is pinned.

## Planner and coder

- The coder no longer receives the planner's raw markdown plan or reasoning.
  It gets a distilled numbered execution list instead.
- With the planner fully off, the coder receives only the user prompt: no
  plan injection, no in-loop planner, no follow-up plan hint.
- The planner prompt is less absolute and leaves room for the model's own
  approach.

## Models, tools and diagnostics

- The LSP panel now lists which language servers are installed and which are
  missing.
- Python uses one linter: pyright when available, `py_compile` only as an
  environment fallback. The per-language lint-engine selector is gone.
- The near-duplicate web-search guard allows eight rephrased searches before
  stopping instead of three.

## Reliability

- The Linux `types` CI job no longer fails on the optional Windows-only
  `keyring` dependency: the import is resolved dynamically and the environment
  token remains the primary path.
- Windows CI path checks and the executor test suite cover the queued
  `send_file` flow and the post-verification delivery event.
