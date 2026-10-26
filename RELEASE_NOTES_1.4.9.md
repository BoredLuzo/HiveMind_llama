# Release 1.4.9

Scope: v1.4.8..v1.4.9. Themes: the git panel, scheduled jobs, speech cold
starts, context on load, and settings that stick.

## Git integration
- Test connection names the problem: missing or invisible repo, invalid token,
  no access, unreachable, not GitHub — each with the next step. A rate-limited
  check no longer reads as a permission problem, and a repo that does not exist
  is no longer the same message as a token that cannot see it.
- Create repo ensures the GitHub repo first, so an empty workspace with a
  remote that does not exist yet no longer dead-ends in the clone step. New
  repos are private by default.
- A local repo without a remote gets one created and `origin` attached,
  instead of "already exists".
- The panel acts on the project it shows: validate, init, stash, reset,
  branches and config follow the workspace field.
- The Agents tab carries a branch/remote line with a Configure link.
- Clone into an empty workspace answered 500 after a successful clone; fixed,
  and a failed clone no longer leaves a half-written `.git` behind.

## Scheduled jobs
- A daily job fired once and never again: `/cron at 09:00` recomputed its next
  slot from the wall clock, so it stayed on the same day once the slot had
  passed. It now advances from the slot that fired.

## Speech
- Cold start: a request during the first dependency install saw "process
  alive", skipped the health check and errored after about 8 s while the
  install takes about 40 s. The fast path requires confirmed health now.
- The Voice line states the real cold start (up to about a minute).
- Three failed starts no longer wedge TTS until a restart; the counter decays
  after 10 minutes without a new failure.

## Models and context
- `POST /vram/load` accepts `num_ctx` (or `ctx`) and reports the value used
  plus where it came from (request, profile, default). Before, a start was
  sized only from the table and settings, so a profile's context never reached
  llama-server.
- Settings the UI derived on a page render are no longer written back, so a
  preset saved from the page keeps the values you set.
- A `settings.json` changed on disk since the engine read it now logs a warning
  with both states instead of silently overwriting.

## Hardening
- Every `/git` endpoint rejects a workspace that is not a directory on this
  machine, so reset, checkout and stash cannot fall back to the default repo.
- `/chats` and `/skills` drop a dead workspace instead of storing it, and chat
  autosave keeps the chat's own project.
- `git init` rejects clone URLs that git would read as an option or an
  `ext::` transport, and refuses to initialise inside the install directory
  when no workspace is set.
