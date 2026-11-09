#!/usr/bin/env bash
# DeepSWE smoke runner for the official Qwen3.6-35B-A3B (UD-Q4_K_XL)
# with HiveMind's bundled sharp chat template.
#
# Intended host: Linux GPU with Docker (A100 40 GB recommended) or a Colab
# A100 runtime using the Modal sandbox provider. T4/L4 are not recommended for
# the full 35B; L4 only fits a small context.
#
# Usage:
#   bash bench/external/deepswe_qwen36.sh --all --env docker --tasks 5
#   bash bench/external/deepswe_qwen36.sh --setup
#   bash bench/external/deepswe_qwen36.sh --serve
#   bash bench/external/deepswe_qwen36.sh --run --env docker
#   bash bench/external/deepswe_qwen36.sh --stop
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WORK="${DEEPSWE_WORK:-$ROOT/bench/.external/deepswe_qwen36}"
MODEL_DIR="$WORK/models"
LLAMA_DIR="$WORK/llama.cpp"
DEEPSWE_DIR="$WORK/deep-swe"
LOG_DIR="$WORK/logs"
OUT_DIR="${DEEPSWE_OUT:-$ROOT/bench/results/deepswe-qwen36-$(date +%Y%m%d-%H%M%S)}"
PORT="${DEEPSWE_PORT:-8080}"
CTX="${DEEPSWE_CTX:-16384}"
N_TASKS="${DEEPSWE_TASKS:-5}"
SEED="${DEEPSWE_SEED:-0}"
PIER_ENV="${DEEPSWE_PIER_ENV:-docker}"
MODEL_REPO="unsloth/Qwen3.6-35B-A3B-GGUF"
MODEL_TAG="openai/qwen3.6-35b-a3b"
LLAMA_TAG="b12001"
SHARP_URL="https://raw.githubusercontent.com/BoredLuzo/HiveMind_llama/16fdd71/model_configs/chat_template_sharp_v22.5.0.jinja"
MODEL_BASE_URL="${DEEPSWE_MODEL_BASE_URL:-}"

mkdir -p "$WORK" "$MODEL_DIR" "$LLAMA_DIR" "$LOG_DIR" "$OUT_DIR"

log() { printf '\n[deepswe] %s\n' "$*"; }
die() { printf '\n[deepswe][ERROR] %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }

setup_model() {
  need python
  log "downloading $MODEL_REPO (UD-Q4_K_XL) into $MODEL_DIR"
  python - "$MODEL_REPO" "$MODEL_DIR" <<'PY'
import sys
from huggingface_hub import list_repo_files, hf_hub_download
repo, out = sys.argv[1], sys.argv[2]
files = list_repo_files(repo)
matches = [f for f in files if f.lower().endswith("ud-q4_k_xl.gguf")]
if not matches:
    raise SystemExit(f"no UD-Q4_K_XL gguf in {repo}; files={files[:10]}...")
path = hf_hub_download(repo_id=repo, filename=matches[0], local_dir=out)
print(path)
PY
  log "downloading sharp template"
  curl -fsSL "$SHARP_URL" -o "$WORK/chat_template_sharp_v22.5.0.jinja"
  wc -c "$WORK/chat_template_sharp_v22.5.0.jinja"
}

setup_llama() {
  need git
  need cmake
  if [ ! -x "$LLAMA_DIR/build/bin/llama-server" ]; then
    log "building llama.cpp $LLAMA_TAG with CUDA"
    rm -rf "$LLAMA_DIR"
    git clone --depth 1 --branch "$LLAMA_TAG" https://github.com/ggml-org/llama.cpp.git "$LLAMA_DIR"
    cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" -DGGML_CUDA=ON -DLLAMA_CURL=OFF
    cmake --build "$LLAMA_DIR/build" --target llama-server -j"$(nproc)"
  fi
}

model_file() {
  local f
  f="$(find "$MODEL_DIR" -maxdepth 2 -iname '*UD-Q4_K_XL.gguf' -print -quit)"
  [ -n "$f" ] || die "model not found under $MODEL_DIR (run --setup)"
  printf '%s' "$f"
}

start_server() {
  local model; model="$(model_file)"
  [ -f "$WORK/chat_template_sharp_v22.5.0.jinja" ] || die "sharp template missing (run --setup)"
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    log "server already up on :$PORT"; return
  fi
  log "starting llama-server on :$PORT ctx=$CTX"
  "$LLAMA_DIR/build/bin/llama-server" \
    -m "$model" \
    --host 0.0.0.0 --port "$PORT" \
    --ctx-size "$CTX" --n-gpu-layers 99 \
    --flash-attn on --jinja \
    --chat-template-file "$WORK/chat_template_sharp_v22.5.0.jinja" \
    --reasoning on \
    --temp 1.0 --top-p 0.95 --top-k 20 \
    --no-warmup \
    >"$LOG_DIR/llama-server.log" 2>&1 &
  echo $! > "$WORK/llama-server.pid"
  for _ in $(seq 1 180); do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
      log "server ready: http://127.0.0.1:$PORT/v1"
      return
    fi
    sleep 2
  done
  tail -40 "$LOG_DIR/llama-server.log" >&2 || true
  die "llama-server did not become healthy"
}

stop_server() {
  if [ -f "$WORK/llama-server.pid" ]; then
    kill "$(cat "$WORK/llama-server.pid")" 2>/dev/null || true
    rm -f "$WORK/llama-server.pid"
    log "server stopped"
  fi
}

setup_pier() {
  need git
  if [ ! -d "$DEEPSWE_DIR/.git" ]; then
    git clone --depth 1 https://github.com/datacurve-ai/deep-swe.git "$DEEPSWE_DIR"
  fi
  if ! command -v pier >/dev/null 2>&1; then
    log "installing datacurve-pier"
    python -m pip install -q -U datacurve-pier
  fi
  pier --version
}

run_bench() {
  need pier
  [ -d "$DEEPSWE_DIR/tasks" ] || die "DeepSWE tasks missing (run --setup)"
  if [ -z "$MODEL_BASE_URL" ]; then
    if [ "$PIER_ENV" = "docker" ]; then
      MODEL_BASE_URL="http://host.docker.internal:$PORT/v1"
    else
      die "for --env modal set DEEPSWE_MODEL_BASE_URL to a public tunnel URL ending in /v1"
    fi
  fi
  log "running $N_TASKS DeepSWE tasks (seed=$SEED, env=$PIER_ENV)"
  log "model endpoint: $MODEL_BASE_URL"
  pier run -p "$DEEPSWE_DIR/tasks" \
    --agent mini-swe-agent \
    --model "$MODEL_TAG" \
    --env "$PIER_ENV" \
    --n-tasks "$N_TASKS" --sample-seed "$SEED" --n-concurrent 1 \
    --jobs-dir "$OUT_DIR" --yes \
    --ae "OPENAI_API_KEY=sk-local" \
    --ae "OPENAI_BASE_URL=$MODEL_BASE_URL" \
    --ae "OPENAI_API_BASE=$MODEL_BASE_URL" \
    --agent-timeout-multiplier 1.0
  log "results: $OUT_DIR"
}

ACTION=""
ENV_SET=0
while [ $# -gt 0 ]; do
  case "$1" in
    --setup) ACTION="setup" ;;
    --serve) ACTION="serve" ;;
    --run) ACTION="run" ;;
    --all) ACTION="all" ;;
    --stop) ACTION="stop" ;;
    --env) PIER_ENV="${2:?--env needs docker|modal|daytona}"; ENV_SET=1; shift ;;
    --tasks) N_TASKS="${2:?--tasks needs a number}"; shift ;;
    --seed) SEED="${2:?--seed needs a number}"; shift ;;
    --port) PORT="${2:?--port needs a number}"; shift ;;
    --ctx) CTX="${2:?--ctx needs a number}"; shift ;;
    --model-base-url) MODEL_BASE_URL="${2:?--model-base-url needs a URL}"; shift ;;
    -h|--help)
      sed -n '1,28p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

[ -n "$ACTION" ] || ACTION="all"
case "$ACTION" in
  setup) setup_model; setup_llama; setup_pier ;;
  serve) start_server ;;
  run) setup_pier; run_bench ;;
  all) setup_model; setup_llama; start_server; setup_pier; run_bench ;;
  stop) stop_server ;;
esac
