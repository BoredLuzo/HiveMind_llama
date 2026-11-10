# Polyglot Python subset (local benchmark fixtures)

Source: [Aider-AI/polyglot-benchmark](https://github.com/Aider-AI/polyglot-benchmark),
commit `7e0611e77b54e2dea774cdc0aa00cf9f7ed6144f` (Exercism Python exercises).
This is a **fixed 10-task subset** for external-anchor comparisons, not a
shipped product asset. Exercise content remains © Exercism; see the source
repository for licensing.

Selected tasks (pre-registered, no cherry-picking after a run):

1. `affine-cipher`
2. `beer-song`
3. `bowling`
4. `grade-school`
5. `list-ops`
6. `phone-number`
7. `robot-name`
8. `simple-linked-list`
9. `tree-building`
10. `wordy`

Each task has:

- `repo/` — the exercise stub and any non-test support files
- `tests/` — the hidden test file, installed only at check time
- `task.json` — prompt (the official instructions) and the pytest check

`.meta/example.py` from the source repo is **never copied**; it was only used
once to prove red-before/green-after. All 10 tasks were validated: stub fails,
example solution passes.

## Regenerate

```bash
git clone --depth 1 https://github.com/Aider-AI/polyglot-benchmark /tmp/polyglot-benchmark
python bench/import_polyglot.py \
  --source /tmp/polyglot-benchmark/python/exercises/practice \
  --out bench/tasks_polyglot
```

## Run

```bash
python bench/runner.py \
  --tasks bench/tasks_polyglot \
  --conditions hivemind_duo,aider \
  --no-duo-planner \
  --model qwen3.5:4b-mtp \
  --llama http://127.0.0.1:8101 \
  --hivemind http://127.0.0.1:8001 \
  --runs 3 \
  --aider-bin "$HOME/aider-venv/bin/aider" \
  --out bench/results/run-polyglot10-YYYYMMDD.json
```

Report pass@1 with Wilson CI, per-condition net tokens/time/rounds, and the
paired discordant tasks. Do not compare absolute scores against published
Aider leaderboard numbers (different model, quant, harness settings).
