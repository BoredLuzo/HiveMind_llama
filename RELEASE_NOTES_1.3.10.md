# HiveMind 1.3.10

A fix release for the workspace regression that 1.3.9 introduced, plus the first
piece of the 1.4 voice work (off by default).

## The workspace bug (please read if 1.3.9 made your files land in the wrong place)

The web UI sends the global **Workspace** field with every run. 1.3.9 let that
value beat every other source, so:

- a chat's own workspace was overridden by the global field - files landed in the
  wrong project, and
- if the field pointed at a folder that only exists on *another* machine (a
  synced settings file, a moved project), the run ended up with **no workspace at
  all**: `write_file` had no root, and the model behaved as if it had forgotten
  the project. On a slow machine the retries made a single `write_file` look like
  a ten-minute hang.

1.3.10 treats a UI value that is identical to the setting as the default it is
(only a value that differs counts as an explicit request), falls back to the
normal workspace chain when the value is stale - with auto-commit off and a
message that names the directory actually used - and never stores an
engine-directory fallback as "last used".

## Also fixed

- `searxng.bat` no longer closes silently: it probes the Docker **daemon**, not
  just the CLI on PATH, explains a stopped Docker Desktop, keeps the window open
  when double-clicked, and reports a stopped container instead of
  "[OK] SearXNG is running".
- A CPU-only install with a large context now prints `[PERF-WARN]` with the real
  context size and the practical 8k-16k range, instead of silently spending
  minutes on the first prompt.

## Added (off by default, no UI yet)

Speech-to-text groundwork, **English only**: `POST /stt`, `GET /stt/status`,
`deploy/fetch_whisper.py` for the whisper.cpp / Parakeet binaries and models, and
the `stt_*` settings. Nothing runs until `stt_enabled` is switched on, the audio
packages are optional (`requirements-voice.txt`), and there is no microphone
button yet - this is the foundation for the 1.4 voice work, not a feature.

## Upgrading

Run `update.bat` (or install the zip over the existing folder - settings,
presets and models are untouched). If a run keeps reporting that the requested
workspace does not exist, set the *Workspace* field in the UI to a folder that
exists on that machine; that is the intended way to fix it.

## Known issues

- Linux is suite-verified only; this release was smoke-tested on Windows.
- Speech-to-text has no UI and is not covered by the release checks beyond its
  own suites.
- A manual `searxng.bat` run still needs Docker Desktop started; the message now
  tells you so instead of vanishing.
