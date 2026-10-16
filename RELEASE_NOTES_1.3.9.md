# HiveMind 1.3.9

## Tool use
- `run_bash` names the shell that actually executes, in the hint, the tool
  schema and the NONZERO error line. Without Git Bash it falls back to
  PowerShell instead of failing silently.
- The destructive-command gate gates genuinely destructive patterns again;
  routine `python ...` calls no longer trigger a confirmation loop.
- `read_file` reports the real size: range reads carry the full line and
  character count plus a continuation line, and the executor cap no longer
  cuts that note away.
- A repeated identical failing command returns an explicit repeat hint.

## Context
- Workspace resolution is deterministic: request, then real resume, then chat
  record. The environment variable is never primary, and `PUT /chats/{id}`
  rejects dead workspace paths.
- Chunk resume is durable: checkpoints are written on every completion path,
  and a checkpoint that did not persist is reported instead of logged as
  saved.
- No-think and vision-retract corrections are append-only, so prefix cache
  reuse survives them; `fuzzy_edit` no longer collapses on files with many
  identical lines.

## Installer / updater
- The installer verifies imports after installing and offers a guided repair
  when a package is broken.
- `update.bat --zip <path>` applies a locally built release zip, with a layout
  guard and documented rollback.
- Each package writes `BUILD_INFO.txt` (version, build, commit, date) and the
  start banner prints it.

## Host-RAM caps, MoE pin, task_complete
- `--cache-ram` and `--ctx-checkpoints` are configurable via
  `llama_cache_ram_mib` / `llama_ctx_checkpoints` (plus `HIVEMIND_*` env
  overrides); the previous binary defaults remain reachable.
- `HIVEMIND_MTP_SPEC=0` really switches speculative decoding off for every
  model; it used to force `--spec-type draft-mtp` on instead.
- A model profile's own MoE answer beats the name heuristic, and
  `task_complete` reflects the real test result unless a status is explicit.

## Known limitations
- Without Git Bash installed, `run_bash` runs through PowerShell;
  `install.bat` neither installs Git nor warns that Git Bash is missing.
- The installer check and every fix above are verified by the offline
  regression suite only, not on a foreign machine.
- No performance claim: runtime behaviour (models, speed, VRAM, prefill) is
  unmeasured for this release.
