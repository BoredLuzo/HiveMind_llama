#!/usr/bin/env bash
# HiveMind — Linux install (counterpart to install.bat)
# LF line endings, executable bit required (chmod +x install.sh).
set -euo pipefail
cd "$(dirname "$0")"

echo ""
echo "  H I V E M I N D  —  Linux install"
echo "  ==================================="
echo ""

# ── [1/4] Python ─────────────────────────────────────────────────────────────
PY=""
for candidate in python3.14 python3.13 python3.12 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver=$("$candidate" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=${ver%%.*}
        if [ "$major" -ge 3 ]; then
            PY="$candidate"
            echo "  [OK] Python: $candidate ($ver)"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "  [ERROR] Python 3.12+ not found. Install python3 and retry."
    exit 1
fi

# ── [2/4] Virtual environment ────────────────────────────────────────────────
if [ ! -d .venv ]; then
    echo "  [..] Creating .venv..."
    "$PY" -m venv .venv
fi
VPY=".venv/bin/python"
echo "  [OK] venv python: $VPY"

echo "  [..] Installing dependencies (requirements.txt)..."
"$VPY" -m pip install --quiet --upgrade pip
"$VPY" -m pip install --quiet -r requirements.txt
echo "  [OK] Dependencies installed."

# ── [3/4] Settings ───────────────────────────────────────────────────────────
if [ ! -f settings.json ]; then
    echo "  [..] Writing settings.json..."
    read -rp "  VRAM budget in GB [8.0]: " HM_VRAM
    read -rp "  Server port [8001]: " HM_PORT
    HM_VRAM="${HM_VRAM:-8.0}"
    HM_PORT="${HM_PORT:-8001}"
    "$VPY" -c "
import os, re
from settings import load_settings, save_settings
s = load_settings()
vram_raw = os.environ.get('HM_VRAM', '$HM_VRAM').replace(',', '.')
_m = re.match(r'\d+(\.\d+)?', vram_raw)
s['vram_budget_gb'] = float(_m.group(0)) if _m else 8.0
s['server_port'] = int('$HM_PORT') if '$HM_PORT'.isdigit() else 8001
s.setdefault('workspace', '')
save_settings(s)
print('   settings.json written (vram_budget_gb={}, server_port={})'.format(s['vram_budget_gb'], s['server_port']))
"
else
    echo "  [OK] settings.json already exists — skipping."
fi

# ── [4/4] llama.cpp binary ───────────────────────────────────────────────────
LLAMA_DIR="llama"
if [ ! -d "$LLAMA_DIR" ]; then
    echo ""
    echo "  [NOTE] No llama/ directory found."
    echo "  You need a llama-server binary (Linux build) at: $PWD/llama/<build>/llama-server"
    echo "  Download from https://github.com/ggml-org/llama.cpp/releases"
    echo "  and place it in llama/<build>/ (or set the path in settings.json)."
    echo ""
    read -rp "  Download llama.cpp Linux build now? [y/N]: " DL
    if [ "$DL" = "y" ] || [ "$DL" = "Y" ]; then
        echo "  [ERROR] Automatic download not implemented for Linux yet."
        echo "  Download manually and place llama-server in llama/<build>/."
        exit 1
    fi
fi

echo ""
echo "  ==================================="
echo "  Install complete."
echo "  Start: ./start_hivemind.sh"
echo "  ==================================="
