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
  `duo_chunking=false`, `duo_tool_rounds=50`, `duo_coder_tool_tier=core`,
  `duo_test_feedback* = false`, coder ctx 32768. Install settings cannot
  silently change what a condition measures (one run measured
  `duo_tool_rounds=0`, i.e. a coder without tools).
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

### Relative block (D1, since 2026-11-09)

The headline is the **delta against the baselines**, never an absolute
score. Both the runner JSON and the analyzer output carry an explicit
`relative` block (computed by `bench/relative.py`), alongside additive new
fields in the per-condition summary rows. All older JSON keys keep their
meaning, so previous tooling and result files stay readable.

New fields per condition in `summary`:

| Field | Meaning |
|---|---|
| `delta_vs_raw` | pass-rate difference vs the strict `raw` baseline (was already there) |
| `delta_vs_raw_files` | pass-rate difference vs the `raw_files` baseline; `null` when that baseline is not in the run |
| `cached_share` | `cached_tokens_total / tokens_total`; `null` when the condition spent no tokens |
| `rounds_total`, `rounds_median` | tool-loop rounds per condition (0 for no-tool conditions) |

Compact `relative` block in the JSON:

```json
"relative": {
  "baseline": "raw",
  "baselines_present": ["raw", "raw_files"],
  "reference": "aider",
  "conditions": {
    "hivemind_direct": {
      "pass_rate": 0.92,
      "delta_vs_raw": 0.92,          // null when raw absent
      "delta_vs_raw_files": 0.21,    // null when raw_files absent
      "categories": {                // per-category rates AND deltas
        "bugfix": {"pass_rate": 1.0, "delta_vs_raw": 1.0, "delta_vs_raw_files": 0.5}
      },
      "tokens": {
        "total": 1600, "net_total": 1200, "cached_total": 800,
        "cached_share": 0.5,
        "net_vs_aider_ratio": 6.0,   // null when aider absent or zero tokens
        "total_vs_aider_ratio": 8.0
      },
      "rounds": {"total": 6, "median": 1.0}
    }
  }
}
```

Reading guide: `delta_vs_raw` / `delta_vs_raw_files` are the gain that must
be earned; `net_vs_aider_ratio` says how many times more (uncached) tokens
the condition burned than the same-model CLI reference; `cached_share`
shows how much of the prompt traffic the server served from cache; the
category deltas show where the gain (or the cost) actually lives.
Missing baselines and zero-token conditions are `null`, never crashes or
fabricated zeros. The analyzer prints the same numbers in readable form
(`d_raw`, `d_raw_files`, `net/aider`, cached %, rounds, per-category
deltas) after its CI/McNemar section.

## External harnesses (D8)

Claude Code and Codex CLI can run the same task suite through the adapter
under [`bench/external/`](../../bench/external/) - non-invasive, with a
`--list` dry run, clean skips when a binary/credentials are missing, and
result rows the analyzer reads unchanged. HiveMind/Aider conditions are
untouched; external numbers use their own models/servers and are always
reported separately from the same-model comparison. Prerequisites,
procedure and caveats: [bench/external/README.md](../../bench/external/README.md).
**No external results have been recorded yet - none are claimed.**

## Documents

- [ROADMAP.md](ROADMAP.md) - phased plan, time budget, fairness, Go/No-Go.
- [RESULTS_2026-11-08.md](RESULTS_2026-11-08.md) - smoke, 21-task run,
  coder-alone run, corrections.
- [TOKEN_EFFICIENCY.md](TOKEN_EFFICIENCY.md) - why HiveMind processes more
  tokens, what caching does, and what to improve.
