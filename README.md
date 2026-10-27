# HiveMind

[![release](https://img.shields.io/github/v/release/BoredLuzo/HiveMind_llama?label=release&color=2ea44f)](https://github.com/BoredLuzo/HiveMind_llama/releases/latest)
[![downloads](https://img.shields.io/github/downloads/BoredLuzo/HiveMind_llama/total?label=downloads)](https://github.com/BoredLuzo/HiveMind_llama/releases)
[![license](https://img.shields.io/badge/license-see%20LICENSE-blue)](LICENSE)
[![python](https://img.shields.io/badge/python-3.14-3776ab)](pyproject.toml)

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

## Linux

```bash
# one-shot installer: uv + Python 3.14, venv, llama.cpp, systemd -> /opt/hivemind
sudo deploy/install_linux.sh                    # HIVEMIND_GPU_BACKEND=vulkan|cpu|rocm

# or manually:
uv python install 3.14
uv sync --all-extras
./.venv/bin/python deploy/fetch_llamacpp.py --backend vulkan   # vulkan | cuda | cpu | rocm
./.venv/bin/python run.py
```

Then open http://localhost:8001 and set a workspace. Models go into the models
folder as GGUFs, or `./.venv/bin/python deploy/fetch_models.py`.

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
cost - tune `llama_ubatch_size` (512/1024) and the thread defaults, and watch the
`[CACHE]` lines in `logs/hivemind.log`.

First-run smoke test: start `run.py`, load a small model (`lfm2.5:2.6b`), send a
short message and check `logs/hivemind.log` for `[PRE-FLIGHT] ... -> OK`. No
binary found? Point `HIVEMIND_LLAMA_BIN` at the extracted llama-server.

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

## Tools

Mode-scoped toolset (`tools/definitions.py`):

| Area | Tools |
|------|-------|
| Explore | `read_file`, `get_signatures`, `find_references`, `list_dir`, `find_files`, `search_code`, `subagent_research` |
| Write | `write_file`, `write_file_append`, `edit_file`, `replace_lines`, `edit_ast`, `undo_last` |
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
  `vram_gb_override` — llama.cpp launch overrides (`--n-cpu-moe`,
  `--spec-type draft-mtp`, `--n-gpu-layers`, projector, VRAM estimate).
- `sampling` — optional per-mode values (`thinking`, `non_thinking`,
  `sampling_thinking_code`, `sampling_thinking_text`, `sampling_text`) that beat
  the family profiles; only entered fields are stored.

The wizard writes every field, so hand-editing is only needed for the ones above.

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
| `hermes3.6:35b-a3b-uncensored-genesis-final-mtp-apex-compact` | LuffyTheFox/Qwen3.6-35B-A3B-Uncensored-Genesis-Hermes-Final-GGUF | coder / hermes agent (MoE) | 17.4 GB + 0.9 GB mmproj |
| `qwen3.5:9b-mtp` | unsloth/Qwen3.5-9B-MTP-GGUF | heavy duo coder (MTP) | 5.9 GB + 0.9 GB mmproj |
| `qwen3.5:4b-mtp` | unsloth/Qwen3.5-4B-MTP-GGUF | duo coder/planner default (MTP) | 2.8 GB + 0.7 GB mmproj |
| `qwen3.5:2b-mtp` | unsloth/Qwen3.5-2B-MTP-GGUF | refiner (MTP) | 1.3 GB + 0.7 GB mmproj |
| `qwen3.5:0.8b-ud` | unsloth/Qwen3.5-0.8B-GGUF | subagent-lite, smallest ladder tier | 0.6 GB + 0.2 GB mmproj |
| `lfm2.5:2.6b` | LiquidAI/LFM2.5-2.6B-GGUF | subagent / judge (+ DSpark drafter) | 1.7 GB + 0.2 GB drafter |
| `lfm2.5:24b-a2b` | LiquidAI/LFM2.5-24B-A2B-GGUF | coder / writer (MoE 24B/2B active, + DSpark drafter) | 13.5 GB + 0.6 GB drafter |
| `spark-x2.6:4b` | XHToken/Spark-X2.6-4B-GGUF | fast judge / direct helper (dense 4B) | 3.0 GB |
| `minicpm5:2b-sharp` | peculiar-ragdoll/Sharp-MiniCPM5-2B-GGUF | light all-rounder, recommended tier (Q6_K_XL) | 2.2 GB |
| `minicpm5:2b-sharp-q4` | peculiar-ragdoll/Sharp-MiniCPM5-2B-GGUF | same model, smaller/weaker tier (Q4_K_XL) | 1.6 GB |

Text-only models (`minicpm5`, `lfm2.5`, `spark-x2.6`) download a single GGUF; the
vision models pull their projector automatically, and `gemma-4` additionally gets a
~60 MB MTP drafter from the same repo. `lfm2.5` (2.6B and 24B-A2B) ships a DSpark
spec-dec drafter as a sidecar - it is never registered as a model.

GGUFs already on disk can be registered without downloading anything
(`setup_models.bat` -> `[R]`) or given a full config via `[C]`.

Multimodal: gemma-4 has a built-in vision encoder; qwen3.5/3.6/hermes use an
`mmproj` projector (auto-downloaded with the model). Non-multimodal models fall
back to the vision-agent preprocessing path.

Chat templates: qwen3.5/3.6/hermes models launch with the bundled
`model_configs/chat_template22.5.jinja`; gemma-4 uses its own built-in template.
Per-model overrides live in `model_configs/models/<tag>.json`.

## UI and control

- Single-page UI, no build step, no framework. Live code display, VRAM
  monitor, agent toggles, mode selection, SSE streaming.
- Ask-User pause + resume with configurable timeout, optional VRAM eviction
  during long pauses, countdown badge. Auto-answer in Until-Finished runs;
  hard-pause when the agent asks too often (>5/10min).
- Graceful stop (finish at chunk boundary, then force) and multi-state pause,
  both persisted for resume.
- Context meter with pressure warnings at 60% and 85%.
- Windows toast notifications when runs stop or need input; wake-lock during
  runs (`keep_awake_during_run`).

## Voice (optional, beta)

Speech in and out, entirely local and off by default. Nothing is downloaded or
started until you switch it on.

- **Speech to text** (English only): a Telegram voice note or the UI microphone
  becomes text. whisper.cpp/Parakeet on the CPU; you pick the tier - `parakeet-q4k`
  (396 MB, 3.6 % WER) down to `tiny` (74 MB, weak CPUs).
- **Text to speech**: answers spoken aloud with Pocket TTS (`cosette` by default,
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
| Pocket TTS | ~200 MB + PyTorch in uv's cache | text to speech | voice `cosette`, speed `1.08x` |
| whisper.cpp / parakeet.cpp | 21 MB | the STT binaries | `whisper-cli`, `whisper-server`, `parakeet-cli` |

Voices: `anna` (default), `alba`, `cosette`, `eve`, `jane`, `vera`, `bill_boerst`.
`cosette` is a **non-commercial** recording (Expresso, CC BY-NC 4.0) - available,
but no longer the shipped default; `bill_boerst` is CC0. Speech to text lands in
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
Full provenance: [docs/voice_licenses.md](docs/voice_licenses.md).

**Gated model - one-time token setup.** The Pocket TTS model lives behind a
Hugging Face gate: accept the terms on
[kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts), create a
**Read** token at <https://huggingface.co/settings/tokens> and either run
`uvx --from huggingface_hub hf auth login` once or set `HF_TOKEN`. Step by step
(Windows, Linux/systemd, background-user pitfalls, 401/403 troubleshooting):
[docs/huggingface_token.md](docs/huggingface_token.md). Speech to text needs no
token - whisper and parakeet are public repos.

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
- MCP client (beta): external MCP servers as tools (`settings: mcp_client_enabled`,
  off by default) - see [docs/mcp_client.md](docs/mcp_client.md).
- MCP server (Model Context Protocol, `infra/mcp_server.py`), stdio + HTTP on - see [docs/mcp.md](docs/mcp.md) for the tiers, limits and the editor-context endpoint.
  port 8090 (`start_mcp.bat`), tiered read/write/exec permissions.
- `/automap/*` for routing profiles, `/model_configs/*` for base/learned/
  effective configs and learning logs.

## Requirements

- Windows 10/11 or Linux
- Python 3.14 (auto-installed by the setup; older interpreters are refused by the dependency metadata in `pyproject.toml`)
- uv (auto-installed)
- 8 GB+ VRAM GPU recommended (AMD/Intel via Vulkan, NVIDIA via CUDA). Smart Preload (model prefetch) is off by default - turn it on when the card has headroom
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

Not reachable? Start Docker Desktop and run `searxng.bat install <port>` again
(force-recreates the container). `invalid mount config for type "bind"` or empty
results usually mean a stale container from an older version:
`docker rm -f hivemind-searxng`, then install again.

### Search engines

Search runs through your local SearXNG, so quality depends on the engines that
instance can actually serve: on stock instances google sits CAPTCHA-suspended and
wikipedia answers little under broad language tags, so HiveMind defaults to
`searxng_engines = "bing,duckduckgo"`, `searxng_language = "all"`, safesearch on,
and caps results per hostname. If everything looks like junk, open
`http://localhost:8888/search?q=test` and adjust both keys to the engines that
work for you.

### Scripts

| Script | Purpose |
|--------|---------|
| `start_hivemind.bat` | start the server (port from settings.json, default 8001) |
| `start_llama.bat` | alias for start_hivemind.bat |
| `stop_llama.bat` | stop server + all llama-server.exe (frees VRAM) |
| `update_llama.bat` | update llama.cpp to the latest nightly |
| `update.bat` | update HiveMind itself from the latest GitHub release |
| `setup_models.bat` | download / register / add custom models |
| `start_mcp.bat` | MCP HTTP server for IDEs, port 8090 |
| `searxng.bat` | SearXNG manager |

### Updating

`update.bat` checks GitHub for the latest release, backs the current code up to
`update_backup_<version>\` and applies the new build over this folder.
`settings.json`, `models.json`, `.venv\`, `llama\`, `logs\` and `sessions\` are
never touched; new settings keys pick up their defaults on the next start, and
`start_hivemind.bat` installs new Python packages on demand; the update itself syncs from `uv.lock`, so a release installs exactly the versions it was tested with.

Rollback: copy the *contents* of `update_backup_<version>\` back over the folder.
Once it runs, the backup can be deleted. Manual alternative: extract a release
zip NEXT TO (not over) the installation and copy the files across.

## Configuration

All settings live in `settings.json`, generated from the `settings.py`
defaults. The important ones:

Change them in the UI (or `POST /settings`): the running engine holds the
authoritative copy and rewrites the file on the next save, so a hand edit
made while it runs is lost on the next settings write.

| Setting | Default | Description |
|---------|---------|-------------|
| `vram_budget_gb` | 7.5 | GPU VRAM limit for model loading |
| `duo_worker_slots` | 2 | parallel llama-server slots |
| `duo_tree_scout_enabled` | true | tree-scout codebase analysis |
| `duo_static_map_chars` | 0 | static repo-map budget (0 = auto) |
| `duo_pre_explore` | false | LLM pre-explore before planning |
| `duo_chunking` | false | task decomposition into subtasks (opt-in) |
| `duo_agentic_mode` | false | agentic (single-model) instead of duo |
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

## Contributing

Branches, the pre-commit hook and the release steps are in
[CONTRIBUTING.md](CONTRIBUTING.md) - it belongs to the repository, not to the
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
- A run keeps its progress: finished chunks are checkpointed, so an engine
  restart resumes at the next chunk. Pause, timeout and throttle state is not
  persisted, and a crash mid-chunk repeats that chunk.
- No container sandbox; `run_bash` runs with filesystem trust (mitigated by the
  Job-Object sandbox and the optional approval gate).
- Retrieval is a static repo-map plus optional LLM pre-explore; there is no
  vector DB (memory uses small hash embeddings).

## License

Business Source License 1.1 (see `LICENSE`). Personal, non-commercial use is
free; commercial use needs a license from the author. After the Change Date
(2030-09-01) HiveMind becomes MIT licensed.

© 2026 Luzo (BoredLuzo)
