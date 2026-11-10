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
HM_BRANCH="${HM_BRANCH:-dev}"
HM_DIR="${HM_DIR:-$HOME/HiveMind_llama}"
BENCH_TGZ="${BENCH_TGZ:-$HOME/bench_bundle.tgz}"
HM_MODEL="${HM_MODEL:-qwen3.6:35b-a3b-ud}"
HM_RUNS="${HM_RUNS:-1}"
HM_CONDITIONS="${HM_CONDITIONS:-hivemind_duo,aider}"
HM_TASKS="${HM_TASKS:-bench/tasks}"
AIDER_VENV="${AIDER_VENV:-$HOME/aider-venv}"
MODELS_DIR="${HIVEMIND_MODELS_DIR:-$HM_DIR/models}"
PORT="${HIVEMIND_PORT:-8001}"
HM_LLAMA_URL="${HM_LLAMA_URL:-}"
HM_LLAMA_CUDA="${HM_LLAMA_CUDA:-12.8}"
LOG="$HM_DIR/bench_engine.log"

log() { printf '\n[bench] %s\n' "$*"; }
die() { printf '\n[bench][ERROR] %s\n' "$*" >&2; exit 1; }
SUDO=""; [ "$(id -u)" -eq 0 ] || SUDO="sudo"
PY="$HM_DIR/.venv/bin/python"

setup() {
  command -v nvidia-smi >/dev/null || die "no NVIDIA driver (nvidia-smi missing) - wrong instance?"
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
  log "system packages"
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq git curl ca-certificates tmux rsync build-essential
  command -v uv >/dev/null || { curl -LsSf https://astral.sh/uv/install.sh | sh; }
  export PATH="$HOME/.local/bin:$PATH"

  log "clone $HM_REPO ($HM_BRANCH)"
  [ -d "$HM_DIR/.git" ] || git clone --branch "$HM_BRANCH" "$HM_REPO" "$HM_DIR"
  if [ -f "$BENCH_TGZ" ]; then
    log "overlay bench/ from $BENCH_TGZ"
    tar xzf "$BENCH_TGZ" -C "$HM_DIR"
  fi
  [ -f "$HM_DIR/bench/runner.py" ] || die "bench/runner.py missing: push it or provide BENCH_TGZ"
  git -C "$HM_DIR" log -1 --format='[bench] engine commit: %h %s'

  log "python 3.14 + locked deps"
  ( cd "$HM_DIR" && uv python install 3.14 && uv sync --all-extras --frozen )

  log "llama.cpp (CUDA $HM_LLAMA_CUDA)"
  ( cd "$HM_DIR" && "$PY" deploy/fetch_llamacpp.py --backend cuda --cuda-version "$HM_LLAMA_CUDA" )
  # Linux CUDA builds link the CUDA runtime dynamically; the release ships it
  # in a separate cudart archive next to the main build.
  _exe="$(find "$HM_DIR/llama" -name llama-server -type f | head -1)"
  _bdir="$(dirname "$_exe")"
  _build="${_bdir##*llama-b}"
  if [ -n "$_build" ]; then
    _cudart_url="https://github.com/ggml-org/llama.cpp/releases/download/b${_build}/cudart-llama-b${_build}-bin-ubuntu-cuda-${HM_LLAMA_CUDA}-x64.tar.gz"
    if ! find "$HM_DIR/llama" -name 'libcudart.so*' | grep -q .; then
      log "CUDA runtime split: downloading cudart for b${_build}"
      curl -L --fail --retry 3 -o /tmp/cudart.tar.gz "$_cudart_url" || log "WARN: cudart download failed"
      [ -s /tmp/cudart.tar.gz ] && tar xzf /tmp/cudart.tar.gz -C "$HM_DIR/llama" || true
    fi
  fi

  log "model $HM_MODEL -> $MODELS_DIR (~20 GB)"
  mkdir -p "$MODELS_DIR"
  ( cd "$HM_DIR" && "$PY" deploy/fetch_models.py --models-dir "$MODELS_DIR" \
      --only-keys "$HM_MODEL" --yes )
  ls -lh "$MODELS_DIR" | head

  log "aider (own venv, python 3.12)"
  [ -x "$AIDER_VENV/bin/aider" ] || {
    uv venv --python 3.12 "$AIDER_VENV"
    uv pip install --python "$AIDER_VENV/bin/python" -q aider-chat
  }
  "$AIDER_VENV/bin/aider" --version

  log "setup done - next: serve, then smoke"
}

detect_llama_url() {
  for _p in 8101 8102 8080; do
    if curl --max-time 2 -fsS "http://127.0.0.1:$_p/health" >/dev/null 2>&1; then
      HM_LLAMA_URL="http://127.0.0.1:$_p"; return
    fi
  done
  HM_LLAMA_URL="${HM_LLAMA_URL:-http://127.0.0.1:8101}"
  log "WARN: no llama-server health endpoint found; using $HM_LLAMA_URL"
}

serve() {
  [ -x "$PY" ] || die "run setup first"
  # Benchmark profile for the 80 GB A100: full GPU offload, no CPU experts,
  # CUDA-friendly prefill chunk. Written before the engine starts because
  # LLAMA_FIT / LLAMA_UBATCH are read at import time.
  ( cd "$HM_DIR" && "$PY" - <<'PY'
import json
from pathlib import Path
p = Path("settings.json")
d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
d.update({
    "moe_cpu_experts": {"qwen3.6:35b-a3b": 0, "hermes3.6:35b-a3b": 0},
    "vram_budget_gb": 76.0,
    "llama_fit": False,
    "llama_gpu_layers_auto": False,
    "duo_ubatch": 1024,
    "llama_ubatch_size": 1024,
})
p.write_text(json.dumps(d, indent=2) + "\n", encoding="utf-8")
PY
  )
  if curl --max-time 3 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    log "engine already up on :$PORT"
  else
    log "starting HiveMind engine on 127.0.0.1:$PORT (log: $LOG)"
    _cudart_lib="$(find "$HM_DIR/llama" -name 'libcudart.so*' -print -quit 2>/dev/null || true)"
    [ -n "$_cudart_lib" ] && export LD_LIBRARY_PATH="$(dirname "$_cudart_lib"):${LD_LIBRARY_PATH:-}"
    ( cd "$HM_DIR" && \
      HIVEMIND_HOST=127.0.0.1 HIVEMIND_PORT="$PORT" HIVEMIND_GPU_BACKEND=cuda \
      HIVEMIND_MODELS_DIR="$MODELS_DIR" PYTHONUNBUFFERED=1 \
      setsid "$PY" run.py >"$LOG" 2>&1 < /dev/null & \
      echo $! >"$HM_DIR/bench_engine.pid" )
    for _ in $(seq 1 90); do
      curl --max-time 3 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && { log "engine healthy"; break; }
      sleep 2
    done
    curl --max-time 3 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || { tail -40 "$LOG" >&2 || true; die "engine did not become healthy"; }
  fi
  curl --max-time 10 -fsS -X POST "http://127.0.0.1:$PORT/settings" -H "Content-Type: application/json" \
    -d '{"moe_cpu_experts":{"qwen3.6:35b-a3b":0,"hermes3.6:35b-a3b":0},"vram_budget_gb":76.0}' >/dev/null \
    || log "WARN: moe_cpu_experts override failed - 35B may run with CPU experts"
  log "preloading $HM_MODEL (ctx 32768)"
  curl --max-time 600 -fsS -X POST "http://127.0.0.1:$PORT/vram/load" -H "Content-Type: application/json" \
    -d "{\"model\":\"$HM_MODEL\",\"num_ctx\":32768,\"keep_alive\":\"-1\"}" >/dev/null \
    || log "WARN: /vram/load failed; the first HiveMind request will load the model"
  for _ in $(seq 1 180); do
    if curl --max-time 2 -fsS "http://127.0.0.1:8101/health" >/dev/null 2>&1 || curl --max-time 2 -fsS "http://127.0.0.1:8102/health" >/dev/null 2>&1; then break; fi
    sleep 2
  done
  detect_llama_url
  log "benchmark llama endpoint: $HM_LLAMA_URL"
}

run_bench() {  # $1 = extra args
  [ -x "$AIDER_VENV/bin/aider" ] || die "aider missing (run setup)"
  [ -n "$HM_LLAMA_URL" ] || detect_llama_url
  # Cap per-cell time so one stuck agent cannot burn the rental budget.
  ( cd "$HM_DIR" && "$PY" -u bench/runner.py \
      --tasks "$HM_TASKS" \
      --model "$HM_MODEL" \
      --conditions "$HM_CONDITIONS" \
      --runs "$HM_RUNS" \
      --cell-timeout "${HM_CELL_TIMEOUT:-900}" \
      --aider-bin "$AIDER_VENV/bin/aider" \
      --hivemind "http://127.0.0.1:$PORT" \
      --llama "$HM_LLAMA_URL" \
      $_tf_flag \
      $1 )
}

smoke() {
  serve
  log "smoke: 3 tasks, HiveMind vs aider - note the wall time per condition"
  local t0; t0=$(date +%s)
  local _only="--only fix_add,fix_off_by_one,multi_move"
  if [ "$HM_TASKS" = "bench/tasks_polyglot" ]; then
    _only="--only polyglot_affine_cipher,polyglot_beer_song,polyglot_grade_school"
  fi
  HM_RUNS=1 run_bench "$_only"
  log "smoke wall time: $(( $(date +%s) - t0 )) s  (full run ~ this x tasks/3 x runs)"
  log "check: pass/fail per condition, tokens > 0, 'aider is not installed' absent"
}

polyglot() {
  # Subset or full set of the official Aider polyglot (python) exercises.
  local src="$HOME/polyglot-benchmark"
  [ -d "$src/.git" ] || git clone --depth 1 https://github.com/Aider-AI/polyglot-benchmark.git "$src"
  ( cd "$HM_DIR" && "$PY" bench/import_polyglot.py \
      --source "$src/python/exercises/practice" --out bench/tasks_polyglot \
      --names "${HM_POLYGLOT_NAMES:-all}" ${HM_POLYGLOT_LIMIT:+--limit "$HM_POLYGLOT_LIMIT"} )
  log "now: HM_TASKS=bench/tasks_polyglot bash $0 full"
}

full() {
  serve
  local name="hmbench"
  tmux has-session -t "$name" 2>/dev/null && die "tmux session '$name' already running"
  log "starting detached run in tmux '$name' (attach: tmux attach -t $name)"
  tmux new-session -d -s "$name" \
    "cd '$HM_DIR' && HM_RUNS='$HM_RUNS' HM_CONDITIONS='$HM_CONDITIONS' HM_TASKS='$HM_TASKS' \
     bash '$0' _run 2>&1 | tee bench_full.log; echo FINISHED >> bench_full.log"
}

status() { tail -n 40 "$HM_DIR/bench_full.log" 2>/dev/null || echo "no run log yet"; ls -1t "$HM_DIR"/bench/results/*.json 2>/dev/null | head -3; }

stop() {
  tmux kill-session -t hmbench 2>/dev/null || true
  curl --max-time 15 -fsS -X POST "http://127.0.0.1:$PORT/vram/kill_all" >/dev/null 2>&1 || true
  # setsid makes the saved PID unreliable; kill the engine by its command line.
  pkill -f "$HM_DIR/.venv/bin/python run.py" 2>/dev/null || true
  pkill -f "HiveMind_llama.*run.py" 2>/dev/null || true
  pkill -f llama-server 2>/dev/null || true
  [ -f "$HM_DIR/bench_engine.pid" ] && kill "$(cat "$HM_DIR/bench_engine.pid")" 2>/dev/null || true
  for _ in $(seq 1 20); do
    curl --max-time 3 -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 || break
    sleep 0.5
  done
  log "stopped. Download bench/results/*.json, then DELETE the rental instance."
}

case "${1:-}" in
  setup) setup ;;
  serve) serve ;;
  smoke) smoke ;;
  polyglot) polyglot ;;
  full) full ;;
  _run) run_bench "" ;;
  status) status ;;
  stop) stop ;;
  *) sed -n '2,25p' "$0"; exit 2 ;;
esac
