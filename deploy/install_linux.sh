#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/hivemind"
SERVICE_FILE="/etc/systemd/system/hivemind.service"
LOG_DIR="/var/log/hivemind"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== HiveMind Service Installation ==="

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: run as root (sudo $0)"
  exit 1
fi

if ! id -u hivemind &>/dev/null; then
  echo "[1/5] Creating hivemind user..."
  useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" hivemind
else
  echo "[1/5] User hivemind already exists."
fi

echo "[2/5] Creating log directory..."
mkdir -p "$LOG_DIR"
chown hivemind:hivemind "$LOG_DIR"

echo "[3/5] Installing systemd service..."
cp "$SCRIPT_DIR/hivemind.service" "$SERVICE_FILE"

if [ ! -d "$INSTALL_DIR" ]; then
  echo "Warning: $INSTALL_DIR does not exist."
  echo "  Copy your HiveMind installation there, then run:"
  echo "    chown -R hivemind:hivemind $INSTALL_DIR"
fi

echo "[4/5] Reloading systemd..."
systemctl daemon-reload

echo "[5/5] Enabling service..."
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
echo "Configuration:"
echo "  Edit $SERVICE_FILE to change:"
echo "    - WorkingDirectory (installation path)"
echo "    - ExecStart (Python path / venv)"
echo "    - MemoryMax (default 12G, adjust for your RAM)"
echo "    - Environment vars"
echo ""
echo "After editing: sudo systemctl daemon-reload && sudo systemctl restart hivemind"
