# Release 1.4.13

Scope: the fixes and measurements after the 1.4.12 upload (which was
replaced three times on GitHub and is superseded by this release).

## Speed: the 35B coder loads without mmap

- The apex/compact coder profile pins `load_mode: none`: llama-bench
  tg128 measured 2.92 t/s with mmap+mlock vs 13.77 t/s with none, live
  15-16 t/s. The expert pages page in from NVMe under RAM pressure -
  that was the "35B drops to 10 t/s after an hour" mechanism.
- A RAM-GUARD in the loader rejects `load_mode: none` when free RAM is
  below GGUF size + 8 GB (live: three stacked hermes commits froze the
  machine at 56 GB commit, SSD at 100 %) and falls back to plain mmap.
- Spark-X2.6 on Vulkan runs ~6 t/s (bench-verified twice, one
  unexplained 21 t/s reading) - prefer Qwen3.5-4B-MTP for coding.
- MiniCPM-V 4.7 is vision-capable again (projector wired, bench and
  live verified).

## Fixes

- Presets: a load applies the llama startup flags immediately, warns
  about models missing locally, re-saving keeps custom prompts, the
  autoload guard survives daylight-saving stamp compares.
- MCP client audit fixes: servers that never started no longer show as
  running, handshake error statuses fail loudly, `GET /mcp/servers`
  redacts env/header values, `close_all` forgets dead servers.
- One context resolver (chat, load, duo) instead of three copies.
- A trailing `-ud` in a model name reaches the KV family table again.
- `update.bat` ignored `server_port` in settings.json (read without
  JSON parsing), so the "instance running" guard always checked port
  8001.
- Gateway: `/mode auto` on the phone hands every message to the engine
  decider, and an answering phone chat keeps the run parameters it
  started with.

## Benchmarks behind the defaults (2026-10-30)

Raw runs, RX 6600, b11889, ub 256/512/1024/2048 at pp 8192, fresh process per
variant:

| ub | pp8192 t/s | Dedicated peak | Shared peak |
|---|---|---|---|
| 256 (live) | 77.64 | 5493 MiB | 164 MiB |
| 512 | 119.96 | 5466 MiB | 206 MiB |
| 1024 | aborted | - | 80 -> 340 MiB |
| 2048 | 193.59 | 5816 MiB | 2348 MiB |

At pp 20480 the same comparison gives 118.49 t/s (ub 2048) vs 62.16 t/s
(ub 256). ub 1024/2048 were rejected for the default despite higher
throughput: both spill into shared GPU memory (the 2048 peak of 2348 MiB is
system RAM over PCIe), which is exactly the slow path this release avoids.
**Recommended engine ubatch: 512** - nearly the 2048 prefill rate without
shared-memory spill (changing it stays a manual setting).

MTP A/B on Qwen3.5-4B-MTP (median of 6 rounds, prosa + tools): draft off
97.04 t/s, draft-max 3 95.38, draft-max 2 92.16. A/B on the 35B MoE with
tg128 x 3 reps, interleaved: hermes ncmoe 35 -> 18.84 t/s vs ncmoe 41 ->
17.58; MiniCPM-V 4.7 ncmoe 35 -> 19.49 vs ncmoe 41 -> 18.32. Both models
keep `moe_cpu_experts: 35` (clear wins, dedicated peak 2.6-5.4 GiB, shared
flat at ~114 MiB).
