"""Underdog Woof local CPU TTS backend (ConwayResearch/Underdog-Woof).

Pure-NumPy int8 English TTS (~40.6 MB weights, no PyTorch, no GPU, no HF gate)
with two built-in voices: ``juno`` and ``mira``. Model weights and code stay
outside the git tree under ``<HiveMind>/voice_models/underdog_woof/`` and are
fetched on demand via ``deploy/fetch_woof.py``.

License: Underdog Non-Commercial License 1.0 (non-commercial use only; visible
credit "Voice by Underdog Woof" / underdog.ai required in UI & docs).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

from core import tts_audio
from deploy import fetch_woof

logger = logging.getLogger(__name__)

WOOF_VOICES = [
    ("juno", "Woof: Juno", "female, crisp (non-commercial)", "underdog-nc-1.0",
     "ConwayResearch/Underdog-Woof (juno, non-commercial)"),
    ("mira", "Woof: Mira", "female, warm/clear (non-commercial)", "underdog-nc-1.0",
     "ConwayResearch/Underdog-Woof (mira, non-commercial)"),
]
DEFAULT_WOOF_VOICE = "juno"
WOOF_ATTRIBUTION = (
    "Voice by Underdog Woof (underdog.ai) - "
    "Underdog Non-Commercial License 1.0 (non-commercial use only)"
)

_lock = threading.RLock()
_instances: dict[tuple[str, str], object] = {}
_last_used: float = 0.0


def _ensure_numpy():
    try:
        import numpy as np  # noqa: F401
        return np
    except ImportError as exc:
        raise RuntimeError("numpy is required for Underdog Woof (pip install numpy)") from exc


def resolve_voice(voice: str = "") -> str:
    """Return a valid Underdog Woof voice key, defaulting to ``juno``."""
    want = str(voice or "").strip().lower()
    keys = {v[0] for v in WOOF_VOICES}
    return want if want in keys else DEFAULT_WOOF_VOICE


def is_installed() -> bool:
    """True when Underdog Woof runtime and weights exist locally."""
    return fetch_woof.is_installed()


def fetch_state() -> dict:
    """Return downloader progress metadata."""
    return fetch_woof.fetch_state()


def is_running() -> bool:
    """True while at least one Woof model instance is loaded warm in RAM."""
    with _lock:
        return bool(_instances)


def stop() -> None:
    """Unload warm Woof instances to free RAM."""
    global _last_used
    with _lock:
        _instances.clear()
        _last_used = 0.0


def maybe_idle_stop(idle_stop_s: float) -> None:
    """Release warm instances if idle longer than ``idle_stop_s``."""
    with _lock:
        if _instances and _last_used and (time.time() - _last_used) > idle_stop_s:
            logger.info("[TTS-WOOF] idle for %.0fs - unloading model", time.time() - _last_used)
            _instances.clear()
            return


def _load_instance(voice: str, *, auto_download: bool = True):
    global _last_used
    np = _ensure_numpy()
    root = fetch_woof.woof_root()
    if not fetch_woof.is_installed(root):
        if not auto_download:
            raise FileNotFoundError(f"Underdog Woof is not installed in {root}")
        logger.info("[TTS-WOOF] downloading ConwayResearch/Underdog-Woof to %s ...", root)
        fetch_woof.download(root)
    key = (str(root), voice)
    with _lock:
        inst = _instances.get(key)
        if inst is None:
            rstr = str(root)
            if rstr not in sys.path:
                sys.path.insert(0, rstr)
            # Clear cached package if pointed at a different temp/install dir (tests/relocation)
            existing = sys.modules.get("underdog_woof")
            existing_file = getattr(existing, "__file__", "") or ""
            if existing is not None and rstr not in existing_file:
                for mod_name in [m for m in sys.modules if m == "underdog_woof" or m.startswith("underdog_woof.")]:
                    sys.modules.pop(mod_name, None)
            from underdog_woof import Woof  # type: ignore[import-not-found]
            t0 = time.perf_counter()
            inst = Woof(weights=str(Path(root) / "woof_int8.npz"), voice=voice)
            _instances[key] = inst
            logger.info("[TTS-WOOF] loaded voice=%s in %.2fs", voice, time.perf_counter() - t0)
        _last_used = time.time()
        return inst, np


def preload(voice: str = "") -> bool:
    """Warm an already-installed Woof voice without triggering a surprise download."""
    if not is_installed():
        return False
    try:
        _load_instance(resolve_voice(voice), auto_download=False)
        return True
    except (OSError, RuntimeError, ImportError, ValueError) as exc:
        logger.info("[TTS-WOOF] preload skipped: %s", exc)
        return False


def synthesize_wav(text: str, voice: str = "", *, auto_download: bool = True) -> bytes:
    """Synthesize ``text`` via Underdog Woof and return raw 16-bit mono PCM WAV bytes."""
    global _last_used
    v = resolve_voice(voice)
    with _lock:
        # Hold lock across call because upstream underdog_woof.runtime shares a module RNG.
        inst, np = _load_instance(v, auto_download=auto_download)
        wav_f32 = inst(text)
        sr = int(getattr(inst, "sr", 24000) or 24000)
        samples = np.asarray(wav_f32, dtype="float64") * 32767.0
        _last_used = time.time()
    return tts_audio.samples_to_wav(samples, sr)
