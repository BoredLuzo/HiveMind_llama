"""Speech-to-text through the whisper.cpp / parakeet.cpp binaries.

OWNER DECISIONS (2026-10-17, docs/plan_1.4_voice.md)
====================================================
* **English only** in 1.4. Every request is forced to ``en`` (the model file is
  the only thing that changes between tiers). The key ``stt_language`` exists so
  a second language is a setting later, not a code change - but nothing else is
  tested or promised yet.
* **One CLI invocation per request.** No idle process, no port, no supervisor.
  ``stt_preload`` opts into a warm ``whisper-server`` for whisper models;
  parakeet has no server and loads in well under a second, so it is never
  preloaded. If the warm path fails for any reason the request falls back to the
  CLI, so preloading can never make things worse.
* **The model file is the tier**, measured on real English speech (73 LibriSpeech
  utterances, 481 s, micro-WER, 8 threads):

  ==========================  ======  ========  =====  ======
  model                       disk    WER       RTFx   RAM
  ==========================  ======  ========  =====  ======
  parakeet-tdt-0.6b-v3-q4_k   396 MB  3.57 %    18.7   552 MB
  ggml-small                  465 MB  7.30 %     4.1   582 MB
  ggml-base                   141 MB  9.91 %    14.1   317 MB
  ggml-tiny                    74 MB  12.43 %   23.6   205 MB
  ==========================  ======  ========  =====  ======

  Default is parakeet q4_k on a machine with >= 8 GB RAM, ``ggml-base`` below
  that. Nothing here loads a GPU: the binaries are CPU/BLAS builds.

The module is import-safe without the binaries, without ``soundfile`` and without
any model: every entry point raises :class:`SttError` with a stable code instead
of crashing, so the engine (and the test suite) runs on a machine that never
installed voice.
"""
from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_IS_WIN = platform.system() == "Windows"


class SttError(RuntimeError):
    """A speech-to-text failure with a stable, user-facing code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"code": self.code, "error": self.message}


@dataclass(frozen=True)
class SttModel:
    """One downloadable model file (= one quality/speed tier)."""

    key: str
    filename: str
    engine: str          # "whisper-cli" or "parakeet-cli"
    repo: str            # Hugging Face repo the file comes from
    size_mb: int
    ram_mb: int
    wer: float           # measured English WER, real speech
    rtfx: float
    label: str
    notes: str = ""


# Order matters: the first entry is the recommendation for a strong machine.
MODELS: dict[str, SttModel] = {
    "parakeet-q4k": SttModel(
        "parakeet-q4k", "ggml-parakeet-tdt-0.6b-v3-q4_k.bin", "parakeet-cli",
        "ggml-org/parakeet-GGUF", 396, 552, 0.0357, 18.7,
        "Parakeet 0.6B v3 (q4_k)", "best English accuracy per byte, default",
    ),
    "base": SttModel(
        "base", "ggml-base.bin", "whisper-cli", "ggerganov/whisper.cpp",
        141, 317, 0.0991, 14.1, "Whisper base", "light default, multilingual",
    ),
    "tiny": SttModel(
        "tiny", "ggml-tiny.bin", "whisper-cli", "ggerganov/whisper.cpp",
        74, 205, 0.1243, 23.6, "Whisper tiny", "weak CPUs",
    ),
    "small": SttModel(
        "small", "ggml-small.bin", "whisper-cli", "ggerganov/whisper.cpp",
        465, 582, 0.0730, 4.1, "Whisper small", "accurate but slow",
    ),
}

#: 1.4 is English-only (owner decision). Kept as a tuple so the endpoint and the
#: resolver agree on what is accepted.
LANGUAGES = ("en",)

#: Fallback tiers by total system RAM (GB). Highest first.
DEFAULT_BY_RAM = ((8, "parakeet-q4k"), (0, "base"))

_TRANSCRIPT_SKIP = ("load_backend", "whisper_", "parakeet_", "ggml_",
                    "error", "warning", "usage:", "supported audio formats")
_BLANK_MARKERS = ("[blank_audio]", "[silence]", "[music]", "(silence)")


# ── paths ───────────────────────────────────────────────────────────────────
def stt_root() -> Path:
    """Where the binaries and models live (``HIVEMIND_STT_DIR`` wins)."""
    env = (os.environ.get("HIVEMIND_STT_DIR") or "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "stt"


def _exe(name: str) -> str:
    return f"{name}.exe" if _IS_WIN else name


def binary_path(engine: str, root: Path | None = None) -> Path | None:
    """Absolute path of an engine binary, or None when it is not installed."""
    base = root or stt_root()
    for cand in (base / "bin" / _exe(engine), base / "bin" / engine):
        if cand.is_file():
            return cand
    return None


def model_path(key: str, root: Path | None = None) -> Path | None:
    m = MODELS.get(key)
    if not m:
        return None
    p = (root or stt_root()) / "models" / m.filename
    return p if p.is_file() else None


def installed_models() -> list[str]:
    return [k for k in MODELS if model_path(k)]


# ── settings resolution ─────────────────────────────────────────────────────
def total_ram_gb() -> float:
    """Total physical RAM in GB; 0.0 when it cannot be read (never raises)."""
    try:
        if _IS_WIN:
            import ctypes

            class _MemStat(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = _MemStat()
            stat.dwLength = ctypes.sizeof(_MemStat)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return round(stat.ullTotalPhys / (1024 ** 3), 1)
            return 0.0
        with open("/proc/meminfo", encoding="ascii", errors="ignore") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return round(int(line.split()[1]) / (1024 ** 2), 1)
    except (OSError, ValueError, AttributeError, ImportError):
        pass
    return 0.0


def resolve_model(settings: dict | None = None) -> str:
    """Model key for this machine: explicit setting, else the RAM-based default."""
    want = str((settings or {}).get("stt_model") or "").strip()
    if want and want in MODELS:
        return want
    if want:
        logger.warning("[STT] unknown stt_model %r - using the RAM default", want)
    ram = total_ram_gb()
    for floor, key in DEFAULT_BY_RAM:
        if ram >= floor:
            return key
    return DEFAULT_BY_RAM[-1][1]


def resolve_language(settings: dict | None = None) -> str:
    """Always ``en`` in 1.4 (owner decision); off-list values degrade to it."""
    want = str((settings or {}).get("stt_language") or "").strip().lower()
    if want and want not in LANGUAGES:
        logger.info("[STT] language %r is not shipped yet - using en", want)
    return "en"


def resolve_threads(settings: dict | None = None) -> int:
    try:
        want = int((settings or {}).get("stt_threads") or 0)
    except (TypeError, ValueError):
        want = 0
    if want > 0:
        return max(1, min(16, want))
    return max(1, min(4, (os.cpu_count() or 4) // 2))


def resolve_timeout(settings: dict | None = None) -> float:
    try:
        want = float((settings or {}).get("stt_timeout_s") or 120)
    except (TypeError, ValueError):
        want = 120.0
    return max(10.0, min(900.0, want))


def _wants_preload(settings: dict | None = None) -> bool:
    return bool((settings or {}).get("stt_preload"))


# ── transcript parsing ──────────────────────────────────────────────────────
def parse_transcript(stdout: str) -> str:
    """Transcript from CLI stdout, minus backend/log noise."""
    lines = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(low.startswith(skip) for skip in _TRANSCRIPT_SKIP):
            continue
        lines.append(line)
    return " ".join(lines).strip()


def is_blank_transcript(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low:
        return True
    return any(marker in low for marker in _BLANK_MARKERS) and len(low) < 40


# ── the engine ──────────────────────────────────────────────────────────────
def build_argv(engine: str, exe: Path, model: Path, wav: Path, *, threads: int,
               language: str = "en") -> list[str]:
    """argv for one transcription (list form only - never a shell string)."""
    if engine == "parakeet-cli":
        return [str(exe), "-m", str(model), "-f", str(wav), "-t", str(threads)]
    return [str(exe), "-m", str(model), "-f", str(wav), "-l", language,
            "-t", str(threads), "-nt"]


def transcribe(wav_path: str | Path, *, settings: dict | None = None,
               model_key: str | None = None) -> str:
    """Transcribe a 16 kHz mono WAV file and return the text.

    Raises :class:`SttError` with one of STT_DISABLED / STT_NO_ENGINE /
    STT_NO_MODEL / STT_FAILED / STT_TIMEOUT / STT_EMPTY.
    """
    settings = settings or {}
    if not settings.get("stt_enabled", False):
        raise SttError("STT_DISABLED", "speech-to-text is disabled in the settings")
    key = model_key or resolve_model(settings)
    m = MODELS.get(key)
    if not m:
        raise SttError("STT_NO_MODEL", f"unknown model {key!r}")
    exe = binary_path(m.engine)
    if not exe:
        raise SttError(
            "STT_NO_ENGINE",
            f"{m.engine} is not installed - run: python deploy/fetch_whisper.py",
        )
    mp = model_path(key)
    if not mp:
        raise SttError(
            "STT_NO_MODEL",
            f"model {m.filename} is missing - run: python deploy/fetch_whisper.py "
            f"--model {key}",
        )
    wav = Path(wav_path)
    if not wav.is_file():
        raise SttError("STT_DECODE_FAILED", f"audio file not found: {wav.name}")

    argv = build_argv(m.engine, exe, mp, wav,
                      threads=resolve_threads(settings),
                      language=resolve_language(settings))
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=resolve_timeout(settings),
                              check=False)
    except subprocess.TimeoutExpired as exc:
        raise SttError("STT_TIMEOUT",
                       f"transcription took longer than {resolve_timeout(settings):.0f}s") from exc
    except OSError as exc:
        raise SttError("STT_FAILED", f"could not start {m.engine}: {exc}") from exc

    text = parse_transcript(proc.stdout or "")
    if proc.returncode != 0 and not text:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or [""]
        raise SttError("STT_FAILED", f"{m.engine} exited {proc.returncode}: {tail[0][:200]}")
    if is_blank_transcript(text):
        raise SttError("STT_EMPTY", "no speech detected in the audio")
    return text


# ── optional warm whisper-server ────────────────────────────────────────────
_WARM: dict = {"proc": None, "port": 0, "model": ""}


def warm_is_active(model_key: str = "") -> bool:
    proc = _WARM.get("proc")
    if proc is None or proc.poll() is not None:
        return False
    return not model_key or _WARM.get("model") == model_key


def stop_warm() -> None:
    """Stop the preloaded whisper-server (idempotent, never raises)."""
    proc = _WARM.get("proc")
    _WARM.update({"proc": None, "port": 0, "model": ""})
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def preload(settings: dict | None = None, *, model_key: str | None = None) -> bool:
    """Start a warm ``whisper-server`` for whisper models. Best effort.

    Returns True when a warm server is (now) serving this model. Parakeet has no
    server binary, so preloading it is a no-op that returns False.
    """
    settings = settings or {}
    key = model_key or resolve_model(settings)
    m = MODELS.get(key)
    if not m or m.engine != "whisper-cli":
        logger.info("[STT] preload not available for %s (CLI loads in <1 s)", key)
        return False
    if warm_is_active(key):
        return True
    stop_warm()
    exe = binary_path("whisper-server")
    mp = model_path(key)
    if not exe or not mp:
        return False
    port = _free_port()
    log = open(os.path.join(tempfile.gettempdir(), "hivemind_stt_warm.log"),
               "w", encoding="utf-8")  # noqa: SIM115 - closed in stop_warm via GC
    try:
        proc = subprocess.Popen(
            [str(exe), "-m", str(mp), "--host", "127.0.0.1", "--port", str(port),
             "-t", str(resolve_threads(settings)), "--language", resolve_language(settings)],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        logger.warning("[STT] preload failed to start: %s", exc)
        return False
    _WARM.update({"proc": proc, "port": port, "model": key})
    logger.info("[STT] preloaded %s (whisper-server on 127.0.0.1:%d)", key, port)
    return True


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def status(settings: dict | None = None) -> dict:
    """What the UI shows in the Voice card (never raises)."""
    settings = settings or {}
    key = resolve_model(settings)
    m = MODELS.get(key)
    return {
        "enabled": bool(settings.get("stt_enabled", False)),
        "model": key,
        "model_label": m.label if m else key,
        "model_installed": bool(model_path(key)),
        "engine": m.engine if m else "",
        "engine_installed": bool(binary_path(m.engine)) if m else False,
        "language": resolve_language(settings),
        "threads": resolve_threads(settings),
        "preload": bool(settings.get("stt_preload")),
        "warm": warm_is_active(key),
        "ram_gb": total_ram_gb(),
        "installed_models": installed_models(),
        "available_models": [
            {"key": k, "label": v.label, "size_mb": v.size_mb, "wer": v.wer,
             "installed": bool(model_path(k))}
            for k, v in MODELS.items()
        ],
        "root": str(stt_root()),
    }


def _selftest() -> int:  # pragma: no cover - manual CLI
    print("root:", stt_root())
    print("ram:", total_ram_gb(), "GB")
    print("model:", resolve_model({}), "| threads:", resolve_threads({}))
    print("binaries:", {e: bool(binary_path(e)) for e in ("whisper-cli", "parakeet-cli")})
    print("models:", installed_models() or "none")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(_selftest())
