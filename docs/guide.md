# HiveMind user guide

The detailed documentation: requirements, installation, run modes, tools,
models, compression, voice, Telegram, MCP, LSP, configuration, updating,
troubleshooting, testing, use cases and known limitations. For the overview,
quick start, hardware minimums and the extras list see the
[README](../README.md).

## Requirements

- Windows 10/11 or Linux
- Python 3.12–3.14 (Python 3.14 is auto-installed by the setup)
- uv (auto-installed)
- 8 GB+ VRAM GPU recommended for single-model / sequential **Code Duo** runs (one active model slot at a time; AMD/Intel via Vulkan, NVIDIA via CUDA). **Smart Preload**, multiple simultaneously loaded models, or large 100k+ context runs need more headroom (16 GB+ VRAM or CPU/hybrid offload) — Smart Preload is off by default
- ~30 GB disk for the recommended model set
- Git (optional, autocommit/diff integration)
- Docker Desktop (optional, SearXNG web search)
- Windows Core Utils (optional but recommended): `winget install Microsoft.CoreUtils`.
  Models are trained on GNU coreutils and write `grep`, `sed`, `awk`, `head`,
  `find`; without them those commands fail on Windows. HiveMind detects the
  situation and tells the coder either way (in the prompt) or points it at the
  built-in tools (`search_code`, `read_file`, `find_files`). Check with
  `python tools/env_check.py`.

## Installation (Windows)

`install.bat` asks before every download step: Python 3.14 (only if missing),
uv, venv in `.venv\`, dependencies, GPU backend choice, llama.cpp nightly into
`llama\`, then it opens `setup_models.bat` for the models. Inside the models
step, one optional extra asks before installing anything: the routing decider
(a small CPU classifier that routes direct answer vs. code run, see
`routing_decider` in `docs/settings.md`) - off by default, and it says how to
add it later. The judge role is not a separate download: assign any installed
model to Judge in the UI. Afterwards SearXNG is installed when Docker is
present, and the Telegram gateway can be set up (`docs/gateway_setup.md`).
Then run `start_hivemind.bat` and open http://localhost:8001.

`setup_models.bat [custom models folder]`:

- `[D]` Download: recommended set or single-select (`1,4`), goes to
  `<repo>\models\`.
- `[C]` Custom: wizard to add your own model with config (see below).
- `[R]` Register own folder: scans a folder without network access and
  registers every GGUF in `models.json`.
- Vision (mmproj) files download automatically for vision-capable models. MTP
  variants are not auto-downloaded (identical filenames would mis-register).

Manual installation:

1. Python 3.12–3.14 (3.14 recommended) with "Add python.exe to PATH".
2. Install uv.
3. `uv venv -p 3.14 .venv` (or `-p 3.12` / `-p 3.13`) and `uv pip install -r requirements.txt`.
4. Extract a llama.cpp build (`win-vulkan-x64.zip` / `win-cuda-x64.zip`) into
   `llama\` (highest build wins), or set `HIVEMIND_LLAMA_BIN`.
5. GGUFs into `models\` or `HIVEMIND_MODELS_DIR` (easiest via
   `setup_models.bat`).
6. Check `settings.json`: `gpu_backend`, `vram_budget_gb`.
7. `python run.py` or `start_hivemind.bat`.

## Linux details

Environment variables (all optional; where they overlap, they beat
settings.json):

```bash
export HIVEMIND_HOST=0.0.0.0                  # bind beyond localhost (containers)
export HIVEMIND_PORT=8001
export HIVEMIND_GPU_BACKEND=vulkan            # vulkan | cuda | cpu | rocm
export HIVEMIND_LLAMA_BIN=/path/to/llama-server   # custom build beats discovery
export HIVEMIND_MODELS_DIR=/path/to/ggufs
```

Own llama.cpp build instead of the release download:

```bash
cmake -B build -G Ninja -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_TESTS=OFF
cmake --build build
export HIVEMIND_LLAMA_BIN=$PWD/build/bin/llama-server
```

Backends: `vulkan` (default; AMD/Intel/NVIDIA via Mesa), `cuda` (NVIDIA, runtime
matched to the driver via nvidia-smi), `cpu` (no GPU: pre-flight skipped,
`--n-gpu-layers 0`, CPU thread defaults, and the plain `ubuntu-x64` build is
preferred over a backend binary that cannot start without its loader).

mlock: weights are locked in RAM by default (`llama_mlock`). Raise
RLIMIT_MEMLOCK (systemd: `LimitMEMLOCK=infinity`, docker run:
`--ulimit memlock=-1`) or set `llama_mlock=false` in settings.json, or the
model load dies on the mmap/mlock step.

Runtime notes: live VRAM comes from `nvidia-smi` when present, otherwise the
static size tables are used and reclaim waits are skipped instead of stalling.
Port cleanup is `fuser` -> `/proc/net/tcp` inode scan, cmdline-checked, no extra
packages; `force_kill_all` (UI) kills every llama-server by name. `python` maps
to `python3` (and PowerShell pipes to `head`/`tail`); install pyright globally if
you want the lint tier.

CPU hosts: every compression re-prefills the whole prompt, which dominates the
cost - tune `duo_ubatch` (256 shipped; 512/1024 are measured opt-ins) and the
thread defaults, and watch the
`[CACHE]` lines in `logs/hivemind.log`.

First-run smoke test: start `run.py`, load a small model (`lfm3:1.7b-qad`), send a
short message and check `logs/hivemind.log` for `[PRE-FLIGHT] ... -> OK`. No
binary found? Point `HIVEMIND_LLAMA_BIN` at the extracted llama-server.

## Run modes

| Mode | When | What happens |
|------|------|--------------|
| Simple / Direct | trivial tasks (default) | single model, direct answer, optional web-search tool calls |
| Auto | judge-routed | the model assigned to the Judge role (any installed LLM) classifies complexity, routes to Direct / Pipeline / Duo |
| Pipeline | analysis, complex docs | Analyst -> Refiner -> Critic -> Synthesizer, optional constraint loop |
| Map (AutoMap, *experimental*) | mixed tasks | heuristic scorer picks the best model per agent role |
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
| Basic | `readonly` | `ask_user`, `task_complete`, `get_datetime` (+ web when Websearch optional is on) |
| Research | `research` | read/search tools + optional web/subagent (no write/exec) |
| Python | `python` | read tools + optional web/subagent + `run_python` |
| MCP | `mcp` | connected `mcp_*` tools (+ `task_complete`, `ask_user`, `get_datetime`) |
| Full | `full` | read/write/exec: `edit_file`, `run_bash`, background, optionals, ... |

The tier only escalates what the model may call; `full` is what you want for
real edit/build requests. Without the web-search module the Websearch tier
falls back to pure chat; an unreachable SearXNG returns an error string.

### Action approval gate

Optional safety valve for agentic runs (`duo_action_approval_enabled`,
UI: Agents tab → Execution → 🛡 Action Approval, off by default, works
mid-run). When on, the run pauses before everything that changes the
project — code/cmd execution (`run_bash`, `run_python`,
`install_package`, `start_background`), file writes (`write_file`,
`edit_file`, `write_file_append`) and `git_commit` — and shows an
approval card with the exact command/code plus an optional message field:

| Button | Effect |
|--------|--------|
| ✓ Approve once | this call runs, nothing remembered |
| ✓ Approve this file / this exact call (remember) | remembered for the current chat — writes per file, commands 1:1 per arguments; a new chat asks again |
| ✕ Deny | the model gets a denial error (your message included) and must take a different approach |
| ✉ Input only | sends your message without approving or denying — the tool does not run |

The card appears while the code is still being generated - decide in parallel and
watch it stream in the right-hand panel; if you have not decided when generation
finishes, the run waits. Desktop notifications fire when the run needs you.

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

### Codebase understanding

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

## Tools

Mode-scoped toolset (`tools/definitions.py`):

| Area | Tools |
|------|-------|
| Explore | `read_file`, `get_signatures`, `find_references`, `list_dir`, `find_files`, `search_code` (+ optional `subagent_research`) |
| Write | `write_file`, `write_file_append`, `edit_file`, `replace_lines`, `edit_ast`, `apply_patch`, `undo_last` |
| Run | `run_bash`, `run_python`, `install_package`, `start_background`, `get_background_output`, `stop_background` |
| Test | `run_tests` (auto-detects pytest/npm/vitest/jest/cargo/go/maven/dotnet) |
| Git | `git_status`, `git_commit` (optional `+Git`) |
| Task | `task_complete`, `ask_user` (pause + resume) |
| Browser | `browser` (headless Chromium: navigate, snapshot, screenshot, click, type, evaluate, console, close) |
| Misc | `get_datetime`, `remember` (durable key/value memory), `hivemind_pipeline` (OpenAI-compatible agent endpoint) |
| Web | `web_search`, `web_fetch` (SearXNG, optional `+Websearch` when available) |

Tool scoping per phase: `duo_core` (the default coder set: 18 base + `+Subagent` optional = 19),
`duo_lean`, `duo_full`, `duo_readonly`, `pre_explore`, `critic_verify`, `tool_agent`,
`mcp_agent`, `openai_agent`. Optional toggles (`+Websearch`, `+Git`, `+Subagent`) live under
**Tools & Optionals** in the sidebar.

### Language servers (LSP)

Beyond the built-in analysis tools, the coding modes can use the language
servers already installed on the machine through one generic client. LSP is
**on by default** — it activates through `lsp_enabled: true` in
`settings.json` (shipped default; no UI switch needed): once a language
server for the file type is installed, the `lsp_*` tools appear. There are
no per-language flags, and `lsp_servers` overrides a command.

| LSP tool | What it does |
|---|---|
| `lsp_status` | Lists the registry: language, resolved command, found/missing. Never starts a server. |
| `lsp_diagnostics` | Compiler/linter diagnostics for one file (`[ERROR] path:line:col message`). Push + LSP 3.17 pull; waits 5s for a cold server. |
| `lsp_definition` | Definition location(s) at a 1-based line/character position. |
| `lsp_references` | Workspace-wide references at a 1-based position. |
| `lsp_hover` | Type signature/docs at a 1-based position. |
| `lsp_symbols` | Document outline: classes, functions, methods, variables with kinds and line ranges. |
| `lsp_workspace_symbols` | Project-wide symbol search by name/prefix. |
| `lsp_code_actions` | Quickfix/refactor titles for a 1-based range; nothing is applied. |
| `lsp_rename` | Project-wide rename plan with per-file edit lines; writes nothing, apply with `edit_file`; approval-gated. |
| `lsp_format` | Formats a file through its language server and rewrites it atomically; approval-gated. |

Read-only modes get the eight read tools plus `lsp_code_actions`;
`lsp_rename` and `lsp_format` are full-mode only. A missing server answers
`LSP_UNAVAILABLE`
with an install hint. A successful write/edit warms the server in the
background (`lsp_auto_warm_on_write`, default true, bounded 3 s wait) and
attaches a compact `[LSP auto-diagnostics …]` block (8-line cap); when the
LSP is unavailable, the pyright/per-language lint block stays as the
fallback. `lsp_auto_diagnostics` (default true) turns the block off. With
`lsp_enabled=false` the `lsp_*` schemas are removed entirely;
TSX/JSX are sent as `typescriptreact`/`javascriptreact`.

Full tool arguments, tiers, errors and the server registry:
[lsp.md](lsp.md).

What the optional installer covers (`deploy\install_lsp_packs.bat`; verify the
result with `deploy\lsp_selftest.py`):

| Pack | Language(s) | Server | What it downloads |
|---|---|---|---|
| 1 | Python | pyright | npm global `pyright` (Node package) |
| 2 | TypeScript / JavaScript | typescript-language-server | npm global `typescript@5` + `typescript-language-server` |
| 3 | HTML / CSS / JSON / YAML | vscode language servers + yaml-language-server | npm global `vscode-langservers-extracted` + `yaml-language-server` |
| 4 | Bash | bash-language-server | npm global `bash-language-server` |
| 5 | Rust | rust-analyzer | rustup component `rust-analyzer` (needs a Rust toolchain) |
| 6 | C / C++ | clangd | winget package `LLVM.clangd` (~250 MB) |
| — | Go | gopls | manual: `go install golang.org/x/tools/gopls@latest` (needs Go) |
| — | Lua | lua-language-server | manual: LuaLS release binary (no npm/winget pack) |

The installer downloads only server packages, never model weights. Go and Lua
stay manual because they need a toolchain or a GitHub release asset; the UI
lists both commands under Settings → Language servers.

**Licences:** every server above is permissively licensed (MIT, Apache-2.0 or
BSD-3-Clause: pyright MIT, TypeScript Apache-2.0, typescript-language-server
MIT, vscode-langservers-extracted MIT, yaml-language-server MIT,
bash-language-server MIT, rust-analyzer MIT/Apache-2.0, clangd Apache-2.0,
gopls BSD-3-Clause, LuaLS MIT). HiveMind bundles none of them: your package
manager downloads them on request and each keeps its own licence.

### Model Context Protocol (MCP)

HiveMind speaks MCP in **both directions**. Both are opt-in and loopback-only.

**As a server — your IDE uses HiveMind's tools** (`start_mcp.bat`, HTTP 8090;
`stdio` for editors that spawn a process):

| | |
|---|---|
| Protocols | 2024-11-05, 2025-03-26 / 2025-06-18 (sessions, `GET /mcp` SSE with `Last-Event-ID` replay) and 2026-07-28 (`server/discover`, `_meta` version check, stateless) |
| Tools | the MCP server's own tool set (23 names, see `mcp.md`) — read/write/edit/run/test/git plus `model_status`; every tool carries annotations (`readOnlyHint`, `destructiveHint`, …) |
| Resources | `resources/list`, `resources/read`, `resources/templates/list` — workspace-contained, text-only, 256 KB cap, secret denylist (`settings.json`, `gateway.toml`, `.env*`, keys) |
| Prompts | `review_diff`, `explain_file`, `fix_tests` |
| Tiers | read / write / exec; exec **and** every mutating tool require `HIVEMIND_INTERNAL_TOKEN`, read-only tools stay free |
| Limits | 2 MB bodies, 64 sessions, 1 h idle TTL, per-session SSE ring of 500 events |

**As a client — external MCP servers become tools** (`mcp_client_enabled`,
off by default): stdio or HTTP, per-server call budgets
(`mcp_server_budgets`, rolling hour → `MCP_BUDGET_EXCEEDED` names server,
limit and reset time), remote annotations passed through, SSE streaming with
a body cap and one 404-session recovery.

**Tiers are opt-in by environment:** `MCP_ALLOW_WRITE=1` (write tools),
`MCP_ALLOW_EXEC=1` (exec tools, **stdio only**) and `MCP_ALLOW_QUERY=1`
(the chat tool). An editor that spawns the process uses

```json
{"mcpServers": {"hivemind": {
  "command": "python",
  "args": ["infra/mcp_server.py"],
  "cwd": "C:/path/to/HiveMind",
  "env": {"MCP_ALLOW_WRITE": "1"}
}}}
```

`python tools/mcp_config_gen.py` prints ready-to-paste configs for Claude
Desktop, Cline, Zed and VS Code — no real token in the output unless
`--include-secret` is passed.

Details: [mcp.md](mcp.md) (server),
[mcp_client.md](mcp_client.md) (client).

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

Drop a `.gguf` into the models folder (`models\`, or `HIVEMIND_MODELS_DIR`).
The filename becomes the canonical tag (`Qwen3.5-4B-UD-Q4_K_XL.gguf` ->
`qwen3.5:4b-ud`) and the model appears in the UI - no config needed.

### 2. Wizard: `setup_models.bat` -> `[C]ustom`

Asks for the GGUF source, the canonical name, capabilities and launch
settings, writes `models.json` plus a per-model config file, and can assign the
model to an agent role. Scriptable variant:

```bat
python deploy\add_model.py --json path\to\config.json
```

### 3. Per-model config files

`model_configs/models/<canonical-or-base>.json` configures a model without
touching code (loaded at startup, merged over the built-in tables: config file
-> hardcoded profiles -> heuristics).

```json
{
  "model": "my-model:7b",
  "capabilities": { "thinking": true, "vision": false, "tool_call": true },
  "num_ctx": 8192,
  "chat_template": null,
  "jinja": false,
  "mtp": false,
  "gpu_layers": null,
  "mmproj_filename": null,
  "sampling": { "thinking": { "temperature": 0.6, "top_k": 20, "seed": 42 } }
}
```
Field notes:

- `model` — canonical name (defaults to the file name; `:` becomes `_`).
- `capabilities` — thinking / vision / tool_call; drives AutoMap and the UI badges.
- `num_ctx*` — context per role (`num_ctx_analyst`, `num_ctx_duo_coder`, ...),
  the role-specific value wins.
- `chat_template` / `jinja` / `reasoning` / `distilled` — template file under
  `model_configs\`, `--jinja`, and `--reasoning` handling.
- `moe_cpu_experts` / `mtp` / `gpu_layers` / `mmproj_filename` /
  `vram_gb_override` / `kv_bytes_per_token` — llama.cpp launch overrides
  (`--n-cpu-moe`, `--spec-type draft-mtp`, `--n-gpu-layers`, projector, VRAM
  estimate). For a MoE whose VRAM was measured, `vram_gb_override` (active GPU
  part) plus `kv_bytes_per_token` give the pre-flight a correct estimate without
  a table entry.
- `sampling` — optional per-mode values (`thinking`, `non_thinking`,
  `sampling_thinking_code`, `sampling_thinking_text`, `sampling_text`) that beat
  the family profiles; only entered fields are stored.
- `mtp_draft_n_max` / `mtp_draft_p_min` — MTP draft limits for
  `--spec-draft-n-max` / `--spec-draft-p-min` when `mtp: true`.
- `kv_cache_type` (`q4_0`/`q8_0`/`f16`) and `load_mode`
  (`none`/`mmap`/`mlock`/`mmap+mlock`) — KV quantization and loader mode.

The wizard writes every field, so hand-editing is only needed for the ones above.

### Sizing a model (VRAM estimate and `--n-cpu-moe`)

The pre-flight compares a **VRAM estimate** against the free GPU memory before
every load. For a model that is not in the built-in table two numbers decide
whether that estimate is right:

- `vram_gb_override` — the part that stays **resident on the GPU**: attention
  and SSM layers, embeddings/output, the shared expert and the router
  (for a 35B-A3B class model roughly 2.5-3 GB), **plus** the KV cache and the
  compute buffer. Typical example: 2.6 GB always-resident + 0.8 GB KV at 64k
  with q8_0 + ~0.9 GB buffers ≈ **4.3 GB**.
- `kv_bytes_per_token` — the KV growth per token, so the estimate scales with
  the context you actually request.

`moe_cpu_experts` (`--n-cpu-moe N`) then controls how many expert blocks stay
in system RAM: every +1 frees roughly one expert block on the GPU (0.2-0.5 GB
depending on the model) and moves it to CPU. **Both fields are required for a
profile to be used for sizing** — set only `moe_cpu_experts` and the estimate
silently falls back to the dense heuristic (~0.6 GB per billion parameters),
which for a 35B-A3B is several GB too high.

A starting value for an 8 GB card, from the Cyber-Tiel model card:

```
--n-cpu-moe = 40 - floor((VRAM_GB - 4.3) / 0.49)     # 33 on 8 GB
```

Then step the value down and watch tokens/second: fewer CPU experts means
faster decode until the GPU runs out. Never push the MTP head's block to the
CPU — for the qwen35moe builds it is the **last** block (40 of 0..40); a value
of 41 disables the draft head instead of speeding anything up.

**What a model needs, minimum:** the GGUF, a profile JSON under
`model_configs/models/` (name, `num_ctx`, `jinja`, `reasoning`,
`moe_cpu_experts`), and a `models.json` alias if the file name does not match
the tag. Vision models additionally need their `mmproj_filename`; models with
an MTP head need `mtp: true` (the flags are only passed when the binary
supports them). Everything else has a sane default.

### Context compression: what it costs and which toggles help

Compression itself is cheap: the local compressor truncates tool output and
drops old turns in milliseconds. The expensive part is the **re-prefill**.
llama.cpp caches an exact token prefix; when compression replaces early
messages with a summary, the new prompt diverges near the start and the whole
prompt is recomputed. Measured on a 19B MoE run: 20,978 -> 13,571 tokens
compressed in 9 ms, then the next round had 15,000 prompt tokens, `cached=0`,
and paid ~117 s at ~128 t/s. Before compression the same slot reused 98-100 %.

Hybrid models (`full_attention_interval=4`, e.g. qwen3.6): only every
fourth layer carries a growing KV cache, and a recurrent state cannot be
shifted or partially rewound. `--cache-reuse`/KV-shift therefore does not help
there; `--ctx-checkpoints` is the only restore path. Keep retained messages
byte-identical (do not truncate them in place); prefer dropping whole old
messages.

When the summary plus the kept raw tail is still above the compression
threshold, the compressor now drops the oldest **whole** tail turns (never an
assistant `tool_calls` message without its tool results), keeps the newest
four messages, and adds a `[CONTEXT DROPPED: …]` marker. The rule-based
fallback does the same after masking old tool outputs. This is the last
deterministic lever before the next round; it also releases the read guard for
dropped `read_file` paths.

| Setting | Default | Effect |
|---|---|---|
| `duo_coder_ctx_agentic` | model/profile | Coder context ceiling. A larger ctx does **not** make prefill faster per token; it makes full re-prefills rarer and slower at depth. On 8 GB, 65k-100k is the practical range; 150k risks shared-memory spill. |
| `duo_compress_auto_floor` | `0.70` | Auto trigger as a fraction of ctx when `duo_compress_threshold` is 0. `0.85-0.9` gives hysteresis: compress late, keep a long runway. |
| `duo_compress_threshold` | `0` | Absolute token threshold; overrides the floor. |
| `duo_compress_overflow_reserve` | `1024` | Tokens kept free for output; larger = earlier compression. |
| `duo_compress_local_only` | `false` | Skip the LLM summary and use the local rule/tail compressor: fast and deterministic. Enabling it removes the summary-model call from the critical path. |
| `duo_compress_model` | `auto` | Summary model when local-only is off. |
| `duo_compress_llm_timeout_s` | `180` | Summary timeout. |
| `duo_compress_tail_tool_max_chars` | `8000` | Keep only the last N chars of a tool output. 3000-4000 for tool-heavy runs; `0` disables. Truncating a retained message changes the prefix, so prefer limiting output at the source. |
| `duo_partial_compression` | `false` | Compress only part of the history, keeping the tail stable. On hybrid models there is no KV shift, so the gain is limited. |
| `duo_max_compressions` | `40` | Hard cap per run. |
| `duo_tool_output_ttl` | `3` | How many rounds a tool output stays in context. Evicting earlier mutates history; do it at compression time, not between rounds. |
| `llama_cache_ram_mib` | `1024` | Host-side prompt cache. Roughly 18 KiB/token: 32k ~0.6 GB, 100k ~1.8 GB. It only helps when the prefix matches. |
| `llama_ctx_checkpoints` | `4` | Restore points per slot. More can provide a reuse point before the summary, at host-RAM cost. |
| `llama_cache_reuse` | `256` | Chunk reuse for non-hybrid models. Hybrid/recurrent models cannot shift, so it does not help hybrid models (e.g. qwen3.6). |

Rules of thumb:

- **Hysteresis**: trigger at 85-90 % of ctx, compress to an absolute target of
  8-12k. The cost per compression is the target prefill; the trigger only
  decides how often you pay it. Overhead per 1k new context is
  `(target / prefill_rate) / (trigger - target)`. The ceiling is the resolved
  compression threshold; if the summary plus kept tail exceeds it, whole tail
  turns are dropped until it fits. There is no separate absolute target
  setting.
- **No-op guard**: a compression that saves only a few percent is pure loss;
  skip it and trim tool output instead. This is not automatic yet.
- **8 GB starting point**: ctx 65536-100000, `duo_compress_auto_floor=0.85`,
  `duo_compress_local_only=true`, tail cap 4000 (or 0 plus source limits),
  `llama_cache_ram_mib=2048` for 100k, `-ub 1024`.
- **Diagnosis**: check `[CACHE] reuse`, `pages_in_s` and the shared-memory peak
  before assuming the model is slow. A cold re-prefill is deterministic; a
  paging model is not.


A config named after the base only (example name: `gemma-4.json`) applies to
all tags of that base; the exact-tag form (example: `gemma-4_e4b-it.json`)
applies to that one tag. Both example names are conventions, not shipped
files - a profile exists once you add the model; `model_configs/models/`
holds the ones that do.

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
model, temperature, max_tokens, thinking, thinking_budget).

**Learned configs** are the pipeline's own tuning notes per model and agent:
`model_configs/learned/<model>/<agent>.json` (temperature, max_tokens and friends
that a run measured to work for that combination). They are created and edited in
the UI Configs tab or via `/model_configs/learned/...`, and they **only apply when
`learning_preset_mode` is on** - off by default, so a fresh install always uses the
base config plus your agent settings. Deleting the folder resets the learning.

## Download catalog

`setup_models.bat` offers exactly these models (every repo is pinned - no fuzzy
search). The list lives in `deploy/fetch_models.py`; the sizes below were measured
on the Hub on 2026-10-18 and are the download figures for the GGUF plus its
projector/drafter. A repo can rename or re-quantise a file, so
`python deploy\fetch_models.py --list-only` is always the authoritative answer.

| Model | Repo | Role | Download |
|-------|------|------|----------|
| `gemma-4:e4b-it-qat` | unsloth/gemma-4-E4B-it-qat-GGUF | all-rounder / vision (+ MTP drafter) | 4.2 GB + 1.0 GB mmproj |
| `gemma-4:e2b-it-qat` | unsloth/gemma-4-E2B-it-qat-GGUF | small all-rounder / vision (+ MTP drafter) | 2.6 GB + 1.0 GB mmproj |
| `gemma-4:e4b-uncensored-hauhaucs-aggressive` | HauhauCS/Gemma-4-E4B-Uncensored-HauhauCS-Aggressive | uncensored all-rounder | 5.4 GB + 1.0 GB mmproj |
| `qwen3.6:35b-a3b-ud` | unsloth/Qwen3.6-35B-A3B-GGUF | coder / planner (MoE) | 22.4 GB + 0.9 GB mmproj |
| `hermes3.6:35b-a3b-uncensored-genesis-apex-mini` | LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-Hermes-GGUF | coder / hermes agent (MoE + MTP + vision) | 20.2 GB + 0.9 GB mmproj |
| `groot:19b-a3b-261023` | GrootTeam/Groot-19B-A3B-261023-GGUF | agent coder, **experimental** (MoE 19B/3B, 128 experts, 128K ctx) | 11.7 GB |
| `qwen3.5:9b-mtp` | unsloth/Qwen3.5-9B-MTP-GGUF | heavy duo coder (MTP) | 5.9 GB + 0.9 GB mmproj |
| `qwen3.5:4b-mtp` | unsloth/Qwen3.5-4B-MTP-GGUF | duo coder/planner default (MTP) | 2.8 GB + 0.7 GB mmproj |
| `qwen3.5:2b-mtp` | unsloth/Qwen3.5-2B-MTP-GGUF | refiner (MTP) | 1.3 GB + 0.7 GB mmproj |
| `qwen3.5:0.8b-ud` | unsloth/Qwen3.5-0.8B-GGUF | subagent-lite, smallest ladder tier | 0.6 GB + 0.2 GB mmproj |
| `lfm3-omni:2b` | LiquidAI/LFM3-Omni-2B-GGUF | subagent / vision worker (needs b12001+) | 1.0 GB + 0.3 GB mmproj |
| `lfm3:1.7b-qad` | LiquidAI/LFM3-1.7B-GGUF | tool-calling subagent (lowest latency tier, needs b12001+) | 1.0 GB |
| `lfm3:1.7b-uncensored-ad` | AtomicChat/LFM3-1.7B-Uncensored-GGUF | uncensored subagent / helper (Q4_K_M or Q6_K fallback, needs b12001+) | 1.1 GB |
| `lfm2.5:24b-a2b` | LiquidAI/LFM2.5-24B-A2B-GGUF | coder / writer (MoE 24B/2B active, + DSpark drafter) | 13.5 GB + 0.6 GB drafter |
| `spark-x2.6:4b` | XHToken/Spark-X2.6-4B-GGUF | fast direct helper (dense 4B) | 3.0 GB |
| `minicpm5:2b-sharp` | peculiar-ragdoll/Sharp-MiniCPM5-2B-GGUF | light all-rounder, recommended tier (Q6_K_XL) | 2.2 GB |
| `minicpm5:2b-sharp-q4` | peculiar-ragdoll/Sharp-MiniCPM5-2B-GGUF | same model, smaller/weaker tier (Q4_K_XL) | 1.6 GB |

Text-only models (`minicpm5`, `lfm2.5`, `spark-x2.6`) download a single GGUF; the
vision models pull their projector automatically, and `gemma-4` additionally gets a
~60 MB MTP drafter from the same repo. `lfm2.5` 24B-A2B ships a DSpark spec-dec
drafter as a sidecar - it is never registered as a model.

GGUFs already on disk can be registered without downloading anything
(`setup_models.bat` -> `[R]`) or given a full config via `[C]`.

Multimodal: gemma-4 has a built-in vision encoder; qwen3.5/3.6/hermes use an
`mmproj` projector (auto-downloaded with the model). Non-multimodal models fall
back to the vision-agent preprocessing path.

Chat templates: a profile's `chat_template` wins; without one, qwen3.5/3.6/
hermes models launch with the bundled `model_configs/chat_template_sharp_v22.5.0.jinja`
(22.5 is the fallback). gemma-4 uses its built-in GGUF template. Per-model
overrides live in `model_configs/models/<tag>.json`.

## UI and control

- Single-page UI, no build step, no framework. Live code display, VRAM
  monitor, agent toggles, mode selection, SSE streaming.
- Ask-User pause + resume with configurable timeout, optional VRAM eviction
  during long pauses, countdown badge. Auto-answer in Until-Finished runs;
  hard-pause when the agent asks too often (>5/10min).
- Graceful stop (finish at chunk boundary, then force) and multi-state pause,
  both persisted for resume.
- Context meter with pressure warnings at 70% and 90%, tooltips on every
  perf field, and a model filter that accepts several search terms.
- Presets: full settings snapshots (all keys, agent cards and per-preset
  prompts) saved under one name and restored in one click; loading merges
  instead of replacing, so keys a preset never knew keep their current
  value. Saved in `presets.json` plus the `custom_prompts\` folder - copy
  both to move presets between installs.
- Windows toast notifications when runs stop or need input; wake-lock during
  runs (`keep_awake_during_run`).

## Voice (optional, beta)

Speech in and out, entirely local and off by default. Nothing is downloaded or
started until you switch it on.

- **Speech to text** (English only): a Telegram voice note or the UI microphone
  becomes text. whisper.cpp/Parakeet on the CPU; you pick the tier - `parakeet-q4k`
  (396 MB, 3.6 % WER) down to `tiny` (74 MB, weak CPUs).
- **Text to speech**: answers spoken aloud with Pocket TTS (`anna` by default,
  speed `1.08x`). The model is gated, so a one-time `hf auth login` is needed; the
  engine runs via `uvx`, so PyTorch never enters HiveMind's environment.
- The installer asks once (speech to text / both / text to speech / skip) and
  downloads what you picked. Skipping needs **no reinstall later**: the Voice card
  or `deploy\setup_voice.bat` adds it whenever you want.

**Audio models** (nothing is bundled - fetched on demand, CPU only, English):

| Model | Size | Role | Notes |
|---|---|---|---|
| `parakeet-q4k` (default) | 396 MB | speech to text | 3.6 % micro-WER - best accuracy per byte |
| `base` | 141 MB | speech to text | 9.9 % - light default for small machines |
| `small` | 465 MB | speech to text | 7.3 % - slower, small gain |
| `tiny` | 74 MB | speech to text | 12.4 % - weak CPUs |
| Pocket TTS | ~200 MB + PyTorch in uv's cache | text to speech | voice `anna` (default), speed `1.08x` |
| whisper.cpp / parakeet.cpp | 21 MB | the STT binaries | `whisper-cli`, `whisper-server`, `parakeet-cli` |

Voices: `anna` (default), `alba`, `cosette`, `eve`, `jane`, `vera`, `bill_boerst`.
`cosette` is the Expresso recording (**CC BY-NC 4.0** - non-commercial only);
the others are CC BY 4.0 or CC0. You can switch voices at any time. `cosette`
is available but no longer the shipped default; `bill_boerst` is CC0. Speech to
text lands in
`<install>/stt/`, TTS in uv's and the Hugging Face cache - never in the repository,
never in a release zip.

**Licences of the audio stack.** HiveMind's own code is under this repo's
licence. Nothing below is vendored: the engines and models are **downloaded at
first use** (never part of a release zip), and they keep their own terms.

| Component | Comes from | Licence | What it obliges |
|---|---|---|---|
| Pocket TTS **model** | `kyutai/pocket-tts` (Hugging Face, gated) | **CC BY 4.0** + gate terms | attribution; the gate forbids voice impersonation/cloning without consent and deceptive use |
| `pocket-tts` **code** | PyPI, installed in an isolated `uvx` env | **MIT** | keep the notice (not bundled) |
| **Voice recordings** | `kyutai/tts-voices` | **no blanket licence in that repo** - each voice keeps its source licence | see below |
| Whisper **models** (`ggml-*.bin`) | `ggerganov/whisper.cpp` release assets | **MIT** | notice |
| whisper.cpp **binaries** (`whisper-cli`, `whisper-server`, `parakeet-cli`) | `ggerganov/whisper.cpp` releases | **MIT** | notice |
| Parakeet **GGUF** | `ggml-org/parakeet-GGUF` | repo says **MIT**, upstream NVIDIA Parakeet-TDT weights are **CC BY 4.0** | treat as **CC BY 4.0** (the stricter one): attribution if redistributed |

**Voices, per recording** (`kyutai/tts-voices` carries no single licence, so each
file is governed by its source dataset):

| Voice | Source dataset | Licence |
|---|---|---|
| `anna`, `eve`, `jane`, `vera` | VCTK (University of Edinburgh) | **CC BY 4.0** |
| `alba` | Alba MacKenna | **CC BY 4.0** |
| `bill_boerst` | Voice-Zero | **CC0** (no obligations) |
| `cosette` | Expresso (Meta) | **CC BY-NC 4.0 - non-commercial only** |

`anna` (the default), `alba`, `eve`, `jane`, `vera` and `bill_boerst` are fine for
commercial use; `cosette` is the non-commercial Expresso recording - usable
privately, not in a paid product. HiveMind does no voice cloning, never sends a
recording as a voice prompt, and discards the user's own audio right after
transcribing it.

Attribution shown in the UI and in `GET /tts/status`:
*"Voice: Kyutai Pocket TTS (model CC BY 4.0, code MIT) - recordings: VCTK / Alba
MacKenna (CC BY 4.0), Voice-Zero (CC0), Expresso (CC BY-NC 4.0, non-commercial)"*.
Full provenance: [voice_licenses.md](voice_licenses.md).

**Gated model - one-time token setup.** The Pocket TTS model lives behind a
Hugging Face gate: accept the terms on
[kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts), create a
**Read** token at <https://huggingface.co/settings/tokens> and either run
`uvx --from huggingface_hub hf auth login` once or set `HF_TOKEN`. Step by step
(Windows, Linux/systemd, background-user pitfalls, 401/403 troubleshooting):
[huggingface_token.md](huggingface_token.md). Speech to text needs no
token - whisper and parakeet are public repos.

## Telegram (optional)

Run HiveMind from your phone. The gateway is a separate process
(`start_gateway.bat` / headless variant), talks to the engine over loopback and
needs your own bot token from [@BotFather](https://t.me/BotFather) — setup:
[gateway_setup.md](gateway_setup.md).

| | |
|---|---|
| Pairing | `/start` with the one-time code from the engine; only the paired owner id is answered, everything else is ignored |
| Mirroring | runs you start in the browser appear on the phone: progress, result, approval cards. Your phone texts go **into** the running run as steering (one mirrored run at a time) |
| Phone runs | full runs with `/mode auto\|agentic\|chat\|pipeline\|automap\|off`, `/models`, `/setModel`, `/ctx`, `/planner`, `/preset`, `/workspace`, `/send`, `/tools`, `/gate`, `/restrict`, `/status`, `/stop`, `/new`, `/follow`, `/cron`, `/purge`, `/shutdown` |
| **Files out** | `/send <path>` delivers a workspace file immediately; in agentic phone runs and direct runs with full tools `send_file` queues a file the run wrote; delivery happens after the run’s final verification/tool loop, as the last action. Workspace-confined, secret names blocked, 50 MB Telegram limit |
| Voice in | a voice note is transcribed on the CPU and runs like typed text — **a spoken note keeps the selected conversational mode** (chat/simple/direct) instead of being rerouted |
| Voice out | answers can come back as a voice note (`/tts` behaviour follows the Voice card: text, voice or both) |
| **Images** | a photo (or an image sent as a file) becomes a run with the **caption as the prompt**; the engine loads the model's vision projector on demand. Largest Telegram size, JPEG/PNG/WebP, 10 MB cap |
| **PDFs / text files** | a PDF, `.txt`, `.md`, `.log` or `.csv` is extracted on the engine (`/documents/extract`, pypdf) and run as text with the caption as the question. 40 pages / 120k characters per document (the answer says when it was cut), 20 MB upload (Telegram's bot download limit); a scanned PDF without a text layer answers "send the pages as photos" |
| Safety | owner-only, private-chat-only, rate-limited, `telegram_phone_restricted` limits phone runs to read tools; approvals can be answered on the phone |

Switches live in the **Telegram** card (mirror, voice in/out, **images in**,
result format, progress lines) and are read by the gateway per message — no
restart needed.

## Learning and memory

- Soul engine *(experimental)*: peer-rating-based personality evolution,
  injects learned traits into system prompts.
- Skill distiller: semantic insight compaction (decay/merge/evict), top
  insights exported to `learning_logs/skills/`.
- Persistent memory: SQLite FTS5 (bm25 + porter stemming + prefix match) with
  a hash-cosine fallback when the sqlite build lacks FTS5 (no model
  download), path-based relevance boosting,
  deduplicated debounced persistence.
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
- MCP in both directions — server on port 8090 (`start_mcp.bat`) for IDEs,
  client for external servers: see [Model Context Protocol](#model-context-protocol-mcp).
- `/automap/*` for routing profiles, `/model_configs/*` for base/learned/
  effective configs and learning logs.
- `/stt`, `/tts`, `/documents/extract` — speech in/out and document text for
  surfaces that have no local filesystem (the Telegram gateway uses them).

## Configuration

All settings live in `settings.json`, generated from the `settings.py`
defaults. The important ones:

Change them in the UI (or `POST /settings`): the running engine holds the
authoritative copy and rewrites the file on the next save, so a hand edit
made while it runs is lost on the next settings write.

A fresh install ships this working default configuration (the installer
wires the agent roles to whatever `models.json` actually contains, see
`deploy/apply_model_defaults.py`):

| Area | Default |
|------|---------|
| Mode | `chat` (direct answers); `code_duo` starts in **agentic** (one model, solo) |
| Planner | on, and it shares the coder context — no second model load |
| Agent roles | `direct`/pipeline → gemma-4:e4b-it-qat, `duo_coder`/`duo_critic`/`duo_planner` → qwen3.5:4b-mtp, `refiner`/`judge` → minicpm5:2b-sharp, subagent ladder lfm3:1.7b-qad → minicpm5:2b-sharp → qwen3.5:4b-mtp |
| Vision | `vision_agent_model` = gemma-4:e4b-it-qat (used when the vision agent is enabled) |
| Context | coder ctx follows the VRAM budget, planner = coder ctx, compression on with automatic floor |
| Unchanged / off | smart preload, pre-explore, chunking, git autocommit/checkpoints, web search, routing decider |

| Setting | Default | Description |
|---------|---------|-------------|
| `vram_budget_gb` | 7.5 | GPU VRAM limit for model loading |
| `duo_worker_slots` | 2 | parallel llama-server slots |
| `duo_tree_scout_enabled` | true | tree-scout codebase analysis |
| `duo_static_map_chars` | 0 | static repo-map budget (0 = auto) |
| `duo_pre_explore` | false | LLM pre-explore before planning |
| `duo_chunking` | false | task decomposition into subtasks (opt-in) |
| `duo_agentic_mode` | true | agentic (single-model) instead of duo |
| `duo_git_autocommit` | false | auto-commit after each chunk |
| `duo_git_checkpoints` | false | git commit at chunk start (writes to your repo - opt-in) |
| `duo_websearch_enabled` | false | web search as real tool calls in coding |
| `searxng_host` | http://localhost:8888 | SearXNG instance URL |
| `duo_runtime_profile` | balanced | fast / balanced / critical |
| `duo_coder_fallback_model` | empty (off) | VRAM fallback for the coder |
| `duo_partial_compression` | false | keep a byte-identical raw tail at compression (KV-cache reuse) |
| `duo_compress_local_only` | false | skip the compression LLM summary, instant local fallback |
| `duo_test_feedback_final` | true | auto-run tests before task_complete |
| `disable_thinking_in_planner` | false | force planner to skip thinking |
| `model_capability_overrides` | {} | per-model capability overrides |
| `ctx_overrides` | empty | per-role / per-model context overrides |
| `duo_tool_sandbox` | true | Windows Job-Object tool sandbox |
| `keep_awake_during_run` | true | system wake-lock during runs |
| `desktop_notifications` | true | Windows toast notifications |

Token budgets (tool schema, tool results, injected hints, read caps) are
derived at run time from the model's context length; the planner output and
thinking budgets are set in the UI's Advanced section. Full list (generated):
`docs/settings.md`, source of truth `settings.py`.

## SearXNG (web search)

```bat
searxng.bat install [port]   # generate secret, build image, start (default 8888)
searxng.bat start|stop|restart|status
searxng.bat external URL     # point at an already-running SearXNG (no Docker)
```

`settings.yml` is baked into the image, no host bind mounts (avoids Docker
Desktop WSL2 path translation errors), container restarts unless stopped.
Without Docker web search stays disabled.

Not reachable? Start Docker Desktop and run `searxng.bat install <port>` again
(force-recreates the container). `invalid mount config for type "bind"` or empty
results usually mean a stale container from an older version:
`docker rm -f hivemind-searxng`, then install again.

### Search engines

Search runs through your local SearXNG, so quality depends on the engines that
instance can actually serve: on stock instances google sits CAPTCHA-suspended and
broad language tags bury good hits under localized shop pages. HiveMind therefore
defaults to `searxng_engines = ""` (use every engine the instance has enabled -
the bundled `searxng-config` enables duckduckgo and mojeek, bing/google/brave
stay off) and `searxng_language = "en"`, safesearch on, and caps results per
hostname. If everything looks like junk, open
`http://localhost:8888/search?q=test` and adjust both keys to the engines that
work for you.

## Scripts

| Script | Purpose |
|--------|---------|
| `start_hivemind.bat` | start the server (port from settings.json, default 8001) |
| `start_llama.bat` | alias for start_hivemind.bat |
| `stop_llama.bat` | stop server + all llama-server.exe (frees VRAM) |
| `update_llama.bat` | update llama.cpp to the latest nightly |
| `update.bat` | update HiveMind itself from the latest GitHub release (shim for `deploy/updater.py`) |
| `setup_models.bat` | download / register / add custom models |
| `start_mcp.bat` | MCP HTTP server for IDEs, port 8090 |
| `searxng.bat` | SearXNG manager |
| `watchdog_engine.bat` | keeps the engine up: restarts it headless when port 8001 dies; `watchdog_engine.bat stop` ends the loop |
| `Ctrl+K` in the web UI | Feature search: every setting, button and tab by name or alias (`ctx`, `tts`, `mlock`, …) |
| `start_gateway.bat` / `start_gateway_headless.bat` | Telegram gateway (mirrors chats to your phone, voice notes and photos in, approvals out) |

## Updating

`update.bat` checks GitHub for the latest release, backs the current code up to
`update_backup_<version>\` and applies the new build over this folder.
The logic lives in `deploy/updater.py`; `update.bat` is a thin shim that
resolves Python and hands over, and the updater swaps in the new shim as its
last step (self-swap) so the running batch file is never overwritten mid-run.
`settings.json`, `models.json`, `.venv\`, `llama\`, `logs\` and `sessions\` are
never touched; new settings keys pick up their defaults on the next start, and
`start_hivemind.bat` installs new Python packages on demand; the update itself syncs from `uv.lock`, so a release installs exactly the versions it was tested with.

Rollback: copy the *contents* of `update_backup_<version>\` back over the folder.
Once it runs, the backup can be deleted. Manual alternative: extract a release
zip NEXT TO (not over) the installation and copy the files across.

At startup `start_hivemind.bat` also runs a lightweight update check. It asks
GitHub for newer releases (including pre-releases when the installed version
is a pre-release), caches the answer for 24 hours, and prints one short
`[Y/n]` notice. Nothing is downloaded or installed without an explicit `Y`;
`HIVEMIND_NO_UPDATE_CHECK=1` disables the check, and
`HIVEMIND_UPDATE_CHECK_PRERELEASES=1` also offers pre-releases on a stable
install. The check is silent when offline. An accepted update downloads the
release ZIP, verifies GitHub's sha256 digest and applies it through
`update.bat --zip`; restart the launcher afterwards.

### Updating llama.cpp

`update_llama.bat`, or manually
`python deploy\fetch_llamacpp.py --backend vulkan --force` (add
`--cuda-version X.Y` for a specific runtime; otherwise detected via
nvidia-smi).

## Deployment

- Linux: `sudo bash deploy/install_linux.sh` (systemd, auto-restart).
- Windows: `deploy\install_windows.bat` as Administrator (NSSM, auto-restart
  after crash/OOM/power loss).
- Health monitoring: /health pings with auto-slot restart, orphan-process
  rehabilitation on startup.

## Testing

The regression suite is deterministic - no LLM, no running server, no models:

```bat
python tests\run_regressions.py
```

It guards tool dispatch, context budgets, sandboxing, planner/coder wiring, the
model registry, workspace guards and release integrity. New test:
`tests\test_<area>.py` (standalone script, exit 0/1), registered in
`tests\run_regressions.py`. Before a release: suite + smoke test (start run.py,
open the UI, check `/websearch/status`).

Note: the test suite is dev-only and not part of the release zip (the published
tree contains only what is needed to run HiveMind).

Branches, the pre-commit hook and the release steps are in
[CONTRIBUTING.md](../CONTRIBUTING.md) - it belongs to the repository, not to the
release zip (the zip is what users download).

## Use cases

Unattended batch runs (Until-Finished with plan tracking, auto-test, graceful
stop, resume), private local coding without API costs (an 8h agent run costs
pennies in electricity instead of API dollars), multi-agent pipelines, and a
fully local alternative to cloud coding tools.

## Known limitations

- Speed depends on model size, not on "local vs cloud": the 1-4B models answer
  faster than a hosted API on any gaming GPU, while the large MoE coders are
  slower than hosted models of the same class. Interactive chat feels the
  difference, long unattended runs do not.
- Model architecture matters more than size on Vulkan. Measured on an
  RX 6600, single live-server requests (not llama-bench):
  Spark-X2.6-4B (hybrid sliding-window attention, no MTP) 5.62 t/s over 64
  tokens -- `llama_server_8102_20261028-155559.log:27`, build b11889;
  Qwen3.5-4B-MTP 55.29 t/s over 600 tokens with draft-max 2 --
  `llama_server_8103_20261020-131700.log:26`, build b11456. Prefer the MTP
  models for coding. (Different builds and prompt depths: the two numbers
  show the order of magnitude, not a controlled A/B.)

- A run keeps its progress: finished chunks are checkpointed, so an engine
  restart resumes at the next chunk. Pause, timeout and throttle state is not
  persisted, and a crash mid-chunk repeats that chunk.
- No container sandbox; `run_bash` runs with filesystem trust (mitigated by the
  Job-Object sandbox and the optional approval gate).
- Retrieval is a static repo-map plus optional LLM pre-explore; there is no
  vector DB (memory uses SQLite FTS5 with a hash-cosine fallback).

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

Web search empty: is SearXNG up? Run `searxng.bat install` once (it seeds
`searxng-config/docker-compose.yml` from the example), then
`docker compose -f searxng-config/docker-compose.yml up -d`, then `curl -m 10
http://localhost:8888/healthz`. `searxng_language` takes one language
(`all` = unrestricted).

PowerShell quirks: 5.1 aliases curl (stripped automatically); native commands
writing to stderr may exit 1 despite success; add `-m 10` to curl health
checks.

Blank frontend: F12 console, make sure you're on http://localhost:8001,
Ctrl+Shift+R, check the server console output.
