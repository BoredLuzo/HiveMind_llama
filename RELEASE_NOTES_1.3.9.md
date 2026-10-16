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
- `--cache-ram` and `--ctx-checkpoints` default to 1024 MiB / 4 and are
  overridden with the environment variables `HIVEMIND_CACHE_RAM_MIB` and
  `HIVEMIND_CTX_CHECKPOINTS`; the previous binary defaults remain reachable.
  (There is no UI field or documented settings key for these two values.)
- `HIVEMIND_MTP_SPEC=0` really switches speculative decoding off for every
  model; it used to force `--spec-type draft-mtp` on instead.
- A model profile's own MoE answer beats the name heuristic, and
  `task_complete` reflects the real test result unless a status is explicit.

## Known issues
- **Linux:** the installer path (`deploy/install_linux.sh`, the systemd unit,
  the ubuntu llama.cpp assets) is verified by the offline suite and code review
  only - it has not been run on a Linux host yet.
- **Model loading with a mixed preset:** if one role holds a small model while
  another role asks for a model that does not fit, the pre-flight now refuses
  that load with `[PRE-FLIGHT-REFUSE]` instead of evicting a pinned or in-use
  slot. Before this fix the pre-flight could kill the slot a running coder
  round was using (visible as `[CODER-ROUND-FAIL] ... ConnectError`); the guard
  covers pinned and in-flight slots, but a load that still cannot be satisfied
  is now refused rather than forced.
- **Cache reuse with MTP models:** llama.cpp disables `--cache-reuse` when MTP
  speculative decoding is active ("cache_reuse is not supported by this
  context"), so rounds after a large tool result re-prefill part of the prompt
  (measured 63-98% reuse). Not a regression, but the reuse numbers in the log
  are lower than on non-MTP models.

## Known limitations
- Without Git Bash installed, `run_bash` runs through PowerShell;
  `install.bat` neither installs Git nor warns that Git Bash is missing.
- The smoke-test nudge only *instructs* the model to write a test containing a
  real assertion. The auto-test gate checks that a test-looking command ran,
  not what the test file contains.
- The installer check and every fix above are verified by the offline
  regression suite only, not on a foreign machine.
- No performance claim: runtime behaviour (models, speed, VRAM, prefill) is
  unmeasured for this release.

## Release content and upgrading
- The zip no longer ships the developer regression suite (`tests/`) or
  `deploy/release_checks.bat`. Updating from 1.3.8 moves existing copies of
  them (and of `docs/roadmap_1.4.md`) into `update_removed_1.3.9\` on the
  target install - reversible, and nothing user-side is touched.
