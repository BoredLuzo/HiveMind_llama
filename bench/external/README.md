# External harness adapters (D8)

Run the **same task suite** as `bench/runner.py` through Claude Code and
Codex CLI - when they are installed - and record comparable rows. This is a
scaffold: **no results are claimed** until actual runs happen, and external
harness numbers are always reported **separately** from the same-model
comparison.

## Non-invasive guarantees

- `bench/runner.py` and its conditions (`raw`, `raw_files`,
  `hivemind_direct`, `hivemind_duo`, `hivemind_duo_lean`, `aider`) are **not
  touched** by this adapter.
- The adapter reuses the runner's task loading, fresh workspace preparation,
  code-churn metrics and the identical hidden-test check (tests are copied
  in only at check time, overwriting same-named agent files). A task passes
  for Claude Code exactly when it passes for HiveMind.
- Results go to `bench/results/external/run-<timestamp>.json` and are never
  mixed into a runner result file. Rows use condition names `claude_code`
  and `codex` and carry the same fields as runner rows, so
  `bench/analyze.py` reads them unchanged.

## Prerequisites (hard requirements - never auto-installed)

| Harness | Binary | Credentials | Verify |
|---|---|---|---|
| `claude` | Claude Code CLI on PATH, or `--claude-bin` / `HIVEMIND_CLAUDE_BIN` | logged-in session or `ANTHROPIC_API_KEY` | `claude --version` |
| `codex` | Codex CLI on PATH, or `--codex-bin` / `HIVEMIND_CODEX_BIN` | logged-in session or `OPENAI_API_KEY` | `codex --version` |

Missing binaries are **skipped cleanly** with a printed reason; a run where
none of the requested harnesses is available exits with code 3 and writes no
result file. Neither this adapter nor the runner installs anything or touches
credentials.

## Dry run / list (no execution)

```bat
python bench/external/run_external.py --list
python bench/external/run_external.py --list --harness claude --only fix_add
```

`--list` prints every task id, whether each harness binary was found (with
its prerequisite hint when not), the model that would be used and the exact
command that would run. Nothing is executed and no file is written.

## Running

```bat
python bench/external/run_external.py --harness claude --runs 3
python bench/external/run_external.py --harness claude,codex --only fix_add,fix_int_div
python bench/external/run_external.py --harness codex --codex-model gpt-5.1-codex
python bench/external/run_external.py --harness claude --claude-model claude-sonnet-4-5
```

Per harness/model defaults: without `--claude-model` / `--codex-model` the
CLI runs **its own default model** - that is the point of the reference
comparison. Record the model version the CLI reports (`model_detected`,
where available) when publishing numbers.

What is recorded per cell:

- `command` (full argv, secrets never included - none are passed),
  `model`, `exit_code`;
- wall-clock `seconds` plus `duration_reported_s` (Claude Code reports
  `duration_ms`);
- `tokens`, `cached_tokens`, `net_tokens` (Claude Code usage: uncached
  input + cache read/creation + output; Codex: last `total_token_usage`
  event, best-effort because the JSONL shape varies between versions);
- `cost_usd` when the harness reports it (Claude Code `total_cost_usd`;
  Codex currently reports none);
- `rounds` (Claude Code `num_turns`);
- pass/fail via the **same hidden-test check** as every other condition,
  plus the same code-churn fields.

## Analysis

External rows are shaped like runner rows, so the analyzer works directly:

```bat
python bench/analyze.py --results bench\results\external\run-*.json
```

To see deltas against the in-house baselines, merge files in one call:

```bat
python bench/analyze.py --results bench\results\run-<stamp>.json bench\results\external\run-<stamp>.json
```

The `relative` block then contains token ratios vs `aider` and, if the opt-in
raw baselines were merged too, their deltas. Interpret with care: the
external harnesses run different models on different servers, so those rows
compare *products*, not *harnesses*.

## Fairness caveats

- **Different model, different server.** Claude Code and Codex use their own
  hosted models; only Aider is the same-model comparison. Report the two
  parts separately, exactly as ROADMAP Phase 5 requires.
- **Turn/sandbox limits differ.** `--max-turns` (default 30) caps Claude
  Code turns; Codex runs with `--sandbox workspace-write` and
  `--skip-git-repo-check`. Document the flags used alongside any numbers.
- **Cost is money, not tokens.** `cost_usd` (when present) reflects the
  vendor's pricing; local llama-server tokens are free. Never compare the
  two directly.
- Non-interactive flags may need updating when the CLIs change; the command
  builders live in one place each (`build_claude_command`,
  `build_codex_command` in `run_external.py`).

## Honesty rule

This scaffold ships **without any recorded external results**. A "better
than Claude Code/Codex" claim requires: the same task suite, multiple runs,
the versions/models used, and the numbers published with the caveats above.
Internal benchmark notes stay local and out of this repository.
