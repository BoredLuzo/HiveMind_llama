# Release 1.4.14

Scope: the 2026-10-30/31 audit round - tool-loop correctness, run deadline,
MoE sizing, LFM3, installer, downloader. Everything in this release is
offline-tested; the live validation is the next step (Groot stays
"experimental", Hermes stays the default coder).

## Tool loop: the harness now handles its own failure modes

A live Groot run produced 7/7 coder rounds with tool calls as plain text and
an empty workspace. The audit found three independent causes, all fixed:

- **No tools were attached at all.** `duo_tool_rounds` shipped `0`, and
  `_tool_round_base` requires `> 0`. Agentic chunk runs therefore never
  entered the tool loop; the model improvised `write_file`/`{"updates": ...}`
  syntax that nothing parses. The default is now **50** (an explicit `0`
  still disables the loop). With tools on, the same model emitted 16 parsed
  calls in one run (17 tool attempts: 16 parsed + 1 truncated) and 18/18 in a small qwen3.5:4b-MTP run.
- **A truncated `edit_file` retried itself.** `write_file` already salvaged
  the partial content with a `<<<TRUNCATION_MARKER>>>`; `edit_file` cannot be
  applied half-way, so it is dropped - but the retry shipped the *same*
  clamped budget (live: 2404 tokens against a 20 455-token prompt, deterministic
  truncation). A dropped `edit_file` at `finish=length` now forces a
  compression first and tells the model to split the edit.
- **The viability floor was fixed at 1024.** A 2404-token budget passed as
  "viable" although an 11.6 KB edit needs several thousand tokens. The floor
  is now `max(1024, min(4096, request//2))`.
- **Shrink guards were absolute.** The `write_file` guard only caught <=3
  lines or >=100 -> <=10; the live 11 616 -> 1 716 byte collapse (~40 -> 9
  lines) passed. Both write guards now also block at >=30 content lines and
  <=25 % without `confirm_shrink`.

`[TOOL-CALL-MISS]` logs one WARNING per failed tool round with the request
state needed to diagnose the next incident: thinking flag and
`chat_template_kwargs`, tool count/schema size/sha256, `tool_choice`, actual
slot ctx and requested ctx, prompt/cached tokens, `finish_reason`, completion
tokens and `max_tokens`, every sampler value, and 500 chars each of content,
reasoning and raw SSE. A small qwen3.5:4b-MTP run (162 s, 18 parsed calls,
`hello.txt` correct, tests green) showed the line fires only for final prose
answers, not for tool attempts.

## Deadline: enforced inside a round, grace bounded

The deadline was checked only at round heads. One long generation plus a
compression could run past it (live: 900 s configured, the coder phase ran 1064 s). The
heartbeat now cancels the executor at the deadline (which also drops the
server-side generation) and reports `timeout` in its own stop-reason bucket
instead of `runtime_load_error`. A successful write grants at most **one**
+300 s grace extension per run (auto-verify shares the ledger; the wedge
handoff keeps its documented exemption).

## MoE sizing: the stored split is authoritative everywhere

The load gate used `vram_of_moe`, but two other paths still answered with the
dense 0.6 GB/B heuristic. Now:

| model | before | after |
|---|---|---|
| hermes3.6:35b-a3b / qwen3.6:35b-a3b | 21.0 GB | 4.66 GB |
| minicpm-v-4.7:35b | 21.0 GB | 4.64 GB |
| lfm2.5:24b-a2b | 14.4 GB | 5.91 GB |
| mellum2.1:12b-a2.5b | 7.2 GB | 4.0 GB |
| groot:19b-a3b @131k | 29.07 GB | 5.37 GB |

`vram_of()` answers every `moe_cfg_for` hit with its `active_gpu_gb`, and
`_enforce_vram_budget` classifies by `moe_cfg_for` instead of name substrings
(`"35b-a3b"`, ...), which had missed lfm2.5 and mellum and forced evictions.
Mellum keeps its measured 4.1 KB/token inside the MoE table; the Groot
profile carries `vram_gb_override 4.6` and `kv_bytes_per_token 6336`.

## ubatch: 256 shipped, 512/1024 opt-in

`duo_ubatch` defaults to **256** (the tight-GPU baseline) in code, settings
and docs. The measured 512/1024 prefill gains stay available per install. A
preset load now goes through one `apply_runtime_flags()` helper with guarded
casts and the same resolver as boot, so an explicit `duo_ubatch` survives a
preset change and a garbage value cannot 500 the apply after the settings
were already mutated.

## Models and installer

- LFM3 1.7B QAD replaces the retired LFM2.5-2.6B line: installer judge step,
  direct/judge roles, fallback ladders, `safe_profile_matrix`, README.
  `BINARY_MIN_BUILD` gates `lfm3` and `lfm3-omni` at b12001 (b11889 dies with
  `unknown model architecture: 'lfm3'`).
- Downloader: HTTP 416 with fewer bytes on disk than expected is no longer
  "Already complete"; the partial file is removed and fetched again.

## Hygiene

Log-once for profile MoE resolutions (was 10-25 INFO lines per load);
`--help` probing runs in a thread and does not cache failures; slot-log
telemetry reads a 40 KB tail from a `__file__`-anchored path instead of the
whole file per round; `save_presets` uses a PID temp name + retry; the
settings lock is an `RLock` (a re-entrant callback used to deadlock); the
Telegram start note shows the pinned chat mode; `/vram/table` includes
profile-sized MoEs. Dead code removed: the duplicate fallback ladder in
`duo_runner`, `_RAM_HEADROOM_GB`, unused `llama_server_manager` imports, the
dead KV rows, the `src_preset` ternary.

## Tests

232/232 suites, ruff clean. New: `tool_round_default`, `runtime_flags`,
`deadline_and_416`. Updated: `output_reserve` (viable floor, truncation
policy, timeout bucket), `duo_split` (new seam field), `duo_ubatch` (256 +
preset helper), `vram_profile_sizing` (table-MoE `vram_of`), `groot_profile`
(sizing fields), `tool_call_miss_log` (raw field, whitespace-robust).

## Known limits (deliberate, not regressions)

- The run deadline is a **soft** budget with one bounded grace extension; a
  single in-flight request is cancelled at the deadline, but the grace can
  move it by +300 s.
- A truncated `edit_file` is still **dropped** (never applied half-way); the
  fix is compression + a smaller retry, not partial application.
- `mutate_settings` still covers one production call site (the ~15-path
  rollout and the two god functions `run_code_duo` / `_start_process` are
  follow-ups, not part of this release).
