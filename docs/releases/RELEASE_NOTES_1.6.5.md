# Release 1.6.5

Template-driven reasoning effort controls & OpenRouter effort ratios, decoupled
sidebar Planner Effort, pure-stdlib `view_image` tool, modularized `static/js/`
UI architecture with live streaming metrics, SSE keepalive & journal reattach
reliability, mid-stream repetition-drift cutoff, Inkling-2-Mini (`inkling2_loop`)
& `llama.cpp b12676` support (including optional Tier-4 installer entry),
hardware-autofit defaults, model-discovery deduplication fixes, and pre-release
deep-audit hardening across security, agentic loops, performance, and stability.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or extract the release zip next to your old install —
`settings.json`, `models.json`, `sessions\`, `logs\`, `voice_models\`, `stt\`,
and `.venv` are kept).

## Template-Driven Reasoning Effort, OpenRouter Ratios & Sidebar Planner Effort

- **Automatic template inspection (`backend/gguf_meta.py`, `core/model_sampling.py`):**
  Reads supported reasoning levels directly from GGUF chat templates and
  external `.jinja` templates (`effort`, `binary` On/Off, or `none`), clamping
  requests safely so strict templates never crash.
- **OpenRouter effort ratios, timeout lifts & `duo_concise_write_thinking` (`core/model_sampling.py`, `core/duo_helpers.py`, `core/duo_runner.py`, `core/direct_runner.py`, `hive_functions/planner.py`, `settings_defaults.py`):**
  Explicit graduated efforts (`minimal`/`low`/`medium`/`high`/`max`) scale
  total `max_tokens` via OpenRouter ratios (`10%/20%/50%/80%/95%` of free
  context) plus guaranteed answer/tool reserve and matching wall-clock timeouts
  (`900s`/`1800s`/`3600s`), while honoring manual Think-Budget sliders. An opt-in
  `duo_concise_write_thinking` live switch (Advanced Settings -> Agentic)
  keeps `<think>` concise before `write_file`/`edit_file`/`apply_patch`.
- **Clean single composer button + independent Sidebar Planner Effort (`index.html`, `static/js/*`, `core/duo_runner.py`, `core/duo_inloop_planner.py`, `hive_functions/planner.py`):**
  The chat composer shows one consistent Thinking effort pill for Direct / Coder
  (`Effort:` override badges, Think-Budget sliders kept independent), while
  Planner Effort is configured directly in the sidebar (`🧠 Thinking` in Code
  mode) with unavailable levels grayed out for the Planner model. Same-model Planner ↔ Coder handoffs preserve
  `--reasoning on/off` slot state, and the legacy Coder `Escalation` UI toggle
  has been removed.
- **User-driven Planner budgets & leak-proof fallback (`core/duo_helpers.py`, `hive_functions/planner.py`):**
  Drops hardcoded 750/2500 thinking-token caps in favor of user-configured
  budgets (`0` = model/template default), while dynamically capping streamed
  thinking at 70% of `Planner Max Tokens` and extracting only structured steps
  on fallback so raw thinking prose never leaks into the Coder context.

## Pure-Stdlib `view_image` Tool, Drift Recovery & Live Metrics

- **Pure-stdlib `view_image` (`tools/handlers/view_image.py`, `tools/definitions.py`, `core/tool_executor.py`, `core/tool_loop.py`):**
  Enables vision-active (`mmproj`) Direct and Duo agents to inspect local
  workspace images (`10 MB` cap, magic-byte validated via stdlib with zero new
  dependencies) and browser screenshots visually under full workspace/secret
  guards.
- **Mid-stream repetition-drift cutoff & `/no_think` recovery (`utils/tool.py`, `core/agentic_tool_loop.py`, `core/duo_policies.py`, `core/duo_runner.py`):**
  Detects degenerate cyclic/line repetition loops in `thinking_token` and text
  streams, cuts them within seconds, and retries `thinking_drift` / reasoning
  `length` overruns directly with `/no_think`.
- **Live streaming metrics, UI polishes & Gateway continuity (`static/js/stream_transport.js`, `static/js/stream_handlers.js`, `core/duo_ctx_meter.py`, `core/duo/_vram.py`, `hivemind_gateway/`):**
  Fixes frozen live `out:`/`ctx:`/`t/s` counters during `thinking_token` and
  `tool_gen` bursts, unifies decimal `k` CTX labels (`65k`), guards SWA
  compression on Round 0, and preserves Gateway thinking and chat continuity.

## Modular Frontend, Streaming Reliability & Live UI Polish

- **16-module `static/js/` frontend (`static/app.js`, `static/js/*`):**
  Modularizes the UI script layer into 16 focused modules verified by a headless
  Playwright smoke suite, stream-transport recovery, and hardened settings I/O.
- **SSE 15s keepalive & journal reattach (`server.py`, `static/js/stream_transport.js`):**
  Prevents premature browser disconnects during long cold prefills, stops
  detached queue-overflow log spam, polls `/run/journal` immediately on drop,
  and updates the reattach banner cleanly once frames resume.
- **Live thinking scroll & chronological tool stream (`static/js/*`, `routers/chats.py`):**
  Scrolling up inside live thinking blocks stays put while tokens stream;
  thinking, tool calls, and truncation/loop notices render in exact
  chronological order; saved chats load faster via `.chats_index.cache`.

## Models, `llama.cpp b12676` & Hardware Autofit

- **Inkling-2-Mini in the installer & `b12676` pin (`deploy/llama_build_pin.txt`, `deploy/fetch_models.py`, `model_configs/models/inkling-2-mini.json`, `model_configs/arch_compat.json`):**
  Looped-MoE core-on-GPU offload (`ot15`, `load_mode=none`, `ubatch=512`,
  `moe_cache_mib=0`, `kv_bytes_per_token=9216`), `binary_min_build: 12600`
  guard, Qwen3.6 expert-cache CPU-share tuning, `b12676` pin, and optional
  Tier-4 catalog entry (`inkling-2-mini`, `UD-IQ3_XXS` + `mmproj`).
- **Clean fresh-install model recognition (`backend/llama_models.py`, `deploy/fetch_models.py`):**
  Deduplicates `models.json` overrides against parsed GGUF aliases, aligns
  `mmproj_dest` casing, adds case-safe `mmproj` detection on Linux and
  Windows, and strips `Authorization` headers across cross-origin redirects.
- **Hardware-autofit & pre-release security/performance sweep:**
  Hardware-matched installer defaults, Linux CUDA autodetect, destructive-gate
  flag/alias closure, ReDoS & event-loop stall elimination, bounded tool/MCP
  buffers, ToolLoop/subagent malformed-JSON history sanitizer, Direct
  prompt-cache clock preservation, blind approval-card blocking, German Windows
  `ABHÖREN` port cleanup, headless `watchdog_engine.bat` launch, UTF-8 BOM /
  corrupt-JSON protection, atomic JSON state persistence, bounded runtime
  registries, scheduler & `ModelSlot` leak fixes, and clean Pyright baseline.
