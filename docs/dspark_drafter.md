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

## Measured: it does not pay off for Sharp-MiniCPM5-2B

Sharp-MiniCPM5-2B (Q6_K_XL) + `MiniCPM5-2.6B-DSpark.gguf`, decode rate from the
server's own timings, same prompt, 400 tokens, temperature 0:

| Hardware | no drafter | n-max 2 | n-max 4 | n-max 6 |
|---|---|---|---|---|
| RX 6600 8 GB (Vulkan, dGPU) | **83.1 tok/s** | 85.1 (+2 %) | 64.7 (-22 %) | 55.2 (-34 %) |
| Radeon iGPU (ThinkPad E15, Vulkan) | 14.1 tok/s | 10.6 | 7.7 | - |

Draft acceptance on the dGPU: 52 % at n-max 2, 32 % at 4, 22 % at 6, with a mean
proposal length of ~2 tokens.

Reading:

- at n-max 2 the dGPU result is **within noise** (+2 %), everything above that is
  clearly worse - rejected proposals and the extra 650 MB cost more than the two
  accepted tokens save,
- the target is a 2 B model at 83 tok/s: there is not enough decode time per token
  for the drafter to amortise its per-step cost,
- on an iGPU it is worse everywhere (less bandwidth to share).

So the drafter is opt-in, and for this model pair **off is the honest default**.
It is worth a try when the target gets bigger or slower (a larger model, a longer
context with slower decode) - then the same two accepted tokens save much more
wall time.

## Why the drafter is not registered as a model

`write_models_json` and `llama_models` exclude every `*dspark*` name, so the
drafter never appears in the model picker and cannot be selected as a chat model.
