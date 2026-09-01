


import os
from pathlib import Path

# Default model (Fallback-Fallback — normalerweise aus settings.json)
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "ministral-3:3b")

MODEL_ANALYST     = os.environ.get("MODEL_ANALYST",     DEFAULT_MODEL)
MODEL_REFINER     = os.environ.get("MODEL_REFINER",     DEFAULT_MODEL)
MODEL_CRITIC      = os.environ.get("MODEL_CRITIC",      "granite-4.1:3b")
MODEL_SYNTHESIZER = os.environ.get("MODEL_SYNTHESIZER", DEFAULT_MODEL)
MODEL_DIRECT      = os.environ.get("MODEL_DIRECT",      DEFAULT_MODEL)
MODEL_JUDGE       = os.environ.get("MODEL_JUDGE",       "granite-4.1:3b")

# Pipeline
MAX_ITERATIONS = int(os.environ.get("MAX_ITERATIONS", "2"))
SESSIONS_DIR   = Path(__file__).parent / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

# Smart Preload
SMART_PRELOAD_KEEP_ALIVE = os.environ.get("SMART_PRELOAD_KEEP_ALIVE", "10m")
