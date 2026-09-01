"""Vision-Preprocessing (aus server.py extrahiert)."""
from __future__ import annotations

import asyncio, base64, json, logging, os, re, struct, time
from pathlib import Path

THIS_FILE = Path(__file__)
VISION_MODEL_FILE = THIS_FILE.parent.parent / "vision_model.json"
import httpx

from core.duo_helpers import RE_THINK_CLEANUP as _re_think_cleanup

logger = logging.getLogger("hivemind.vision")

settings = None
_get_num_ctx = None
is_valid_preprocessing_model = None
VISION_PREPROCESS_PROMPT = ""


def init_vision_preprocess(settings_dict=None, get_num_ctx_fn=None,
                           is_valid_preprocessing_fn=None, vision_preprocess_prompt=""):
    global settings, _get_num_ctx, is_valid_preprocessing_model, VISION_PREPROCESS_PROMPT
    if settings_dict is not None:
        settings = settings_dict
    if get_num_ctx_fn:
        _get_num_ctx = get_num_ctx_fn
    if is_valid_preprocessing_fn:
        is_valid_preprocessing_model = is_valid_preprocessing_fn
    if vision_preprocess_prompt:
        VISION_PREPROCESS_PROMPT = vision_preprocess_prompt


def _load_vision_model_cfg() -> dict:
    if VISION_MODEL_FILE.exists():
        try:
            return json.loads(VISION_MODEL_FILE.read_text(encoding="utf-8"))
        except Exception as _e:
            logger.warning("Vision config read failed: %s", _e)
    return {"model": "", "enabled": False, "prompt": "Describe this image in detail. Include all relevant visual details, texts, diagrams, and structures."}


def _save_vision_model_cfg(cfg: dict):
    VISION_MODEL_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


_vision_cfg: dict = _load_vision_model_cfg()

# KV-cache poisoning detection
_VISION_POISON_MARKERS: tuple[str, ...] = (
    "ich bin hivemind",
    "bin hivemind",
    "hivemind ist ein lokales",
    "ein lokales ki-system, das vollstaendig",
    "ein lokales ki-system, das vollst",
    "nachrichtenherkunft",
    "[nutzer]\n",
    "i am hivemind",
)


def _build_vision_prompt(user_query: str, custom_prompt: str = "") -> str:
    _lang_lock = "IMPORTANT: Your entire response must be in ENGLISH only, regardless of the language of the user question below.\n\n"
    if custom_prompt:
        return _lang_lock + f"{custom_prompt}\n\nUser question (answer in English): {user_query}"

    q = user_query.lower().strip()

    if any(kw in q for kw in ["solve", "calculate", "equation", "exercise",
                                "löse", "berechne", "rechne", "aufgabe", "aufgaben",
                                "gleichung", "formel", "lösung", "ergebnis"]):
        return (
            "Analyze this image as a task or problem statement.\n"
            "Extract: 1) The exact task/question, 2) All numbers/formulas/equations, "
            "3) Relevant context information.\n"
            "Be precise and complete — the extracted data will be used to solve the problem.\n"
            f"User question (your response must be in English): {user_query}"
            )

    if any(kw in q for kw in ["read", "extract", "written", "ocr", "scan", "text",
                                "lies", "lese", "was steht", "schrift", "lesen", "erkennen", "tabelle"]):
        return (
            "Extract all visible text from this image.\n"
            "Preserve formatting, structure, and numbers exactly.\n"
            f"Request: {user_query}"
        )

    if any(kw in q for kw in ["hot", "pretty", "attractive", "look", "fashion", "style",
                                "sexy", "attraktiv", "schön", "hässlich", "gefällt",
                                "stil", "outfit", "mode", "aussehen", "bewerte"]):
        return (
            "Describe this image focused on visual aesthetics and appearance.\n"
            "Cover: style, clothing, expression, overall impression.\n"
            f"User question: {user_query}"
        )

    if any(kw in q for kw in ["what is", "what happens", "describe", "scene", "who is",
                                "was passiert", "was ist", "was zeigt", "wer ist",
                                "beschreib", "erkläre", "szene", "bild"]):
        return (
            "Describe this scene precisely and in a structured way.\n"
            "Include: people, objects, actions, environment, relevant details.\n"
            f"User question: {user_query}"
        )

    return (
        "IMPORTANT: Respond in ENGLISH ONLY, regardless of the language below.\n"
        f"Analyze this image in context of this question: \"{user_query}\"\n"
        "Describe only what is relevant. Be precise, no intro phrases."
    )


def _png_size_from_data_url(img: str) -> tuple[int, int] | None:
    if not isinstance(img, str):
        return None
    if not img.startswith("data:image/png") or "," not in img:
        return None
    try:
        b64 = img.split(",", 1)[1]
        raw = base64.b64decode(b64, validate=False)
        if len(raw) < 24:
            return None
        if raw[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        width, height = struct.unpack(">II", raw[16:24])
        return int(width), int(height)
    except Exception:
        return None


def _filter_vision_images(images: list) -> tuple[list, str | None]:
    valid: list = []
    dropped_tiny_png = 0
    for img in images or []:
        dims = _png_size_from_data_url(img) if isinstance(img, str) else None
        if dims and (dims[0] < 2 or dims[1] < 2):
            dropped_tiny_png += 1
            continue
        valid.append(img)

    status = None
    if dropped_tiny_png:
        status = (
            f"[Vision-Input verworfen: {dropped_tiny_png} PNG(s) kleiner als 2x2 Pixel]"
        )
    return valid, status


async def _preprocess_images_to_text(images: list, user_query: str) -> str | None:
    global _vision_cfg
    if not _vision_cfg.get("enabled") or not _vision_cfg.get("model"):
        logger.debug("[Vision-Prepro] Abbruch: enabled=%s model=%r", _vision_cfg.get("enabled"), _vision_cfg.get("model"))
        return None

    model = _vision_cfg["model"]

    import logging as _vlog
    _logger_v = _vlog.getLogger("hivemind.vision")

    if not is_valid_preprocessing_model(model):
        _logger_v.warning(
            f"Vision model '{model}' is not on the allowlist — "
            "images may be rejected (no vision encoder)."
        )

    full_prompt = _build_vision_prompt(user_query, _vision_cfg.get("prompt", ""))

    img_data = [
        b.split(",", 1)[1] if isinstance(b, str) and b.startswith("data:") and "," in b else b
        for b in images
    ]

    _vp_ctx = _get_num_ctx(model, agent_role="vision") or 8192

    logger.info(
        f"Vision-Preprocessing: model={model!r} imgs={len(img_data)} "
        f"ctx={_vp_ctx} query={user_query[:60]!r}"
    )

    from backend.llama_server_manager import manager as _lsm_v
    try:
        _vp_load_timeout = float(settings.get("vision_preprocess_load_timeout_seconds", 45.0) or 45.0)
        _vp_port = await asyncio.wait_for(
            _lsm_v.ensure_loaded(model, num_ctx=_vp_ctx, vision=True),
            timeout=_vp_load_timeout,
        )
        logger.debug("[Vision-Prepro] ensure_loaded OK → port=%d", _vp_port)
    except asyncio.TimeoutError:
        logger.error(
            "Vision ensure_loaded Timeout nach %.1fs (model=%s)",
            _vp_load_timeout, model,
        )
        logger.warning(
            "[Vision-Prepro] ensure_loaded TIMEOUT nach %.1fs (model=%s)",
            _vp_load_timeout, model,
        )
        return f"[Vision-Preprocessing-Error: server start timeout after {_vp_load_timeout:.1f}s]"
    except Exception as _le:
        _err_cls = type(_le).__name__
        _err_txt = repr(_le)
        logger.error("Vision ensure_loaded failed (%s): %s", _err_cls, _err_txt)
        logger.warning("[Vision-Prepro] ensure_loaded ERROR (%s): %s", _err_cls, _err_txt)
        return f"[Vision-Preprocessing-Error: server start failed ({_err_cls}): {_err_txt[:100]}]"

    _vp_content: list = [{"type": "text", "text": full_prompt}]
    for _b64_orig, _b64 in zip(images, img_data):
        _mime = "image/jpeg"
        if isinstance(_b64_orig, str) and _b64_orig.startswith("data:image/"):
            _mime = _b64_orig.split(";")[0][5:]
        _vp_content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{_mime};base64,{_b64}"},
        })
    logger.debug("[Vision-Prepro] Sende Request an port=%d content_parts=%d img_count=%d", _vp_port, len(_vp_content), len(img_data))

    parts: list[str] = []
    try:
        _vp_read_s = float(settings.get("duo_llm_slow_timeout_s", 300))
        async with httpx.AsyncClient(timeout=httpx.Timeout(
                connect=10.0, read=_vp_read_s, write=10.0, pool=5.0)) as _vc:
            async with _vc.stream(
                "POST",
                f"http://127.0.0.1:{_vp_port}/v1/chat/completions",
                json={
                    "model":    model,
                    "messages": [
                        {"role": "system", "content": VISION_PREPROCESS_PROMPT},
                        {"role": "user",   "content": _vp_content},
                    ],
                    "stream":         True,
                    "temperature":    0.1,
                    "max_tokens":     800,
                    "repeat_penalty": 1.35,
                    "repeat_last_n":  64,
                    "stop":           ["<|im_end|>", "<|endoftext|>", "\n\n\n"],
                    "cache_prompt":   False,
                },
            ) as _vp_resp:
                async for _vp_line in _vp_resp.aiter_lines():
                    if not _vp_line or not _vp_line.startswith("data:"):
                        continue
                    _vp_raw = _vp_line[6:].strip()
                    if _vp_raw == "[DONE]":
                        break
                    try:
                        _vp_d   = json.loads(_vp_raw)
                        _vp_tok = (_vp_d.get("choices", [{}])[0]
                                       .get("delta", {})
                                       .get("content", "") or "")
                        if _vp_tok:
                            parts.append(_vp_tok)
                    except Exception:
                        continue
    except Exception as _ve:
        _err_cls = type(_ve).__name__
        _err_txt = repr(_ve)
        logger.error("Vision httpx error (%s): %s", _err_cls, _err_txt)
        logger.warning("[Vision-Prepro] httpx ERROR (%s): %s", _err_cls, _err_txt)
        return f"[Vision-Preprocessing-Error ({_err_cls}): {_err_txt[:120]}]"

    result = _re_think_cleanup.sub("", "".join(parts)).strip()
    logger.debug("[Vision-Prepro] response received: %d tokens, result_len=%d, start=%r", len(parts), len(result), result[:60])
    if not result:
        logger.error(
            f"Vision model '{model}' returned 0 tokens. "
            "Possible causes: (1) no --mmproj at server start (model missing from _MMPROJ_REQUIRED_BASES), "
            "(2) wrong mmproj path in models.json, (3) ctx too small for the image (8192+ needed)."
        )
        logger.warning("[Vision-Prepro] ERROR: 0 tokens for %r - mmproj loaded? ctx=8192+?", model)
        return f"[Vision preprocessing error: model '{model}' gave no answer — mmproj correct? See log.]"

    for _stop in ("<|im_end|>", "<|endoftext|>", "<|"):
        if _stop in result:
            result = result[:result.index(_stop)].strip()

    # Sanity check: detect KV-cache poisoning.
    result_lower = result.lower()
    for poison in _VISION_POISON_MARKERS:
        if poison in result_lower:
            logger.warning("[Vision-Prepro] KV-cache poisoning detected: poison marker '%s' in answer, using fallback", poison)
            return "[Vision fallback: image description failed — generic analysis not possible. treat image as visual input.]"

    return result
