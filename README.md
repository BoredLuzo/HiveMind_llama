# HiveMind v1.0.1

Local multi-agent AI coding assistant powered by **llama.cpp**. Runs entirely on
your own hardware — no cloud, no API keys, no data leaving your machine.

**Author:** Luzo (BoredLuzo) — https://github.com/BoredLuzo

## What Is HiveMind

HiveMind orchestrates multiple local LLM "agents" (Analyst, Coder, Critic,
Explorer, Judge, …) through structured pipelines to analyze, plan, and execute
software engineering tasks. Everything runs through a single-page web UI
(`http://localhost:8001`) with live SSE streaming, a VRAM monitor, and full
control over agents, models, and run modes.

## Quick Start (Windows)

```bat
install.bat          REM interactive installer (Python, uv, deps, llama.cpp)
setup_models.bat     REM download / register / add custom models
start_hivemind.bat   REM start the server
```

Then open **http://localhost:8001**, set a workspace folder in the UI, and send
your first message.

> **Important:** HiveMind has **no default workspace**. Set your project folder
> in the UI "Workspace" field before the first run.

## How HiveMind Understands Your Code

Codebase understanding is the default `duo_*` / `code_duo` path. It works in
three layers — two of them are pure code analysis (no LLM, fast, deterministic):

1. **Tree-Scout** (`hive_functions/tree_scout.py`) — renders a project tree,
   filters build artifacts / binaries, and builds a file-level import graph that
   is PageRank-ranked to find the most "central" files. **On by default**
   (`duo_tree_scout_enabled=true`).
2. **Static Repo-Map** (`hive_functions/static_repomap.py`) — deterministic,
   LLM-free symbol + import extraction per partition (tree-sitter / AST with
   regex fallback), a cross-partition dependency graph, and a token-budgeted
   map that is injected into the Planner and Coder context. **On by default**
   (`duo_static_map_chars=0` → auto-budget).
3. **LLM Pre-Explore** (`hive_functions/pre_explore.py`) — *optional* deep read:
   parallel worker models read the codebase and emit structured TOML contracts
   (exports, dependencies, entry points, complexity). **Off by default**
   (`duo_pre_explore=false`) — enable it when the static map is not enough.

Repo-memory (`duo_repo_memory_enabled`) and symbol-reference hints
(`duo_symbol_ref_enabled`) further enrich the Coder context with previously
learned insights about the repository.

## Run Modes

| Mode | When | What happens |
|------|------|--------------|
| **Auto** | default | Judge model classifies complexity → routes to Direct / Pipeline / Duo |
| **Simple / Direct** | trivial tasks | Single model, direct answer, optional web-search tool calls |
| **Pipeline** | analysis / complex docs | Analyst → Refiner → Critic → Synthesizer, optional constraint feedback loop |
| **Map (AutoMap)** | mixed tasks | Judge picks the best model *per agent role* based on task type + capabilities + VRAM |
| **Code Duo** | coding | Coder + Critic loop (see below); sub-modes **Critic-Duo** and **Agentic** |

Mode buttons live in the sidebar ("Agents" tab). AutoMap routing is
conservative by default (`automap_mode="conservative"`) and can learn from
run outcomes (`routing_weights.json`).

### Code Duo (Critic-Duo / Agentic)

- **Critic-Duo** — Coder writes → Critic reviews (with optional tool-loop:
  reads files, runs tests; max 3 rounds) → approve or fix round.
- **Agentic** — single-model loop with tool execution, auto-test self-fix,
  verification guard (requires a successful `run_bash` after edits), and a grace
  round on budget exhaustion.
- **Chunking** — the Planner splits the task into subtasks; each chunk gets a
  fresh context, goal pinning, auto-test on completion, and self-fix retry.
- **Planner with Thinking** — contract-aware planning, per-model thinking
  budgets, and a wall-clock safety net.
- **Plan Tracker** — deviation detection (hard rules → soft rules → heuristic
  classifier), graduated reminders, and plan rebuilding on replan.

## Tools

Agents have a rich, mode-scoped toolset (`tools/definitions.py`):

| Area | Tools |
|------|-------|
| **Explore** | `read_file`, `get_signatures`, `find_references`, `list_dir`, `find_files`, `search_code`, `subagent_research` |
| **Write** | `write_file`, `write_file_append`, `edit_file`, `patch_file`, `replace_lines`, `edit_ast`, `undo_last` |
| **Run** | `run_bash`, `run_python`, `install_package`, `start_background`, `get_background_output`, `stop_background` |
| **Test** | `run_tests` (auto-detects pytest/npm/vitest/jest/cargo/go/maven/dotnet) |
| **Git** | `git_status`, `git_commit` |
| **Task** | `task_complete`, `ask_user` (pause + resume) |
| **Browser** | `browser` (headless Playwright/Chromium: navigate, snapshot, screenshot, click, type, evaluate, console) |
| **Web** | `web_search`, `web_fetch` (SearXNG, added when available) |

Tool scoping per phase: `duo_full`, `duo_readonly`, `pre_explore`,
`critic_verify`, `tool_agent`, `mcp_agent`, `openai_agent`.

## VRAM & Models

- **llama.cpp backend** — Vulkan (AMD/Intel), CUDA (NVIDIA), CPU. Multi-slot
  worker architecture, dedicated port per loaded model.
- **VRAM-aware loading** — budget-based management (`vram_budget_gb`), automatic
  eviction of workers before larger models, KV-cache estimation, MoE-aware
  expert handling (`moe_cpu_experts`).
- **Smart preload / prefetch** — background model loading with keep-alive tiers
  (pin / evict / idle timeout), planner-critical phase blocking.
- **Safe profile policy** — hardware-specific VRAM safety matrix
  (`model_configs/safe_profile_matrix.json`, e.g. `default_8gb_v1`).

## Adding Your Own Models

HiveMind ships with a recommended model set, but adding your own model is a
first-class workflow — including full config. Three ways, from easiest to most
complete:

### 1. Quick path — just drop a GGUF in

Place a `.gguf` file into the models folder (`models\` by default, or
`HIVEMIND_MODELS_DIR`). The filename is parsed into a canonical tag
(`Qwen3.5-4B-UD-Q4_K_XL.gguf` → `qwen3.5:4b-ud`). The model then appears in the
UI (Models tab, Agent-Card dropdowns) — no config required for basic use.

### 2. Interactive wizard — `setup_models.bat` → `[C]ustom`

Run `setup_models.bat` and choose **`[C]`** (Custom model add). The wizard
(`deploy/add_model.py`) prompts you through everything:

1. **GGUF source** — single file path, or pick from GGUFs already in your models
   folder.
2. **Canonical name** — auto-detected, editable.
3. **Capabilities** — thinking / vision / tool-call (sensible defaults
   pre-filled).
4. **Context & launch settings** — `num_ctx`, `num_ctx_duo_coder`, optional
   `mmproj_filename`, `jinja`, `reasoning`, `moe_cpu_experts`, `gpu_layers`,
   `vram_gb_override`.
5. **Write** — registers the model in `models.json` and writes a per-model
   config file `model_configs/models/<name>.json`.
6. **Optional agent assignment** — assign the model to an agent role
   (analyst / refiner / critic / synthesizer / direct / judge / duo_coder /
   duo_critic). `settings.json` is updated and the model shows up in the
   Agent-Cards dropdown automatically.

Non-interactive variant for scripting:

```bat
python deploy\add_model.py --json path\to\config.json
```

### 3. Per-model config files — full control

A config file `model_configs/models/<canonical-or-base>.json` fully configures a
model *without touching Python code*. It is loaded at startup
(`model_configs/models_registry.py`) and merged over the built-in tables —
precedence: config file → hardcoded profiles → heuristics.

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
  "vram_gb_override": null
}
```

Field notes:

- **`model`** — optional; the canonical name (`base:tag`). Defaults to the file
  name (`:` encoded as `_` because Windows forbids `:` in filenames).
- **`capabilities`** — `thinking` (reasoning tokens), `vision` (images directly),
  `tool_call` (function calling). Used by AutoMap routing and the UI badges.
- **`vision_preprocessing`** — allowlist membership for the image→text
  preprocessing path (`vision/preprocess.py`).
- **`num_ctx*`** — context sizes per role; the role-specific value wins.
- **`chat_template`** — absolute path or filename under `model_configs\` for
  `--chat-template-file`.
- **`jinja`** — pass `--jinja` (use the GGUF-embedded chat template).
- **`reasoning`** — `"on"` / `"off"` for `--reasoning`.
- **`distilled`** — force reasoning on for distilled models.
- **`moe_cpu_experts`** — `--n-cpu-moe` override.
- **`mtp`** — enable multi-token prediction / speculative decoding
  (`--spec-type draft-mtp`).
- **`gpu_layers`** — `--n-gpu-layers` override.
- **`mmproj_filename`** — explicit vision-projector file for this model.
- **`vram_gb_override`** — VRAM estimate override (display + planning).

> A config file whose name is only the base (e.g. `qwen3.5.json`) applies to
> **all** tags of that base; a `qwen3.5_9b-ud.json` file applies only to that
> exact tag.

### `models.json`

`models.json` maps canonical names to GGUF paths and is the highest-priority
override (above auto-detection). Auto-generated by `setup_models.bat`, but
hand-editable:

```json
{
  "qwen3.5:9b-ud": "C:\\models\\Qwen3.5-9B-UD-Q4_K_XL.gguf",
  "my-model:7b": "D:\\ml\\my-model-7b.gguf",
  "my-model:7b_mmproj": "D:\\ml\\mmproj-bf16.gguf"
}
```

Prefix a key with `_` to skip it (e.g. notes). `TODO:` paths are ignored.
`<model>_mmproj` keys pin a vision projector.

### Per-agent & learned config

- **Agent assignment** — UI "Agents" tab or `settings.json.agents`
  (model, temperature, max_tokens, thinking, thinking_budget). "Set all to
  model" assigns one model to every agent. Presets in `presets.json`.
- **Learned configs** — `model_configs/learned/<model>/<agent>.json`
  (temperature, max_tokens, system_prompt_override, notes), managed via the UI
  "Configs" tab or `/model_configs` API. Learned values override base defaults
  at runtime.

## Recommended Model Set

| Model | Role | Download | VRAM |
|-------|------|----------|------|
| `gemma-4:e4b-it` (Q4_K_M) | Allrounder/Vision | ~3 GB | ~3 GB |
| `qwen3.6:35b-a3b-ud` (UD-Q4_K_XL) | Coder/Planner (MoE) | ~20 GB | ~5 GB (experts in RAM) |
| `qwen3.5:4b-ud` (UD-Q4_K_XL) | Analyst/Critic/Speed | ~3 GB | ~3 GB |
| `qwen3.5:9b-ud` (UD-Q4_K_XL) | Direct/Duo-Coder | ~6 GB | ~6 GB |
| `qwen3.5:2b` (Q4_K_M) | Refiner | ~1.3 GB | ~1.5 GB |
| `lfm2.5:2.6b` (Q4_K_M) | Subagent/Judge | ~2 GB | ~2 GB |
| `qwen3.5:0.8b-ud` (UD-Q4_K_XL) | Subagent ladder | ~0.6 GB | ~0.6 GB |

Standard configs:

| Set | Models | Use | VRAM |
|-----|--------|-----|------|
| **Minimal (1 model)** | `lfm2.5:2.6b` or `gemma-4:e4b-it` | Everything (Direct/Agentic/Coder) | ~2–3GB |
| **Standard (default install)** | `qwen3.5:9b-ud` (Coder) + `qwen3.5:4b-ud` (Analyst/Critic/Synth) + `qwen3.5:2b` (Refiner) + `lfm2.5:2.6b` (Judge/Subagent) | Default agent configuration | ~7–8GB |
| **Quality** | + `qwen3.6:35b-a3b-ud` (heavy Coder/Planner) + `gemma-4:e4b-it` (Vision) | Full pipeline + Vision | ~10GB |

**Multimodal (images):** gemma-4 has a built-in vision encoder; qwen3.5/qwen3.6/
tiel-coder use `mmproj-BF16.gguf` (auto-downloaded by `setup_models.bat` or
pinned via `models.json` / `mmproj_filename`). Non-multimodal models fall back
to the vision-agent/preprocessing path.

## User Experience & Control

- **Single-page UI** (`index.html`) — no build step, no framework. Live code
  display, VRAM monitor, agent toggles, mode selection, SSE streaming.
- **SSE streaming** — ~56 event types (tokens, thinking, tool calls, planner
  output, context meter, test results, …).
- **Ask-User (pause + resume)** — agents can pause and ask for input; configurable
  timeout, optional VRAM eviction during long pauses, countdown badge in the UI.
- **Graceful Stop / Manual Pause** — two-state stop (graceful at chunk boundary
  → force abort) and multi-state pause, both persisted for resume.
- **Ask-User timeout & throttle** — auto-answer after a timeout in Until-Finished
  runs; hard-pause if the agent asks too many questions (>5/10min).
- **Context meter** — real-time token utilization with pressure warnings at 60%
  and 85%.
- **Desktop notifications & keep-awake** — Windows toasts when runs stop / need
  input; system wake-lock during runs (`keep_awake_during_run=true`).

## Learning & Memory

- **Soul Engine** — peer-rating-based personality evolution; injects learned
  traits into system prompts.
- **Skill Distiller / Writing** — semantic insight compaction (decay/merge/
  evict) and top-insights exported to `learning_logs/skills/`.
- **Persistent memory** — 96-dim hash embeddings with cosine similarity
  retrieval, path-based relevance boosting, deduplicated debounced persistence.
- **Insights & token stats** — post-run insight extraction and persisted
  per-run/per-phase token tracking.

## Safety & Reliability

- **Stuck detection** — Jaccard similarity ≥ 0.92 on consecutive outputs breaks
  loops; tool-name-aware.
- **Tool sandbox** — child processes (run_bash/run_python/background) run in a
  Windows Job Object with `KILL_ON_JOB_CLOSE` (`duo_tool_sandbox=true`).
- **Semantic context eviction** — TTL-based with recall markers, stale tool
  outputs evicted to preserve budget.
- **File transactions** — safe writes with rollback.
- **Auto-test gate** — `run_tests` before `task_complete` (blocking with fix
  rounds), plus optional per-chunk auto-test.
- **Auto-lint** — lint check after edits/writes/patches, language-dependent.

## API & Extensibility

- **OpenAI-compatible** `/v1/chat/completions` (agent tool-loop mode).
- **MCP Server** — Model Context Protocol v2 (`infra/mcp_server.py`), stdio +
  HTTP on port 8090 (`start_mcp.bat`); tiered read/write/exec permissions.
- **AutoMap API** — `/automap/*` endpoints for preview/apply routing profiles.
- **Model configs API** — `/model_configs/*` for base/learned/effective configs,
  learning logs, ratings.

## Requirements

- **Windows 10/11** (installer scripts are `.bat`; Linux works manually)
- **Python 3.12+** (3.14 recommended; auto-installed by the setup)
- **uv** (fast Python package manager; auto-installed)
- **8 GB+ VRAM GPU** recommended (AMD/Intel via Vulkan, NVIDIA via CUDA)
- **~30 GB disk** for the recommended model set
- **Git** (optional, autocommit/diff integration)
- **Docker Desktop** (optional, SearXNG web search)

## Installation (Windows)

### `install.bat`

Interactive installer — asks before every download step:

1. **Python 3.14** — detects existing installs, installs only if missing
   (winget / python.org).
2. **uv** — installed automatically if missing.
3. **Virtual environment** — `uv venv` → `.venv\` (system Python untouched).
4. **Dependencies** — `uv pip install -r requirements.txt`.
5. **GPU backend** — Vulkan (AMD/Intel) or CUDA (NVIDIA) + VRAM budget.
6. **llama.cpp** — downloads the matching nightly build into `llama\`,
   verifies `llama-server.exe`.
7. **Models** — opens `setup_models.bat` (download recommended set, select
   single models, register own folder, or add custom model).
8. **SearXNG** — optional, offered when Docker is installed.

Then run **`start_hivemind.bat`** and open **http://localhost:8001**.

### `setup_models.bat`

Standalone: `setup_models.bat [custom models folder]`

- **`[D]` Download** — recommended set or single-select (comma-separated
  numbers, e.g. `1,4`); downloads to `<repo>\models\`.
- **`[C]` Custom** — interactive wizard to add your own model **with config**
  (see *Adding Your Own Models*).
- **`[R]` Register own folder** — scans your folder (no download, no network)
  and registers every GGUF in `models.json`.
- Vision (`mmproj`) is auto-downloaded for vision-capable models. MTP variants
  are not auto-downloaded (identical filenames would mis-register).

### Manual installation

1. Install **Python 3.14** (enable "Add python.exe to PATH").
2. Install **uv**.
3. `uv venv -p 3.14 .venv` then `uv pip install -r requirements.txt`.
4. Extract a llama.cpp release (`win-vulkan-x64.zip` / `win-cuda-x64.zip`) into
   `llama\` (highest build wins), or set `HIVEMIND_LLAMA_BIN`.
5. Download GGUFs into `models\` (or `HIVEMIND_MODELS_DIR`) — easiest via
   `setup_models.bat`.
6. Check `settings.json` — `gpu_backend` ("vulkan"/"cuda") and
   `vram_budget_gb`.
7. Start: `python run.py` (or `start_hivemind.bat`).

### SearXNG (web search)

```bat
searxng.bat install [port]   # generate secret, build image, start (default 8888)
searxng.bat start|stop|restart|status
```

`settings.yml` is baked into the image (no host bind mounts). Container uses
`restart: unless-stopped`. Without Docker, web search stays disabled.

### Other scripts

| Script | Purpose |
|--------|---------|
| `start_hivemind.bat` | Start the server (port from `settings.json`, default 8001) |
| `start_llama.bat` | Alias for `start_hivemind.bat` |
| `stop_llama.bat` | Stop server + all `llama-server.exe` (frees VRAM) |
| `update_llama.bat` | Update llama.cpp to the latest nightly |
| `setup_models.bat` | Download / register / **add custom** models |
| `start_mcp.bat` | MCP HTTP server for IDEs on port 8090 |
| `searxng.bat` | SearXNG manager |

## Configuration

All settings live in `settings.json` (auto-generated from `settings.py`
defaults). Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `vram_budget_gb` | 7.5 | GPU VRAM limit for model loading |
| `duo_worker_slots` | 2 | Parallel llama-server slots |
| `duo_tree_scout_enabled` | true | Tree-Scout codebase analysis |
| `duo_static_map_chars` | 0 | Static Repo-Map budget (0 = auto) |
| `duo_pre_explore` | false | LLM Pre-Explore before planning |
| `duo_chunking` | true | Task decomposition into subtasks |
| `duo_agentic_mode` | false | Agentic (single-model) instead of Duo |
| `duo_git_autocommit` | false | Auto-commit after each chunk |
| `duo_git_checkpoints` | true | Git checkpoint at chunk start |
| `duo_websearch_enabled` | false | Web search as real tool calls in coding |
| `searxng_host` | http://localhost:8888 | SearXNG instance URL |
| `duo_runtime_profile` | balanced | fast / balanced / critical |
| `duo_coder_fallback_model` | qwen3.5:4b-ud | VRAM fallback for the coder (empty = off) |
| `duo_test_feedback_final` | true | Auto-run tests before `task_complete` |
| `duo_planner_max_tokens` | 8000 | Planner output budget (0 = none) |
| `duo_planner_thinking_budget` | 8000 | Planner thinking budget (0 = none) |
| `disable_thinking_in_planner` | false | Force planner to skip thinking |
| `model_capability_overrides` | {} | Per-model capability overrides |
| `ctx_overrides` | {} | Per-role / per-model context overrides |
| `duo_tool_sandbox` | true | Windows Job-Object tool sandbox |
| `keep_awake_during_run` | true | System wake-lock during runs |
| `desktop_notifications` | true | Windows toast notifications |

See `settings.py` for the full list (189 configurable settings; auto-generated reference in `docs/settings.md`).

## Updating llama.cpp

**Easy way — `update_llama.bat`.** Manual: `python deploy\fetch_llamacpp.py
--backend vulkan --force` (add `--cuda-version X.Y` for a specific CUDA
runtime; the driver's CUDA version is auto-detected via `nvidia-smi`).

## Troubleshooting

### "Model not found" or llama-server.exe fails to start

Auto-discovery: `<repo>\llama\llama-bXXXX-*\llama-server.exe` (highest build,
then backend match, then CUDA version). Re-run `install.bat` step 5 or
`python deploy\fetch_llamacpp.py --backend vulkan`. Override explicitly with
`HIVEMIND_LLAMA_BIN`. A model is "not found" when its GGUF is neither in
`models\` nor mapped in `models.json` — use `setup_models.bat` → `[C]`/`[R]`.

### Port 8001 already in use

```cmd
netstat -ano | findstr :8001
taskkill /F /PID <pid>
```

### VRAM overflow / CUDA out of memory

- Reduce `vram_budget_gb` (try 6.5 on 8 GB cards).
- Reduce context sizes / use a smaller coder model.
- Disable vision agent if not needed.

### Vulkan errors

- Update GPU drivers.
- Use the `win-vulkan-x64` build (not `win-cuda-x64`).
- Add `--no-vulkan` for CPU fallback (edit `run.py`).

### llama.cpp download slow or fails

- Download manually from https://github.com/ggml-org/llama.cpp/releases
- GitHub API rate limit: 60 req/hour unauthenticated; set `GITHUB_TOKEN`.

### Web search returns no results

- SearXNG running? `docker compose -f searxng-config/docker-compose.yml up -d`
- Reachable? `curl -m 10 http://localhost:8888/healthz`
- `searxng_language` accepts **one** language (`all` = unrestricted).

### PowerShell (Windows) specials

- PowerShell 5.1 aliases `curl` → the tool strips it automatically.
- Native commands writing to stderr may exit 1 despite success — check output.
- Add `-m 10` to curl health checks to avoid hangs.

### Frontend blank page

- Browser dev tools (F12) → Console.
- Make sure you're on http://localhost:8001.
- Clear cache (Ctrl+Shift+R). Check `server.py` console output.

## Deployment

- **Linux**: `sudo bash deploy/install_linux.sh` (systemd, auto-restart).
- **Windows**: `deploy\install_windows.bat` as Administrator (NSSM,
  auto-restart after crash/OOM/power loss).
- Health monitoring: llama-server `/health` pings with auto-slot restart,
  orphan-process rehabilitation on startup.

## Use Cases

- **Unattended batch runs** — Until-Finished mode with plan tracking, auto-test,
  graceful stop, and resume-after-crash.
- **Save-cost alternative to Claude Code** — an 8h agent run costs <2 EUR
  (electricity) vs $50-150 API.
- **Privacy-first** — 100% local.
- **Multi-agent pipelines** — Analyst, Refiner, Critic, Synthesizer; dual
  Coder+Critic loop for code.

## Competitors & Unique Selling Points

| Dimension | HiveMind | Claude Code | Aider | Cursor/Cline |
|-----------|----------|-------------|-------|--------------|
| Local-first (no cloud) | Yes | No | Partial | No |
| VRAM multi-model orchestration | Yes | N/A | No | No |
| Deterministic codebase map (no LLM) | Yes | No | No | No |
| Resume after crash | Yes | No | No | No |
| Manual pause / graceful stop | Yes | No | No | No |
| Plan tracker + auto-replan | Yes | Partial | No | No |
| Ask-user throttle | Yes | No | No | No |
| Soul evolution + peer ratings | Yes | No | No | No |
| 8h run cost | <2 EUR | $50-150 | varies | $20/mo cap |

## Known Limitations (v1)

- **~27 tok/s on a mid-range GPU** — slower than API models, acceptable for
  unattended runs.
- **Server restart loses in-memory state** — pause/timeout/throttle state not
  fully persisted.
- **No Docker sandbox** — `run_bash` runs with filesystem trust (mitigated by
  the Windows Job-Object sandbox).
- **No semantic vector DB** — codebase retrieval uses static Repo-Map + optional
  LLM Pre-Explore; memory uses lightweight 96-dim hash embeddings.
- **Auto-resume on crash is v2** — chunked runs can be resumed in-session; crash-recovery on restart is not implemented yet.

## Roadmap

- **v2**: Auto-Resume on crash (server detects pending resume blocks on startup).
- **v2**: Central SSE event type registry (~56 event types as magic strings).
- **v3**: Embeddings-based codebase retrieval (alongside the static Repo-Map).
- **v3**: Docker sandbox for `run_bash` isolation.
- **v3**: Soul Engine empirical validation (A/B test on 50 standard tasks).

## License

HiveMind is licensed under the **Business Source License 1.1**
(`LICENSE`). Personal, non-commercial use is freely permitted; commercial
use requires a license from the author. From the Change Date (2030-09-01)
onward, HiveMind becomes available under the MIT License.

© 2026 Luzo (BoredLuzo)
