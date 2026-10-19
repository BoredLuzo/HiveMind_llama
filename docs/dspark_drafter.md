# DSpark speculative decoding (drafter sidecars)

A DSpark model is a small GGUF that proposes tokens; llama.cpp verifies them in a
single pass. Whether it pays off depends on the target model and the hardware: the
drafter costs VRAM and memory bandwidth, and it only helps when the accepted
proposals outweigh that cost.

## Turning it on

The model profile (`model_configs/models/<tag>.json`) carries the keys:

```json
"dspark_draft_filename": "MiniCPM5-2.6B-DSpark.gguf",
"dspark_draft_n_max": 2
```

- `dspark_draft_filename` - file name inside the models directory. The installer
  downloads it as a *sidecar*; it is never registered as a model of its own.
- `dspark_draft_n_max` - tokens the drafter may propose per step. The knob that
  decides the outcome.
- `HIVEMIND_DSPARK_SPEC=0` turns the drafter off machine-wide, without touching a
  profile.
- Needs a llama.cpp build with `--spec-type draft-dspark`; the loader warns and
  skips the drafter when the installed build lacks it.

## Measured: it depends on what the model is bound by

### A MoE with experts in RAM - a large win

MiniCPM-V 4.7 35B-A3B (mix_q3gud_im, 14.4 GiB) + `dspark-MiniCPM-V-4_7-35B-A3B-Q8_0.gguf`,
`--n-cpu-moe 35` (all experts in RAM), ctx 4096, 8 threads, RX 6600. Decode rate
from the server's own timings, 260 tokens, temperature 0:

| drafter | decode | vs off | draft acceptance |
|---|---|---|---|
| off | 21.0 tok/s | - | - |
| **n-max 2** | **31.3 tok/s** | **+49 %** | 61 % |
| n-max 4 | 28.0 tok/s | +33 % | 43 % |
| n-max 7 | 21.2 tok/s | +1 % | 27 % |

Why it works here: with the experts in RAM, every token is limited by the memory
bandwidth needed to gather ~3 B active parameters. A verified draft token reuses
the expert weights that were already read for its draft - so accepted tokens are
close to free, and n-max 2 with 61 % acceptance turns one RAM pass into ~1.5
tokens. Above n-max 2 the hit rate drops faster than the proposal length grows.

The same effect explains why a smaller context helps: at ctx 8192 the same model
measured 16.5 tok/s instead of 21.0 without a drafter (bigger KV and compute
buffers leave less room, and the extra prefill work dominates).

### A small dense model - no win

Sharp-MiniCPM5-2B (Q6_K_XL, 2.2 GB) + `MiniCPM5-2.6B-DSpark.gguf`, iGPU and dGPU:

| Hardware | no drafter | n-max 2 | n-max 4 | n-max 6 |
|---|---|---|---|---|
| RX 6600 8 GB (Vulkan, dGPU) | **83.1 tok/s** | 85.1 (+2 %) | 64.7 (-22 %) | 55.2 (-34 %) |
| Radeon iGPU (ThinkPad E15, Vulkan) | 14.1 tok/s | 10.6 | 7.7 | - |

That model is fast and compute-bound at 83 tok/s; the drafter's per-step cost is
larger than what two accepted tokens save, and the extra 650 MB costs VRAM. Kept
opt-in for it.

### Practical default

- MoE whose experts live in RAM: `dspark_draft_n_max: 2` pays off (+30-50 %).
- Small dense model: leave the drafter off, or expect noise.
- Always measure per model: acceptance is printed as
  `draft acceptance = 0.61 (142 accepted / 234 generated)` in the llama-server log.

## Why the drafter is not registered as a model

`write_models_json` and `llama_models` exclude every `*dspark*` name, so the
drafter never appears in the model picker and cannot be selected as a chat model.
