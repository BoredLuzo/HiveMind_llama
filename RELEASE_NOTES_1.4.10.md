# Release 1.4.10

Scope: v1.4.9..v1.4.10. Themes: chunking that plans what you asked for, a bash
path that stops inventing files, an installer with real choices, and a smaller
duo runner.

## Chunking plans what you asked for
- "Create greet.py and main.py" no longer turns into a project scaffold. Steps
  that only set up a shell (README, src/, requirements.txt, .gitignore,
  Dockerfile) are dropped unless the request asks for them. If the plan is
  nothing but scaffolding and the request names files, the plan is built from
  those files.
- The plan ends where "Completed Tasks" begins: finished work came back as extra
  chunks, so the coder re-ran what it had just done.
- The "Chunking is off" notice reads the run's own flag, not the saved setting.
  A run started with chunking on no longer claims its plan was skipped.
- A chunk counts as done after its test verdict, not before, and the last chunk
  needs the critic's approval like the others. Both used to pass too early.
- Chunking and pre-explore stay off by default, and both work when enabled.

## Bash without junk files
- `run_bash` no longer creates files named after shell operators. The Windows
  shims for touch, rm and mkdir act on every whitespace token, so
  `touch a.py && echo done` also touched "&&", "echo" and "done". A command
  containing a shell operator is refused with instructions now: one command per
  call, write_file for files.

## Installer
- The judge/decider model is an optional step, asked like Voice. Skipping it
  leaves the decider on its keyword fallback until you install it later.
- Playwright installs through the venv interpreter, reports the real error and
  the manual retry; HiveMind runs without it, only the browser tool needs it.
- Setup offers an essential model set without the judge/decider helper.

## Load and context
- `POST /vram/load` accepts `num_ctx` (or `ctx`) and reports the value used plus
  its source: request, profile or server default.
- The context card states the honest speed picture where you set it.

## Under the hood
- `duo_runner.py` is split into named functions (planner phase, critic review,
  tool-round execution, wedge handoff, context-pressure check, port and context
  resolution). Behaviour unchanged, covered by the e2e smoke suite and a real
  run.
- README no longer pins a release number.
