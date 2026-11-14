#!/usr/bin/env bash
# Rented-GPU bootstrap for the HiveMind benchmark (NOT tested on a real host yet:
# run `smoke` first and read the output before paying for `full`).
#
# One machine, one model (Qwen3.6-35B-A3B UD-Q4_K_XL), one llama-server:
#   hivemind_duo -> aider  (same task list, same hidden-test check)
# HiveMind starts and owns the llama-server; aider reuses it (runner.py
# auto-detects it on :8080/:8101/:8102), so the model is the same in every row.
# `raw` remains an explicit diagnostic (`HM_CONDITIONS=hivemind_duo,raw,aider`);
# it is not part of the default run because it only guesses a diff.
#
# Usage (Ubuntu + NVIDIA driver, >= 40 GB VRAM, >= 100 GB disk, root or sudo):
#   bash rent_bootstrap.sh setup     # deps, repo, python, llama.cpp, model, aider
#   bash rent_bootstrap.sh serve     # start the HiveMind engine (background)
#   bash rent_bootstrap.sh smoke     # 3 tasks x 2 conditions - check cost/time!
#   bash rent_bootstrap.sh full      # detached tmux run, results in bench/results/
#   bash rent_bootstrap.sh status    # tail the run
#   bash rent_bootstrap.sh stop
#
# Env (all optional):
#   HM_REPO     default https://github.com/BoredLuzo/HiveMind_llama.git
#   HM_BRANCH   default dev  (must contain bench/, see BENCH_TGZ below)
#   HM_DIR      default $HOME/HiveMind_llama
#   BENCH_TGZ   tarball with the bench/ folder (default $HOME/bench_bundle.tgz);
#               extracted over the clone when present. Needed while bench/ is
#               not pushed to GitHub:  tar czf bench_bundle.tgz bench
#   HM_MODEL    default qwen3.6:35b-a3b-ud (key from deploy/fetch_models.py)
#   HM_RUNS     default 1       HM_CONDITIONS default hivemind_duo,aider
#   HM_TASKS    default bench/tasks (use bench/tasks_polyglot after `polyglot`)
set -euo pipefail

HM_REPO="${HM_REPO:-https://github.com/BoredLuzo/HiveMind_llama.git}"
HM_BRANCH="${HM_BRANCH:-bench-stable}"
HM_DIR="${HM_DIR:-$HOME/HiveMind_llama}"
BENCH_TGZ="${BENCH_TGZ:-$HOME/bench_bundle.tgz}"
PORT="${HIVEMIND_PORT:-8001}"
MODELS_DIR="${HIVEMIND_MODELS_DIR:-$HM_DIR/models}"
HM_RUNS="${HM_RUNS:-1}"
HM_CONDITIONS="${HM_CONDITIONS:-hivemind_duo,aider}"
HM_TASKS="${HM_TASKS:-bench/tasks}"
AIDER_VENV="${AIDER_VENV:-$HOME/aider-venv}"
HERMES_VENV="${HERMES_VENV:-$HOME/hermes-venv}"
HM_LLAMA_URL="${HM_LLAMA_URL:-}"
HM_LLAMA_CUDA="${HM_LLAMA_CUDA:-12.8}"
LOG="$HM_DIR/bench_engine.log"
export PATH="$HOME/.local/node/bin:$HOME/.pi/agent/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

find_pi_bin() {
  for _p in \
    "${HIVEMIND_PI_BIN:-}" \
    "$HOME/.pi/agent/bin/pi" \
    "$HOME/.local/node/bin/pi" \
    "$HOME/.local/bin/pi" \
    "/usr/local/bin/pi" \
    "$(command -v pi 2>/dev/null || true)"; do
    if [ -n "$_p" ] && [ -x "$_p" ] && "$_p" --version >/dev/null 2>&1; then
      echo "$_p"
      return 0
    fi
  done
  return 1
}

# Resolve active model: env > .active_model > running engine > GPU VRAM auto-detection
if [ -z "${HM_MODEL:-}" ] && [ -f "$HM_DIR/.active_model" ]; then
  _saved_m="$(cat "$HM_DIR/.active_model" 2>/dev/null | tr -d '[:space:]' || true)"
  [ -n "$_saved_m" ] && HM_MODEL="$_saved_m"
fi
if [ -z "${HM_MODEL:-}" ]; then
  # Check if engine is running and has a model loaded
  _live_m=$(curl --max-time 2 -fsS "http://127.0.0.1:$PORT/vram/ps" 2>/dev/null | grep -o '"name":"[^"]*"' | head -1 | cut -d'"' -f4 || true)
  [ -n "$_live_m" ] && HM_MODEL="$_live_m"
fi
if [ -z "${HM_MODEL:-}" ]; then
  _vram_mb=0
  if command -v nvidia-smi >/dev/null 2>&1; then
    _vram_mb=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | cut -d. -f1 || echo 0)
  fi
  if [ "${_vram_mb:-0}" -gt 0 ] && [ "$_vram_mb" -lt 20000 ]; then
    # Smaller GPUs (e.g. Tesla T4 16GB) default to 4B MTP
    HM_MODEL="qwen3.5:4b-mtp"
  else
    # Larger GPUs (L4 24GB, A100 40/80GB) default to 35B MoE
    HM_MODEL="qwen3.6:35b-a3b-ud"
  fi
fi

if [ -z "${HM_CTX:-}" ] && [ -f "$HM_DIR/.active_ctx" ]; then
  _saved_ctx="$(cat "$HM_DIR/.active_ctx" 2>/dev/null | tr -d '[:space:]' || true)"
  [ -n "$_saved_ctx" ] && HM_CTX="$_saved_ctx"
fi
if [ -z "${HM_CTX:-}" ]; then
  HM_CTX="102400"
fi

log() { printf '\n[bench] %s\n' "$*"; }
die() { printf '\n[bench][ERROR] %s\n' "$*" >&2; exit 1; }
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
PY="$HM_DIR/.venv/bin/python"

setup() {
  command -v nvidia-smi >/dev/null || die "no NVIDIA driver (nvidia-smi missing) - wrong instance?"
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
  export DEBIAN_FRONTEND=noninteractive

  # Clean any stale apt processes or locks from previously aborted runs
  pkill -9 -f "apt-get|dpkg" 2>/dev/null || true
  rm -f /var/lib/apt/lists/lock /var/cache/apt/archives/lock /var/lib/dpkg/lock* 2>/dev/null || true

  # In Colab/cloud images git, curl, build-essential are already installed.
  # Only install missing utilities (tmux, rsync) without hanging on dead apt mirrors.
  if ! command -v tmux >/dev/null || ! command -v rsync >/dev/null; then
    log "[Step 0/5] Installing tmux / rsync..."
    $SUDO apt-get install -y -qq --no-install-recommends tmux rsync 2>/dev/null \
      || ($SUDO apt-get update -qq 2>/dev/null && $SUDO apt-get install -y -qq --no-install-recommends tmux rsync 2>/dev/null) \
      || true
    command -v tmux >/dev/null || log "WARN: tmux missing — use 'fg' instead of 'full'"
  fi

  log "[Step 1/5] Installing uv package manager..."
  if ! command -v uv >/dev/null; then
    python3 -m pip install uv 2>/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null || true
  fi
  export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  command -v uv >/dev/null || die "failed to install uv"
  uv --version

  log "[Step 2/5] Checking repository ($HM_DIR)..."
  [ -d "$HM_DIR/.git" ] || git clone --branch "$HM_BRANCH" "$HM_REPO" "$HM_DIR"
  # Only overlay a local bundle when explicitly requested. An old
  # $HOME/bench_bundle.tgz otherwise silently overrides the fresh clone
  # (bench/ is tracked now); use BENCH_TGZ_OVERLAY=1 for the old workflow.
  if [ -f "$BENCH_TGZ" ] && [ "${BENCH_TGZ_OVERLAY:-0}" = "1" ]; then
    log "overlay bench/ from $BENCH_TGZ (BENCH_TGZ_OVERLAY=1)"
    tar xzf "$BENCH_TGZ" -C "$HM_DIR"
  fi
  [ -f "$HM_DIR/bench/runner.py" ] || die "bench/runner.py missing: push it or provide BENCH_TGZ"
  git -C "$HM_DIR" log -1 --format='[bench] engine commit: %h %s'

  log "[Step 3/5] Setting up Python 3.14 & locked dependencies via uv..."
  ( cd "$HM_DIR" && uv python install 3.14 && uv sync --all-extras --frozen )
  log "Python 3.14 environment and all dependencies installed successfully."

  log "[Step 4/5] Downloading llama.cpp with CUDA $HM_LLAMA_CUDA support..."
  ( cd "$HM_DIR" && "$PY" deploy/fetch_llamacpp.py --backend cuda --cuda-version "$HM_LLAMA_CUDA" )
  # Linux CUDA builds link the CUDA runtime dynamically; the release ships it
  # in a separate cudart archive next to the main build.
  _exe="$(find "$HM_DIR/llama" -name llama-server -type f | head -1)"
  _build="$(printf '%s\n' "$_exe" | sed -n 's/.*llama-b\([0-9][0-9]*\).*/\1/p')"
  if [ -n "$_build" ]; then
    _cudart_url="https://github.com/ggml-org/llama.cpp/releases/download/b${_build}/cudart-llama-b${_build}-bin-ubuntu-cuda-${HM_LLAMA_CUDA}-x64.tar.gz"
    if ! find "$HM_DIR/llama" -name 'libcudart.so*' | grep -q .; then
      log "CUDA runtime split: downloading cudart for b${_build}..."
      curl -L --progress-bar --fail --retry 3 -o /tmp/cudart.tar.gz "$_cudart_url" || log "WARN: cudart download failed"
      [ -s /tmp/cudart.tar.gz ] && tar xzf /tmp/cudart.tar.gz -C "$HM_DIR/llama" || true
    fi
  fi

  if [ "${HM_MODEL:-}" != "none" ] && [ "${HM_MODEL:-}" != "skip" ]; then
    log "model $HM_MODEL -> $MODELS_DIR (~20 GB)"
    mkdir -p "$MODELS_DIR"
    ( cd "$HM_DIR" && "$PY" deploy/fetch_models.py --models-dir "$MODELS_DIR" \
        --only-keys "$HM_MODEL" --yes )
    ls -lh "$MODELS_DIR" | head
  else
    log "Model download skipped in setup (select download cell 3a or 3b)."
  fi

  log "[Step 5/5] Installing external harnesses: Aider, Hermes & Pi..."
  [ -x "$AIDER_VENV/bin/aider" ] || {
    log "Installing Aider CLI into $AIDER_VENV..."
    uv venv --python 3.12 "$AIDER_VENV"
    uv pip install --python "$AIDER_VENV/bin/python" aider-chat
  }
  "$AIDER_VENV/bin/aider" --version

  if [ ! -x "$HERMES_VENV/bin/hermes" ] && ! command -v hermes >/dev/null; then
    log "Installing Hermes-agent CLI..."
    uv venv --python 3.12 "$HERMES_VENV" 2>/dev/null || true
    uv pip install --python "$HERMES_VENV/bin/python" hermes-agent 2>/dev/null \
      || curl -fsSL https://hermes-agent.nousresearch.com/install.sh 2>/dev/null | bash 2>/dev/null || log "WARN: hermes-agent install skipped"
  fi
  if [ -x "$HERMES_VENV/bin/hermes" ]; then
    "$HERMES_VENV/bin/hermes" --version 2>/dev/null || true
  elif command -v hermes >/dev/null 2>&1; then
    hermes --version 2>/dev/null || true
  fi

  if ! find_pi_bin >/dev/null 2>&1; then
    log "Installing Pi coding agent CLI..."
    curl -fsSL https://pi.dev/install.sh | sh 2>&1 || true
  fi
  if ! find_pi_bin >/dev/null 2>&1; then
    _node_major=0
    if command -v node >/dev/null 2>&1; then
      _node_major="$(node -v 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/' || echo 0)"
    fi
    if [ "${_node_major:-0}" -lt 22 ]; then
      log "Installing Node.js 22 LTS into $HOME/.local/node for Pi coding agent..."
      mkdir -p "$HOME/.local/node"
      _node_tar="$(curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt | awk '/linux-x64\.tar\.xz$/ {print $2; exit}' || true)"
      if [ -n "$_node_tar" ]; then
        curl -fsSL "https://nodejs.org/dist/latest-v22.x/$_node_tar" | tar -xJ --strip-components=1 -C "$HOME/.local/node" || true
        export PATH="$HOME/.local/node/bin:$PATH"
        ln -sf "$HOME/.local/node/bin/node" /usr/local/bin/node 2>/dev/null || $SUDO ln -sf "$HOME/.local/node/bin/node" /usr/local/bin/node 2>/dev/null || true
        ln -sf "$HOME/.local/node/bin/npm" /usr/local/bin/npm 2>/dev/null || $SUDO ln -sf "$HOME/.local/node/bin/npm" /usr/local/bin/npm 2>/dev/null || true
      fi
    fi
    if command -v npm >/dev/null 2>&1; then
      npm install -g --ignore-scripts @earendil-works/pi-coding-agent 2>&1 || log "WARN: pi npm install failed"
    fi
  fi
  _pi_found="$(find_pi_bin 2>/dev/null || true)"
  if [ -n "$_pi_found" ]; then
    ln -sf "$_pi_found" /usr/local/bin/pi 2>/dev/null || $SUDO ln -sf "$_pi_found" /usr/local/bin/pi 2>/dev/null || true
    log "Pi installed: $_pi_found ($("$_pi_found" --version 2>/dev/null || echo ok))"
  else
    log "WARN: pi install skipped/failed (Node >= 22 required)"
  fi

  log "ALL SETUP STEPS FINISHED SUCCESSFULLY! Next: choose model (Zelle 4a oder 4b) and serve."
}

detect_llama_url() {
  local _base_port=$(( PORT + 100 ))
  for _p in "$_base_port" $(( _base_port + 1 )) 8101 8102 8103 8080; do
    if curl --max-time 2 -fsS "http://127.0.0.1:$_p/health" >/dev/null 2>&1; then
      HM_LLAMA_URL="http://127.0.0.1:$_p"; return
    fi
  done
  HM_LLAMA_URL="${HM_LLAMA_URL:-http://127.0.0.1:$_base_port}"
}

serve() {
  [ -x "$PY" ] || die "run setup first"
  mkdir -p "$MODELS_DIR"

  # Check if engine AND llama-server are already up with this exact model and context
  local _prev_ctx=""
  _prev_ctx="$(cat "$HM_DIR/.active_ctx" 2>/dev/null | tr -d '[:space:]' || true)"

  # Persist active model & ctx immediately so any subsequent script/smoke call knows what was selected
  echo "$HM_MODEL" > "$HM_DIR/.active_model" 2>/dev/null || true
  echo "$HM_CTX" > "$HM_DIR/.active_ctx" 2>/dev/null || true

  detect_llama_url
  local _already_loaded=0
  if curl --max-time 2 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && \
     [ -n "$HM_LLAMA_URL" ] && curl --max-time 2 -fsS "$HM_LLAMA_URL/health" >/dev/null 2>&1; then
    local _ps_out
    _ps_out=$(curl --max-time 2 -fsS "http://127.0.0.1:$PORT/vram/ps" 2>/dev/null || true)
    if echo "$_ps_out" | grep -q "\"name\":\"$HM_MODEL\"" && [ "$_prev_ctx" = "$HM_CTX" ]; then
      _already_loaded=1
    fi
  fi

  if [ "$_already_loaded" -eq 1 ]; then
    log "Model '$HM_MODEL' (ctx $HM_CTX) is ALREADY loaded in VRAM and healthy ($HM_LLAMA_URL) — skipping reload."
    return 0
  fi

  # 1. Model existence check: auto-download if not present locally
  local _model_found=0
  ( cd "$HM_DIR" && HIVEMIND_MODELS_DIR="$MODELS_DIR" "$PY" - <<PY
import sys, os
from backend.llama_models import resolve_model_path
try:
    p = resolve_model_path('$HM_MODEL')
    if p and os.path.isfile(p):
        sys.exit(0)
    sys.exit(1)
except Exception:
    sys.exit(1)
PY
  ) >/dev/null 2>&1 && _model_found=1 || _model_found=0

  if [ "$_model_found" -ne 1 ]; then
    log "Model '$HM_MODEL' not found in $MODELS_DIR — auto-downloading now..."
    download "" "$HM_MODEL"
  else
    log "Model '$HM_MODEL' verified present in $MODELS_DIR"
  fi

  # Dynamic GPU/RAM profile: sizes MoE CPU offload for 35B from real GPU VRAM & HM_CTX.
  # A100 (>=35GB): 0 CPU experts | L4 (24GB): 4-7 | T4 (16GB): 21-25 (fits VRAM safely).
  ( cd "$HM_DIR" && HM_CTX="$HM_CTX" HM_MODEL="$HM_MODEL" "$PY" - <<'PY'
import json, math, os, subprocess
from pathlib import Path
p = Path("settings.json")
d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}

budget_gb = 76.0
try:
    r = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True, check=True)
    total_mib = float(r.stdout.strip().splitlines()[0])
    budget_gb = round(total_mib * 0.92 / 1024, 1)
except Exception:
    pass

env_budget = os.environ.get("HM_VRAM_BUDGET")
if env_budget:
    try:
        budget_gb = float(env_budget)
    except ValueError:
        pass

ram_gb = 0.0
try:
    for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
        if line.startswith("MemTotal:"):
            ram_gb = round(int(line.split()[1]) / (1024 * 1024), 1)
            break
except Exception:
    pass

try:
    ctx_tokens = int(os.environ.get("HM_CTX") or 32768)
except ValueError:
    ctx_tokens = 32768

if budget_gb >= 35.0:
    cpu_experts = 0
else:
    # qwen3.6:35b-a3b-ud: ~20.2 GB base weights @0 offload (~19.3 GB @2),
    # 4.66 GB @35 (~0.44 GB freed per MoE layer) + KV/compute reserve.
    reserve_gb = (2.4 if budget_gb < 20.0 else 2.0) + (ctx_tokens / 131072.0) * 1.5
    target_model_gb = max(4.66, budget_gb - reserve_gb)
    cpu_experts = min(35, max(2, math.ceil((20.2 - target_model_gb) / 0.44)))

ubatch = 512 if budget_gb < 20.0 else 1024
gpu_patch = {
    "moe_cpu_experts": {
        "qwen3.6:35b-a3b": cpu_experts,
        "qwen3.6:35b-a3b-ud": cpu_experts,
        "hermes3.6:35b-a3b": cpu_experts,
        "hermes3.6:35b-a3b-uncensored-genesis-apex-mini": cpu_experts,
    },
    "vram_budget_gb": budget_gb,
    "llama_mlock": False,
    "llama_fit": False,
    "llama_gpu_layers_auto": False,
    "vram_preflight_gate": False,
    "startup_preload_enabled": False,
    "duo_ubatch": ubatch,
    "llama_ubatch_size": ubatch,
}
d.update(gpu_patch)
p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
Path("bench_gpu_settings.json").write_text(json.dumps(gpu_patch) + "\n", encoding="utf-8")
print(f"[bench] GPU budget: {budget_gb} GB VRAM, RAM: {ram_gb} GB, MoE CPU experts: {cpu_experts}, ctx: {ctx_tokens}")
if "35b" in os.environ.get("HM_MODEL", "").lower() and cpu_experts >= 15 and 0 < ram_gb < 22.0:
    print(f"[bench] WARN: low System RAM ({ram_gb} GB) with {cpu_experts} CPU MoE experts; using mmap (slower decode on free T4). L4/A100 or High-RAM recommended for 35B.")
PY
  )
  if curl --max-time 3 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    log "engine already up on :$PORT — clearing previous VRAM slots before reload"
    curl --max-time 10 -fsS -X POST "http://127.0.0.1:$PORT/vram/kill_all" >/dev/null 2>&1 || true
  else
    log "starting HiveMind engine on 127.0.0.1:$PORT (log: $LOG)"
    _cudart_lib="$(find "$HM_DIR/llama" -name 'libcudart.so*' -print -quit 2>/dev/null || true)"
    [ -n "$_cudart_lib" ] && export LD_LIBRARY_PATH="$(dirname "$_cudart_lib"):${LD_LIBRARY_PATH:-}"
    ( cd "$HM_DIR" && \
      HIVEMIND_HOST=127.0.0.1 HIVEMIND_PORT="$PORT" HIVEMIND_GPU_BACKEND=cuda \
      HIVEMIND_MODELS_DIR="$MODELS_DIR" PYTHONUNBUFFERED=1 \
      setsid "$PY" run.py >"$LOG" 2>&1 < /dev/null & \
      echo $! >"$HM_DIR/bench_engine.pid" )
    printf '[bench] waiting for engine startup'
    for _ in $(seq 1 90); do
      if curl --max-time 3 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        printf ' OK\n'
        log "engine healthy on port $PORT"
        break
      fi
      printf '.'
      sleep 2
    done
    curl --max-time 3 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || { printf ' FAILED\n'; tail -40 "$LOG" >&2 || true; die "engine did not become healthy"; }
  fi
  ( cd "$HM_DIR" && curl --max-time 10 -fsS -X POST "http://127.0.0.1:$PORT/settings" \
      -H "Content-Type: application/json" -d @bench_gpu_settings.json >/dev/null 2>&1 || true )
  log "preloading $HM_MODEL into VRAM (ctx $HM_CTX)..."
  local _tmp_resp
  _tmp_resp=$(mktemp)
  curl --max-time 600 -s -w "\n%{http_code}" -X POST "http://127.0.0.1:$PORT/vram/load" \
      -H "Content-Type: application/json" \
      -d "{\"model\":\"$HM_MODEL\",\"num_ctx\":$HM_CTX,\"keep_alive\":\"-1\"}" > "$_tmp_resp" 2>&1 &
  local _curl_pid=$!

  printf '[bench] allocating KV cache & loading CUDA weights'
  while kill -0 "$_curl_pid" 2>/dev/null; do
    printf '.'
    sleep 2
    if ! curl --max-time 2 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      printf ' FAILED\n'
      log "ERROR: HiveMind engine died during model load!"
      kill "$_curl_pid" 2>/dev/null || true
      tail -40 "$LOG" >&2 || true
      rm -f "$_tmp_resp"
      die "HiveMind engine process terminated unexpectedly"
    fi
  done
  wait "$_curl_pid" 2>/dev/null || true

  local _vram_resp
  local _vram_code
  local _vram_body
  _vram_resp=$(cat "$_tmp_resp" 2>/dev/null || echo "")
  rm -f "$_tmp_resp"
  _vram_code=$(echo "$_vram_resp" | tail -n1)
  _vram_body=$(echo "$_vram_resp" | sed '$d')

  if [ "$_vram_code" != "200" ]; then
    printf ' FAILED\n'
    log "ERROR: /vram/load failed with HTTP $_vram_code:"
    echo "$_vram_body" >&2
    log "Recent engine logs ($LOG):"
    tail -30 "$LOG" >&2 || true
    die "Engine could not load $HM_MODEL into VRAM"
  fi
  printf ' OK\n'

  detect_llama_url
  if ! curl --max-time 3 -fsS "$HM_LLAMA_URL/health" >/dev/null 2>&1; then
    printf '[bench] waiting for llama-server endpoint'
    for _ in $(seq 1 30); do
      curl --max-time 2 -fsS "$HM_LLAMA_URL/health" >/dev/null 2>&1 && { printf ' OK\n'; break; }
      printf '.'
      sleep 1
    done
  fi
  log "benchmark llama endpoint: $HM_LLAMA_URL"
  echo "$HM_MODEL" > "$HM_DIR/.active_model" 2>/dev/null || true
  echo "$HM_CTX" > "$HM_DIR/.active_ctx" 2>/dev/null || true
  log "Engine and model ($HM_MODEL @ ctx $HM_CTX) fully ready for benchmarks!"
}

run_bench() {  # $@ = extra args (optional)
  local -a _extra_args=()
  if [ -z "${HM_SYNC_DIR:-}" ] && [ -d "/content/drive/MyDrive" ]; then
    HM_SYNC_DIR="/content/drive/MyDrive/hivemind_bench/results"
  fi
  [ -n "${HM_SYNC_DIR:-}" ] && mkdir -p "$HM_SYNC_DIR" || true
  if [ "${HM_CTX:-0}" -lt 65536 ] && printf ',%s,' "$HM_CONDITIONS" | grep -q ',hermes,'; then
    log "[warn] Hermes erfordert >=64k Kontext (aktiv: ${HM_CTX}) -> uebersprungen"
    HM_CONDITIONS="$(printf '%s' "$HM_CONDITIONS" | tr ',' '\n' | grep -vx 'hermes' | paste -sd, -)"
  fi
  if [ -z "${HM_OUT:-}" ] && ! printf ' %s ' "$*" | grep -q ' --only '; then
    local _suite="custom" _mtag="custom" _k="$(( ${HM_CTX:-65536} / 1024 ))k"
    case "$HM_TASKS" in
      *tasks_polyglot*) _suite="polyglot" ;;
      *tasks_deepswe*) _suite="deepswe" ;;
      bench/tasks|*/bench/tasks) _suite="core" ;;
      *) _suite="$(basename "$HM_TASKS")" ;;
    esac
    case "$HM_MODEL" in
      *4b*|*4B*) _mtag="4b" ;;
      *35b*|*35B*) _mtag="35b" ;;
      *) _mtag="$(printf '%s' "$HM_MODEL" | tr ':/ ' '___')" ;;
    esac
    HM_OUT="$HM_DIR/bench/results/${_suite}_${_mtag}_${_k}.json"
    HM_RESUME="${HM_RESUME:-1}"
  fi
  if echo "$HM_CONDITIONS" | grep -q "aider"; then
    if [ -x "$AIDER_VENV/bin/aider" ]; then
      _extra_args+=(--aider-bin "$AIDER_VENV/bin/aider")
    elif command -v aider >/dev/null; then
      _extra_args+=(--aider-bin "$(command -v aider)")
    else
      die "aider missing (run setup or omit aider from HM_CONDITIONS)"
    fi
  elif [ -x "$AIDER_VENV/bin/aider" ]; then
    _extra_args+=(--aider-bin "$AIDER_VENV/bin/aider")
  fi
  [ -n "$HM_LLAMA_URL" ] || detect_llama_url
  if [ -x "$HERMES_VENV/bin/hermes" ]; then
    _extra_args+=(--hermes-bin "$HERMES_VENV/bin/hermes")
  elif command -v hermes >/dev/null; then
    _extra_args+=(--hermes-bin "$(command -v hermes)")
  fi
  _pi_exe="$(find_pi_bin 2>/dev/null || true)"
  if [ -n "$_pi_exe" ]; then
    _extra_args+=(--pi-bin "$_pi_exe")
  elif command -v pi >/dev/null; then
    _extra_args+=(--pi-bin "$(command -v pi)")
  fi
  [ -n "${HM_TASK_TIMEOUT:-}" ] && _extra_args+=(--task-timeout "$HM_TASK_TIMEOUT")
  [ -n "${HM_ONLY:-}" ] && _extra_args+=(--only "$HM_ONLY")
  [ -n "${HM_OUT:-}" ] && _extra_args+=(--out "$HM_OUT")
  [ -n "${HM_SYNC_DIR:-}" ] && _extra_args+=(--sync-dir "$HM_SYNC_DIR")
  [ "${HM_RESUME:-0}" = "1" ] && _extra_args+=(--resume)
  [ "${HM_RESUME_FAILED:-0}" = "1" ] && _extra_args+=(--resume-failed)
  log "run_bench: model=$HM_MODEL ctx=$HM_CTX tasks=$HM_TASKS runs=$HM_RUNS conds=$HM_CONDITIONS out=${HM_OUT:-auto} sync=${HM_SYNC_DIR:-off} resume=${HM_RESUME:-0}"
  local _rc=0
  # Cap per-cell time so one stuck agent cannot burn the rental budget.
  ( cd "$HM_DIR" && "$PY" -u bench/runner.py \
      --tasks "$HM_TASKS" \
      --model "$HM_MODEL" \
      --conditions "$HM_CONDITIONS" \
      --runs "$HM_RUNS" \
      --num-ctx "$HM_CTX" \
      --run-offset "${HM_RUN_OFFSET:-0}" \
      --cell-timeout "${HM_CELL_TIMEOUT:-900}" \
      --hivemind "http://127.0.0.1:$PORT" \
      --llama "$HM_LLAMA_URL" \
      "${_extra_args[@]}" \
      "$@" ) || _rc=$?
  if [ -d "/content/drive/MyDrive" ]; then
    local _dlogs="/content/drive/MyDrive/hivemind_bench/logs"
    mkdir -p "$_dlogs" 2>/dev/null || true
    cp -f "$HM_DIR/bench_full.log" "$HM_DIR/bench_engine.log" "$_dlogs/" 2>/dev/null || true
  fi
  if [ -n "${HM_OUT:-}" ] && [ -f "$HM_OUT" ]; then
    "$PY" -c 'import json,pathlib,sys; p=pathlib.Path(sys.argv[1]); r=json.loads(p.read_text()).get("results",[]); ok=sum(1 for x in r if x.get("ok")); print(f"[bench] Saved {p.name}: {len(r)} rows ({ok} PASS) | {p.stat().st_size/1024:.1f} KB")' "$HM_OUT" 2>/dev/null || true
  fi
  return "$_rc"
}

smoke() {
  serve
  log "smoke: 3 tasks, HiveMind vs aider - note the wall time per condition"
  local t0; t0=$(date +%s)
  local -a _only=(--only fix_add,fix_off_by_one,multi_move)
  if [ "$HM_TASKS" = "bench/tasks_polyglot" ]; then
    _only=(--only polyglot_affine_cipher,polyglot_beer_song,polyglot_grade_school)
  fi
  HM_RUNS=1 run_bench "${_only[@]}"
  log "smoke wall time: $(( $(date +%s) - t0 )) s  (full run ~ this x tasks/3 x runs)"
  log "check: pass/fail per condition, tokens > 0, 'aider is not installed' absent"
}

polyglot() {
  # Subset or full set of the official Aider polyglot (python) exercises.
  local src="$HOME/polyglot-benchmark"
  [ -d "$src/.git" ] || git clone --depth 1 https://github.com/Aider-AI/polyglot-benchmark.git "$src"
  ( cd "$HM_DIR" && "$PY" -u bench/import_polyglot.py \
      --source "$src/python/exercises/practice" --out bench/tasks_polyglot \
      --names "${HM_POLYGLOT_NAMES:-all}" ${HM_POLYGLOT_LIMIT:+--limit "$HM_POLYGLOT_LIMIT"} )
  log "now: HM_TASKS=bench/tasks_polyglot bash $0 full"
}

deepswe() {
  # Pure-Python repo tasks from datacurve-ai/deep-swe
  ( cd "$HM_DIR" && "$PY" -u bench/import_deepswe.py --out bench/tasks_deepswe )
  log "installing deepswe test dependencies (hypothesis, trio, pytest)"
  python3 -m pip install -q hypothesis trio attrs pytest pytest-asyncio anyio 2>/dev/null || true
  "$PY" -m pip install -q hypothesis trio attrs pytest pytest-asyncio anyio 2>/dev/null || true
  command -v uv >/dev/null && uv pip install --python "$PY" -q hypothesis trio attrs pytest pytest-asyncio anyio 2>/dev/null || true
  log "now: HM_TASKS=bench/tasks_deepswe HM_CTX=131072 HM_CELL_TIMEOUT=1800 bash $0 full"
}

full() {
  serve
  local name="hmbench"
  tmux has-session -t "$name" 2>/dev/null && die "tmux session '$name' already running"
  log "starting detached run in tmux '$name' (attach: tmux attach -t $name)"
  tmux new-session -d -s "$name" \
    "cd '$HM_DIR' && HIVEMIND_PORT='$PORT' HIVEMIND_MODELS_DIR='$MODELS_DIR' HM_LLAMA_URL='$HM_LLAMA_URL' AIDER_VENV='$AIDER_VENV' HERMES_VENV='$HERMES_VENV' HM_MODEL='$HM_MODEL' HM_RUNS='$HM_RUNS' HM_CONDITIONS='$HM_CONDITIONS' HM_TASKS='$HM_TASKS' HM_CTX='$HM_CTX' HM_RUN_OFFSET='${HM_RUN_OFFSET:-0}' HM_CELL_TIMEOUT='${HM_CELL_TIMEOUT:-900}' HM_TASK_TIMEOUT='${HM_TASK_TIMEOUT:-}' HM_OUT='${HM_OUT:-}' HM_SYNC_DIR='${HM_SYNC_DIR:-}' HM_RESUME='${HM_RESUME:-0}' \
     bash '$0' _run 2>&1 | tee bench_full.log; echo FINISHED >> bench_full.log"
}

status() {
  printf '\n=======================================================\n'
  printf '             HIVEMIND BENCHMARK STATUS                 \n'
  printf '=======================================================\n'
  if tmux has-session -t hmbench 2>/dev/null; then
    printf '[-] Benchmark Runner : RUNNING (tmux session "hmbench" active)\n'
  elif pgrep -f "bench/runner\.py" >/dev/null 2>&1; then
    printf '[-] Benchmark Runner : RUNNING (foreground)\n'
  else
    printf '[-] Benchmark Runner : IDLE / STOPPED\n'
  fi

  if curl --max-time 2 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    printf '[-] HiveMind Engine  : UP & HEALTHY (http://127.0.0.1:%s)\n' "$PORT"
  else
    printf '[-] HiveMind Engine  : DOWN / STOPPED\n'
  fi

  detect_llama_url
  if curl --max-time 2 -fsS "$HM_LLAMA_URL/health" >/dev/null 2>&1; then
    printf '[-] Llama-Server     : UP & READY (%s)\n' "$HM_LLAMA_URL"
  else
    printf '[-] Llama-Server     : DOWN / NOT LOADED\n'
  fi

  printf '[-] Active Model     : %s\n' "$(cat "$HM_DIR/.active_model" 2>/dev/null || echo 'not set')"
  printf '[-] Active Context   : %s tokens\n' "$(cat "$HM_DIR/.active_ctx" 2>/dev/null || echo 'not set')"

  if command -v nvidia-smi >/dev/null 2>&1; then
    printf '[-] GPU Memory       : %s\n' "$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader 2>/dev/null || echo 'n/a')"
  fi

  printf '\n--- Letzte Log-Zeilen (bench_full.log) ---\n'
  if [ -f "$HM_DIR/bench_full.log" ]; then
    tail -n 25 "$HM_DIR/bench_full.log"
  else
    printf 'Kein bench_full.log gefunden.\n'
  fi

  printf '\n--- Neueste Ergebnis-Dateien (bench/results/*.json) ---\n'
  if compgen -G "$HM_DIR/bench/results/*.json" >/dev/null 2>&1 || compgen -G "$HM_DIR/bench/results/external/*.json" >/dev/null 2>&1; then
    "$PY" -c '
import glob, json, pathlib
files = sorted(glob.glob("'"$HM_DIR"'/bench/results/*.json") + glob.glob("'"$HM_DIR"'/bench/results/external/*.json"), key=lambda p: pathlib.Path(p).stat().st_mtime)[-8:]
for p in files:
    d = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    rows = d.get("results", [])
    ok = sum(1 for r in rows if r.get("ok"))
    print(f"  {pathlib.Path(p).name:30s} | rows={len(rows):3d} | pass={ok}/{len(rows)} | model={d.get('\''model'\'','\''?'\'')} ctx={d.get('\''num_ctx'\'','\''?'\'')}")
' 2>/dev/null || ls -lh "$HM_DIR"/bench/results/*.json 2>/dev/null | tail -5
  else
    printf 'Noch keine Ergebnisse vorhanden.\n'
  fi
  printf '=======================================================\n\n'
}

stop() {
  tmux kill-session -t hmbench 2>/dev/null || true
  curl --max-time 15 -fsS -X POST "http://127.0.0.1:$PORT/vram/kill_all" >/dev/null 2>&1 || true
  # setsid makes the saved PID unreliable; kill the engine by its command line.
  pkill -f "$HM_DIR/.venv/bin/python run.py" 2>/dev/null || true
  pkill -f "HiveMind_llama.*run.py" 2>/dev/null || true
  pkill -f llama-server 2>/dev/null || true
  sleep 1
  pkill -9 -f llama-server 2>/dev/null || true
  pkill -9 -f "HiveMind_llama.*run.py" 2>/dev/null || true
  [ -f "$HM_DIR/bench_engine.pid" ] && kill "$(cat "$HM_DIR/bench_engine.pid")" 2>/dev/null || true
  for _ in $(seq 1 20); do
    curl --max-time 3 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || break
    sleep 0.5
  done
  log "stopped. Download bench/results/*.json, then DELETE the rental instance."
}

download() {
  [ -x "$PY" ] || die "run setup first"
  local target="${2:-${HM_MODEL:-qwen3.6:35b-a3b-ud}}"
  log "downloading model: $target -> $MODELS_DIR"
  mkdir -p "$MODELS_DIR"
  ( cd "$HM_DIR" && "$PY" deploy/fetch_models.py --models-dir "$MODELS_DIR" \
      --only-keys "$target" --yes )
  ls -lh "$MODELS_DIR" | grep -iE 'gguf|bin' || true
}

case "${1:-}" in
  setup) setup ;;
  download) download "$@" ;;
  serve) serve ;;
  smoke) smoke ;;
  polyglot) polyglot ;;
  deepswe) deepswe ;;
  full) full ;;
  fg) serve; run_bench 2>&1 | tee -a "$HM_DIR/bench_full.log" ;;
  _run) run_bench ;;
  status) status ;;
  stop) stop ;;
  *) sed -n '2,25p' "$0"; exit 2 ;;
esac
