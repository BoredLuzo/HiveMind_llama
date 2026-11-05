# Release 1.5.5

Model-registry hardening and repo maintainability on top of 1.5.4. No
settings are reset and no model has to be re-downloaded.

Upgrade with `update.bat` (or the startup check offers it), or extract the zip
next to the old install. Settings, models, sessions and the venv are kept.

## Model registry

- `models.json` is **merged** instead of rewritten: a scan no longer deletes
  manual registrations (for example a model added by path via `add_model.py`).
  Entries whose file is gone are dropped with a note.
- One `invalidate_caches()` entry point drops the index, name list and
  filename manifest; `save_model_mapping` calls it, so a renamed/moved model
  is reflected immediately instead of after the TTL.
- The model picker's dedup prefers the manifest/profile name over the longest
  parsed alias — `gemma-4:e4b-it-qat` is selectable again, its per-model
  config applies.
- Override keys whose file no longer exists are no longer listed (they used
  to fail only at load time).
- Quant-alias collisions fall back to numbered aliases instead of silently
  dropping a file from the registry.
- `save_model_mapping` writes atomically and refuses llama.cpp slot ids
  (`llamacpp:<sha>…`) — such ghost entries can no longer reach the picker.
- `add_model.py` stores absolute paths, so a model registered by path works
  regardless of the start directory.

## Maintainability

- `DEFAULT_SETTINGS` and `DEFAULT_AGENT_CFG` moved to
  `settings_defaults.py` (pure data, no I/O). `settings.py` re-exports both,
  so nothing changes for embedders; the schema is now diffable on its own.
- The regression runner executes suites in parallel (6 workers, `--jobs` to
  change): the full 314-suite gate dropped from ~18 minutes to ~2 minutes.
  Output order is unchanged; `--jobs 1` restores strict sequential mode.
  Suites that spawn real subprocesses (mcp_client) run in an exclusive
  sequential lane after the batch — they flaked under parallel load.
- The settings documentation generator is split-aware and reads comments from
  both settings files.
- Dead code removed from `fetch_models.py`; the stray `.orig` files are gone.

## Verification

- Full regression gate: 314/314 suites in parallel.
- Pyright baseline: no new errors.
- Linux push gate: portable suites including the new model-registry, cron
  proxy and scheduler suites.
