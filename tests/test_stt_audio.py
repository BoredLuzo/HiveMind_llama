"""Audio → 16 kHz mono WAV (opus in, and the caps around it).

Two layers, so this suite is meaningful with and without the optional `voice`
dependency group:

* the CONTRACT layer always runs: without soundfile/numpy every call must raise
  SttError("STT_NO_AUDIO_LIB") with the install hint - never an ImportError crash
  escaping into the engine;
* the DECODE layer runs when soundfile is installed: WAV to 16 kHz mono, a
  sample-rate change, the duration cap, the size cap, and the Ogg/Opus round
  trip that Telegram voice notes need (measured 15 ms decode, identical
  transcript - docs/plan_1.4_voice.md §3.3).

Run: python tests/test_stt_audio.py
"""
import math
import os
import struct
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import stt_audio  # noqa: E402
from core.stt_engine import SttError  # noqa: E402

passed = 0
failed = 0
skipped = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def skip(name, why):
    global skipped
    skipped += 1
    print(f"  SKIP {name} ({why})")


def _wav_bytes(seconds=1.0, rate=16000, freq=440.0, channels=1) -> bytes:
    n = int(seconds * rate)
    frames = bytearray()
    for i in range(n):
        v = int(12000 * math.sin(2 * math.pi * freq * i / rate))
        for _ in range(channels):
            frames += struct.pack("<h", v)
    buf = tempfile.SpooledTemporaryFile()
    with wave.open(buf, "wb") as wf:            # type: ignore[arg-type]
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(bytes(frames))
    buf.seek(0)
    return buf.read()


try:
    import numpy  # noqa: F401
    import soundfile  # noqa: F401
    HAVE_AUDIO = True
except ImportError:
    HAVE_AUDIO = False

print(f"  .. optional audio libs installed: {HAVE_AUDIO}")

# ── contract layer (always) ─────────────────────────────────────────────────
check("size cap is the Telegram download limit", stt_audio.MAX_BYTES == 20 * 1024 * 1024)
check("target sample rate is 16 kHz", stt_audio.SAMPLE_RATE == 16000)
_src = (Path(__file__).resolve().parent.parent / "core" / "stt_audio.py").read_text(encoding="utf-8")
check("no external process at all (so no ffmpeg, no shell)", "subprocess" not in _src)
check("temp files come from tempfile", "tempfile.mkstemp" in _src)
check("nothing is written into the workspace", "HIVEMIND_WORKSPACE" not in _src)
check("cleanup is a no-op on None", stt_audio.cleanup(None) is None)
check("cleanup swallows a missing file", stt_audio.cleanup(os.path.join(os.sep, "nope.wav")) is None)


def _code(fn):
    try:
        fn()
    except SttError as exc:
        return exc.code
    return "NO-ERROR"


if not HAVE_AUDIO:
    check("missing libs -> STT_NO_AUDIO_LIB (not ImportError)",
          _code(lambda: stt_audio.to_wav16k(_wav_bytes())) == "STT_NO_AUDIO_LIB")
    try:
        stt_audio.to_wav16k(b"")
    except SttError as exc:
        check("the error carries the install command",
              "pip install" in exc.message and "voice" in exc.message.lower())
    else:
        check("the error carries the install command", False)
    print()
    print(f"SUMMARY stt_audio: {passed} passed, {failed} failed, {skipped} skipped")
    print("  (install the voice group to run the decode layer: pip install -r requirements-voice.txt)")
    sys.exit(0 if failed == 0 else 1)

# ── decode layer (soundfile present) ───────────────────────────────────────
raw_wav = _wav_bytes(seconds=1.0, rate=16000)
wav, dur = stt_audio.to_wav16k(raw_wav)
try:
    check("a 16 kHz WAV decodes", wav.is_file() and abs(dur - 1.0) < 0.05, f" ({dur:.2f}s)")
    with wave.open(str(wav), "rb") as wf:
        check("output is mono 16-bit 16 kHz",
              wf.getnchannels() == 1 and wf.getsampwidth() == 2 and wf.getframerate() == 16000)
    check("empty input is refused", _code(lambda: stt_audio.to_wav16k(b"")) == "STT_DECODE_FAILED")
    check("garbage is refused",
          _code(lambda: stt_audio.to_wav16k(b"not audio at all")) == "STT_DECODE_FAILED")
finally:
    stt_audio.cleanup(wav)

wav8, dur8 = stt_audio.to_wav16k(_wav_bytes(seconds=0.5, rate=8000))
try:
    check("an 8 kHz source is resampled to 16 kHz", abs(dur8 - 0.5) < 0.05, f" ({dur8:.2f}s)")
    with wave.open(str(wav8), "rb") as wf:
        check("resampled output is 16 kHz", wf.getframerate() == 16000)
finally:
    stt_audio.cleanup(wav8)

wav_st, _ = stt_audio.to_wav16k(_wav_bytes(seconds=0.4, channels=2))
try:
    with wave.open(str(wav_st), "rb") as wf:
        check("stereo is downmixed to mono", wf.getnchannels() == 1)
finally:
    stt_audio.cleanup(wav_st)

check("the duration cap is enforced",
      _code(lambda: stt_audio.to_wav16k(_wav_bytes(seconds=3.0), max_seconds=1)) == "STT_DECODE_FAILED")
oversize = b"\0" * (stt_audio.MAX_BYTES + 1)
check("the size cap is enforced",
      _code(lambda: stt_audio.to_wav16k(oversize)) == "STT_DECODE_FAILED")

# The Telegram format: Ogg/Opus. Encode with libsndfile, decode back, and check
# that the pipeline still yields a valid 16 kHz WAV (the transcript equality was
# verified against whisper.cpp in the spike).
import io  # noqa: E402

import soundfile as sf  # noqa: E402

opus_path = Path(tempfile.mkstemp(suffix=".ogg")[1])
try:
    data, rate = sf.read(io.BytesIO(raw_wav), dtype="int16")
    sf.write(opus_path, data, rate, format="OGG", subtype="OPUS")
    opus_bytes = opus_path.read_bytes()
    check("opus encoding works (the Telegram format)", opus_bytes[:4] == b"OggS")
    wav_from_opus, dur_opus = stt_audio.to_wav16k(opus_bytes)
    try:
        check("Ogg/Opus decodes to a 16 kHz WAV",
              wav_from_opus.is_file() and abs(dur_opus - 1.0) < 0.1, f" ({dur_opus:.2f}s)")
    finally:
        stt_audio.cleanup(wav_from_opus)
finally:
    stt_audio.cleanup(opus_path)

print()
print(f"SUMMARY stt_audio: {passed} passed, {failed} failed, {skipped} skipped")
sys.exit(0 if failed == 0 else 1)
