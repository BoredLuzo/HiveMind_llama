# A100 Smoke — HiveMind vs raw vs Aider (2026-11-10)

Environment: Google Colab **A100-SXM4-80GB**, Qwen3.6-35B-A3B
`UD-Q4_K_XL` (~20.8 GB), HiveMind `dev` @ `bf8690d`, sharp chat template,
llama.cpp CUDA 12.8, Aider 0.86.2. `duo_core`, planner off, 3 tasks.

## Result

| Condition | Pass | Median | Gross tokens | Net tokens | Cache | Rounds |
|---|---:|---:|---:|---:|---:|---:|
| **HiveMind `duo_core`** | **3/3** | 23.0 s | 94,448 | 24,531 | 74% | 6 |
| `raw` | 0/3 | 18.0 s | 5,816 | 5,816 | 0% | 0 |
| Aider 0.86.2 | 3/3 | 12.8 s | 4,232 | 4,232 | 0% | 0 |

Smoke wall time: **244 s**. HiveMind net-token ratio vs Aider: **5.8×**.
Tasks: `fix_add`, `fix_off_by_one`, `multi_move`.

The first HiveMind cell (`fix_add`) took 101.7 s (cold model/prefill); the next
two took 23.0 s and 13.8 s. This is the expected cold-start shape.

## What `raw` means

`raw` is the **bare-model baseline**, not Aider and not a tool harness:

- one prompt,
- no file contents attached (`raw_files` would attach full files),
- the model must return a unified diff,
- no tools, no test feedback.

It fails all three tasks because the model gets the instruction but not the
repository content. That is intentional: it is the "no assistance" lower bound.
`raw_files` is the fairer one-shot file-context baseline; it was not part of
this smoke run.

## Tokens per second

The benchmark JSON records `seconds` and `tokens` per cell, so aggregate
tokens/second can be derived as `tokens / seconds`. The engine log also records
per-round decode speed (`[CACHE] decode=…t/s`, `[PERF] … tps=…`). There is no
explicit `tokens_per_second` field in the result JSON yet.

## Caveats

- Smoke only: 3 tasks, 1 run/cell; not a headline benchmark.
- `engine already up on :8001` means this smoke reused the running engine. The
  full 21-task run was started separately.
- Raw is expected to fail; do not read it as a HiveMind/Aider regression.
