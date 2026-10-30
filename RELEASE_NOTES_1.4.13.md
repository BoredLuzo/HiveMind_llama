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
- Spark-X2.6 on Vulkan runs ~5.6 t/s (single live request, 64 tokens,
  b11889 slot log `llama_server_8102_20261028-155559.log:27`); an early
  21 t/s reading and the older "~6 t/s, verified twice" note had no archived
  raw line and are dropped. Prefer Qwen3.5-4B-MTP for coding.
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

**Method labels - do not compare rows across tables.**
`llama-bench` = fresh process per variant, `-p 8192 -n 64` unless stated;
"live server" = real llama-server with the profile flags, values from the
response `timings`. All on an RX 6600.

Table 1 - `llama-bench`, **b11889**, ub 256/512/1024/2048, pp 8192,
`-n 64`, absolute shared memory (the idle baseline of ~470 MiB was not
subtracted here - see the note below the table):

| ub | pp8192 t/s | Dedicated peak | Shared peak |
|---|---|---|---|
| 256 (live) | 77.64 | 5493 MiB | 164 MiB |
| 512 | 119.96 | 5466 MiB | 206 MiB |
| 1024 | aborted | - | 80 -> 340 MiB |
| 2048 | 193.59 | 5816 MiB | 2348 MiB |

At pp 20480 the same comparison gives 118.49 t/s (ub 2048) vs 62.16 t/s
(ub 256), again `llama-bench` on b11889.

**Correction (same day):** the absolute shared numbers above were the wrong
yardstick. Measured idle baseline on this machine: dedicated 1386 MiB,
shared **470 MiB**. Judged as a delta against that baseline / against the
neighbouring variant on the **live server**, the picture changes:

| setting | live-server result | shared delta | verdict |
|---|---|---|---|
| ub 512 -> 1024 (Hermes, 18 011 tok) | prefill 91.7 -> 113.6 t/s (**+23.9 %**) | +52 MiB vs the 512 run | **1024 is the default** |
| ub 512 -> 1024 (Groot, 4.2k/10.5k) | prefill +27.7 % / +17.1 % | +58 MiB | ok |
| ncmoe 28 -> 32 (Groot, ctx 131072) | tg 14.89 -> 18.21 (shallow) | 1624 -> 1360 MiB (**-264**) | **32 in the profile** |
| MTP off -> nmax2 (Groot) | tools 15.12 -> 24.30 (**+60.7 %**), prose -4.9 % | +91 MiB | **on** |

Final Groot profile (ncmoe 32, ub 1024, MTP nmax2, ctx 131072) on the live
server, 10 rounds with tool calls, build **b12001**: **31.65 t/s mean**
(min 25.00, max 33.91), 10/10 tool calls parsed, dedicated peak 6079 MiB,
shared peak 1874 MiB, no crash. Same-model comparison across builds:
`llama-bench` tg128 gave 15.17 (b11889) vs 15.16 (b12001) - the 31.65 comes
from the live profile (MTP + ub 1024 + ncmoe 32) and a different method.

MTP A/B on Qwen3.5-4B-MTP (median of 6 rounds, prosa + tools): draft off
97.04 t/s, draft-max 3 95.38, draft-max 2 92.16. A/B on the 35B MoE with
tg128 x 3 reps, interleaved: hermes ncmoe 35 -> 18.84 t/s vs ncmoe 41 ->
17.58; MiniCPM-V 4.7 ncmoe 35 -> 19.49 vs ncmoe 41 -> 18.32. Both models
keep `moe_cpu_experts: 35` (clear wins, dedicated peak 2.6-5.4 GiB, shared
flat at ~114 MiB).
