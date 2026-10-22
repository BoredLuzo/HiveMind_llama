"""Audio → 16 kHz mono WAV for speech-to-text.

Two sources, one format (docs/plan_1.4_voice.md §3.3):

* **Telegram voice notes** are Ogg/**Opus** - whisper.cpp and parakeet.cpp cannot
  read Opus without ffmpeg (verified: ``miniaudio`` has no Opus decoder, and
  ``whisper-server --convert`` exits when ffmpeg is missing).
* **The UI push-to-talk button** encodes WAV in the browser, so it arrives as
  WAV already.

``soundfile`` (a ~2 MB wheel that bundles libsndfile) does the Opus decode; the
measured cost is 15 ms for 17.6 s of audio and the transcript from the decoded
file is identical to the one from the original WAV. No ffmpeg, no webm decoder:
webm never reaches this module because the browser sends WAV.

``soundfile`` and ``numpy`` live in the optional ``voice`` dependency group. When
they are missing every entry point raises ``SttError("STT_NO_AUDIO_LIB")`` with
the install command - the import never crashes and the engine keeps running.
"""
from __future__ import annotations

import io
import logging
import os
import secrets
import tempfile
import wave
from pathlib import Path

from core.stt_engine import SttError

logger = logging.getLogger(__name__)

#: Telegram caps bot downloads at 20 MB; anything bigger is refused before decode.
MAX_BYTES = 20 * 1024 * 1024
#: Hard cap on decoded duration (default, overridable by stt_max_seconds).
DEFAULT_MAX_SECONDS = 300
SAMPLE_RATE = 16000

_INSTALL_HINT = ("audio support is not installed - run: "
                 "pip install -r requirements-voice.txt")


def _load_audio_lib():
    try:
        import numpy as np
        import soundfile as sf
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise SttError("STT_NO_AUDIO_LIB", _INSTALL_HINT) from exc
    return np, sf


def to_wav16k(data: bytes | str | Path, *, max_seconds: int = DEFAULT_MAX_SECONDS,
              tmp_dir: str | None = None) -> tuple[Path, float]:
    """Decode any libsndfile-readable audio to 16 kHz mono 16-bit WAV.

    Returns ``(wav_path, duration_seconds)``. The caller owns the temp file and
    must delete it (``DELETE_AFTER_TMPDIR``); nothing is written into the
    workspace.
    """
    np, sf = _load_audio_lib()

    raw: bytes
    if isinstance(data, (str, Path)):
        p = Path(data)
        if not p.is_file():
            raise SttError("STT_DECODE_FAILED", f"audio file not found: {p.name}")
        if p.stat().st_size > MAX_BYTES:
            raise SttError("STT_DECODE_FAILED",
                           f"audio is larger than {MAX_BYTES // (1024 * 1024)} MB")
        raw = p.read_bytes()
    else:
        raw = data or b""
        if len(raw) > MAX_BYTES:
            raise SttError("STT_DECODE_FAILED",
                           f"audio is larger than {MAX_BYTES // (1024 * 1024)} MB")
    if not raw:
        raise SttError("STT_DECODE_FAILED", "empty audio")

    try:
        # libsndfile reads from a memory buffer: no temp file for the input.
        samples, rate = sf.read(io.BytesIO(raw), dtype="int16", always_2d=False)
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        raise SttError("STT_DECODE_FAILED",
                       f"unsupported or broken audio ({type(exc).__name__})") from exc

    if getattr(samples, "ndim", 1) > 1:
        samples = samples.mean(axis=1).astype("int16")
    n = int(len(samples))
    if rate <= 0 or n == 0:
        raise SttError("STT_DECODE_FAILED", "audio contains no samples")
    duration = n / float(rate)
    if duration > float(max_seconds) + 0.5:
        raise SttError("STT_DECODE_FAILED",
                       f"audio is {duration:.0f}s long - the limit is {int(max_seconds)}s")

    if int(rate) != SAMPLE_RATE:
        target = int(round(n * SAMPLE_RATE / float(rate)))
        idx = np.linspace(0.0, float(n - 1), target)
        samples = np.interp(idx, np.arange(n, dtype="float64"),
                            samples.astype("float64")).astype("int16")
        duration = target / float(SAMPLE_RATE)

    # FD-CLOSE (2026-10-17, live finding): mkstemp keeps the file OPEN for
    # writing. The CLI child then cannot read it - miniaudio reports "failed to
    # read audio data" and every /stt request came back as STT_EMPTY. Soundfile
    # reads the same file happily, so only a real CLI run exposes this.
    # BOUNDED (2026-10-19, same trap as write_file): tempfile.mkstemp retries
    # PermissionError up to TMP_MAX - a locked-down %TEMP% hung the STT request.
    # TMP-DIR DEFAULT (2026-10-22, live): the loop used to run with tmp_dir=None
    # whenever the caller passed no explicit dir - Path(None) raised TypeError
    # and EVERY voice note 500ed before the engine ever saw audio.
    tmp_dir = tmp_dir or tempfile.gettempdir()
    _fd = _tmp_name = None
    for _ in range(16):
        _cand = Path(tmp_dir) / f"hivemind_stt_{secrets.token_hex(8)}.wav"
        try:
            _fd = os.open(str(_cand), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            _tmp_name = str(_cand)
            break
        except FileExistsError:
            continue
    if _fd is None:
        raise OSError("could not create the STT temp file")
    os.close(_fd)
    out = Path(_tmp_name)
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(samples.tobytes())
    logger.info("[STT] decoded %.1fs audio -> %s (%.0f KB)",
                duration, out.name, out.stat().st_size / 1024)
    return out, duration


def cleanup(path: str | Path | None) -> None:
    """Delete a temp WAV produced by :func:`to_wav16k` (never raises)."""
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass
