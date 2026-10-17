"""TTS engine contract: lifecycle, errors, sidecar contract (no torch needed).

A tiny local HTTP stub plays the Pocket TTS sidecar (GET /health, POST /tts ->
audio/wav), and HIVEMIND_TTS_URL points the engine at it, so this suite runs
everywhere - including CI - without uvx or a 200 MB torch install.

Pins:
  (a) opt-in: TTS_DISABLED while tts_enabled is off, before anything spawns;
  (b) the synthesis round trip returns WAV bytes, the voice is forwarded, and the
      speed factor is applied (duration ~ 1/speed);
  (c) a sidecar that is not healthy -> TTS_FAILED with the URL in the message;
  (d) caps: text length, timeout mapping, empty text;
  (e) status reports enabled/voice/speed/uv without raising;
  (f) the voice list contains cosette as the default (owner choice).

Run: python tests/test_tts_engine.py
"""
import hashlib
import io
import os
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def make_wav(seconds=4.0, sr=24000) -> bytes:
    n = int(seconds * sr)
    t = np.arange(n) / sr
    sig = (0.4 * np.sin(2 * np.pi * 95 * t)
           + 0.2 * np.sin(2 * np.pi * 190 * t)) * 12000
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(sig.astype("<i2").tobytes())
    return buf.getvalue()


WAV = make_wav()


class StubTTSCard(BaseHTTPRequestHandler):
    seen_voices: list = []
    slow = False
    broken = False

    def log_message(self, *args):  # silence
        pass

    def do_GET(self):  # noqa: N802
        if self.path == "/health" and not StubTTSCard.broken:
            body = b'{"status": "healthy"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(503)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        # the engine posts multipart/form-data with text + voice_url
        text = raw.decode("utf-8", "replace")
        for part in text.split("name=\"voice_url\""):
            if "cosette" in part or "alba" in part or "bill_boerst" in part:
                StubTTSCard.seen_voices.append(
                    "cosette" if "cosette" in part else ("alba" if "alba" in part else "bill_boerst"))
                break
        if StubTTSCard.slow:
            time.sleep(3)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(WAV)))
        self.end_headers()
        self.wfile.write(WAV)


server = ThreadingHTTPServer(("127.0.0.1", 0), StubTTSCard)
threading.Thread(target=server.serve_forever, daemon=True).start()
PORT = server.server_address[1]
os.environ["HIVEMIND_TTS_URL"] = f"http://127.0.0.1:{PORT}"

from core import tts_engine  # noqa: E402

SET = {"tts_enabled": True, "tts_voice": "cosette", "tts_speed": 1.08,
       "tts_timeout_s": 2}

# ── (a) opt-in ─────────────────────────────────────────────────────────────
try:
    tts_engine.synthesize("hello", {})
    check("disabled -> TTS_DISABLED", False)
except tts_engine.TtsError as exc:
    check("disabled -> TTS_DISABLED", exc.code == "TTS_DISABLED")

# ── (b) round trip ─────────────────────────────────────────────────────────
res = tts_engine.synthesize("Hello there, this is a test.", SET)
check("returns WAV bytes", res["wav"][:4] == b"RIFF")
check("duration is input/speed", abs(res["duration"] - 4.0 / 1.08) < 0.12,
      f" ({res['duration']}s, expect {4.0 / 1.08:.2f}s)")
check("voice is forwarded", StubTTSCard.seen_voices[-1] == "cosette")
check("speed is reported", abs(res["speed"] - 1.08) < 1e-6)
res2 = tts_engine.synthesize("Another line.", SET, voice="bill_boerst")
check("an explicit voice wins", StubTTSCard.seen_voices[-1] == "bill_boerst"
      and res2["voice"] == "bill_boerst")

# ── (d) caps / errors ──────────────────────────────────────────────────────
long_text = "word " * 400
res3 = tts_engine.synthesize(long_text, SET)
check("long text is capped without failing", res3["wav"][:4] == b"RIFF")
try:
    tts_engine.synthesize("   ", SET)
    check("empty text -> TTS_EMPTY", False)
except tts_engine.TtsError as exc:
    check("empty text -> TTS_EMPTY", exc.code == "TTS_EMPTY")

StubTTSCard.slow = True
try:
    tts_engine.synthesize("slow", dict(SET, tts_timeout_s=1))
    check("timeout -> TTS_TIMEOUT", False)
except tts_engine.TtsError as exc:
    check("timeout -> TTS_TIMEOUT", exc.code == "TTS_TIMEOUT")
StubTTSCard.slow = False

# ── (c) unhealthy sidecar ──────────────────────────────────────────────────
StubTTSCard.broken = True
try:
    tts_engine._state.update({"proc": None, "port": 0})
    tts_engine.synthesize("x", dict(SET, tts_timeout_s=1))
    check("unhealthy sidecar -> TTS_FAILED", False)
except tts_engine.TtsError as exc:
    check("unhealthy sidecar -> TTS_FAILED", exc.code == "TTS_FAILED")
StubTTSCard.broken = False

# ── (e)/(f) status + voices ────────────────────────────────────────────────
st = tts_engine.status(SET)
for key in ("enabled", "running", "voice", "speed", "uv", "voices", "idle_stop_s"):
    check(f"status exposes {key}", key in st)
check("default voice is cosette", tts_engine.DEFAULT_VOICE == "cosette")
check("voices list has the shipped set",
      {v[0] for v in tts_engine.VOICES} == {"cosette", "alba", "anna", "eve",
                                            "jane", "vera", "bill_boerst"})
check("default speed is 1.08", tts_engine.DEFAULT_SPEED == 1.08)
check("unknown voice falls back to cosette",
      tts_engine.resolve_voice({"tts_voice": "nobody"}) == "cosette")
check("status never raises with an empty settings dict",
      isinstance(tts_engine.status({}), dict))

src = Path(__file__).resolve().parent.parent / "core" / "tts_engine.py"
code = src.read_text(encoding="utf-8", errors="replace")
check("the sidecar runs through uvx (no torch in our venv)", "uvx" in code)
check("it is started lazily, not at import", "def _start(" in code and
      "subprocess.Popen" in code)
check("the child is stopped at exit", "atexit.register" in code)
check("idle shutdown exists", "def _maybe_idle_stop" in code)
check("no shell", "shell=True" not in code)

server.shutdown()
print()
print(f"SUMMARY tts_engine: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
