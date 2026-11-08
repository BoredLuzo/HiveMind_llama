# HiveMind coding benchmark (D1 scaffold)

A deliberately small local harness for one question: **does HiveMind add value
over the same model without tools?** Absolute scores are not the goal — in
RExBench every tested agent (Aider, Claude Code, OpenHands) fails most tasks
even with hints, and WebGen-Bench tops out at 27.8 %. The number that matters
is the **delta against the raw-model baseline**.

## Conditions (same model, same llama-server)

| Condition | What runs |
|---|---|
| `raw` | One prompt, no tools. The model must answer with a unified diff, which the runner applies with `git apply`. |
| `raw_files` | One prompt, no tools. The model returns full corrected files (`### FILE: path` blocks); the runner writes them into the workspace. Opt-in (not in the default condition list), because it is easier than a diff and must be reported separately. |
| `hivemind_direct` | HiveMind `/stream`, `mode=simple`, direct tools tier `full`. |
| `hivemind_duo` | HiveMind `/stream`, `mode=code_duo`, `duo_agentic_mode=true`. |
| `aider` | Aider CLI against the same OpenAI-compatible llama-server. |

`--model` is forced into every condition: HiveMind runs get `direct_model` /
`duo_coder_model` / `duo_critic_model` / `duo_planner_model` overrides, so the
comparison does not silently use the install's settings models. Token counts
are summed from the SSE `usage_meta` events (ToolLoop rounds included); the
`done` event is only a fallback.

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
python bench/runner.py --conditions raw,hivemind_direct --runs 1 ^
  --llama http://127.0.0.1:8080 --hivemind http://127.0.0.1:8001
```

`--runs N` repeats every task/condition N times; local models are noisy, so
use 3–5 and compare pass rates, not single runs. Results go to
`bench/results/run-<timestamp>.json`. `--keep-work` keeps the per-run
workspaces under `bench/.work/` for inspection.

## Metrics

- **pass_rate** — fraction of runs whose check exits 0.
- **delta_vs_raw** — pass-rate difference (fraction, not percent) to `raw`.
- **median_seconds**, **tokens_total** — a harness that takes twice as long
  has to earn that with better results.

## Starter corpus (21 tasks)

Synthetic, dependency-free, each verified red before the reference fix and
green after it:

- **bugfix:** `fix_add`, `fix_off_by_one`, `fix_mutable_default`,
  `fix_none_guard`, `fix_int_div`, `fix_sort_key`, `fix_path_join`,
  `fix_json_serialize`, `fix_regex_word`
- **feature:** `feature_clamp`, `feature_chunk`, `feature_parse_bool`,
  `feature_word_freq`, `feature_flatten`
- **refactor:** `refactor_extract`, `refactor_rename`, `refactor_dedupe`
- **multi-file:** `multi_move`, `multi_wire`, `multi_config`, `multi_shape`

Still missing for D1: 5–10 tasks from real repositories (where "correct" is
known) and, optionally, Aider's public benchmark tasks as a shared yardstick.

## Notes and limits

- `raw` is intentionally weak: a single-shot diff without tools. That is the
  honest baseline for "no assistance".
- `aider` is optional; if the CLI is missing the run is recorded as failed
  with `aider is not installed`.
- Runs are sequential on purpose: one local server, one model slot.
- The HiveMind conditions give the model real tools. Use a disposable
  install/workspace, keep secrets out of the environment, and review the
  generated changes before applying them anywhere else.
- Absolute numbers will be low. Track the delta over weeks, per task
  category, not a single headline score.
