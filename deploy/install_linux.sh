#!/usr/bin/env bash
# HiveMind Linux installer: system deps + venv + llama.cpp + systemd service.
# Usage: sudo deploy/install_linux.sh [--voice]   (env: HIVEMIND_GPU_BACKEND=vulkan|cuda|rocm|cpu, HIVEMIND_VOICE=1)
set -euo pipefail

INSTALL_DIR="/opt/hivemind"
SERVICE_FILE="/etc/systemd/system/hivemind.service"
LOG_DIR="/var/log/hivemind"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LLAMA_BACKEND="${HIVEMIND_GPU_BACKEND:-vulkan}"
# VOICE=1 (or --voice) also downloads the optional speech-to-text engine
# + model (English, CPU only, ~420 MB). Default: skip.
VOICE="${HIVEMIND_VOICE:-0}"
TTS="${HIVEMIND_TTS:-0}"
for _arg in "$@"; do
  case "$_arg" in
    --voice) VOICE=1 ;;
    --tts) TTS=1 ;;
    --both) VOICE=1; TTS=1 ;;
  esac
done

echo "=== HiveMind Linux Installation (backend: $LLAMA_BACKEND) ==="

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: run as root (sudo $0)"
  exit 1
fi

if [ "$LLAMA_BACKEND" != "vulkan" ] && [ "$LLAMA_BACKEND" != "cpu" ] && [ "$LLAMA_BACKEND" != "rocm" ] && [ "$LLAMA_BACKEND" != "cuda" ]; then
  echo "Error: HIVEMIND_GPU_BACKEND must be vulkan, cpu, rocm or cuda (got: $LLAMA_BACKEND)"
  exit 1
fi

if ! id -u hivemind &>/dev/null; then
  echo "[1/8] Creating hivemind user..."
  useradd --system --shell /usr/sbin/nologin --home-dir "$INSTALL_DIR" hivemind
else
  echo "[1/8] User hivemind already exists."
fi

echo "[2/8] Installing system dependencies..."
# UV-CONSISTENCY (2026-09-09): uv provisions the interpreter and the venv,
# so the host only needs curl. python3/pip stay installed where the distro
# pulls them in anyway - they are the legacy fallback in step [4/8].
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y curl
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm curl
else
  echo "  (unknown package manager — install curl manually)"
fi

command -v uv >/dev/null 2>&1 || {
  echo "  Installing uv (astral.sh)..."
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_PATH="/usr/local/bin" sh
}
# UV-PATH (2026-10-17): uv is installed to /usr/local/bin, which a normal
# login shell has on PATH - but a sudo session with a hardened secure_path, a
# container, or a shell that was already open before the install may not see
# it. Do not fail silently (or half-installed): name the problem and the fix.
if command -v uv >/dev/null 2>&1; then
  uv --version
else
  echo "  [WARN] uv is installed but NOT on this shell's PATH."
  echo "         Open a new shell (or: export PATH=\"\$PATH:/usr/local/bin\") and"
  echo "         re-run this installer. Step [4/8] falls back to python3+pip now."
fi

echo "[3/8] Syncing installation to $INSTALL_DIR ..."
if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
  mkdir -p "$INSTALL_DIR"
  # COPY-WITHOUT-STATE (audit 2026-10-17): a plain `cp -r .` from a developer
  # checkout copied settings.json (the local workspace path!), sessions/ and
  # logs/ into /opt/hivemind - the install then looked like someone else's
  # machine. rsync keeps the tree but skips user state and heavy build dirs.
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
      --exclude 'sessions' --exclude 'logs' --exclude 'stt' \
      --exclude 'node_modules' --exclude 'settings.json' --exclude 'presets.json' \
      --exclude 'gateway.toml' --exclude 'model_configs' \
      "$SCRIPT_DIR"/ "$INSTALL_DIR"/
  else
    tar -C "$SCRIPT_DIR" \
      --exclude='./.git' --exclude='./.venv' --exclude='./__pycache__' \
      --exclude='./sessions' --exclude='./logs' --exclude='./stt' \
      --exclude='./node_modules' --exclude='./settings.json' \
      --exclude='./presets.json' --exclude='./gateway.toml' \
      --exclude='./model_configs' -cf - . | tar -C "$INSTALL_DIR" -xf -
  fi
  rm -f "$INSTALL_DIR/deploy/install_linux.sh"
fi

echo "[4/8] Creating venv + installing Python dependencies..."
cd "$INSTALL_DIR"
if command -v uv >/dev/null 2>&1 && [ -f pyproject.toml ]; then
  # Primary path: managed Python 3.14 + locked dependency sync (same flow as
  # the Windows installer).
  uv python install 3.14
  uv sync --all-extras --frozen
elif [ -f requirements.txt ]; then
  # Legacy fallback: system python3 + pip + requirements.txt.
  python3 -m venv "$INSTALL_DIR/.venv"
  "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip -q
  "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt" -q
else
  echo "Error: neither pyproject.toml nor requirements.txt found in $INSTALL_DIR"
  exit 1
fi

echo "[5/8] Fetching llama.cpp (backend: $LLAMA_BACKEND)..."
cd "$INSTALL_DIR"
if ! "$INSTALL_DIR/.venv/bin/python" deploy/fetch_llamacpp.py --backend "$LLAMA_BACKEND"; then
  echo "  [WARN] llama.cpp download failed — install manually into $INSTALL_DIR/llama/"
  echo "         (github.com/ggml-org/llama.cpp/releases → ubuntu-x64 asset, extract,"
  echo "          chmod +x the llama-server binary inside a llama-bXXXX-... folder)"
fi

# ── VOICE (optional, 1.4) - skipped unless --voice / --tts / --both ─────────
# Own step because it downloads ~620 MB and needs the voice extras. Anything
# skipped here can be added later WITHOUT re-running the installer:
#   sudo -u hivemind bash -lc 'cd /opt/hivemind && ./.venv/bin/python deploy/fetch_whisper.py --model parakeet-q4k'
echo "[5b/8] Voice (optional)..."
if [ "${VOICE}" = "1" ]; then
    echo "[voice] fetching the speech-to-text engine + model (English, CPU)..."
    if "$INSTALL_DIR/.venv/bin/python" -c "import soundfile, numpy" 2>/dev/null; then
        _VOICE_PY="$INSTALL_DIR/.venv/bin/python"
    else
        echo "[voice] audio packages missing - installing requirements-voice.txt..."
        "$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements-voice.txt" || true
        _VOICE_PY="$INSTALL_DIR/.venv/bin/python"
    fi
    if ! "$_VOICE_PY" deploy/fetch_whisper.py --model parakeet-q4k; then
        echo "[voice] download failed - the Voice card can retry it later" >&2
    fi
else
    echo "  (speech to text skipped - add later: deploy/fetch_whisper.py)"
fi

if [ "${TTS}" = "1" ]; then
    echo "[tts] preparing the Pocket TTS environment (PyTorch in uv's cache)..."
    # run as the SERVICE user: uv's cache must be the hivemind user's, not root's
    # (otherwise the first spoken answer re-downloads 200 MB as hivemind).
    _TTS_ENV=(env HOME="$INSTALL_DIR" UV_CACHE_DIR="$INSTALL_DIR/.cache/uv"
                 HF_HOME="$INSTALL_DIR/.cache/huggingface")
    if ! "${_TTS_ENV[@]}" uvx --python 3.12 --index https://download.pytorch.org/whl/cpu \
            --from pocket-tts python -c "import pocket_tts"; then
        echo "[tts] environment prep failed - the UI can retry on first use" >&2
    else
        echo "[tts] fetching the model (needs a Hugging Face token)..."
        if "${_TTS_ENV[@]}" uvx --python 3.12 --index https://download.pytorch.org/whl/cpu \
                pocket-tts generate --text "Voice setup complete." \
                --output-path /tmp/hivemind_tts_check.wav; then
            rm -f /tmp/hivemind_tts_check.wav
        else
            echo "[tts] model fetch failed. The model is gated:"
            echo "      add Environment=HF_TOKEN=hf_... to /etc/systemd/system/hivemind.service"
            echo "      after accepting https://huggingface.co/kyutai/pocket-tts" >&2
        fi
    fi
else
    echo "  (text to speech skipped - add later from Settings -^> Voice)"
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
echo "  - Environment=HIVEMIND_GPU_BACKEND=$LLAMA_BACKEND  (vulkan | cuda | rocm | cpu)"
echo "  - MemoryMax (default 12G, adjust for your RAM)"
echo ""
echo "Models: copy GGUFs into the models folder or run, as hivemind:"
echo "  cd $INSTALL_DIR && ./.venv/bin/python deploy/fetch_models.py"
echo "After editing the unit: sudo systemctl daemon-reload && sudo systemctl restart hivemind"
