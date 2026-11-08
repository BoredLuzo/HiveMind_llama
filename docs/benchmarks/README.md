# HiveMind Benchmarks

Local, reproducible comparisons for HiveMind, a raw model, and Aider on the
same tasks and the same llama-server. The harness lives in [`bench/`](../../bench/);
this folder holds the plan, results and analysis.

## Quick start

```bat
python bench/runner.py --list
python bench/runner.py --model qwen3.5:4b-mtp --runs 3
python bench/runner.py --conditions raw,raw_files,aider ^
  --aider-bin C:\Users\NtheP\Desktop\HiveMind_bench_tools\aider\.venv\Scripts\aider.exe
python bench/analyze.py --results bench\results\run-*.json
```

Aider may live in its own venv; pass `--aider-bin` or set
`HIVEMIND_AIDER_BIN`. The runner adds the workspace files to Aider's chat
automatically.

## Conditions

| Condition | What runs |
|---|---|
| `raw` | One prompt, no tools. The model returns a unified diff; the runner applies it with `git apply`. |
| `raw_files` | One prompt, no tools. The model returns full files (`### FILE: path`); the runner writes them. Opt-in, reported separately. |
| `hivemind_direct` | HiveMind `/stream`, `mode=simple`, direct tools tier `full`. The assistant path. |
| `hivemind_duo` | HiveMind `/stream`, `mode=code_duo`, `duo_agentic_mode=true`. The coding path. |
| `aider` | Aider CLI against the same OpenAI-compatible endpoint. |

## Task format

```
bench/tasks/<task>/
  task.json      {"id": "...", "prompt": "...", "check": "python -m unittest -q test_task", "timeout_s": 300}
  repo/          starting workspace, visible to the agent
  tests/         hidden tests, installed only for the check (tamper-proof)
```

The check command runs with the workspace as cwd; exit code 0 = pass. Hidden
tests overwrite same-named agent files immediately before the check.

## Fairness rules

- **Same model:** `--model` is forced into every condition. HiveMind requests
  carry `direct_model` and all duo role overrides; Aider uses the same
  OpenAI-compatible endpoint.
- **Pinned agentic settings:** `until_finished=true`, `duo_planner` explicit,
  `duo_chunking=false`, `duo_tool_rounds=50`. Install settings cannot silently
  change what a condition measures (one run measured `duo_tool_rounds=0`,
  i.e. a coder without tools).
- **Tool tiers:** the coder defaults to `duo_core` (read/edit/run/tests +
  LSP diagnostics); `duo_coder_tool_tier=full` opts into
  git/web/MCP/browser/LSP extras. A benchmark comparing tiers must pin the
  setting per condition.
- **Fresh workspace** per run/condition; the task fixtures are never modified.
- **No claims beyond the data:** synthetic tasks and one model do not prove a
  general coding advantage.

## Metrics

- Cell-level pass rate with a 95% Wilson interval.
- Paired majority verdict per task (more than half of its runs) and exact
  McNemar test for A/B comparisons.
- Median wall time and tokens per cell.
- **Code churn:** `files_changed`, `lines_added`, `lines_removed`,
  `new_files`, `deleted_files` per cell (agent output before hidden tests).
  A green test with a full-file rewrite is not the same quality as a
  surgical edit; the next run can compare churn.
- **Token caveat:** `tokens` is the sum of `usage` prompt+completion across
  every tool round, not unique content and not a local billing unit. Aider's
  number is its own "sent/received" report. See
  [TOKEN_EFFICIENCY.md](TOKEN_EFFICIENCY.md).

## Documents

- [ROADMAP.md](ROADMAP.md) - phased plan, time budget, fairness, Go/No-Go.
- [RESULTS_2026-11-08.md](RESULTS_2026-11-08.md) - smoke, 21-task run,
  coder-alone run, corrections.
- [TOKEN_EFFICIENCY.md](TOKEN_EFFICIENCY.md) - why HiveMind processes more
  tokens, what caching does, and what to improve.
