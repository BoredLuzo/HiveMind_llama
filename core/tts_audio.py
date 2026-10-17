"""Audio post-processing for text-to-speech.

Pocket TTS speaks at its own rate and has **no** speed parameter (verified in the
CLI flags and in ``TTSModel.generate_audio``), so "speak a bit faster" is a
post-process here: WSOLA (waveform-similarity overlap-add) shortens the duration
while keeping the pitch. A naive resample would shift the pitch (chipmunk).

Measured on the chosen voice (cosette, 24 kHz): 1.08x -> 5.48 s instead of
5.96 s, F0 unchanged at ~94.9 Hz, and the syllable peaks of the original land at
``t / speed`` within ~14 ms - i.e. the same speech, compressed, nothing cut off.
(The first prototype moved the read pointer by ``hop / speed``, which truncated
the tail: shorter but not faster. The synthesis hop is fixed, the analysis hop is
``hop * speed``.)

Also does the Telegram half: WAV -> Ogg/Opus (``sendVoice`` requires Opus) via
soundfile, measured at 11x smaller with an identical transcript on the STT side.
"""
from __future__ import annotations

import io
import logging
import wave

import numpy as np

logger = logging.getLogger(__name__)

FRAME = 1024          # WSOLA frame, ~42 ms at 24 kHz
TOLERANCE = 160       # +/- alignment search window in samples
MIN_SPEED = 0.8
MAX_SPEED = 1.5
#: Hard cap so one request cannot synthesise a whole book (Telegram allows
#: longer, but a 600-char cap keeps latency and memory predictable).
MAX_CHARS = 600


#: Target peak after normalisation. Pocket TTS returns speech at about -15.5
#: dBFS peak / -30 dBFS RMS (measured 2026-10-18), i.e. 12-15 dB quieter than a
#: normal speech file - audible as "why is it so quiet" on any speaker. Raising
#: the peak to -1.5 dBFS leaves headroom for the resampling in the browser and
#: makes the browser volume slider meaningful.
TARGET_PEAK_DBFS = -1.5


def normalize(samples: np.ndarray, target_dbfs: float = TARGET_PEAK_DBFS) -> np.ndarray:
    """Peak-normalise to ``target_dbfs`` (silence stays untouched)."""
    x = np.asarray(samples, dtype="float64")
    if x.size == 0:
        return x
    peak = float(np.max(np.abs(x)))
    if peak <= 1.0:                       # already a -1..1 float signal
        peak *= 32768.0
        x = x * 32768.0
    if peak <= 1e-3:                      # silence / near-silence: no gain
        return x
    target = 32768.0 * (10.0 ** (target_dbfs / 20.0))
    return np.clip(x * (target / peak), -32768, 32767)



def clamp_speed(speed) -> float:
    try:
        val = float(speed)
    except (TypeError, ValueError):
        return 1.0
    return max(MIN_SPEED, min(MAX_SPEED, val))


def wav_to_samples(data: bytes | str) -> tuple[np.ndarray, int]:
    """(mono float64 samples, sample rate) from WAV bytes or a path."""
    if isinstance(data, (bytes, bytearray)):
        src = io.BytesIO(bytes(data))
    else:
        src = str(data)
    with wave.open(src, "rb") as w:          # type: ignore[arg-type]
        sr, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw != 2:
        raise ValueError(f"expected 16-bit PCM, got {sw * 8} bit")
    arr = np.frombuffer(raw, dtype="<i2").astype("float64")
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    return arr, sr


def samples_to_wav(samples: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(np.clip(samples, -32768, 32767).astype("<i2").tobytes())
    return buf.getvalue()


def wsola(x: np.ndarray, speed: float) -> np.ndarray:
    """Time-stretch ``x`` by ``speed`` (1.08 = 8 % shorter), pitch preserved."""
    x = np.asarray(x, dtype="float64")
    speed = clamp_speed(speed)
    if abs(speed - 1.0) < 1e-3 or x.size < FRAME * 4:
        return x.copy()

    hop_out = FRAME // 2                       # synthesis hop (fixed)
    hop_in = max(1, int(round(hop_out * speed)))   # analysis hop: faster = more
    win = np.hanning(FRAME)
    n_out = int(np.ceil(x.size / speed)) + FRAME
    out = np.zeros(n_out)
    wsum = np.zeros(n_out)

    pos = 0.0
    o = 0
    prev_tail = None
    while pos + FRAME + TOLERANCE <= x.size and o + FRAME <= n_out:
        base = int(round(pos))
        shift = 0
        if prev_tail is not None and base - TOLERANCE >= 0:
            seg = x[base - TOLERANCE: base + FRAME + TOLERANCE]
            corr = np.correlate(seg, prev_tail, mode="valid")
            if corr.size:
                shift = int(np.argmax(corr)) - TOLERANCE
        start = base + shift
        if start < 0 or start + FRAME > x.size:
            start = base
        chunk = x[start:start + FRAME]
        if chunk.size < FRAME:
            break
        out[o:o + FRAME] += chunk * win
        wsum[o:o + FRAME] += win
        prev_tail = chunk[hop_out:].copy()
        o += hop_out
        pos += hop_in

    out = out[:o]
    wsum = wsum[:o]
    loud = wsum > 1e-6
    out[loud] /= wsum[loud]
    return out


def stretch_wav(wav_bytes: bytes, speed: float, normalize_out: bool = True) -> bytes:
    """Apply the speed factor to a WAV and return WAV bytes (16-bit mono).

    ``normalize_out`` lifts the peak to TARGET_PEAK_DBFS: the sidecar's own level
    is ~14 dB below a normal speech file.
    """
    speed = clamp_speed(speed)
    samples, sr = wav_to_samples(wav_bytes)
    out = samples if abs(speed - 1.0) < 1e-3 else wsola(samples, speed)
    if normalize_out:
        out = normalize(out)
    return samples_to_wav(out, sr)


def to_ogg_opus(wav_bytes: bytes, speed: float = 1.0) -> bytes:
    """WAV -> Ogg/Opus (what Telegram's sendVoice needs), speed applied first."""
    import soundfile as sf

    data = stretch_wav(wav_bytes, speed)
    samples, sr = wav_to_samples(data)
    audio = (samples / 32768.0).astype("float32")
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="OGG", subtype="OPUS")
    return buf.getvalue()


def duration_seconds(wav_bytes: bytes) -> float:
    samples, sr = wav_to_samples(wav_bytes)
    return round(samples.size / float(sr), 2) if sr else 0.0


def looks_like_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"
