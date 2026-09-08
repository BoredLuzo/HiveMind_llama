#!/usr/bin/env bash
# HiveMind Linux installer: system deps + venv + llama.cpp + systemd service.
# Usage: sudo deploy/install_linux.sh   (env: HIVEMIND_GPU_BACKEND=vulkan|cpu|rocm)
set -euo pipefail

INSTALL_DIR="/opt/hivemind"
SERVICE_FILE="/etc/systemd/system/hivemind.service"
LOG_DIR="/var/log/hivemind"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LLAMA_BACKEND="${HIVEMIND_GPU_BACKEND:-vulkan}"

echo "=== HiveMind Linux Installation (backend: $LLAMA_BACKEND) ==="

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: run as root (sudo $0)"
  exit 1
fi

if [ "$LLAMA_BACKEND" != "vulkan" ] && [ "$LLAMA_BACKEND" != "cpu" ] && [ "$LLAMA_BACKEND" != "rocm" ]; then
  echo "Error: HIVEMIND_GPU_BACKEND must be vulkan, cpu or rocm (got: $LLAMA_BACKEND)"
  exit 1
fi

if ! id -u hivemind &>/dev/null; then
  echo "[1/8] Creating hivemind user..."
  useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" hivemind
else
  echo "[1/8] User hivemind already exists."
fi

echo "[2/8] Installing system dependencies..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip curl ca-certificates
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip curl
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm python python-pip curl
else
  echo "  (unknown package manager — install python3 + venv manually)"
fi

echo "[3/8] Syncing installation to $INSTALL_DIR ..."
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
  mkdir -p "$INSTALL_DIR"
  cp -r "$SCRIPT_DIR"/. "$INSTALL_DIR"/
  rm -f "$INSTALL_DIR/deploy/install_linux.sh"
fi

echo "[4/8] Creating venv + installing Python dependencies..."
python3 -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --upgrade pip -q
"$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q

echo "[5/8] Fetching llama.cpp (backend: $LLAMA_BACKEND)..."
cd "$INSTALL_DIR"
if ! "$INSTALL_DIR/.venv/bin/python" deploy/fetch_llamacpp.py --backend "$LLAMA_BACKEND"; then
  echo "  [WARN] llama.cpp download failed — install manually into $INSTALL_DIR/llama/"
  echo "         (github.com/ggml-org/llama.cpp/releases → ubuntu-x64 asset, extract,"
  echo "          chmod +x the llama-server binary inside a llama-bXXXX-... folder)"
fi

echo "[6/8] Creating log directory..."
mkdir -p "$LOG_DIR"
chown -R hivemind:hivemind "$INSTALL_DIR" "$LOG_DIR"

echo "[7/8] Installing systemd service..."
cp "$INSTALL_DIR/deploy/hivemind.service" "$SERVICE_FILE"
systemctl daemon-reload

echo "[8/8] Enabling service..."
systemctl enable hivemind.service

echo ""
echo "=== Installation complete ==="
echo ""
echo "Commands:"
echo "  sudo systemctl start hivemind    # Start"
echo "  sudo systemctl stop hivemind     # Stop"
echo "  sudo systemctl restart hivemind  # Restart"
echo "  sudo systemctl status hivemind   # Status"
echo "  journalctl -u hivemind -f        # Live logs"
echo "  tail -f $LOG_DIR/hivemind.log    # Log file"
echo ""
echo "Configuration ($SERVICE_FILE):"
echo "  - WorkingDirectory / ExecStart (venv path)"
echo "  - Environment=HIVEMIND_GPU_BACKEND=$LLAMA_BACKEND  (vulkan | cuda | cpu)"
echo "  - MemoryMax (default 12G, adjust for your RAM)"
echo ""
echo "Models: copy GGUFs into the models folder or run, as hivemind:"
echo "  cd $INSTALL_DIR && ./.venv/bin/python deploy/fetch_models.py"
echo "After editing the unit: sudo systemctl daemon-reload && sudo systemctl restart hivemind"
