"""Voice (STT/TTS) and Document-Extraction API-Router."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from settings import load_settings

_logger = logging.getLogger("hivemind.server")

router = APIRouter(tags=["Voice"])

# ── Speech-to-text (1.4) ────────────────────────────────────────
# English only in 1.4 (owner decision, docs/plan_1.4_voice.md). The audio is
# the RAW REQUEST BODY, not a multipart upload: that keeps python-multipart out
# of the dependency list and lets the browser POST a Blob directly.
# Formats: anything libsndfile reads (WAV from the UI, Ogg/Opus from Telegram).
# The Telegram path calls core.stt_engine directly instead of going over HTTP.

@router.get("/stt/status")
async def stt_status():
    """What the Voice card shows: engine/model presence, tier, defaults."""
    try:
        from core import stt_engine
        return await asyncio.to_thread(stt_engine.status, load_settings())
    except (ImportError, OSError, ValueError, TypeError, RuntimeError, KeyError) as exc:
        return JSONResponse({"enabled": False, "error": str(exc)[:200]}, status_code=200)


# ── Text-to-speech (1.4) ────────────────────────────────────────
# Opt-in: nothing starts until tts_enabled is on. The sidecar is Pocket TTS in an
# isolated uv environment (uvx), so PyTorch never enters HiveMind's venv.

@router.get("/tts/status")
async def tts_status():
    """Voice card status: enabled, sidecar running, voice, speed, uv present."""
    try:
        from core import tts_engine
        return await asyncio.to_thread(tts_engine.status, load_settings())
    except (ImportError, OSError, ValueError, TypeError, RuntimeError, KeyError) as exc:
        return JSONResponse({"enabled": False, "error": str(exc)[:200]}, status_code=200)


@router.get("/tts/voices")
async def tts_voices(backend: str = ""):
    """Voices for the active (or requested) TTS backend (`pocket` | `woof`)."""
    from core import tts_engine
    cfg = load_settings()
    b = (backend or "").strip().lower() or tts_engine.resolve_backend(cfg)
    if b not in ("pocket", "woof"):
        b = tts_engine.DEFAULT_BACKEND
    voices, default_v, attr = tts_engine.voices_for(b)
    return JSONResponse({
        "backend": b,
        "backends": list(tts_engine.BACKENDS),
        "default": default_v,
        "max_chars": tts_engine.tts_audio.MAX_CHARS,
        "attribution": attr,
        "voices": [{"key": k, "label": label, "note": note, "license": lic,
                    "source": src}
                   for k, label, note, lic, src in voices],
    })


@router.post("/tts/preload")
async def tts_preload():
    """Start or warm the configured TTS backend ahead of the first sentence.

    Called by the gateway when a phone run starts and by nothing else: the UI
    warms on page load, a phone run has no page. Returns immediately when TTS is
    off; the actual start runs in a thread so this endpoint never blocks.
    """
    try:
        from core import tts_engine
        settings = load_settings()
    except (ImportError, OSError, ValueError) as exc:
        return JSONResponse({"warm": False, "error": str(exc)[:200]}, status_code=200)
    if not settings.get("tts_enabled", False):
        return JSONResponse({"warm": False, "reason": "TTS_DISABLED"}, status_code=200)
    warm = await asyncio.to_thread(tts_engine.preload, settings)
    running = await asyncio.to_thread(tts_engine.is_running, settings)
    return JSONResponse({"warm": bool(warm),
                         "running": bool(running)}, status_code=200)


@router.post("/tts/fetch")
async def tts_fetch():
    """Start deploy/fetch_woof.py in the background (ConwayResearch/Underdog-Woof)."""
    import subprocess
    import sys as _sys

    from core import tts_engine, tts_woof

    if tts_woof.fetch_state().get("state") == "running":
        return JSONResponse(tts_engine.TtsError(
            "TTS_BUSY", "a voice model download is already running").as_dict(), status_code=409)
    script = Path(__file__).resolve().parent.parent / "deploy" / "fetch_woof.py"
    if not script.is_file():
        return JSONResponse(tts_engine.TtsError(
            "TTS_FAILED", "deploy/fetch_woof.py is missing").as_dict(), status_code=400)
    try:
        subprocess.Popen([_sys.executable, str(script)],
                         cwd=str(script.parent.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    except OSError as exc:
        return JSONResponse(tts_engine.TtsError(
            "TTS_FAILED", f"could not start the download: {exc}").as_dict(), status_code=500)
    _logger.info("[TTS-WOOF] download started for ConwayResearch/Underdog-Woof")
    return JSONResponse({"started": True, "fetch": tts_woof.fetch_state()}, status_code=202)


@router.post("/tts")
async def tts_speak(request: Request):
    """{"text": ..., "voice"?: ..., "speed"?: ..., "format"?: "wav"|"opus"} -> audio."""
    from core import tts_audio, tts_engine

    settings = load_settings()
    if not settings.get("tts_enabled", False):
        return JSONResponse(tts_engine.TtsError(
            "TTS_DISABLED", "text-to-speech is disabled in the settings").as_dict(),
            status_code=409)
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    if not isinstance(body, dict):
        body = {}
    text = str(body.get("text") or "").strip()
    if not text:
        return JSONResponse(tts_engine.TtsError("TTS_EMPTY", "nothing to say").as_dict(),
                            status_code=400)
    fmt = str(body.get("format") or "wav").lower()
    try:
        # THREAD (audit 2026-10-17): the first request starts the sidecar (up to
        # ~20 s) - on the event loop that froze the whole UI, status polling
        # included. Every blocking part moves into a worker thread.
        res = await asyncio.to_thread(
            tts_engine.synthesize, text, settings,
            voice=str(body.get("voice") or ""), speed=body.get("speed"))
        if fmt == "opus":
            payload = await asyncio.to_thread(tts_audio.to_ogg_opus, res["wav"], 1.0)
            media = "audio/ogg"
        else:
            payload = res["wav"]
            media = "audio/wav"
    except tts_engine.TtsError as exc:
        _logger.warning("[TTS] %s", exc)
        return JSONResponse(exc.as_dict(), status_code=400)
    except (ValueError, OSError, RuntimeError) as exc:
        _logger.warning("[TTS] audio post-processing failed: %s", exc)
        return JSONResponse(tts_engine.TtsError(
            "TTS_FAILED", f"audio post-processing failed: {exc}").as_dict(),
            status_code=500)
    _logger.info("[TTS] %d chars -> %.1fs %s (%s, %sx)", len(text),
                 res["duration"], fmt, res["voice"], res["speed"])
    return Response(content=payload, media_type=media,
                    headers={"X-HiveMind-Duration": str(res["duration"]),
                             "X-HiveMind-Voice": res["voice"],
                             "X-HiveMind-Speed": str(res["speed"])})


@router.post("/stt/fetch")
async def stt_fetch(request: Request):
    """Start deploy/fetch_whisper.py for one model (binaries come with it)."""
    import subprocess
    import sys as _sys

    from core import stt_engine

    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    key = str((body or {}).get("model") or "").strip() or stt_engine.resolve_model(load_settings())
    if key not in stt_engine.MODELS:
        return JSONResponse(stt_engine.SttError(
            "STT_NO_MODEL", f"unknown model {key!r}").as_dict(), status_code=400)
    if stt_engine.fetch_state().get("state") == "running":
        return JSONResponse(stt_engine.SttError(
            "STT_BUSY", "a download is already running").as_dict(), status_code=409)
    script = Path(__file__).resolve().parent.parent / "deploy" / "fetch_whisper.py"
    if not script.is_file():
        return JSONResponse(stt_engine.SttError(
            "STT_NO_ENGINE", "deploy/fetch_whisper.py is missing").as_dict(),
            status_code=400)
    try:
        subprocess.Popen([_sys.executable, str(script), "--model", key,
                          "--backend", "blas"],
                         cwd=str(script.parent.parent),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL)
    except OSError as exc:
        return JSONResponse(stt_engine.SttError(
            "STT_FAILED", f"could not start the download: {exc}").as_dict(),
            status_code=500)
    _logger.info("[STT] download started for %s", key)
    return JSONResponse({"started": True, "model": key,
                         "fetch": stt_engine.fetch_state()}, status_code=202)


@router.post("/stt")
async def stt_transcribe(request: Request):
    """Transcribe one audio blob (raw body) -> {"text": ...} or {"code": ...}."""
    from core import stt_audio, stt_engine

    settings = load_settings()
    if not settings.get("stt_enabled", False):
        return JSONResponse(stt_engine.SttError(
            "STT_DISABLED", "speech-to-text is disabled in the settings").as_dict(),
            status_code=409)
    try:
        max_seconds = int(settings.get("stt_max_seconds") or
                          stt_audio.DEFAULT_MAX_SECONDS)
    except (TypeError, ValueError):
        max_seconds = stt_audio.DEFAULT_MAX_SECONDS
    wav_path = None
    try:
        cl_raw = request.headers.get("content-length")
        if cl_raw is not None:
            try:
                cl_val = int(cl_raw)
            except ValueError:
                return JSONResponse(
                    stt_engine.SttError("STT_DECODE_FAILED", "invalid Content-Length header").as_dict(),
                    status_code=400,
                )
            if cl_val < 0 or cl_val > stt_audio.MAX_BYTES:
                return JSONResponse(
                    stt_engine.SttError(
                        "STT_DECODE_FAILED",
                        f"audio is larger than {stt_audio.MAX_BYTES // (1024 * 1024)} MB",
                    ).as_dict(),
                    status_code=413,
                )
        raw = await request.body()
        if len(raw) > stt_audio.MAX_BYTES:
            return JSONResponse(
                stt_engine.SttError(
                    "STT_DECODE_FAILED",
                    f"audio is larger than {stt_audio.MAX_BYTES // (1024 * 1024)} MB",
                ).as_dict(),
                status_code=413,
            )
        # THREAD (audit 2026-10-17): decoding + the CLI/model run must not block
        # the event loop (a long clip blocked SSE and every other request).
        wav_path, duration = await asyncio.to_thread(
            stt_audio.to_wav16k, raw, max_seconds=max_seconds)
        text = await asyncio.to_thread(stt_engine.transcribe, wav_path, settings=settings)
        if not settings.get("stt_log_transcripts", False):
            _logger.info("[STT] transcribed %.1fs -> %d chars", duration, len(text))
        else:
            _logger.info("[STT] transcript: %s", text[:400])
        return JSONResponse({"text": text, "duration": round(duration, 2),
                             "language": stt_engine.resolve_language(settings)})
    except stt_engine.SttError as exc:
        _logger.warning("[STT] %s", exc)
        return JSONResponse(exc.as_dict(), status_code=400)
    finally:
        stt_audio.cleanup(wav_path)


@router.post("/documents/extract")
async def documents_extract(request: Request):
    """Extract text from one uploaded document (raw body) -> {"text": ...}.

    Mirrors /stt: the caller sends bytes, the engine answers text or a DOC_*
    code. PDF and plain-text (UTF-8/UTF-16) uploads; the size cap keeps a
    huge upload off the event loop (extraction runs in a worker thread)."""
    from core import documents as _docs

    settings = load_settings()
    try:
        max_chars = int(settings.get("document_max_chars") or _docs.DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = _docs.DEFAULT_MAX_CHARS
    try:
        max_pages = int(settings.get("document_max_pages") or _docs.DEFAULT_MAX_PAGES)
    except (TypeError, ValueError):
        max_pages = _docs.DEFAULT_MAX_PAGES
    try:
        raw = await request.body()
        if len(raw) > 32 * 1024 * 1024:
            return JSONResponse({"code": "DOC_TOO_LARGE",
                                 "error": "document larger than 32 MB"},
                                status_code=413)
        info = await asyncio.to_thread(
            _docs.extract_pdf_text, raw, max_pages=max_pages, max_chars=max_chars)
        _logger.info("[DOC] extracted %d chars from %d/%d pages (empty=%d, cut=%s)",
                     len(info["text"]), info["pages_read"], info["pages"],
                     info["empty_pages"], info["truncated"])
        return JSONResponse(info)
    except _docs.DocumentError as exc:
        _logger.info("[DOC] %s", exc.code)
        return JSONResponse(exc.as_dict(), status_code=400)


