"""Text-to-speech through Kyutai Pocket TTS (``uvx pocket-tts serve``).

WHY A SIDECAR
=============
``pocket-tts generate`` loads the model per call (seconds). The ``serve`` mode
keeps it warm - measured here: ``POST /tts`` returns ``audio/wav`` in ~1.5 s for a
5 s sentence. So the engine starts one child process lazily, on the first
synthesis request, and stops it again when it has been idle for
``tts_idle_stop_s``. Nothing runs while TTS is off.

DEPENDENCY STORY (owner constraint: no bloat)
=============================================
The sidecar runs through ``uvx`` - an isolated uv environment - so PyTorch never
enters HiveMind's venv. ``uv`` is already part of the install path. If ``uvx`` is
missing we fail with ``TTS_NO_UV`` instead of pulling 200 MB.

SPEED
=====
Pocket TTS has no speed parameter, so the factor is applied here (WSOLA in
``core.tts_audio``): measured 1.08x -> 5.48 s instead of 5.96 s with the pitch
unchanged. Shipped default voice: anna (CC BY 4.0). cosette stays selectable but is
a non-commercial recording (Expresso, CC BY-NC 4.0) - see docs/voice_licenses.md.

API of the sidecar (verified against pocket-tts 2026.10):
  GET  /health           -> {"status": "healthy"}
  POST /tts (multipart)  -> audio/wav   fields: text, voice_url
"""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

from core import tts_audio

logger = logging.getLogger(__name__)

#: The shipped voices: (key, label, note, license, source). The license column is
#: the reason this table is the single source - see docs/voice_licenses.md.
#:
#:  cc0          public domain (no obligations)
#:  cc-by-4.0    attribution required (commercial use allowed)
#:  cc-by-nc-4.0 NON-COMMERCIAL ONLY - the recording may not be used in a paid
#:                product; fine for private use, so it stays selectable
#:
#: Provenance comes from pocket_tts/utils/utils.py `_ORIGINS_OF_PREDEFINED_VOICES`
#: (verified 2026-10-17 against the installed package, not from memory).
VOICES = [
    ("anna", "Anna", "female, soft", "cc-by-4.0", "VCTK p228 (CC BY 4.0)"),
    ("cosette", "Cosette", "female, clear", "cc-by-nc-4.0",
     "Expresso ex04 (CC BY-NC 4.0) - non-commercial only"),
    ("alba", "Alba", "female, reference voice", "cc-by-4.0", "Alba MacKenna (CC BY 4.0)"),
    ("eve", "Eve", "female, bright", "cc-by-4.0", "VCTK p361 (CC BY 4.0)"),
    ("jane", "Jane", "female, calm", "cc-by-4.0", "VCTK p339 (CC BY 4.0)"),
    ("vera", "Vera", "female, warm", "cc-by-4.0", "VCTK p229 (CC BY 4.0)"),
    ("bill_boerst", "Bill Boerst", "male, serious", "cc0", "Voice-Zero/LibriVox (CC0)"),
]
#: The shipped default is a commercially usable voice. `cosette` stays available
#: (and is what the owner's own settings pin), but a fresh install must not start
#: on a non-commercial recording.
DEFAULT_VOICE = "anna"

#: Obligations that ship with the audio: model CC BY 4.0, code MIT, Most voices
#: CC BY 4.0 (attribution) with one CC BY-NC (non-commercial). Shown in the UI.
ATTRIBUTION = ("Voice: Kyutai Pocket TTS (model CC BY 4.0, code MIT) - "
               "voice recordings: VCTK / Alba MacKenna (CC BY 4.0), "
               "Voice-Zero (CC0), Expresso (CC BY-NC 4.0, non-commercial)")
DEFAULT_SPEED = 1.08
START_TIMEOUT_S = 180.0      # first start downloads nothing (uvx cache) but loads torch
IDLE_STOP_S = 900.0


class TtsError(RuntimeError):
    """A text-to-speech failure with a stable, user-facing code."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message

    def as_dict(self) -> dict:
        return {"code": self.code, "error": self.message}


_state: dict = {
    "proc": None,
    "port": 0,
    "voice": "",
    "started_at": 0.0,
    "last_used": 0.0,
    "starts": 0,        # how often we started it (diagnostics only)
    "failures": 0,      # consecutive failed starts (the restart budget)
    "log": None,        # kept open by the child, closed on stop
}
_lock = threading.RLock()
_http_client = None


# ── settings helpers ────────────────────────────────────────────────────────
def resolve_voice(settings: dict | None = None) -> str:
    want = str((settings or {}).get("tts_voice") or "").strip()
    keys = {v[0] for v in VOICES}
    if want in keys:
        return want
    if want:
        logger.info("[TTS] unknown voice %r - using %s", want, DEFAULT_VOICE)
    return DEFAULT_VOICE


def resolve_speed(settings: dict | None = None) -> float:
    return tts_audio.clamp_speed((settings or {}).get("tts_speed", DEFAULT_SPEED))


def resolve_idle_stop(settings: dict | None = None) -> float:
    try:
        val = float((settings or {}).get("tts_idle_stop_s") or IDLE_STOP_S)
    except (TypeError, ValueError):
        val = IDLE_STOP_S
    return max(60.0, min(7200.0, val))


def _uvx() -> str:
    exe = shutil.which("uvx")
    if not exe:
        raise TtsError("TTS_NO_UV",
                       "uv is not installed - install it from https://docs.astral.sh/uv/ "
                       "or switch TTS off")
    return exe


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def base_url() -> str:
    env = (os.environ.get("HIVEMIND_TTS_URL") or "").strip()
    if env:
        return env.rstrip("/")
    port = _state.get("port") or 0
    return f"http://127.0.0.1:{port}" if port else ""


# ── lifecycle ───────────────────────────────────────────────────────────────
def is_running() -> bool:
    proc = _state.get("proc")
    return proc is not None and proc.poll() is None


def stop(force: bool = False) -> None:
    """Stop the sidecar (idempotent). ``force`` skips the graceful path."""
    with _lock:
        proc = _state.get("proc")
        log = _state.get("log")
        _state.update({"proc": None, "port": 0, "voice": "", "log": None})
    if log is not None:
        try:
            log.close()
        except OSError:
            pass
    if proc is None:
        return
    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


atexit.register(lambda: stop(force=True))


def _maybe_idle_stop(settings: dict | None) -> None:
    if not is_running():
        return
    last = float(_state.get("last_used") or 0.0)
    if last and (time.time() - last) > resolve_idle_stop(settings):
        logger.info("[TTS] idle for %.0fs - stopping the sidecar",
                    time.time() - last)
        stop()


def _wait_healthy(url: str, timeout_s: float, require_proc: bool = True) -> bool:
    """Poll /health. ``require_proc=False`` for an externally managed sidecar
    (HIVEMIND_TTS_URL): there is no child process of ours to watch."""
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if require_proc and not is_running():
            return False
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2.0)
    return False


def _start(settings: dict | None = None, voice: str = "") -> str:
    """Start the sidecar if needed and return its base URL."""
    with _lock:
        if is_running():
            _state["last_used"] = time.time()
            return base_url()
        # An explicit URL means the sidecar is managed elsewhere (tests, a manual
        # `uvx pocket-tts serve`, or a future shared instance).
        env_url = (os.environ.get("HIVEMIND_TTS_URL") or "").strip()
        if env_url:
            url = env_url.rstrip("/")
            if _wait_healthy(url, 20.0, require_proc=False):
                _state["last_used"] = time.time()
                return url
            raise TtsError("TTS_FAILED", f"HIVEMIND_TTS_URL {url} is not answering /health")
        if _state.get("failures", 0) >= 3:
            raise TtsError("TTS_FAILED",
                           "the TTS engine failed to start three times - see logs/")
        uvx = _uvx()
        port = _free_port()
        voice = voice or resolve_voice(settings)
        cmd = [uvx, "--python", "3.12", "pocket-tts", "serve",
               "--host", "127.0.0.1", "--port", str(port),
               "--default-voice", voice]
        log_path = Path(__file__).resolve().parent.parent / "logs" / "pocket_tts.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = open(log_path, "a", encoding="utf-8")
            proc = subprocess.Popen(cmd, stdout=log, stderr=log,
                                    stdin=subprocess.DEVNULL)
        except OSError as exc:
            raise TtsError("TTS_FAILED", f"could not start uvx: {exc}") from exc
        _state["log"] = log
        _state.update({"proc": proc, "port": port, "voice": voice,
                       "started_at": time.time(), "last_used": time.time(),
                       "starts": int(_state.get("starts", 0)) + 1})
        url = base_url()
    logger.info("[TTS] sidecar starting on %s (voice=%s, pid=%s)", url, voice, proc.pid)
    if not _wait_healthy(url, START_TIMEOUT_S):
        _state["failures"] = int(_state.get("failures", 0)) + 1
        stop(force=True)
        raise TtsError("TTS_FAILED",
                       "the TTS engine did not answer /health in time - see logs/pocket_tts.log")
    _state["failures"] = 0          # a healthy start clears the budget
    logger.info("[TTS] sidecar ready after %.1fs", time.time() - _state["started_at"])
    return url


def _client():
    """One keep-alive httpx client for the sidecar (created lazily)."""
    global _http_client
    if _http_client is None:
        import httpx
        _http_client = httpx.Client(
            timeout=httpx.Timeout(connect=5.0, read=600.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_keepalive_connections=4, max_connections=8))
    return _http_client


# ── synthesis ───────────────────────────────────────────────────────────────
def synthesize(text: str, settings: dict | None = None, *,
               voice: str = "", speed: float | None = None) -> dict:
    """Text -> {"wav": bytes, "duration": float, "voice": ..., "speed": ...}."""
    import httpx

    settings = settings or {}
    if not settings.get("tts_enabled", False):
        raise TtsError("TTS_DISABLED", "text-to-speech is disabled in the settings")
    text = (text or "").strip()
    if not text:
        raise TtsError("TTS_EMPTY", "nothing to say")
    if len(text) > tts_audio.MAX_CHARS:
        text = text[:tts_audio.MAX_CHARS].rsplit(" ", 1)[0] + " ..."
    _maybe_idle_stop(settings)
    url = _start(settings, voice)
    voice = voice or resolve_voice(settings)
    spd = tts_audio.clamp_speed(speed if speed is not None else resolve_speed(settings))

    try:
        timeout = float(settings.get("tts_timeout_s") or 120)
    except (TypeError, ValueError):
        timeout = 120.0
    try:
        # CLIENT-REUSE (2026-10-18): a fresh httpx.Client per request threw the
        # connection away every time (20-50 ms on top of the model). The read-aloud
        # asks for one sentence at a time now, so that overhead was a visible part
        # of the delay before the first sound.
        import httpx as _httpx_call
        # the client is shared (keep-alive), but the TIMEOUT is per request: the
        # settings value must still cut a hanging synthesis (test_tts_engine pins
        # it with tts_timeout_s=1 against a slow stub).
        resp = _client().post(
            f"{url}/tts",
            data={"text": text, "voice_url": voice},
            timeout=_httpx_call.Timeout(connect=5.0, read=timeout,
                                        write=10.0, pool=5.0))
        resp.raise_for_status()
        wav = resp.content
    except httpx.TimeoutException as exc:
        raise TtsError("TTS_TIMEOUT", f"the engine took longer than {timeout:.0f}s") from exc
    except httpx.HTTPError as exc:
        raise TtsError("TTS_FAILED", f"engine error: {type(exc).__name__}") from exc
    if not tts_audio.looks_like_wav(wav):
        raise TtsError("TTS_FAILED", "the engine returned something that is not a WAV")
    _state["last_used"] = time.time()
    stretched = tts_audio.stretch_wav(wav, spd)
    return {"wav": stretched, "duration": tts_audio.duration_seconds(stretched),
            "voice": voice, "speed": spd}


def status(settings: dict | None = None) -> dict:
    settings = settings or {}
    return {
        "enabled": bool(settings.get("tts_enabled", False)),
        "running": is_running(),
        "voice": resolve_voice(settings),
        "speed": resolve_speed(settings),
        "uv": bool(shutil.which("uvx")),
        "voices": [{"key": k, "label": label, "note": note, "license": lic,
                    "source": src} for k, label, note, lic, src in VOICES],
        "attribution": ATTRIBUTION,
        "idle_stop_s": resolve_idle_stop(settings),
        "max_chars": tts_audio.MAX_CHARS,
        "port": _state.get("port") or 0,
    }
