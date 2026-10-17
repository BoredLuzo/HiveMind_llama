"""Speech-to-text engine contract (1.4, English only).

No binaries and no models are needed: the binary/model lookups and the process
call are stubbed, so this suite runs on every machine and pins the decisions from
docs/plan_1.4_voice.md §3:

  (a) the model tiers carry the MEASURED numbers (WER/RTFx/RAM) - if a future
      edit changes a number it must be a measurement, not a guess;
  (b) the default tier follows RAM (parakeet q4_k >= 8 GB, else whisper base);
  (c) 1.4 is English only: an off-list language degrades to "en", never passes
      through to a model that would translate on its own;
  (d) argv is a list, never a shell string, and parakeet gets no -l (it has no
      language flag);
  (e) every failure is an SttError with a stable code (never a silent except);
  (f) the transcript parser drops backend noise and detects blank audio.

Run: python tests/test_stt_engine.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import stt_engine  # noqa: E402

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


# ── (a) measured tiers ──────────────────────────────────────────────────────
E = stt_engine
check("default tier exists", "parakeet-q4k" in E.MODELS)
check("parakeet default is the measured one",
      E.MODELS["parakeet-q4k"].wer == 0.0357 and E.MODELS["parakeet-q4k"].rtfx == 18.7)
check("parakeet default RAM figure", E.MODELS["parakeet-q4k"].ram_mb == 552)
check("base tier matches the measurement",
      E.MODELS["base"].wer == 0.0991 and E.MODELS["base"].ram_mb == 317)
check("small tier matches the measurement",
      E.MODELS["small"].wer == 0.0730)
check("tiny tier matches the measurement", E.MODELS["tiny"].wer == 0.1243)
check("parakeet uses parakeet-cli", E.MODELS["parakeet-q4k"].engine == "parakeet-cli")
check("whisper tiers use whisper-cli",
      all(E.MODELS[k].engine == "whisper-cli" for k in ("base", "tiny", "small")))
check("every tier names a HF repo",
      all(m.repo for m in E.MODELS.values()))

# ── (b) RAM defaults ────────────────────────────────────────────────────────
_ram = E.total_ram_gb()
check("RAM probe returns a number", isinstance(_ram, float))
_want = "parakeet-q4k" if _ram >= 8 else "base"
check("auto default follows RAM", E.resolve_model({}) == _want, f" ({_ram} GB -> {_want})")
check("an explicit tier wins", E.resolve_model({"stt_model": "tiny"}) == "tiny")
check("an unknown tier falls back to the default",
      E.resolve_model({"stt_model": "nonsense"}) == _want)
check("non-string tiers do not crash", E.resolve_model({"stt_model": None}) == _want)

# ── (c) English only ────────────────────────────────────────────────────────
check("languages shipped is en only", E.LANGUAGES == ("en",))
check("default language is en", E.resolve_language({}) == "en")
check("a two-letter code passes through as experimental",
      E.resolve_language({"stt_language": "de"}) == "de")
check("garbage is not a language and degrades to en",
      E.resolve_language({"stt_language": "auto"}) == "en"
      and E.resolve_language({"stt_language": "klingon"}) == "en")
check("empty language means en", E.resolve_language({"stt_language": ""}) == "en")
check("translation is off by default", E.resolve_translate({}) is False)
check("translation is opt-in", E.resolve_translate({"stt_translate": True}) is True)

# ── threads / timeout ───────────────────────────────────────────────────────
_cpu = os.cpu_count() or 4
check("default threads are min(4, cpu//2)",
      E.resolve_threads({}) == max(1, min(4, _cpu // 2)), f" ({E.resolve_threads({})})")
check("explicit threads are honoured", E.resolve_threads({"stt_threads": 6}) == 6)
check("threads are clamped", E.resolve_threads({"stt_threads": 999}) == 16)
check("junk threads fall back", E.resolve_threads({"stt_threads": "x"}) >= 1)
check("default timeout", E.resolve_timeout({}) == 120.0)
check("timeout is clamped", E.resolve_timeout({"stt_timeout_s": 99999}) == 900.0)
check("junk timeout falls back", E.resolve_timeout({"stt_timeout_s": "x"}) == 120.0)

# ── (d) argv shape ──────────────────────────────────────────────────────────
_p = Path("C:/x/whisper-cli.exe") if os.name == "nt" else Path("/x/whisper-cli")
_w = Path("C:/x/a.wav") if os.name == "nt" else Path("/x/a.wav")
_m = Path("C:/x/m.bin") if os.name == "nt" else Path("/x/m.bin")
argv_pk = E.build_argv("parakeet-cli", _p, _m, _w, threads=8)
argv_wh = E.build_argv("whisper-cli", _p, _m, _w, threads=8, language="en")
check("parakeet argv has no -l", "-l" not in argv_pk, f" ({argv_pk[1:]})")
check("whisper argv pins the language", "-l" in argv_wh and "en" in argv_wh)
check("whisper argv disables timestamps", "-nt" in argv_wh)
check("argv is a list of strings",
      all(isinstance(a, str) for a in argv_pk + argv_wh))
check("translate adds the whisper flag",
      "--translate" in E.build_argv("whisper-cli", _p, _m, _w, threads=4, translate=True)
      and "--translate" not in E.build_argv("whisper-cli", _p, _m, _w, threads=4))
check("translate never reaches parakeet",
      "--translate" not in E.build_argv("parakeet-cli", _p, _m, _w, threads=4, translate=True))

# ── (e) failure codes ───────────────────────────────────────────────────────
def _code(fn):
    try:
        fn()
    except E.SttError as exc:
        return exc.code
    return "NO-ERROR"


check("disabled -> STT_DISABLED", _code(lambda: E.transcribe("x.wav", settings={})) == "STT_DISABLED")
_empty_root = Path(tempfile.mkdtemp())
with mock.patch.object(E, "stt_root", return_value=_empty_root):
    check("enabled without binaries -> STT_NO_ENGINE",
          _code(lambda: E.transcribe("x.wav", settings={"stt_enabled": True,
                                                        "stt_model": "tiny"})) == "STT_NO_ENGINE")

import wave as _wave  # noqa: E402

_fake_wav = Path(tempfile.mkstemp(suffix=".wav")[1])
with _wave.open(str(_fake_wav), "wb") as _wf:
    _wf.setnchannels(1)
    _wf.setsampwidth(2)
    _wf.setframerate(16000)
    _wf.writeframes(b"\x00\x00" * 160)
with mock.patch.object(E, "binary_path", return_value=Path(sys.executable)), \
     mock.patch.object(E, "model_path", return_value=Path(sys.executable)):
    with mock.patch.object(E.subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 0, " hello\nthere\n", "")):
        check("a good run returns the parsed transcript",
              E.transcribe(_fake_wav, settings={"stt_enabled": True}) == "hello there")
    with mock.patch.object(E.subprocess, "run",
                           side_effect=subprocess.TimeoutExpired("cmd", 1)):
        check("a timeout -> STT_TIMEOUT",
              _code(lambda: E.transcribe(_fake_wav, settings={"stt_enabled": True})) == "STT_TIMEOUT")
    with mock.patch.object(E.subprocess, "run",
                           return_value=subprocess.CompletedProcess([], 1, "", "boom")):
        check("a crash -> STT_FAILED",
              _code(lambda: E.transcribe(_fake_wav, settings={"stt_enabled": True})) == "STT_FAILED")

# ── (f) parsing ─────────────────────────────────────────────────────────────
raw = ("load_backend: loaded CPU backend\n"
       "whisper_print_timings: total time = 1 s\n"
       " Hello there.\n"
       " How are you?\n")
check("backend noise is stripped", E.parse_transcript(raw) == "Hello there. How are you?")
check("empty stdout is empty", E.parse_transcript("") == "")
check("blank audio is detected", E.is_blank_transcript("[BLANK_AUDIO]"))
check("real text is not blank", not E.is_blank_transcript("Hello there."))

# ── status contract / no shell ──────────────────────────────────────────────
st = E.status({"stt_enabled": True, "stt_model": "tiny"})
for key in ("enabled", "model", "model_installed", "engine_installed", "language",
            "threads", "preload", "warm", "installed_models", "available_models"):
    check(f"status exposes {key}", key in st)
src = (Path(__file__).resolve().parent.parent / "core" / "stt_engine.py").read_text(encoding="utf-8")
check("no shell=True anywhere", "shell=True" not in src)
check("preload has no server for parakeet",
      E.preload({"stt_enabled": True, "stt_model": "parakeet-q4k"}) is False)
E.stop_warm()


check("a non-WAV file is refused with a clear code",
      _code(lambda: E.transcribe(Path(tempfile.mkstemp(suffix=".ogg")[1]),
                                 settings={"stt_enabled": True, "stt_model": "tiny"})) == "STT_DECODE_FAILED")
check("is_wav_file accepts a real WAV", E.is_wav_file(_fake_wav))
check("is_wav_file rejects garbage",
      not E.is_wav_file(Path(tempfile.mkstemp(suffix=".bin")[1])))

print()
print(f"SUMMARY stt_engine: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
