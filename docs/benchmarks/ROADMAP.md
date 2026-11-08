# Benchmark Roadmap

**As of:** 2026-11-08 · Branch `dev` @ `281aeeb`
**Goal:** measure the **relative gain over the raw model**, then compare
honestly with Aider/Claude Code/Codex on the same task suite.

## What already exists

- `bench/runner.py` with five conditions: `raw`, `raw_files`,
  `hivemind_direct`, `hivemind_duo`, `aider`.
- 21 starter tasks (9 bugfix, 5 feature, 3 refactor, 4 multi-file), each
  verified red before and green after a reference fix.
- Hidden tests (`tests/` inside a task) are installed only for the check and
  overwrite same-named agent files.
- Result JSON with pass rate, delta vs. `raw`, median duration, tokens;
  atomic write after every cell.
- `bench/analyze.py`: Wilson 95% CI, paired majority verdicts, exact McNemar,
  per-category rates.

## Phases

### Phase 0 - Preconditions (once)
- Isolated HiveMind instance with its own `settings.json`, no production
  workspace, no secrets in the environment.
- One model for all conditions (`qwen3.5:4b-mtp` for speed,
  `qwen3.6:35b-a3b-ud` for quality).
- Record llama-server and HiveMind ports.
- Aider installed (0.86.2 in `HiveMind_bench_tools/aider`), passed via
  `--aider-bin` or `HIVEMIND_AIDER_BIN`.
- Add 5-10 real-repository tasks (see below).

### Phase 1 - Harness smoke - **passed 2026-11-08**
Two tasks, two conditions, one run. Gate: `raw` can apply a diff,
`hivemind_direct` posts `mode=simple` + `direct_tools_tier=full`, the JSON
contains both conditions.
Result: `hivemind_direct` 2/2, `hivemind_duo` 1/1, `raw` 0/2,
`raw_files` 2/2, `aider` 1/1. Three measurement bugs were found and fixed
(mode, model fairness, tool-loop token counting).

### Phase 2 - Raw baseline - **done on the starter corpus**
- **Status 2026-11-08:** `raw` 3x (63 cells, 0%) and `raw_files` 3x
  (63 cells, 71.4%). Real-repository tasks are still missing.
- Format decision: `raw_files` is the primary no-tool baseline; `raw` (diff)
  is the strict negative control. Report both separately.

### Phase 3 - HiveMind conditions - **direct done, coder-alone done**
- `hivemind_direct` 3x: 63 cells, 92.1%.
- `hivemind_duo` was initially 0% because an install setting
  (`duo_tool_rounds=0`) disabled tools. After pinning the shipped default
  (50), coder-alone on 21 tasks/1 run: 95.2% [77-99], 58.7 s,
  108,378 tokens/cell. Same 20/21 tasks as direct (McNemar p=1.0).
- The coder path is the most expensive route to no better pass rate on
  small tasks; real multi-file tasks must justify it.

### Phase 4 - Aider baseline - **done on the starter corpus**
63 cells, 90.5%, 12.0 s, 946 tokens/cell. Aider 0.86.2, same model/server,
workspace files in the chat.

### Phase 5 - External harnesses (later)
- Claude Code / Codex only with their default models; report them separately
  from the same-model comparison.
- No company API keys in the local instance; empty workspace.

### Phase 6 - Analysis and weekly trend
- Per condition: pass rate, delta, median duration, tokens, per category.
- 3-5 runs, Wilson interval, paired McNemar per task.
- Fix a minimum effect size before the run (e.g. +10 percentage points);
  otherwise a delta inside the noise is not a win.

## Time and resource budget

| Phase | Cells | 4B (rough) | 35B (rough) |
|---|---:|---|---|
| Smoke | 4 | 15-30 min | 1-2 h |
| Raw / raw_files 3x | 126 | 2-4 h | 8-16 h |
| Direct + coder 3x | 126 | 4-8 h | 16-32 h |
| Aider 3x | 63 | 2-4 h | 8-16 h |
| **Total** | **~320** | **~1 day** | **several days** |

Run one model at a time (one VRAM slot); prefer overnight runs for 35B.

## Growing the task corpus

5-10 real-repository tasks, criteria:
- small, well-scoped bugfix/feature (1-3 files);
- deterministic check without network/time/randomness;
- "correct" is known (reference commit or maintainer answer);
- no secrets, no large fixtures, runtime < 60 s;
- license allows local use; avoid contamination with model training data.

Format: `task.json`, visible `repo/`, hidden `tests/`. Keep the reference fix
outside `repo/`.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Local models are noisy | 3-5 runs, pass rate, Wilson interval |
| Agent edits hidden tests | tests are installed only at check time and overwrite |
| Workspace contamination | fresh copy per cell, separate `runN/cond` dirs |
| Server/VRAM state biases a run | one model at a time, health check before each run |
| Aider flags/prompts differ | same-model caveat in the report, fixed flags |
| Absolute numbers look low | delta vs. raw as headline; RExBench/WebGen as context |
| A crash loses a run | atomic JSON write after every cell |

## Go/No-Go

- **Go for Phase 2** when Phase 1 is green and at least 20 tasks have a
  stable check.
- **Go for Phase 3** when the raw baseline and its noise are documented.
- **No "better than X" claim** without the same task suite, the same model
  (same-model part) and multiple runs.

## Deliverables

- `bench/results/run-<timestamp>.json` per run (not committed).
- `docs/benchmarks/RESULTS_<date>.md` with the table, delta and caveats.
- A weekly trend, not single values.
