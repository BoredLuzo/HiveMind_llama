# HiveMind coding benchmark (D1 scaffold)

A deliberately small local harness for one question: **does HiveMind add value
over the same model and a mature CLI harness?** Absolute scores are not the
goal — in RExBench every tested agent (Aider, Claude Code, OpenHands) fails
most tasks even with hints, and WebGen-Bench tops out at 27.8 %. The comparison
that matters is HiveMind vs Aider on the same llama-server, with pass rate and
token cost reported together.

## Conditions (same model, same llama-server)

| Condition | What runs |
|---|---|
| `hivemind_direct` | HiveMind `/stream`, `mode=simple`, direct tools tier `full`. |
| `hivemind_duo` | HiveMind `/stream`, `mode=code_duo`, `duo_agentic_mode=true`, `duo_core` tier (`18` desktop + `+Subagent` = 19). |
| `hivemind_duo_plan` | Opt-in A/B variant of `hivemind_duo`: forces Thinking-Planner (`duo_planner=true`, `duo_agentic_thinking=true`) feeding a fast non-thinking coder (`duo_coder_tool_thinking=false`), even when `--no-duo-planner` is active for `hivemind_duo`. |
| `hivemind_duo_lean` | Same, `duo_lean` minimal orthogonal tier (8 tools). |
| `hivemind_duo_full` | Same, `duo_coder_tool_tier=full` (git/web/MCP/browser/LSP extras). |
| `aider` | Aider CLI against the same OpenAI-compatible llama-server. |
| `hermes` | Hermes agent CLI against the same endpoint; tokens read from its `state.db`. |
| `pi` | Pi coding agent CLI with `--mode json`; tokens from the JSON result. |
| `raw` | Diagnostic only, opt-in. One prompt, no file contents, no tools; the model must guess a unified diff. A 0/N here is expected and is not a result. |
| `raw_files` | Diagnostic only, opt-in. One prompt with full files, no tools; the model returns full corrected files (`### FILE: path` blocks). |

`--model` is forced into every condition (`aider`, `hermes`, and `pi` harnesses live in `bench/runner.py`, while `bench/external/run_external.py` wires external CLI benchmarks for `claude` and `codex`): HiveMind runs get `direct_model` /
`duo_coder_model` / `duo_critic_model` / `duo_planner_model` overrides, so the
comparison does not silently use the install's settings models. Token counts
are summed from the SSE `usage_meta` events (ToolLoop rounds included); the
`done` event is only a fallback. Hermes/Pi report near-exact usage; Aider's
CLI prints rounded tokens and no cache split, so its token row is not
cache-aware. All harnesses share `_agent_timeout_s`, so no condition gets a
longer agent budget than another.

## Task format

```
bench/tasks/<task>/
  task.json      {"id": "...", "prompt": "...", "check": "python -m unittest -q test_task", "timeout_s": 300}
  repo/          starting workspace, visible to the agent (copied fresh per run)
  tests/          hidden tests, copied in ONLY for the check (never visible)
```

`check` runs with the workspace as cwd; exit code 0 = pass. Hidden tests are
installed after the agent is done, overwriting any same-named file, so an
agent cannot edit them into passing. The runner never modifies `repo/` or
`tests/` themselves.

## Run

```bat
python bench/runner.py --list
python bench/runner.py --model qwen3.5:4b-mtp --runs 3
python bench/runner.py --conditions hivemind_duo,aider --runs 3 ^
  --llama http://127.0.0.1:8080 --hivemind http://127.0.0.1:8001
python bench/runner.py --conditions aider ^
  --aider-bin %USERPROFILE%\aider-venv\Scripts\aider.exe
```

Aider may live in its own venv (pass `--aider-bin` or set `HIVEMIND_AIDER_BIN`).
The runner adds workspace files to Aider's chat automatically and pins agentic
defaults (`until_finished=true`, `duo_planner=true` unless `--no-duo-planner`,
`duo_chunking=false`, `--num-ctx 32768`, `--cell-timeout 1800`) so local settings
cannot alter what a condition measures. (`bench/watch.py` monitors live progress.)

`--runs N` repeats every task/condition N times; local models are noisy, so
use 3–5 and compare pass rates, not single runs. Results go to
`bench/results/run-<timestamp>.json`. `--keep-work` keeps the per-run
workspaces under `bench/.work/` for inspection.

## Metrics

- **pass_rate** — fraction of runs whose check exits 0.
- **median_seconds**, **tokens_total** — a harness that takes twice as long
  has to earn that with better results.
- **Relative block (D1)** — the result JSON carries a `relative` block
  (built by `bench/relative.py`, shared with `bench/analyze.py`): per
  category rates, `cached_share` (cached / total tokens),
  `net_vs_aider_ratio` / `total_vs_aider_ratio` (token cost of the gain vs
  the same-model CLI reference; `null` when aider is absent or spent zero
  tokens) and the tool-`rounds` counts. Zero tokens are `null`, never
  fabricated zeros; all pre-existing JSON fields are unchanged. The console
  summary prints `net/aider`, cached % and rounds per condition.
- **Diagnostic baselines** — `delta_vs_raw`/`delta_vs_raw_files` are filled
  only when the corresponding opt-in condition is part of the run; they are
  `null` in normal HiveMind-vs-Aider runs. Do not report the raw row as a
  score.
- **External reference (D8)** — `bench/external/run_external.py` runs the
  same tasks through Claude Code/Codex CLI when installed (dry-run
  `--list`, clean skip when binaries/credentials are missing). Their
  numbers use other models/servers and are reported separately; see
  `bench/external/README.md`.

## Starter corpus (23 tasks)

Synthetic, dependency-free, each verified red before the reference fix and
green after it:

- **bugfix:** `fix_add`, `fix_off_by_one`, `fix_mutable_default`,
  `fix_none_guard`, `fix_int_div`, `fix_sort_key`, `fix_path_join`,
  `fix_json_serialize`, `fix_regex_word`
- **feature:** `feature_clamp`, `feature_chunk`, `feature_parse_bool`,
  `feature_word_freq`, `feature_flatten`
- **refactor:** `refactor_extract`, `refactor_rename`, `refactor_dedupe`
- **multi-file:** `multi_move`, `multi_wire`, `multi_config`, `multi_shape`
- **hard:** `hard_cycle_detect`, `hard_lru_cache`

External suites are also wired under `bench/tasks_polyglot/` (10 bundled Exercism tasks; expandable to the full 34-task Python Aider Polyglot set via `bench/import_polyglot.py`) and `bench/external/` (`deepswe_hivemind.py`, `swebench_hivemind.py`).

## Measured results

### Aider Polyglot — 34 Python tasks × 3 runs (`qwen3.6:35b-a3b-mtp`, `120k` ctx, A100 40 GB, HiveMind `v1.6.4`)

510 total cells (`102` per harness, 300 s agent timeout, hidden tests installed only at check time; merged archive: `bench/results/polyglot_35b_mtp_120k_3runs.json`):

| Condition | `pass@1` | 95% Wilson CI | `pass@3` | Run 1 / 2 / 3 | Median s (Pass) | Total Time | Net Tokens | KV Cache | Timeouts |
|---|---|---|---|---|---|---|---|---|---|
| **`hivemind_duo`** | **60/102 (58.8%)** | `[49.1%, 67.9%]` | **27/34 (79.4%)** | `22 · 20 · 18` | **26.9 s** (`23.7 s`) | 99.2 m | 1.70 M | **93.2%** | **2** |
| `hermes` | 57/102 (55.9%) | `[46.2%, 65.1%]` | 24/34 (70.6%) | `19 · 20 · 18` | 71.8 s (`43.6 s`) | 206.1 m | 3.41 M | 80.7% | 17 |
| `aider` | 54/102 (52.9%) | `[43.3%, 62.3%]` | 23/34 (67.6%) | `20 · 17 · 17` | 41.1 s (`31.8 s`) | **88.8 m** | **0.91 M**\* | —\* | 4 |
| `hivemind_duo_lean` | 53/102 (52.0%) | `[42.4%, 61.4%]` | 26/34 (76.5%) | `17 · 18 · 18` | 29.5 s (`20.9 s`) | 103.2 m | 1.63 M | 92.7% | 6 |
| `pi` | 52/102 (51.0%) | `[41.4%, 60.5%]` | 22/34 (64.7%) | `17 · 18 · 17` | 31.6 s (`20.1 s`) | 96.2 m | 1.28 M | 90.1% | 4 |

\* Aider reports rounded token totals with no cache breakdown. `pass@1` 95% CIs overlap at `N = 102` (paired McNemar `hivemind_duo` vs `hermes` `p = 0.66`, vs `aider` `p = 0.39`, vs `pi` `p = 0.17`). Across all three runs `hivemind_duo` finished #1 (or tied #1) in every run, achieved the highest `pass@3` (`27/34`), lowest median wall time (`26.9 s`), highest KV-cache reuse (`93.2%`), and fewest timeouts (`2/102`). Note on thinking mode: `llama-server` ran with Qwen3.6's default `--reasoning on`; `hivemind_duo` / `hivemind_duo_lean` disable tool-round thinking by default (`enable_thinking=false`), whereas `aider`, `hermes`, and `pi` inherit the server default.

### Local starter suite — 21 tasks (`qwen3.5:4b-mtp`)

| Condition | Pass Rate | Median Wall Time | Notes |
|---|---|---|---|
| `hivemind_duo` (`duo_core`) | **95.2%** (20/21) | 27.7 s | Autonomous tool loop + verification gate + edit salvage (~84% cached prefix reuse) |
| `aider` | 85.7% (18/21) | 20.9 s | Lower per-turn prompt tokens; Aider's CLI token count is rounded and not cache-aware |

## Notes and limits

- `raw` is intentionally weak: a single-shot diff without file contents or tools (not a harness score).
- Missing CLI binaries (`aider` / `hermes` / `pi`) fail preflight cleanly with exit code `3` before any cell runs.
- Runs are sequential on purpose: one local server, one model slot.
- The HiveMind conditions give the model real tools. Use a disposable
  install/workspace, keep secrets out of the environment, and review the
  generated changes before applying them anywhere else.
- Absolute numbers will be low. Track the delta over weeks, per task
  category, not a single headline score.
