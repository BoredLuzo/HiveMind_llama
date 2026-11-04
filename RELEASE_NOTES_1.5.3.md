# Release 1.5.3

Replacement for 1.5.2. It fixes the optional-component installer and lets
direct mode deliver files it wrote. No settings are reset and no model has to
be re-downloaded.

Upgrade with `update.bat` (or the startup check offers it), or extract the zip
next to the old install. Settings, models, sessions and the venv are kept.

## Direct-mode file delivery

- With `direct_tools_tier=full`, a Telegram direct run can write a file with
  `write_file` and queue it with `send_file`.
- The file is delivered after the direct tool loop, before the run closes.
  If the model wrote exactly one deliverable and did not queue it, that file
  is sent automatically on a completed run.
- The direct prompt now tells the model not to paste a complete HTML page or
  file into the chat when the user asked for a file.

## Optional components

- `check_optionals.bat` uses `uv` when available instead of assuming the venv
  has `pip` (uv-created venvs often do not). If neither is usable, it
  bootstraps pip with `ensurepip`.
- The routing decider entry now states what is actually needed: the GLiNER2
  model plus `gliner2[local]` and PyTorch, about 2.5 GB. A failed install
  prints the exact retry command instead of a silent warning.
- The missing-components list names the components and their footprint before
  asking to install anything.
- An install that already has the downloaded GLiNER2 model but not the Python
  package is detected as missing too, so 1.5.2 users are offered the repair.

## Reliability

- Direct tool runs initialize the same run-written-file registry as agentic
  runs, so `send_file` can verify that the file really came from the current
  run.
- The Linux `types` check and the full local regression gate remain green.
