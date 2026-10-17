"""TTS audio post-processing: WSOLA speed (pitch preserved) + Ogg/Opus.

Owner decisions (2026-10-17): voice cosette, speed 1.08. Pocket TTS has **no**
speed parameter (verified in the CLI flags and in TTSModel.generate_audio), so
the factor is a HiveMind post-process.

The interesting property is measurable without ears:
  * duration = input / speed (a truncated tail would show up as "shorter than
    expected" - that was the bug in the first prototype),
  * pitch (F0) unchanged, unlike a resample which shifts it,
  * the syllable peaks of the original appear at t/speed within a few ms.

Run: python tests/test_tts_audio.py
"""
import io
import sys
import wave

import numpy as np

sys.path.insert(0, str((__import__("pathlib").Path(__file__).resolve().parent.parent)))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core import tts_audio  # noqa: E402

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


def synth_wav(seconds=3.0, sr=24000, f0=95.0) -> bytes:
    """A voiced-ish signal with syllable-like amplitude modulation."""
    n = int(seconds * sr)
    t = np.arange(n) / sr
    env = 0.5 + 0.5 * np.abs(np.sin(2 * np.pi * 4.0 * t))      # ~4 syllables/s
    sig = env * np.sin(2 * np.pi * f0 * t) + 0.15 * np.sin(2 * np.pi * 2 * f0 * t)
    return tts_audio.samples_to_wav(sig * 12000, sr)


def f0_of(x, sr):
    seg = x[: min(len(x), sr)]
    seg = seg - seg.mean()
    ac = np.correlate(seg, seg, mode="full")[len(seg) - 1:]
    lo, hi = sr // 400, sr // 70
    lag = lo + int(np.argmax(ac[lo:hi]))
    return sr / lag


def peaks_of(x, sr, win_s=0.02):
    n = max(1, int(sr * win_s))
    env = np.sqrt(np.mean(x[: len(x) // n * n].reshape(-1, n) ** 2, axis=1))
    thr = 0.3 * env.std()
    return np.array([i * win_s for i in range(1, env.size - 1)
                     if env[i] > thr and env[i] >= env[i - 1] and env[i] > env[i + 1]])


# ── caps / helpers ─────────────────────────────────────────────────────────
check("speed is clamped low", tts_audio.clamp_speed(0.1) == 0.8)
check("speed is clamped high", tts_audio.clamp_speed(9) == 1.5)
check("junk speed falls back to 1.0", tts_audio.clamp_speed("fast") == 1.0)
check("1.08 survives the clamp", tts_audio.clamp_speed(1.08) == 1.08)
check("char cap exists", tts_audio.MAX_CHARS == 600)

wav = synth_wav(3.0)
check("the helper writes a RIFF/WAVE", tts_audio.looks_like_wav(wav))
check("not-a-wav is detected", not tts_audio.looks_like_wav(b"OggS...."))
check("duration is measured", abs(tts_audio.duration_seconds(wav) - 3.0) < 0.05)

samples, sr = tts_audio.wav_to_samples(wav)
check("samples are mono float", samples.ndim == 1 and sr == 24000)

# ── speed 1.0 is a no-op ───────────────────────────────────────────────────
same = tts_audio.stretch_wav(wav, 1.0)
check("speed 1.0 returns the same bytes", same == wav)

# ── the actual stretch ─────────────────────────────────────────────────────
for speed in (1.08, 1.2):
    out = tts_audio.stretch_wav(wav, speed)
    x, sr2 = tts_audio.wav_to_samples(out)
    dur = len(x) / sr2
    expect = 3.0 / speed
    check(f"{speed}x: duration is input/speed (not a cut tail)",
          abs(dur - expect) < 0.08, f" ({dur:.2f}s vs {expect:.2f}s)")
    check(f"{speed}x: pitch preserved",
          abs(f0_of(x, sr2) - f0_of(samples, sr)) < 3.0,
          f" ({f0_of(x, sr2):.1f} vs {f0_of(samples, sr):.1f} Hz)")
    p0, p1 = peaks_of(samples, sr), peaks_of(x, sr2)
    if p0.size and p1.size:
        devs = [float(np.min(np.abs(p1 - t / speed))) for t in p0]
        med = float(np.median(devs))
        check(f"{speed}x: syllables land at t/speed", med < 0.05,
              f" (median {med * 1000:.1f} ms)")
    else:
        check(f"{speed}x: syllables land at t/speed", False, " (no peaks)")

# a resample shows the difference: pitch must move, otherwise the test is blind
resampled = np.interp(np.arange(0, len(samples), 1.08),
                      np.arange(len(samples)), samples)
check("control: a resample DOES shift the pitch",
      abs(f0_of(resampled, sr) - f0_of(samples, sr)) > 3.0,
      f" ({f0_of(resampled, sr):.1f} Hz)")

# ── ogg/opus for Telegram ──────────────────────────────────────────────────
try:
    import soundfile  # noqa: F401
    opus = tts_audio.to_ogg_opus(wav, 1.0)
    check("opus starts with OggS", opus[:4] == b"OggS")
    check("opus is much smaller than the wav", len(opus) < len(wav) / 3,
          f" ({len(opus)} vs {len(wav)} bytes)")
except ImportError:
    print("  SKIP opus (soundfile not installed)")

print()
print(f"SUMMARY tts_audio: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
