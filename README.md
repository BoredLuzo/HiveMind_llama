# HiveMind

Local multi-agent coding assistant on top of llama.cpp. Runs on your own
hardware, no cloud, no API keys, nothing leaves your machine.

Author: Luzo (BoredLuzo) | https://github.com/BoredLuzo

## What it is

HiveMind runs several local LLM "agents" (Analyst, Coder, Critic, Explorer,
Judge, ...) through structured pipelines that analyze, plan and execute coding
tasks. Everything is driven from a single-page web UI at
`http://localhost:8001` with live SSE streaming, a VRAM monitor and full
control over agents, models and run modes.

How the components fit together: [docs/architecture.md](docs/architecture.md).

## Quick Start (Windows)

```bat
install.bat          REM interactive installer (Python, uv, deps, llama.cpp)
setup_models.bat     REM download / register / add custom models
start_hivemind.bat   REM start the server
```

Open http://localhost:8001, set your project folder in the "Workspace" field,
send a message. There is no default workspace; the first run needs one.

## Quick Start (Linux)

```bash
# one-shot installer: uv + Python 3.14, venv, llama.cpp, systemd -> /opt/hivemind
sudo deploy/install_linux.sh                    # HIVEMIND_GPU_BACKEND=vulkan|cpu|rocm

# or manually:
uv python install 3.14
uv sync --all-extras
./.venv/bin/python deploy/fetch_llamacpp.py --backend vulkan   # vulkan | cuda | cpu | rocm
./.venv/bin/python run.py
```

Same as on Windows after that: open http://localhost:8001, set a workspace.
Models go into your models folder as GGUFs, or run
`./.venv/bin/python deploy/fetch_models.py`. See "Linux details" at the bottom
for backend selection, CPU-only hosts and prefill tuning.

## Codebase understanding

The default `code_duo` path builds its picture of your repo in three layers,
two of them pure code analysis (no LLM, fast, deterministic):

1. Tree-Scout (`hive_functions/tree_scout.py`) — project tree without build
   artifacts, file-level import graph, PageRank-ranked to find the central
   files. On by default (`duo_tree_scout_enabled=true`).
2. Static Repo-Map (`hive_functions/static_repomap.py`) — deterministic symbol
   and import extraction per partition (tree-sitter/AST, regex fallback),
   cross-partition dependency graph, token-budgeted map injected into Planner
   and Coder context. On by default (`duo_static_map_chars=0` = auto budget).
3. LLM Pre-Explore (`hive_functions/pre_explore/`) — optional deep read:
   parallel worker models read the codebase and emit structured TOML contracts.
   Off by default (`duo_pre_explore=false`), enable when the static map is not
   enough.

Repo memory (`duo_repo_memory_enabled`) and symbol-reference hints
(`duo_symbol_ref_enabled`) add previously learned insights about the repo to
the Coder context.

## Run modes

| Mode | When | What happens |
|------|------|--------------|
| Simple / Direct | trivial tasks (default) | single model, direct answer, optional web-search tool calls |
| Auto | judge-routed | judge model classifies complexity, routes to Direct / Pipeline / Duo |
| Pipeline | analysis, complex docs | Analyst -> Refiner -> Critic -> Synthesizer, optional constraint loop |
| Map (AutoMap) | mixed tasks | heuristic scorer picks the best model per agent role |
| Code Duo | coding | Coder + Critic loop, sub-modes Critic-Duo and Agentic |

Mode buttons live in the sidebar (Agents tab). AutoMap routing can learn from
run outcomes (`routing_weights.json`).

Status note: `Auto` and `AutoMap` are untested and not recommended for real
work yet. Most reliable are Simple/Direct and Code-Duo agentic.

### Direct chat tool tiers

In Simple/Direct chat the model can use tools through a tier
(`direct_tools_tier`, UI: "Tool tier"):

| Tier | Value | Tools |
|------|-------|-------|
| Off | `off` | pure chat |
| Websearch | `readonly` | `web_search`, `web_fetch` only |
| Python | `python` | read tools + web + `run_python` |
| Full | `full` | read/write/exec: `edit_file`, `run_bash`, background, git, ... |

The tier only escalates what the model may call; `full` is what you want for
real edit/build requests. Without the web-search module the Websearch tier
falls back to pure chat; an unreachable SearXNG returns an error string.

### Code Duo

- Critic-Duo: Coder writes, Critic reviews (optional tool loop, max 3 rounds),
  then approve or a fix round.
- Agentic: single-model loop with tool execution, auto-test self-fix, a
  verification guard (successful `run_bash` required after edits) and a grace
  round when the budget runs out.
- Chunking: the Planner splits the task into subtasks, each chunk gets a fresh
  context, goal pinning, auto-test and self-fix retry.
- Planner with thinking: contract-aware planning, per-model thinking budgets,
  wall-clock safety net.
- Plan tracker: deviation detection (hard rules -> soft rules -> heuristic
  classifier), graduated reminders, plan rebuilding.

## Tools

Mode-scoped toolset (`tools/definitions.py`):

| Area | Tools |
|------|-------|
| Explore | `read_file`, `get_signatures`, `find_references`, `list_dir`, `find_files`, `search_code`, `subagent_research` |
| Write | `write_file`, `write_file_append`, `edit_file`, `patch_file`, `replace_lines`, `edit_ast`, `undo_last` |
| Run | `run_bash`, `run_python`, `install_package`, `start_background`, `get_background_output`, `stop_background` |
| Test | `run_tests` (auto-detects pytest/npm/vitest/jest/cargo/go/maven/dotnet) |
| Git | `git_status`, `git_commit` |
| Task | `task_complete`, `ask_user` (pause + resume) |
| Browser | `browser` (headless Chromium: navigate, snapshot, screenshot, click, type, evaluate, console, close) |
| Misc | `get_datetime`, `hivemind_pipeline` (OpenAI-compatible agent endpoint) |
| Web | `web_search`, `web_fetch` (SearXNG, added when available) |

Tool scoping per phase: `duo_full`, `duo_readonly`, `pre_explore`,
`critic_verify`, `tool_agent`, `mcp_agent`, `openai_agent`.

## VRAM & models

- llama.cpp backend: Vulkan (AMD/Intel), CUDA (NVIDIA), CPU. Multi-slot worker
  architecture, one port per loaded model.
- VRAM-aware loading: budget management (`vram_budget_gb`), automatic eviction
  before larger loads, KV-cache estimation, MoE expert handling
  (`moe_cpu_experts`).
- Preload/prefetch in the background with keep-alive tiers (pin / evict / idle
  timeout).
- Hardware VRAM safety matrix in `model_configs/safe_profile_matrix.json`
  (e.g. `default_8gb_v1`).

## Adding your own models

Three ways, easiest first.

### 1. Drop a GGUF in

Put a `.gguf` into the models folder (`models\` by default, or
`HIVEMIND_MODELS_DIR`). The filename is parsed into a canonical tag
(`Qwen3.5-4B-UD-Q4_K_XL.gguf` -> `qwen3.5:4b-ud`) and the model shows up in
the UI. No config needed for basic use.

### 2. Wizard: `setup_models.bat` -> `[C]ustom`

Walks you through GGUF source, canonical name (auto-detected), capabilities
(thinking / vision / tool-call), context and launch settings (`num_ctx`,
`mmproj_filename`, `jinja`, `reasoning`, `moe_cpu_experts`, `gpu_layers`,
`vram_gb_override`), writes `models.json` plus a per-model config file, and
can assign the model to an agent role. Scriptable variant:

```bat
python deploy\add_model.py --json path\to\config.json
```

### 3. Per-model config files

`model_configs/models/<canonical-or-base>.json` configures a model without
touching code. Loaded at startup (`model_configs/models_registry.py`), merged
over the built-in tables. Precedence: config file -> hardcoded profiles ->
heuristics.

```json
{
  "model": "my-model:7b",
  "capabilities": { "thinking": true, "vision": false, "tool_call": true },
  "vision_preprocessing": false,
  "num_ctx": 8192,
  "num_ctx_analyst": null,
  "num_ctx_duo_coder": null,
  "num_ctx_vision": null,
  "chat_template": null,
  "jinja": false,
  "reasoning": null,
  "distilled": false,
  "moe_cpu_experts": 0,
  "mtp": false,
  "gpu_layers": null,
  "mmproj_filename": null,
  "vram_gb_override": null,
  "sampling": {
    "thinking": { "temperature": 0.6, "top_k": 20, "seed": 42, "repetition_penalty": 1.0 },
    "non_thinking": { "temperature": 0.95, "top_k": 20, "seed": 42 }
  }
}
```

Field notes:

- `model` — optional canonical name, defaults to the file name (`:` becomes
  `_` because Windows filenames).
- `capabilities` — thinking / vision / tool_call, used by AutoMap and the UI
  badges.
- `vision_preprocessing` — allowlist for the image->text preprocessing path.
- `num_ctx*` — context per role, the role-specific value wins.
- `chat_template` — path or filename under `model_configs\` for
  `--chat-template-file`.
- `jinja` — pass `--jinja` (GGUF-embedded chat template).
- `reasoning` — `"on"` / `"off"` for `--reasoning`; `distilled` forces it on
  for distilled models.
- `moe_cpu_experts` — `--n-cpu-moe` override.
- `mtp` — multi-token prediction (`--spec-type draft-mtp`).
- `gpu_layers` — `--n-gpu-layers` override.
- `mmproj_filename` — explicit vision projector for this model.
- `vram_gb_override` — VRAM estimate override.
- `sampling` — optional per-mode llama.cpp sampling values, highest priority
  over the family profiles. Mode keys: `thinking`, `non_thinking`,
  `sampling_thinking_code`, `sampling_thinking_text`, `sampling_text`.
  llama.cpp "disabled" means `top_p=1.0`, `min_p=0.0`, `presence_penalty=0.0`,
  `repetition_penalty=1.0`. Only entered fields are stored, missing ones fall
  back to the family default.

A config named after the base only (e.g. `qwen3.5.json`) applies to all tags
of that base; `qwen3.5_9b-ud.json` applies to that exact tag.

### models.json

Maps canonical names to GGUF paths, highest-priority override, auto-generated
by `setup_models.bat` but hand-editable:

```json
{
  "qwen3.5:9b-ud": "C:\\models\\Qwen3.5-9B-UD-Q4_K_XL.gguf",
  "my-model:7b": "D:\\ml\\my-model-7b.gguf",
  "my-model:7b_mmproj": "D:\\ml\\mmproj-bf16.gguf"
}
```

A `_` prefix skips a key, `TODO:` paths are ignored, `<model>_mmproj` pins a
vision projector.

### Per-agent and learned config

Agent assignment via the UI Agents tab or `settings.json` (`agents` block:
model, temperature, max_tokens, thinking, thinking_budget). Learned configs
live in `model_configs/learned/<model>/<agent>.json`, managed in the UI
Configs tab or `/model_configs` API, and override base defaults at runtime.

## Download catalog

`setup_models.bat` offers exactly these models (every repo is pinned — no
fuzzy search):

| Model | Role | Download |
|-------|------|----------|
| `gemma-4:e4b-it-qat` (UD-Q4_K_XL) | allrounder / vision (+MTP drafter) | ~4.2 GB + 1 GB mmproj |
| `gemma-4:e2b-it-qat` (UD-Q4_K_XL) | small allrounder / vision (+MTP drafter) | ~2.6 GB + 1 GB mmproj |
| `qwen3.6:35b-a3b-ud` (UD-Q4_K_XL) | coder / planner (MoE) | ~22 GB |
| `hermes3.6:...-v13-mtp-apex-compact` | coder / hermes agent (MoE, MTP) | ~18 GB |
| `qwen3.5:4b-mtp` (Q4_K_M) | duo coder/planner default (MTP) | ~2.8 GB |
| `qwen3.5:2b-mtp` (Q4_K_M) | refiner (MTP) | ~1.3 GB |
| `lfm2.5:2.6b` (Q4_K_M) | subagent / judge (+DSpark drafter) | ~2 GB |

Other GGUFs on disk can be registered without downloading anything
(setup_models.bat -> [R]) or added with a full config via [C].

Multimodal: gemma-4 has a built-in vision encoder; qwen3.5/3.6/hermes use an
`mmproj` projector (auto-downloaded with the model). Non-multimodal models
fall back to the vision-agent preprocessing path.

Chat templates: qwen3.5/3.6/hermes models launch with the bundled
`model_configs/chat_template22.5.jinja`; gemma-4 uses its own built-in
template. Per-model overrides live in `model_configs/models/<tag>.json`.

## UI and control

- Single-page UI, no build step, no framework. Live code display, VRAM
  monitor, agent toggles, mode selection, SSE streaming (~56 event types).
- Ask-User pause + resume with configurable timeout, optional VRAM eviction
  during long pauses, countdown badge. Auto-answer in Until-Finished runs;
  hard-pause when the agent asks too often (>5/10min).
- Graceful stop (finish at chunk boundary, then force) and multi-state pause,
  both persisted for resume.
- Context meter with pressure warnings at 60% and 85%.
- Windows toast notifications when runs stop or need input; wake-lock during
  runs (`keep_awake_during_run`).

## Learning and memory

- Soul engine: peer-rating-based personality evolution, injects learned traits
  into system prompts.
- Skill distiller: semantic insight compaction (decay/merge/evict), top
  insights exported to `learning_logs/skills/`.
- Persistent memory: 96-dim hash embeddings with cosine retrieval, path-based
  relevance boosting, deduplicated debounced persistence.
- Post-run insight extraction and per-run/per-phase token stats.

## Safety and reliability

- Stuck detection: Jaccard similarity >= 0.92 on consecutive outputs breaks
  loops, tool-name-aware.
- Tool sandbox: `run_bash`/`run_python`/background processes run in a Windows
  Job Object with `KILL_ON_JOB_CLOSE` (`duo_tool_sandbox=true`).
- Semantic context eviction: TTL-based, recall markers, stale tool outputs
  evicted to keep the budget.
- File transactions with rollback.
- Auto-test gate: `run_tests` before `task_complete` (blocking, with fix
  rounds), optional per-chunk auto-test.
- Auto-lint after edits/writes/patches, language-dependent.

## API

- OpenAI-compatible `/v1/chat/completions` (agent tool-loop mode).
- MCP server (Model Context Protocol, `infra/mcp_server.py`), stdio + HTTP on
  port 8090 (`start_mcp.bat`), tiered read/write/exec permissions.
- `/automap/*` for routing profiles, `/model_configs/*` for base/learned/
  effective configs and learning logs.

## Requirements

- Windows 10/11 or Linux
- Python 3.12+ (3.14 recommended, auto-installed by the setup)
- uv (auto-installed)
- 8 GB+ VRAM GPU recommended (AMD/Intel via Vulkan, NVIDIA via CUDA)
- ~30 GB disk for the recommended model set
- Git (optional, autocommit/diff integration)
- Docker Desktop (optional, SearXNG web search)

## Installation (Windows)

`install.bat` asks before every download step: Python 3.14 (only if missing),
uv, venv in `.venv\`, dependencies, GPU backend choice, llama.cpp nightly into
`llama\`, then it opens `setup_models.bat` for the models and optionally
installs SearXNG when Docker is present. Afterwards run `start_hivemind.bat`
and open http://localhost:8001.

`setup_models.bat [custom models folder]`:

- `[D]` Download: recommended set or single-select (`1,4`), goes to
  `<repo>\models\`.
- `[C]` Custom: wizard to add your own model with config (see above).
- `[R]` Register own folder: scans a folder without network access and
  registers every GGUF in `models.json`.
- Vision (mmproj) files download automatically for vision-capable models. MTP
  variants are not auto-downloaded (identical filenames would mis-register).

Manual installation:

1. Python 3.14 with "Add python.exe to PATH".
2. Install uv.
3. `uv venv -p 3.14 .venv` and `uv pip install -r requirements.txt`.
4. Extract a llama.cpp build (`win-vulkan-x64.zip` / `win-cuda-x64.zip`) into
   `llama\` (highest build wins), or set `HIVEMIND_LLAMA_BIN`.
5. GGUFs into `models\` or `HIVEMIND_MODELS_DIR` (easiest via
   `setup_models.bat`).
6. Check `settings.json`: `gpu_backend`, `vram_budget_gb`.
7. `python run.py` or `start_hivemind.bat`.

### SearXNG (web search)

```bat
searxng.bat install [port]   # generate secret, build image, start (default 8888)
searxng.bat start|stop|restart|status
searxng.bat external URL     # point at an already-running SearXNG (no Docker)
```

`settings.yml` is baked into the image, no host bind mounts (avoids Docker
Desktop WSL2 path translation errors), container restarts unless stopped.
Without Docker web search stays disabled.

Not reachable? Make sure Docker Desktop is running, then
`searxng.bat install <port>` again (force-recreates the container). If you
get `invalid mount config for type "bind"` or HTTP 403/empty results, a stale
`hivemind-searxng` container from an older version is the usual cause:
`docker rm -f hivemind-searxng`, then install again.

### Scripts

| Script | Purpose |
|--------|---------|
| `start_hivemind.bat` | start the server (port from settings.json, default 8001) |
| `start_llama.bat` | alias for start_hivemind.bat |
| `stop_llama.bat` | stop server + all llama-server.exe (frees VRAM) |
| `update_llama.bat` | update llama.cpp to the latest nightly |
| `setup_models.bat` | download / register / add custom models |
| `start_mcp.bat` | MCP HTTP server for IDEs, port 8090 |
| `searxng.bat` | SearXNG manager |

## Configuration

All settings live in `settings.json`, generated from the `settings.py`
defaults. The important ones:

| Setting | Default | Description |
|---------|---------|-------------|
| `vram_budget_gb` | 7.5 | GPU VRAM limit for model loading |
| `duo_worker_slots` | 2 | parallel llama-server slots |
| `duo_tree_scout_enabled` | true | tree-scout codebase analysis |
| `duo_static_map_chars` | 0 | static repo-map budget (0 = auto) |
| `duo_pre_explore` | false | LLM pre-explore before planning |
| `duo_chunking` | true | task decomposition into subtasks |
| `duo_agentic_mode` | false | agentic (single-model) instead of duo |
| `duo_git_autocommit` | false | auto-commit after each chunk |
| `duo_git_checkpoints` | true | git checkpoint at chunk start |
| `duo_websearch_enabled` | false | web search as real tool calls in coding |
| `searxng_host` | http://localhost:8888 | SearXNG instance URL |
| `duo_runtime_profile` | balanced | fast / balanced / critical |
| `duo_coder_fallback_model` | qwen3.5:4b-ud | VRAM fallback for the coder (empty = off) |
| `duo_partial_compression` | false | keep a byte-identical raw tail at compression (KV-cache reuse) |
| `duo_compress_local_only` | false | skip the compression LLM summary, instant local fallback |
| `duo_test_feedback_final` | true | auto-run tests before task_complete |
| `duo_planner_max_tokens` | 8000 | planner output budget (0 = none) |
| `duo_planner_thinking_budget` | 8000 | planner thinking budget (0 = none) |
| `disable_thinking_in_planner` | false | force planner to skip thinking |
| `model_capability_overrides` | {} | per-model capability overrides |
| `ctx_overrides` | {} | per-role / per-model context overrides |
| `duo_tool_sandbox` | true | Windows Job-Object tool sandbox |
| `keep_awake_during_run` | true | system wake-lock during runs |
| `desktop_notifications` | true | Windows toast notifications |

Full list (generated): `docs/settings.md`, source of truth `settings.py`.

## Updating llama.cpp

`update_llama.bat`, or manually
`python deploy\fetch_llamacpp.py --backend vulkan --force` (add
`--cuda-version X.Y` for a specific runtime; otherwise detected via
nvidia-smi).

## Troubleshooting

Model not found / llama-server.exe won't start: discovery searches
`<repo>\llama\llama-bXXXX-*\llama-server.exe` (highest build, backend match,
CUDA version). Re-run installer step 5 or fetch_llamacpp.py, or set
`HIVEMIND_LLAMA_BIN`. "Model not found" for a loaded model means its GGUF is
neither in `models\` nor in `models.json` — use `setup_models.bat`.

`web_fetch` returns HTTP 403: some sites block plain fetches. HiveMind sends
a browser User-Agent first and retries with a bot User-Agent on 403, which
fixes Wikipedia-style blocks. Cloudflare-protected sites (StackOverflow,
OpenAI) need JavaScript and keep returning 403 — use `web_search` there.

Port 8001 in use:

```cmd
netstat -ano | findstr :8001
taskkill /F /PID <pid>
```

VRAM overflow: lower `vram_budget_gb` (6.5 on 8 GB cards), smaller context or
coder model, disable the vision agent if unused.

Vulkan errors: update GPU drivers, use the `win-vulkan-x64` build (not
cuda). If a model fails to start on the GPU, HiveMind retries it once with
`--n-gpu-layers 0`.

llama.cpp download slow/fails: grab the release manually from
https://github.com/ggml-org/llama.cpp/releases.

Web search empty: is SearXNG up? `docker compose -f
searxng-config/docker-compose.yml up -d`, then `curl -m 10
http://localhost:8888/healthz`. `searxng_language` takes one language
(`all` = unrestricted).

PowerShell quirks: 5.1 aliases curl (stripped automatically); native commands
writing to stderr may exit 1 despite success; add `-m 10` to curl health
checks.

Blank frontend: F12 console, make sure you're on http://localhost:8001,
Ctrl+Shift+R, check the server console output.

## Deployment

- Linux: `sudo bash deploy/install_linux.sh` (systemd, auto-restart).
- Windows: `deploy\install_windows.bat` as Administrator (NSSM, auto-restart
  after crash/OOM/power loss).
- Health monitoring: /health pings with auto-slot restart, orphan-process
  rehabilitation on startup.

## Testing

The regression suite is deterministic — no LLM, no running server, no models:

```bat
python tests\run_regressions.py
```

It guards tool dispatch, context budgets, sandboxing, planner/coder wiring,
the model registry, workspace guards and release integrity. New test:
`tests\test_<area>.py` (standalone script, exit 0/1), registered in
`tests\run_regressions.py`. Before a release: suite + smoke test (start
run.py, open the UI, check `/websearch/status`).

## Use cases

Unattended batch runs (Until-Finished with plan tracking, auto-test, graceful
stop, resume), private local coding without API costs (an 8h agent run costs
pennies in electricity instead of API dollars), multi-agent pipelines, and a
fully local alternative to cloud coding tools.

## Known limitations (v1)

- ~27 tok/s on a mid-range GPU. Slower than API models, fine for unattended
  runs.
- Server restart loses in-memory state; pause/timeout/throttle state is not
  fully persisted yet.
- No Docker sandbox; `run_bash` runs with filesystem trust (mitigated by the
  Job-Object sandbox).
- No vector DB; codebase retrieval is static repo-map + optional LLM
  pre-explore, memory uses 96-dim hash embeddings.
- Crash-resume across restarts is planned for v2 (in-session resume works).

## Roadmap

- v2: auto-resume after crash, central SSE event type registry.
- v3: embeddings-based codebase retrieval alongside the static repo-map,
  Docker sandbox for run_bash, soul engine A/B validation.

## License

Business Source License 1.1 (see `LICENSE`). Personal, non-commercial use is
free; commercial use needs a license from the author. After the Change Date
(2030-09-01) HiveMind becomes MIT licensed.

© 2026 Luzo (BoredLuzo)

## Linux details

Backend selection via `HIVEMIND_GPU_BACKEND=vulkan|cuda|cpu` (env or
`gpu_backend` in settings.json; the systemd unit ships a commented line):

- `vulkan` — default, AMD/Intel/NVIDIA via Mesa Vulkan drivers
  (`llama-bXXXX-bin-ubuntu-vulkan-x64`).
- `cuda` — NVIDIA, runtime matched to the installed driver via nvidia-smi.
- `cpu` — no GPU needed: the VRAM pre-flight is skipped entirely (model +
  KV cache in system RAM), loads with `--n-gpu-layers 0`, CPU-count thread
  defaults. Binary discovery prefers the plain `ubuntu-x64` build over
  vulkan/cuda/rocm on a CPU-only host, since a backend binary can't start
  without its loader.

Process management: the port-cleanup chain is `fuser` -> `/proc/net/tcp` inode
scan -> `pkill` (no extra packages needed); `force_kill_all` maps to
`pkill -9 -f llama-server`.

Tool commands: `python` maps to `python3` and PowerShell pipes to `head`/
`tail` at import time. For python linting install pyright globally
(`npm install -g pyright`), otherwise the lint tier degrades.

Prefill tuning on CPU hosts: after every compression the full prompt is
re-prefilled, which dominates the cost there. Levers: `llama_ubatch_size`
(512/1024 prefill faster, more RAM/compute) and thread defaults. Test with a
real run and watch the `[CACHE]` reuse telemetry in `logs/hivemind.log`.

First-run smoke test: `./.venv/bin/python run.py`, load a small model
(`ling-3.0-tiny` or `lfm2.5:2.6b`), send a short message, check
`logs/hivemind.log` for `[PRE-FLIGHT] ... -> OK` and the llama-server banner.
No binary found? Point `HIVEMIND_LLAMA_BIN` at the extracted llama-server.
