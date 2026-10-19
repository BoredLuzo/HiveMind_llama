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

### Placement and context decide how much of it you keep

Same model, same drafter (n-max 2), decode rate from the server timings, English
prompt, RX 6600, `--n-cpu-moe` = experts left in RAM (35 = all of them):

| context | n-cpu-moe | decode | acceptance | note |
|---|---|---|---|---|
| 4096 | 35 | 25.9 tok/s | 61 % | safe baseline |
| 4096 | 32 | 27.0 tok/s | 61 % | 3 expert layers on the GPU |
| **4096** | **29** | **27.1 tok/s** | 58 % | 6 layers on the GPU - best here |
| 4096 | 26 | 22.3 tok/s | 59 % | 9 layers: VRAM over, collapses |
| 8192 | 29 | 24.1 tok/s (app path) | 43 % | KV eats the headroom again |
| 17408 | 29 | 15.8 tok/s (app path) | 21 % | too much KV, spills |

Two rules follow:

- moving ~6 expert layers to the GPU gives about +5 % - but only while the KV cache
  stays small. With a 17 k context the same setting is *slower* than leaving every
  expert in RAM, because the GPU has no room left and falls back to shared memory.
- `--ubatch-size 128` instead of 256 is neutral (27.2 vs 27.1 tok/s): the reduced
  compute buffer does not translate into throughput.

### Acceptance depends on the text, not just the model

Same run, same flags, only the prompt language changed:

| prompt | acceptance | decode |
|---|---|---|
| English how-to prose | 43-61 % | 24-27 tok/s |
| German how-to prose | 19-21 % | ~17 tok/s |

The drafter is only as good as its hit rate, and that is content-dependent. When
you compare numbers with someone else, compare the same prompt.

### The trap: a profile without `dspark_draft_n_max` inherits 10

`DSPARK_DRAFT_N_MAX` defaults to **10** when a profile names a drafter but no
budget. MiniCPM-V 4.7 35B-A3B AD-Q4 (18.7 GB, ctx 8192, experts in RAM), same
flags, only the budget differs:

| `--spec-draft-n-max` | decode | acceptance |
|---|---|---|
| **10** (inherited default) | **14.1 tok/s** | 19 % |
| 2 | **28.1 tok/s** | 60 % |
| 4 | 25.7 tok/s | 41 % |

That is a factor of two, lost silently. Every profile that names a
`dspark_draft_filename` should set `dspark_draft_n_max` as well - 2 is the value
that held up in every measurement so far.

### MTP beats an external drafter

Same task, same 8 GB card, same ctx (63488), two models:

| | MiniCPM-V 4.7 AD-Q4 + DSpark (n-max 10) | Hermes 35B-A3B + MTP (draft-max 2) |
|---|---|---|
| decode (median) | 19.8 tok/s | **33.0 tok/s** |
| draft acceptance | 21 % | **90 %** |
| coder phase | 18m36s | **7m39s** |
| tokens produced | 16,414 | 20,192 |

MTP proposes with the model's own trained heads, so 90 % of the drafts are
accepted; an external drafter has to guess and tops out around 60 % here. If raw
speed on a MoE is the goal, a model with an MTP head is the better lever than any
drafter tuning.

### Practical default

- MoE whose experts live in RAM: `dspark_draft_n_max: 2` pays off (+30-50 %).
- Small dense model: leave the drafter off, or expect noise.
- Always measure per model: acceptance is printed as
  `draft acceptance = 0.61 (142 accepted / 234 generated)` in the llama-server log.

## Why the drafter is not registered as a model

`write_models_json` and `llama_models` exclude every `*dspark*` name, so the
drafter never appears in the model picker and cannot be selected as a chat model.
