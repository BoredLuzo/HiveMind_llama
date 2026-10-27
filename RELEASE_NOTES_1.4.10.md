# Release 1.4.10

## Chunking plans what you asked for

- A request like "create greet.py and main.py" no longer becomes a project
  scaffold. Steps that only set up a shell (README, src/, requirements.txt,
  .gitignore, Dockerfile, ...) are dropped unless the request mentions them or
  asks for a project. When nothing but scaffolding was planned and the request
  names files, the plan is built from those files instead.
- The plan ends where "Completed Tasks" begins: finished work used to come back
  as extra chunks, so the coder re-ran what it had just done.
- The "Chunking is off" notice reads the run's own flag now. It used to read
  the saved setting, so a run started with chunking on announced that its plan
  was skipped while the chunks ran.
- A chunk is marked done after its test verdict, not before: a failing test
  re-processes the chunk instead of recording it as complete.
- The last chunk needs the critic's approval like every other chunk. It used to
  pass unreviewed, and it is the one carrying the integration work.
- `duo_chunking` and `duo_pre_explore` ship off: both are opt-in, and both work
  when switched on.

## Bash without junk files

- `run_bash` no longer creates files named after shell operators. The Windows
  shims for `touch`, `rm` and `mkdir` split their arguments on whitespace, so
  `touch a.py && echo done` also "touched" `&&`, `echo` and `done`. A command
  that contains a shell operator is refused with an explanation now: one
  command per call, `write_file` for files.

## MCP client (beta)

- HiveMind can use **external MCP servers** as tools, the other direction of the
  existing MCP server. Off by default: `mcp_client_enabled` plus a
  `mcp_servers` list of `{name, command, args, env, cwd, enabled}`.
- Tools appear as `mcp_<server>_<tool>` (capped at 64 characters) with the
  server's `inputSchema` passed through. A schema over 8 kB is replaced by an
  empty one instead of blowing up the request.
- `GET /mcp/servers` shows what is configured, running and discovered;
  `POST /mcp/reload` starts the servers and rebuilds the tool list. Servers
  start once at boot and every child is closed on shutdown.
- A server that hangs, crashes or prints garbage never breaks a run: the call
  returns a tool error and the connection is dead until the next reload. One
  child process per server, the whole tree is killed on a timeout, and an entry
  that leaves the config is closed. stdio only, 8 servers, 64 tools each.
- MCP tools follow the mode gate (write modes only), pass the approval gate,
  and stay blocked for phone runs.

## Coder speed: speculative decoding is back on

- The hermes 35B coder runs with draft-mtp again. Measured on b11889 with
  256-token answers: decode 20.0 -> 31.05 t/s (+55 %), prefill 122.1 -> 120.15
  (-1.6 %), acceptance 0.823. The 2026-10-24 decision was based on 220-token
  answers and a prefill penalty that does not reproduce.
- Reference measurement for `qwen3.5:4b-mtp`: decode 55.5 -> 85.35 t/s
  (+53.8 %), prefill 610 -> 568 (-6.9 %), acceptance 0.911.
- The trade is named: with speculative decoding the greedy output is not
  bit-identical to plain decoding (near-tie tokens flip). That is the price for
  the speed. `HIVEMIND_MTP_SPEC=0` restores byte-identical runs.

## Installer

- The judge/decider model is an optional step, asked like Voice: skipping it
  leaves the decider on its keyword fallback until you install it later.
- Playwright installs through the venv interpreter with the real error text and
  the manual retry spelled out; HiveMind runs without it, only the browser tool
  needs it.
- Setup offers an essential model set that leaves out the judge/decider helper.
- `--no-drafters` skips the drafter sidecars, explicitly only, and warns that it
  costs roughly half the decode speed for the models that need one (Gemma-4 MTP,
  the DSpark pair). `--essential` does NOT skip them: the drafters are in use.

## Load and context

- `POST /vram/load` accepts `num_ctx` (or `ctx`) and reports the value it used
  plus where it came from: request, profile or the server default.
- The context card states the honest speed picture where you set it.

## Diagnostics

- A failed write under Desktop, Documents, Pictures or a OneDrive folder now
  names the real cause: Controlled Folder Access and OneDrive locking, with the
  way out. Every other folder keeps the plain message.

## Repository

- `main` tracks the released state again and `deploy/promote_main.bat` keeps it
  there: fast-forward only, never a merge.
- `deploy/prepush_check.ps1` brings the 13 pre-push checks back into the repo.
- README no longer pins a release number or a zip name.

## Under the hood

- `duo_runner.py` is split: the planner phase, the critic review, the tool-round
  execution, the wedge handoff, the context-pressure check and the port/ctx
  resolution live in named functions instead of one generator. Behaviour is
  unchanged; the split is covered by the e2e smoke suite and a real run.
