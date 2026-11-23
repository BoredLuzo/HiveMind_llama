# Release 1.6.5

Template-driven reasoning effort controls, decoupled sidebar Planner Effort,
SSE keepalive & journal reattach reliability, Inkling-2-Mini (`inkling2_loop`)
and `llama.cpp b12645` support, hardware-autofit defaults, model-discovery
deduplication fixes, and deep-audit hardening across security, performance,
and stability.

No settings are reset and no existing model has to be re-downloaded. Upgrade
with `update.bat` (or extract the release zip next to your old install —
`settings.json`, `models.json`, `sessions\`, `logs\`, `voice_models\`, `stt\`,
and `.venv` are kept).

## Template-Driven Reasoning Effort & Sidebar Planner Effort

- **Automatic template inspection (`backend/gguf_meta.py`, `core/model_sampling.py`):**
  Reads supported reasoning levels directly from GGUF chat templates and
  external `.jinja` templates (`effort`, `binary` On/Off, or `none`), clamping
  requests safely so strict templates never crash.
- **Clean single composer button + independent Sidebar Planner Effort (`index.html`, `static/app.js`, `core/duo_runner.py`, `core/duo_inloop_planner.py`, `hive_functions/planner.py`):**
  The chat composer shows one consistent Thinking effort pill for Direct / Coder
  (keeping Think-Budget sliders independent), while Planner Effort is configured
  directly in the sidebar (`🧠 Thinking` in Code mode and synced in Auto mode's
  Routing card) with unavailable levels grayed out for the Planner model.
  Same-model Planner ↔ Coder handoffs preserve `--reasoning on/off` slot state,
  and the legacy Coder `Escalation` UI toggle has been removed.
- **User-driven Planner budgets & leak-proof fallback (`core/duo_helpers.py`, `hive_functions/planner.py`):**
  Drops hardcoded 750/2500 thinking-token caps in favor of user-configured
  budgets (`0` = model/template default), while dynamically capping streamed
  thinking at 70% of `Planner Max Tokens` and extracting only structured steps
  on fallback so raw thinking prose never leaks into the Coder context.

## Streaming Reliability & Live UI Polish

- **SSE 15s keepalive & journal reattach (`server.py`, `static/app.js`):**
  Prevents premature browser disconnects during long cold prefills, stops
  detached queue-overflow log spam, polls `/run/journal` immediately on drop,
  and updates the reattach banner cleanly once frames resume.
- **Live thinking scroll & chronological tool stream (`static/app.js`, `routers/chats.py`):**
  Scrolling up inside live thinking blocks stays put while tokens stream;
  thinking, tool calls, and truncation/loop notices render in exact
  chronological order; saved chats load faster via `.chats_index.cache`.

## Models, `llama.cpp b12645` & Hardware Autofit

- **Inkling-2-Mini & `b12645` pin (`deploy/llama_build_pin.txt`, `model_configs/models/inkling-2-mini.json`):**
  Looped-MoE core-on-GPU offload tuning (`-ot 14`, `load_mode=none`,
  `ubatch=256`), Qwen3.6 expert-cache tuning, and `b12645` pin including
  upstream Inkling-2 tokenizer (`#32799`) and looped-MoE host-expert (`#32807`)
  fixes.
- **Clean fresh-install model recognition (`backend/llama_models.py`, `deploy/fetch_models.py`):**
  Deduplicates `models.json` catalog overrides against parsed GGUF aliases and
  ensures case-safe `mmproj` detection on Linux and Windows.
- **Hardware-autofit & deep-audit hardening (Waves 1–3C + 5-subsystem sweep):**
  Hardware-matched installer defaults, Linux CUDA autodetect, runtime MoE
  all-GPU lift, slot-lock/busy-port guards, Windows `.cmd` MCP quoting,
  Telegram mirror-journal & UI auto-scroll/reattach fixes, security
  containment, faster loopback readiness, and 32 Pyright fixes.
