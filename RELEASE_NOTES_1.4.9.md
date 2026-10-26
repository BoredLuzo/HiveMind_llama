# HiveMind 1.4.9

### English only

- Tool and installer output is English now: the git tools (`git commit failed`
  instead of `git commit fehlgeschlagen`), the `/vram`, `/git` and `/chats`
  errors, the UI's stale-tab notice, the MCP shell prefix and empty result,
  the decider installer and the speed backfill help. Comments, docstrings and
  the German intent phrases in `routing/agent_intent.py` stay: those are
  behaviour, not text.

### Reliability

- `next_fire_ts` is a function of `(job, now)` again. The `at` branch took the
  calendar date from `datetime.now()` and only compared the timestamp against
  `now`, so firing a daily job whose slot lies tomorrow re-computed the same
  slot: the job was dead, and the cron test flipped between green and red
  depending on the hour it ran.
- The model manager's `asyncio.Lock` is back. It had been deleted in the
  working tree while 16 call sites still used it, so every load, evict and
  health call would have died with `AttributeError` and no suite noticed.

### Tests and housekeeping

- Twelve suites that never ran are registered now. Four of them got a real
  runner: `chat_search`, `compression_plan_survival`, `smoke_harness` and
  `vision_gate` executed nothing under `python tests/x.py` and reported
  success. `test_steer_images` pinned the retired `[USER STEER]` tag.
- `test_v131_hardening` is gone: it pinned version 1.3.2 and a guard form that
  has been widened since.
- `.gitignore` covers scratch scripts, zips, `dist/`, SSE dumps and patch
  leftovers; the tracked underscore modules are re-included.
- The ten engineering audits moved to `docs/audit/` with an index, and the
  seven superseded `RELEASE_NOTES_*.md` files left the tree. Neither is in the
  release zip.


### Git integration

- **Remote states instead of a boolean**: Test connection reports `ok`,
  `missing_or_invisible`, `token_invalid`, `no_access`, `unreachable` or
  `not_github` with the matching next step. A repo that does not exist and
  a token that cannot see a private repo used to collapse into the same
  404 message, and a rate-limited check no longer reads as a permission
  problem.
- **Create repo creates first**: the GitHub repo is ensured before the
  local work, so an empty workspace with a remote that does not exist yet
  no longer dead-ends in the clone step. New repos are private by default.
- **Existing-repo heal**: on a local repo without a remote, Create repo
  creates the GitHub repo and attaches `origin` instead of answering
  "already exists". The response names whether the remote was created and
  whether the local repo is valid.
- **Workspace-aware panel**: `/git/branches` and `/git/config` honor the
  workspace parameter, and validate, init, stash, reset and branches send
  the project shown in the workspace field (the loaded chat's project).
  Before, the panel showed one project while the buttons acted on the
  default repo.
- **Agents tab git line**: a live branch and remote state line with a
  Configure link that opens the Config tab and highlights the Git card.
  The Git Integration card moved to the top of the Config tab, and a
  successful Create repo refreshes the line.
- **Clone path 500 fixed**: `POST /git/init` answered 500 (UnboundLocalError)
  after a successful clone into an empty workspace.

### TTS

- **Cold start race**: a second request while the sidecar was still
  installing saw "process alive", skipped the health check and failed with
  a connection error after about 8 s while the install takes about 40 s.
  The fast path now requires confirmed health and waits within the
  remaining start budget.
- **Idle hint**: the TTS line states the honest cold start (dependency
  install, up to about a minute, later requests succeed) instead of about
  2 seconds.
- **Engine lock**: the health wait, including the external
  `HIVEMIND_TTS_URL` check, no longer runs inside the engine lock, so
  `stop()` and synthesis do not block for up to the full start budget.
  Readiness is finalized under the lock with a re-check when the sidecar
  restarted meanwhile.
- **Failure budget decay**: three failed starts no longer wedge TTS until
  a process restart; the failure counter resets after 10 minutes without
  a new failure.

### VRAM/load

- **Explicit ctx on load**: `/vram/load` accepts `num_ctx` (or `ctx`),
  passes it to llama-server and reports the ctx used. Before, the start was
  sized only from the hardcoded table plus settings overrides, so a
  profile's `num_ctx` and any caller value never reached the server.
- **Withdrawn claim**: the earlier "100k context costs nothing" result was
  measured at 4096, since all four A/B runs sized the server the same way.
  Re-measurement follows.
- **MTP note corrected**: speculative decoding is not bit-lossless at draft
  n-max 2 on the live build (3 of 3 prompts diverged from mtp off at
  temperature 0, output coherent). The coder default stays mtp off and
  n-max stays 2; the measured trade is 42% faster decode for 30% slower
  prefill.

### Settings safety

- **Render no longer writes settings**: the render path applied the
  chunking state and posted the derived values on every page load, so with
  chunking off the saved `duo_test_feedback_chunk=True` could not survive
  a reload and a preset saved from such a page snapshotted the reverted
  value. The `llama_mlock` half of the live revert needs the warning below
  to be provable. Only the chunking checkbox persists now.
- **Foreign-write warning**: `save_settings` logs a warning with both
  states when settings.json changed on disk since this process last read
  or saved it. The save still proceeds; the warning makes the next silent
  revert provable in the log.

### Audit round

- **Workspace guards**: every `/git` endpoint rejects an explicit workspace
  that is not a directory on this machine with 400, so reset, checkout and
  stash can no longer fall back to the default repo.
- **Chats**: `POST /chats` and `/chats/persist` drop a dead workspace with a
  log entry instead of storing it, so autosave keeps the messages; `PUT`
  keeps its strict 400. Chat autosave also keeps the chat's own workspace
  instead of rewriting it with the global setting.
- **Skills**: `/skills` and `/skills/toggle` accept an optional workspace,
  so they stop listing the default project's skills while a chat runs in
  another project, and reject a dead path.
- **git init hardening**: clone and remote URLs that git would read as a
  leading hyphen option or an `ext::` transport are rejected; init refuses
  to run in the install directory when no workspace is configured (it
  would put settings.json with the git token under version control); a
  missing git or a timeout returns `ok: False` instead of 500; the response
  names `cloned`, `repo_valid` and the remote heal result.
- **Agents tab token line**: the token-problem text is built as a text node
  because it embeds the user's repo URL.
- **Tool counter cap**: the per-run tool-use counter no longer grows
  without bound (oldest run buckets are evicted).
- **Regression coverage**: new suites pin the git init paths (clone and
  init), the workspace guards, the TTS failure decay and the settings
  render path.
