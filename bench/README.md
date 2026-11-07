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
| `hivemind_direct` | HiveMind `/stream`, `mode=chat`, direct tools tier `full`. |
| `hivemind_duo` | HiveMind `/stream`, `mode=code_duo`, `duo_agentic_mode=true`. |
| `aider` | Aider CLI against the same OpenAI-compatible llama-server. |

## Task format

```
bench/tasks/<task>/
  task.json      {"id": "...", "prompt": "...", "check": "python -m unittest -q", "timeout_s": 300}
  repo/          starting workspace (copied fresh for every run/condition)
```

`check` runs with the workspace as cwd; exit code 0 = pass. Hidden tests live
in `repo/` and are not repeated in the prompt. The runner never modifies
`repo/` itself.

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
