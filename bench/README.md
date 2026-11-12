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
| `hivemind_duo` | HiveMind `/stream`, `mode=code_duo`, `duo_agentic_mode=true`, `duo_core` tier (18 tools by default). |
| `hivemind_duo_full` | Same, `duo_coder_tool_tier=full` (git/web/MCP/browser/LSP extras). |
| `aider` | Aider CLI against the same OpenAI-compatible llama-server. |
| `hermes` | Hermes agent CLI against the same endpoint; tokens read from its `state.db`. |
| `pi` | Pi coding agent CLI with `--mode json`; tokens from the JSON result. |
| `raw` | Diagnostic only, opt-in. One prompt, no file contents, no tools; the model must guess a unified diff. A 0/N here is expected and is not a result. |
| `raw_files` | Diagnostic only, opt-in. One prompt with full files, no tools; the model returns full corrected files (`### FILE: path` blocks). |

`--model` is forced into every condition: HiveMind runs get `direct_model` /
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

Aider may live in its own venv (recommended, see
`HiveMind_bench_tools/aider`); pass `--aider-bin` or set
`HIVEMIND_AIDER_BIN`. The runner adds the workspace files to Aider's chat
automatically. Agentic defaults are pinned per request (`until_finished=true`,
`duo_planner=true`, `duo_chunking=false`), so an install's settings cannot
silently change what a condition measures.

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

- `raw` is intentionally weak: a single-shot diff without file contents or
  tools. It fails on corpus access alone, so it says nothing about HiveMind or
  Aider; use `raw_files` if a one-shot file-context diagnostic is needed.
- `aider` is optional; if the CLI is missing the run is recorded as failed
  with `aider is not installed`.
- Runs are sequential on purpose: one local server, one model slot.
- The HiveMind conditions give the model real tools. Use a disposable
  install/workspace, keep secrets out of the environment, and review the
  generated changes before applying them anywhere else.
- Absolute numbers will be low. Track the delta over weeks, per task
  category, not a single headline score.
