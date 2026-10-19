# DSpark speculative decoding (drafter sidecars)

A DSpark model is a small GGUF that proposes tokens; llama.cpp verifies them in
one pass. Whether it helps depends on the hardware - the drafter costs VRAM and
bandwidth, which an iGPU does not have to spare and a dedicated GPU does.

## Turning it on

The model profile (`model_configs/models/<tag>.json`) carries the two keys:

```json
"dspark_draft_filename": "MiniCPM5-2.6B-DSpark.gguf",
"dspark_draft_n_max": 2
```

- `dspark_draft_filename` - file name inside the models directory. The installer
  downloads it as a *sidecar* (it is never registered as a model of its own).
- `dspark_draft_n_max` - how many tokens the drafter may propose per step. This is
  the knob that decides the outcome.
- `HIVEMIND_DSPARK_SPEC=0` turns the drafter off machine-wide without touching a
  profile.
- Needs llama.cpp with `--spec-type draft-dspark`; the loader warns and skips the
  drafter when the installed build does not have it.

## Measured

Sharp-MiniCPM5-2B Q6_K_XL, same prompt, 400 max tokens, temperature 0, `mode=direct`:

| Hardware | no drafter | n-max 2 | n-max 4 | n-max 6 | n-max 8 |
|---|---|---|---|---|---|
| RX 6600 (Vulkan, dGPU) | 31.4 tok/s | 34.7 | **37.9** | **39.0** | 32.2 |
| Radeon iGPU (ThinkPad E15, Vulkan) | 14.1 tok/s | 10.6 | 7.7 | - | - |

Acceptance at n-max 4 on the dGPU: ~50 % (204 accepted / 410 drafted, mean
proposal 2.0 tokens).

Reading: on a dedicated GPU 4-6 is the sweet spot (+20-24 %); beyond that the
drafter wastes compute on rejected proposals. On an iGPU the drafter only takes
bandwidth away and *loses* 25-45 %. The shipped default of 2 is the compromise
that does not tank either class; raise it to 4-6 on a dedicated GPU, lower it (or
set `HIVEMIND_DSPARK_SPEC=0`) on an iGPU.

## Why the drafter is not registered as a model

`write_models_json` and `llama_models` exclude every `*dspark*` name, so the
drafter never shows up in the model picker and cannot be selected as a chat model
on its own.
