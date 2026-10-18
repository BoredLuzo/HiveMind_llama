# HiveMind 1.4.1

A patch release on top of 1.4.0: the Windows installer could strand a fresh
machine, and the audio licences are now documented in full.

## Installer: the right Python, or a clear stop

`uv python install 3.14` fails when an unmanaged `python3.14.exe` already sits in
`%USERPROFILE%\.local\bin`. The installer used to note that and carry on - uv
then built the virtual environment on **Python 3.15**, where `tree-sitter` has no
wheel, so pip fell back to a C build that needs Visual Studio and the install
died with `Microsoft Visual C++ 14.0 or greater is required`.

Now:

- the 3.14 install is retried with `--force`,
- the venv is **pinned** (`uv venv --python 3.14`, `uv sync --python 3.14`),
- without a usable 3.14 the installer stops with instructions instead of picking
  another interpreter,
- the finished venv is asserted to be 3.14,
- a leftover `.venv` without `python.exe` is removed first (that used to fail on
  the second run with "not a valid Python environment"),
- an unterminated quote in a `REM` no longer leaks a stray
  `Der Befehl "ERROR-every-second-run"` line at the start.

## Licences: the audio stack, in full

The README now carries the complete table - Pocket TTS model (CC BY 4.0, gated
terms) and its code (MIT), every voice recording with its source dataset
(VCTK/Alba **CC BY 4.0**, Voice-Zero **CC0**, Expresso **CC BY-NC 4.0**), whisper
models (MIT), whisper.cpp and parakeet binaries (MIT) and the parakeet GGUF (MIT
in that repo, upstream weights **CC BY 4.0** - treated as the stricter one).
Nothing is vendored: all models and binaries are downloaded on first use, never
shipped in the zip. `docs/voice_licenses.md` has the per-file provenance.

## Telegram card

With voice switched off, the result-format select and the hint lines are now
greyed out and disabled together with the voice-note toggles - the card no longer
looks half-active.

## Upgrade

Run `update.bat` (it backs the code up first) or extract this zip next to your
installation. `settings.json`, `models.json`, `.venv`, `llama`, `sessions` and
`logs` are never touched.
