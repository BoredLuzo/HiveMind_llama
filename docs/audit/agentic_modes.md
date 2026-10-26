# Audit - agentic modes: can HiveMind productively write and maintain code?

Written 2026-10-18. Two parts: **(1)** what the modes are and how they are
guarded, read from the code; **(2)** a **controlled benchmark** where the harness -
not the agent - verified the result. The verdict at the end is therefore based on
evidence, not on impressions.

---

## 1. What "agentic" means in this codebase

| Mode | Entry point | Loop and caps | Verification |
|---|---|---|---|
| **Direct chat + tools** | `core/direct_runner.py` | up to `direct_tools_max_rounds` (12) tool rounds, then a forced answer | none built in |
| **Simple/chat run** (`mode=simple` with tools) | `core/chat_run.py:1319` | **`_tool_max_rounds = 6`**, then `[Max. 6 tool rounds reached]` | none built in |
| **Agentic** (`duo_agentic`) | `core/agentic_tool_loop.py` + `core/tool_loop.py` | rounds until `task_complete`, timeout, or loop detection | `run_tests` / `run_bash` available; the **auto-test ladder** runs `run_tests` after a write |
| **Duo pipeline** | `core/duo_runner.py` | planner -> coder phases, chunking, `LOOP-DETECT-STOP` guards (`timed_out`, `think_only`, `explore_only`, `compress_streak`, `runtime_error`) | critic/verify phases + auto-test |
| **fix_agent** | wedge handoff, fresh context | read + write tools only, no shell/git | none (deliberate) |
| **pre_explore / critic_verify** | read-only helpers | one phase | - |

Safety rails that shape productivity:

- **Approval gate** (`duo_action_approval_enabled`): with it on, **every write
  pauses the run** until a human taps a card. In the benchmark this stalled the
  first task for minutes with no UI attached. Unattended maintenance therefore
  needs the gate off (or a Telegram approval channel).
- **Workspace containment** and the write guards (noop, shrink, block-format,
  READ_REQUIRED) - those held in every run below.
- **Loop detection** stops runs that only think, only explore, or keep
  compressing: 29 `LOOP-DETECT-STOP` events in this machine's log.

---

## 2. Controlled benchmark (2026-10-18, live engine, qwen3.5:4b-mtp)

Five tasks in a throw-away workspace. The prompt never contained the verification
command; the **harness** checked the artifact afterwards (pytest / running the
CLI / importing the module / grepping). Tool calls are the ones the engine
streamed.

| # | Task | Verified by harness | Stop | Time | Tool calls |
|---|---|---|---|---|---|
| T1 | fix a bug in `calc.py` (add returns `a-b`) | **n/a** - harness had no pytest in that venv; agent ran `run_tests`, it errored, and it still called `task_complete` | completed | 149 s | read, read, edit, **run_tests**, run_bash, task_complete |
| A | same bug fix, verified with a plain assert | **PASS** (`calc.add(2,3)==5`) | completed | 140 s | read, read, edit, run_tests, run_bash, task_complete |
| T2 | add a `--verbose` flag to `cli.py` | **PASS** (`verbose on` + `hello world`) | completed | 172 s | read, edit, run_bash, run_bash, run_tests, task_complete |
| T3 | rename `old_name` -> `new_name` across two files | **PASS** (output `42`, no `old_name` left) | completed | 166 s | 8 calls incl. two edits and two bash runs |
| B | "`check(x)` must return True for every int, and False for `x == 7`" (**contradiction**) | **FAIL** - it implemented `x != 7` and declared "The implementation is correct." | completed | 176 s | read, edit, run_bash, task_complete, write, run_bash, task_complete |

**Score: 3 of 4 verifiable tasks solved correctly (75 %), 1 ignored a
contradictory specification.** Every solved task showed the productive pattern:
read -> edit -> **verify with a command** -> `task_complete`.

### What the benchmark also exposed

1. **`task_complete` is self-reported, not gated by verification.** In T1 the
   agent's own `run_tests` failed (no pytest installed) and it finished anyway
   with "completed". A passing test never had to exist.
2. **No contradiction check.** B is unsolvable; the model "resolved" it by
   dropping one requirement and asserted success - the most dangerous failure
   mode for maintenance work, because the diff looks plausible.
3. **Write truncation is frequent.** 28 `WRITE-CALIBRATION`/salvage events in the
   log: the coder regularly hits the output cap on large files and has to
   continue with `write_file_append`.
4. **Round caps are tight.** 6 rounds for a simple/chat run, 12 for direct tools.
   T3 already needed 8. Anything multi-file will hit the cap.
5. **Loop stops outnumber declared completions in the log's history** (29 vs 18) -
   though that history includes debugging sessions, so it is a weak signal, not a
   success rate.

---

## 3. Verdict

**Yes for supervised, test-covered work on small scopes. No for unattended
maintenance.**

What holds up in practice:

- A concrete, localised change (one function, one flag, one rename) with a way to
  verify it: **reliable**, ~2-3 minutes, 6-8 tool calls, and it *does* run the
  verification itself when a test or command exists.
- The guards (workspace containment, write transaction, `undo_last`, noop/shrink
  blocks) mean a bad edit is recoverable - never a silent catastrophe.
- With the approval gate on, a human sees every write before it happens.

What does not hold up yet:

- **Specifications with a contradiction or a missing fact** - it produces a
  confident wrong answer (proved with B).
- **"Done" without proof** - `task_complete` accepts a failed verification.
- **Multi-file/multi-step refactors** - the round and output caps bite (T3 already
  at 8 calls; truncation on large files).
- **Unattended runs** - the approval gate stalls them by design; without the gate
  nothing checks the result.
- **Maintenance as a habit** (dependency bumps, API migrations, failing-CI triage
  across many files) - that needs many rounds, a test suite, and a verification
  gate, none of which is in place today.

### Ranked improvements (highest value first)

1. **Gate `task_complete` on verification**: if a test suite exists, `run_tests`
   must have run *and* passed this run; otherwise the answer must say "not
   verified" (and the UI should show it). One change, removes failure mode 1.
2. **Contradiction/missing-fact detection before writing**: a cheap pre-flight
   step ("can these requirements hold together?") plus an explicit `ask_user`
   path when the spec is unsatisfiable - failure mode 2 is the expensive one.
3. **Raise the caps for agentic runs** (6 -> 12 simple, 12 -> 24 direct) and make
   truncation visible in the UI as a normal step instead of a surprise.
4. **Unattended mode**: either gate off + verification required, or approvals via
   Telegram (the gateway already relays cards) - today the run simply waits.
5. **A "did the diff make sense" critic** on the final patch (the duo critic
   exists for planning; not for the finish).

With 1 and 2 alone, the agent moves from "usually right" to "right or explicitly
unsure", which is the property maintenance work actually needs.
