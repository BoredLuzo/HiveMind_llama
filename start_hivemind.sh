#!/usr/bin/env bash
# HiveMind — Linux start (counterpart to start_hivemind.bat)
# LF line endings, executable bit required (chmod +x start_hivemind.sh).
set -euo pipefail
cd "$(dirname "$0")"

echo ""
echo "  ┌─┐"
echo "  │H│ I V E M I N D"
echo "  └─┘"
echo ""

# Build info (package_release.bat / sync_to_installs.py writes BUILD_INFO.txt)
if [ -f BUILD_INFO.txt ]; then
    echo "   $(grep '^build:' BUILD_INFO.txt)"
fi

# Resolve server port from settings.json
PORT=$(python3 -c "
import json
try:
    with open('settings.json') as f: s = json.load(f)
    print(s.get('server_port', 8001))
except Exception:
    print(8001)
" 2>/dev/null || echo 8001)

# Check if already running
PID=$(lsof -ti :"$PORT" 2>/dev/null | head -1 || true)
if [ -n "$PID" ]; then
    echo "  [INFO] HiveMind is already running on port $PORT (PID: $PID)."
    echo "  [INFO] Open: http://localhost:$PORT"
    echo ""
    read -rp "Kill the running instance and start fresh? [y/N] " yn
    if [ "$yn" != "y" ] && [ "$yn" != "Y" ]; then
        echo "  [INFO] Kept the running instance. This window can be closed."
        exit 0
    fi
    kill "$PID" 2>/dev/null || true
    sleep 3
fi

echo ""
echo "  ────────────────────────────────────────────────"
echo "  HiveMind  >  http://localhost:$PORT  |  Ctrl+C to stop"
echo "  ────────────────────────────────────────────────"
echo ""

VPY=".venv/bin/python"
if [ ! -f "$VPY" ]; then
    echo "  [ERROR] .venv not found. Run ./install.sh first."
    exit 1
fi

exec "$VPY" server.py
