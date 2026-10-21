"""TTS ohne Audio: klassifizieren, Fallback, Cooldown, UI bleibt ruhig.

Hintergrund (live 21.10.): der Sidecar antwortet 200 ohne WAV, im Log steht
"string keys in translate table must be of length 1". Die UI zeigte nur
"engine failed" plus eine unbehandelte Promise-Rejection.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


from core import tts_engine as T  # noqa: E402

ENGINE = (ROOT / "core" / "tts_engine.py").read_text(encoding="utf-8", errors="replace")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")

# ── 1. Klassifikation aus dem Sidecar-Log ───────────────────────────────
import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    log = Path(tmp) / "pocket_tts.log"
    log.write_text(
        "INFO: something\n"
        "Traceback (most recent call last):\n"
        "ValueError: string keys in translate table must be of length 1\n",
        encoding="utf-8")
    original = T._engine_log_path
    T._engine_log_path = lambda: log          # type: ignore[assignment]
    try:
        hint = T._engine_failure_hint()
    finally:
        T._engine_log_path = original
check("log hint names the translate-table cause", "translate table" in hint, f"({hint})")

T._engine_log_path = lambda: Path("does/not/exist")  # type: ignore[assignment]
check("missing log yields no hint", T._engine_failure_hint() == "")
T._engine_log_path = original                          # type: ignore[assignment]

# ── 2. Cooldown-Verhalten ───────────────────────────────────────────────
T._mark_broken("voice engine produced no audio")
check("broken marks a cooldown", T._broken_remaining() > 0)
check("reason is stored", "no audio" in (T._state.get("broken_reason") or ""))
try:
    T.synthesize("hallo", {"tts_enabled": True})
    check("cooldown raises before starting the sidecar", False, "(no exception)")
except T.TtsError as exc:
    check("cooldown raises TTS_ENGINE_BROKEN", exc.code == "TTS_ENGINE_BROKEN", f"({exc.code})")
    check("cooldown message names the wait", "retrying in" in exc.message, f"({exc.message[:60]})")
# Cooldown wieder aufheben, sonst kippt der naechste Test
T._state["broken_until"] = 0.0
T._state["broken_reason"] = ""

# ── 3. Fallback auf die Default-Stimme ──────────────────────────────────
class _Resp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None


class _Client:
    """First voice returns junk, the default voice returns a WAV."""

    def __init__(self, wav):
        self.calls = []
        self._wav = wav

    def post(self, url, data=None, timeout=None):
        voice = (data or {}).get("voice_url")
        self.calls.append(voice)
        return _Resp(b"not a wav at all" if voice != T.DEFAULT_VOICE else self._wav)


def _wav_bytes() -> bytes:
    import io as _io
    import wave
    buf = _io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 1600)
    return buf.getvalue()


WAV = _wav_bytes()
client = _Client(WAV)
T._client = lambda: client                       # type: ignore[assignment]
T._start = lambda settings=None, voice="": "http://127.0.0.1:9"   # type: ignore[assignment]
T._cache.clear()
T._queue_order = getattr(T, "_cache_order", [])
try:
    out = T.synthesize("Hallo Welt.", {"tts_enabled": True, "tts_voice": "cosette",
                                       "tts_speed": 1.0})
    ok = out.get("wav") and out.get("voice") == T.DEFAULT_VOICE
except T.TtsError as exc:
    ok = False
    out = {"error": exc.message}
check("broken voice falls back to the default voice", bool(ok), f"({out if not ok else ''})")
check("fallback tried the configured voice first",
      client.calls[:2] == ["cosette", T.DEFAULT_VOICE], f"({client.calls[:3]})")

# ── 4. Zweiter Ausfall: kein Endlos-Retry, Cooldown greift ─────────────
client2 = _Client(WAV)
client2._wav = b"still not a wav"
T._client = lambda: client2                      # type: ignore[assignment]
T._cache.clear()
T._state["broken_until"] = 0.0
try:
    T.synthesize("Noch ein Satz.", {"tts_enabled": True, "tts_voice": "cosette"})
    second = None
except T.TtsError as exc:
    second = exc
check("two empty answers set the cooldown", T._broken_remaining() > 0)
check("the second failure is reported as TTS_ENGINE_BROKEN",
      second is not None and second.code == "TTS_ENGINE_BROKEN",
      f"({getattr(second, 'code', None)})")
calls_before = len(client2.calls)
try:
    T.synthesize("Dritter Satz.", {"tts_enabled": True, "tts_voice": "cosette"})
except T.TtsError:
    pass
check("cooldown stops further synthesis attempts", len(client2.calls) == calls_before,
      f"({len(client2.calls)} vs {calls_before})")
T._state["broken_until"] = 0.0

# ── 5. UI: kein unbehandeltes Promise, klare Meldung ───────────────────
check("ui knows TTS_ENGINE_BROKEN", "TTS_ENGINE_BROKEN" in JS)
check("ui shows the server reason for a broken engine",
      "if (code === 'TTS_ENGINE_BROKEN' && j && j.error) return j.error;" in JS)
check("prefetch keeps a no-op catch", "p.catch(function () {});" in JS)
check("read aloud drops the queue on a broken engine",
      "_readAloudQueue.length = 0;" in JS and "translate table" in JS)
check("engine logs the fallback attempt", "retrying with %s" in ENGINE)
check("engine stores the reason for the next error",
      'broken_reason' in ENGINE and "_mark_broken(" in ENGINE)

print()
print(f"SUMMARY tts_broken_engine: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
