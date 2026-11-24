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
import re
import wave

import numpy as np

logger = logging.getLogger(__name__)

FRAME = 1024          # WSOLA frame, ~42 ms at 24 kHz
TOLERANCE = 160       # +/- alignment search window in samples
MIN_SPEED = 0.8
MAX_SPEED = 1.5
#: Hard per-chunk cap so one engine call stays fast and bounded in memory.
MAX_CHARS = 600
#: Multi-chunk total cap for long answers (e.g. Telegram voice notes) so full
#: replies are spoken without mid-sentence cutoff while staying under timeouts.
MAX_TOTAL_CHARS = 3600

_FENCE_RE = re.compile(r"```[\s\S]*?```")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]+\)")
_BARE_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_HRULE_RE = re.compile(r"(?m)^\s*[-*_]{3,}\s*$")
_TABLE_SEP_RE = re.compile(r"(?m)^[ \t]*\|?[ \t:]*-{2,}[ \t:]*(?:\|[ \t:]*-{2,}[ \t:]*)+\|?[ \t]*$")
_BULLET_RE = re.compile(r"(?m)^\s*(?:[-*•]+|\d+[.)])\s+")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_EMPH_RE = re.compile(r"(\*\*|__|\*|~~)")


def clean_for_speech(text: str) -> str:
    """Strip markdown syntax, code fences, tables, and raw URLs so TTS sounds natural."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    s = _FENCE_RE.sub(" ", raw)
    s = _MD_LINK_RE.sub(r"\1", s)
    s = _BARE_URL_RE.sub("", s)
    s = _HRULE_RE.sub(" ", s)
    s = _TABLE_SEP_RE.sub(" ", s)
    # Turn headings into full sentences so TTS pauses after titles.
    s = _HEADING_RE.sub(lambda m: m.group(1).rstrip(".!?:") + ".", s)
    s = _BULLET_RE.sub("", s)
    s = _INLINE_CODE_RE.sub(r"\1", s)
    s = _EMPH_RE.sub("", s)
    # Join multiple paragraphs cleanly with terminal punctuation so TTS pauses.
    raw_lines = []
    for ln in s.splitlines():
        if "|" in ln and ln.count("|") >= 2:
            cells = [c.strip() for c in ln.strip().strip("|").split("|") if c.strip()]
            ln = ", ".join(cells)
        ln = re.sub(r"\s+", " ", ln).strip()
        if ln:
            raw_lines.append(ln)
    paras = raw_lines
    if len(paras) > 1:
        paras = [ln if ln[-1] in ".!?;:," else (ln + ".") for ln in paras]
    return " ".join(paras).strip()


def _cap_at_sentence(text: str, limit: int) -> str:
    """Trim *text* to <= *limit* chars, preferring a complete sentence boundary."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    best = -1
    for sep in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        idx = head.rfind(sep)
        if idx > best:
            best = idx
    if best >= limit // 3:
        return head[:best + 1].strip()
    cut = head.rsplit(" ", 1)[0].strip()
    return (cut or head.strip())


def split_speech_chunks(
    text: str,
    *,
    chunk_chars: int = MAX_CHARS,
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> list[str]:
    """Split *text* into sentence-aligned chunks <= *chunk_chars* up to *max_total_chars*."""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return []
    if max_total_chars > 0 and len(s) > max_total_chars:
        s = _cap_at_sentence(s, max_total_chars)
    if len(s) <= chunk_chars:
        return [s]
    sentences = [p.strip() for p in re.split(r"(?<=[.!?])\s+", s) if p.strip()]
    chunks: list[str] = []
    cur = ""
    for sent in sentences:
        if len(sent) > chunk_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            rem = sent
            while len(rem) > chunk_chars:
                part = rem[:chunk_chars]
                sp = part.rfind(" ")
                cut = sp if sp >= chunk_chars // 3 else chunk_chars
                chunks.append(rem[:cut].strip())
                rem = rem[cut:].strip()
            if rem:
                cur = rem
            continue
        if cur and len(cur) + 1 + len(sent) > chunk_chars:
            chunks.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip() if cur else sent
    if cur:
        chunks.append(cur)
    return [c for c in chunks if c]


def concat_wavs(wav_list: list[bytes], pause_s: float = 0.16) -> bytes:
    """Join multiple 16-bit mono WAV clips with a short silence gap between them."""
    if not wav_list:
        raise ValueError("concat_wavs requires at least one WAV clip")
    if len(wav_list) == 1:
        return wav_list[0]
    parts: list[np.ndarray] = []
    sr_out = 24000
    for idx, wb in enumerate(wav_list):
        arr, sr = wav_to_samples(wb)
        if idx == 0:
            sr_out = sr
        elif sr != sr_out and arr.size > 1:
            # Linear resample only if a chunk unexpectedly differs in sample rate.
            new_len = max(1, int(round(arr.size * (sr_out / float(sr)))))
            arr = np.interp(
                np.linspace(0, arr.size - 1, new_len),
                np.arange(arr.size),
                arr,
            )
        if idx > 0 and pause_s > 0:
            parts.append(np.zeros(int(round(sr_out * pause_s)), dtype="float64"))
        parts.append(arr)
    return samples_to_wav(np.concatenate(parts), sr_out)


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
    if int(sr) <= 0:
        raise ValueError(f"sample rate must be positive (got {sr})")
    finite = np.nan_to_num(
        np.asarray(samples, dtype="float64"),
        nan=0.0,
        posinf=32767.0,
        neginf=-32768.0,
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(np.clip(finite, -32768, 32767).astype("<i2").tobytes())
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
