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
- Direct mode with full tools can do the same: it writes the file, queues it
  with `send_file`, and the gateway uploads it after the direct tool loop.
  The direct prompt no longer treats a requested HTML/file as chat text.
- Delivery is workspace-confined; symlink escapes, secret-looking names
  (`.env`, keys, credentials, token/password names) and files above the 50 MB
  Telegram document limit are refused.
- Phone runs now pass the chat workspace in the run body, so `/workspace`
  takes effect even while the desktop workspace is pinned.
- An image sent in a direct chat stays in context: the next text-only turn
  re-attaches the last image set, so follow-up questions can refer to it.
  The image context is stored per chat while HiveMind runs and is wiped on
  shutdown; with the persistent-image-uploads toggle on it survives restarts.
  Deleting the chat always removes it.
- Direct phone runs select the UI tiers with `/tools off|websearch|python|full`
  (per phone run, desktop setting untouched). `full` still stays
  workspace-locked; `/restrict off` is required for write/shell access.

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

## Optional components

- Installer and updater both install optional Python packages through `uv`
  instead of assuming the venv has `pip` (uv-created venvs often do not). If
  uv is missing, `install.bat` provisions it once; the updater points there
  instead of falling back to a pip that may not exist.
- The routing decider entry now states its real footprint: the GLiNER2 model
  plus `gliner2[local]`/PyTorch, about 2.5 GB. A failed install prints the
  exact retry command instead of a silent warning, and an install that has the
  model but not the Python package is detected as missing too.

## Reliability

- The Linux `types` CI job no longer fails on the optional Windows-only
  `keyring` dependency: the import is resolved dynamically and the environment
  token remains the primary path.
- The executor test suite covers the queued `send_file` flow, direct-mode
  delivery and the post-verification delivery event.
