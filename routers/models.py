"""Models API-Router."""
import asyncio
import logging
import math

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

import backend.gguf_meta as _gguf_meta
import backend.llama_models as _llama_models
import model_configs.models_registry as _models_registry
from routing.model_automap import get_model_display_map
from routing.model_automap import _VISION_PREPROCESSING_ALLOWLIST
from core.model_sampling import _model_profile
from core.state import settings, S_models_cache, registry_all
from backend import api_tags as _bk_tags
from routers.config import _validate_sampling_block

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["Models"])

_PROFILE_BOOL_KEYS = frozenset({
    "moe", "jinja", "distilled", "mtp", "vision_preprocessing",
})

_PROFILE_INT_BOUNDS: dict[str, tuple[int, int | None]] = {
    "num_ctx": (1, None),
    "num_ctx_analyst": (1, None),
    "num_ctx_duo_coder": (1, None),
    "num_ctx_vision": (1, None),
    "moe_cpu_experts": (0, None),
    "gpu_layers": (0, None),
    "thinking_budget": (0, None),
    "kv_bytes_per_token": (1, None),
    "mtp_draft_n_max": (1, 4),
    "dflash_draft_n_max": (0, None),
    "dspark_draft_n_max": (0, None),
    "cache_ram_mib": (0, 32768),
    "ubatch": (64, 4096),
}

_PROFILE_FLOAT_BOUNDS: dict[str, tuple[float, float | None, bool]] = {
    "vram_gb_override": (0.0, None, True),
    "draft_overhead_mib": (0.0, 16384.0, False),
    "mtp_draft_p_min": (0.0, 0.95, False),
    "dflash_draft_p_min": (0.0, 0.95, False),
}

_PROFILE_CHOICES: dict[str, frozenset[str]] = {
    "reasoning": frozenset({"", "on", "off"}),
    "kv_cache_type": frozenset({"", "q4_0", "q8_0", "f16", "bf16", "q4_1", "q5_0", "q5_1"}),
    "load_mode": frozenset({"", "auto", "none", "mmap", "mmap+mlock"}),
    "lazy_mode": frozenset({"", "auto", "on", "off"}),
}

_PROFILE_FILENAME_KEYS = frozenset({
    "mmproj_filename",
    "mtp_draft_filename",
    "dflash_draft_filename",
    "dspark_draft_filename",
    "chat_template",
})


def _validate_safe_filename(field: str, val: object, *, allow_empty: bool = True) -> tuple[str | None, str | None]:
    if not isinstance(val, str):
        return None, f"key '{field}' requires a string or null"
    if any(ord(c) < 32 for c in val):
        return None, f"key '{field}' contains invalid control characters"
    if "/" in val or "\\" in val or ".." in val or ":" in val:
        return None, f"key '{field}' must be a safe filename without path traversal"
    stripped = val.strip()
    if stripped.startswith(".") or any(c in '<>"|?*' for c in stripped):
        return None, f"key '{field}' contains unsafe filename characters"
    if not stripped and not allow_empty:
        return None, f"key '{field}' items must not be empty"
    return (stripped or None), None


def _validate_user_profile_payload(raw: dict, clean_model: str) -> tuple[dict | None, str | None]:
    clean_payload: dict = {}
    for key, val in raw.items():
        if not isinstance(key, str):
            return None, "profile keys must be strings"
        if key == "model":
            continue
        if key in _PROFILE_BOOL_KEYS:
            if val is not None and not isinstance(val, bool):
                return None, f"key '{key}' requires a boolean or null"
            clean_payload[key] = val
            continue
        if key == "flash_attn":
            if val is None or isinstance(val, bool):
                clean_payload[key] = val
                continue
            if isinstance(val, str):
                low = val.strip().lower()
                if low == "":
                    clean_payload[key] = None
                    continue
                if low in ("on", "true"):
                    clean_payload[key] = True
                    continue
                if low in ("off", "false"):
                    clean_payload[key] = False
                    continue
            return None, "key 'flash_attn' must be boolean, 'on'/'off'/'true'/'false', or null"
        if key in _PROFILE_INT_BOUNDS:
            if val is None:
                clean_payload[key] = None
                continue
            if isinstance(val, bool) or not isinstance(val, int):
                return None, f"key '{key}' requires an integer or null"
            lo, hi = _PROFILE_INT_BOUNDS[key]
            if val < lo or (hi is not None and val > hi):
                bound_msg = f"between {lo} and {hi}" if hi is not None else f">= {lo}"
                return None, f"key '{key}' must be {bound_msg}"
            clean_payload[key] = val
            continue
        if key in _PROFILE_FLOAT_BOUNDS:
            if val is None:
                clean_payload[key] = None
                continue
            if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(float(val)):
                return None, f"key '{key}' requires a finite number or null"
            fval = float(val)
            lo, hi, lo_excl = _PROFILE_FLOAT_BOUNDS[key]
            too_low = (fval <= lo) if lo_excl else (fval < lo)
            if too_low or (hi is not None and fval > hi):
                if hi is not None:
                    return None, f"key '{key}' must be between {lo:g} and {hi:g}"
                op = ">" if lo_excl else ">="
                return None, f"key '{key}' must be {op} {lo:g}"
            clean_payload[key] = fval
            continue
        if key in _PROFILE_CHOICES:
            if val is None:
                clean_payload[key] = None
                continue
            if not isinstance(val, str):
                return None, f"key '{key}' requires a string or null"
            norm = val.strip().lower()
            if norm not in _PROFILE_CHOICES[key]:
                return None, f"invalid value {val!r} for '{key}'"
            clean_payload[key] = norm or None
            continue
        if key in _PROFILE_FILENAME_KEYS:
            if val is None:
                clean_payload[key] = None
                continue
            clean_name, err = _validate_safe_filename(key, val, allow_empty=True)
            if err:
                return None, err
            clean_payload[key] = clean_name
            continue
        if key == "dflash_draft_alternates":
            if val is None:
                clean_payload[key] = None
                continue
            if not isinstance(val, list):
                return None, "key 'dflash_draft_alternates' requires a list of strings or null"
            clean_alts: list[str] = []
            for item in val:
                clean_item, err = _validate_safe_filename("dflash_draft_alternates", item, allow_empty=False)
                if err or clean_item is None:
                    return None, err or "dflash_draft_alternates items must be safe filenames"
                clean_alts.append(clean_item)
            clean_payload[key] = clean_alts
            continue
        if key == "extra_args":
            if val is None:
                clean_payload[key] = None
                continue
            if isinstance(val, str):
                if any(ord(c) < 32 for c in val):
                    return None, "key 'extra_args' cannot contain control characters or newlines"
                clean_payload[key] = [tok for tok in val.split() if tok]
                continue
            if isinstance(val, list):
                toks: list[str] = []
                for item in val:
                    if not isinstance(item, str):
                        return None, "key 'extra_args' items must be strings"
                    if any(ord(c) < 32 for c in item):
                        return None, "key 'extra_args' items cannot contain control characters or newlines"
                    tok = item.strip()
                    if tok:
                        toks.append(tok)
                clean_payload[key] = toks
                continue
            return None, "key 'extra_args' requires a list of strings, string, or null"
        if key == "capabilities":
            if val is None:
                clean_payload[key] = None
                continue
            if not isinstance(val, dict):
                return None, "key 'capabilities' requires an object or null"
            clean_caps: dict[str, bool] = {}
            for ck, cv in val.items():
                if ck not in ("thinking", "vision", "tool_call"):
                    return None, f"unsupported capability '{ck}'"
                if cv is None:
                    continue
                if not isinstance(cv, bool):
                    return None, f"capabilities.{ck} requires boolean or null"
                clean_caps[ck] = cv
            if clean_caps:
                clean_payload[key] = clean_caps
            continue
        if key == "sampling":
            if val is None:
                continue
            if not isinstance(val, dict):
                return None, "key 'sampling' requires an object or null"
            err = _validate_sampling_block(val)
            if err:
                return None, err
            clean_payload[key] = val
            continue
        return None, f"unsupported profile key '{key}'"

    # Keep user override JSON files sparse: null means "inherit shipped/GGUF default".
    clean_payload = {k: v for k, v in clean_payload.items() if v is not None}
    clean_payload["model"] = clean_model
    return clean_payload, None


def _update_models_cache(models: list[str]) -> None:
    global S_models_cache
    if isinstance(S_models_cache, list):
        S_models_cache[:] = models
    else:
        S_models_cache = models
    import core.state as _state
    if isinstance(_state.S_models_cache, list):
        _state.S_models_cache[:] = models
    else:
        _state.S_models_cache = models


def _load_profile_payload(clean: str) -> dict:
    profile = _models_registry.get_profile(clean)
    source = _models_registry.get_profile_source(clean)
    file_path = _models_registry.find_profile_file(clean)
    try:
        gguf = dict(_gguf_meta.read_gguf_metadata(clean) or {})
    except (ImportError, OSError, ValueError, TypeError) as exc:
        _logger.debug("read_gguf_metadata failed for %s: %s", clean, exc)
        gguf = {}
    gguf.pop("chat_template", None)
    return {
        "ok": True,
        "model": clean,
        "source": source,
        "profile": profile,
        "gguf": gguf,
        "file": file_path.name if file_path else None,
    }


def _build_models_profiles(models: list[str]) -> list[dict]:
    """Build enriched per-model profile dicts synchronously (run via to_thread)."""
    try:
        prof_dict = get_model_display_map(models)
        prof_list = [{"name": k, **v} for k, v in prof_dict.items()]
    except Exception:
        prof_list = [{"name": m, "tags": [], "vram_gb": 4.0, "tool_call": False, "vision": False} for m in models]

    from model_configs.models_registry import (
        get_profile as _gp,
        get_profile_source as _gps,
        is_mtp as _im,
        get_load_mode as _glm,
        get_thinking_budget as _gtb,
    )

    for p in prof_list:
        _mname = p.get("name", "")
        _mp = _model_profile(_mname)
        if "thinking" not in p:
            p["thinking"] = _mp.get("thinking", False)
        if "vision" not in p:
            p["vision"] = _mp.get("vision", False)
        if "tool_call" not in p:
            p["tool_call"] = _mp.get("tool_call", False)
        try:
            _reg_prof = _gp(_mname) or {}
            p["mtp"] = bool(_im(_mname))
            p["load_mode"] = _glm(_mname) or _reg_prof.get("load_mode") or "auto"
            _tb = _gtb(_mname)
            p["thinking_budget"] = _tb if _tb is not None else _reg_prof.get("thinking_budget")
            _ri = _gguf_meta.get_effective_reasoning_info(_mname)
            _r_mode = str(_ri.get("mode") or "none")
            _r_levels = list(_ri.get("levels") or [])
            if p.get("thinking") and _r_mode == "none" and not _ri.get("template_source", "").startswith("external:"):
                _r_mode = "binary"
                _r_levels = ["none", "on"]
            elif _r_mode != "none":
                p["thinking"] = True
            p["reasoning_mode"] = _r_mode
            p["reasoning_levels"] = _r_levels
            p["reasoning_template_source"] = str(_ri.get("template_source") or "none")
            _samp = _reg_prof.get("sampling")
            _samp = _samp if isinstance(_samp, dict) else {}
            _re_val = ""
            if _r_mode == "effort":
                for _cand_blk in (
                    _reg_prof,
                    _samp.get("sampling_thinking_code"),
                    _samp.get("sampling_thinking_text"),
                    _samp.get("thinking"),
                    _reg_prof.get("sampling_thinking_code"),
                    _reg_prof.get("sampling_thinking"),
                ):
                    if isinstance(_cand_blk, dict) and _cand_blk.get("reasoning_effort"):
                        _re_val = str(_cand_blk.get("reasoning_effort") or "")
                        break
                if not _re_val and _ri.get("template_default"):
                    _re_val = str(_ri.get("template_default") or "")
            _re_norm = _re_val.strip().lower()
            p["reasoning_effort"] = _re_norm if (_r_mode == "effort" and _re_norm and _re_norm not in ("auto", "on")) else None
            p["source"] = _gps(_mname)
            _kv_bpt = _reg_prof.get("kv_bytes_per_token")
            if not _kv_bpt:
                try:
                    _gmeta = _gguf_meta.read_gguf_metadata(_mname) or {}
                    _fa_off = _reg_prof.get("flash_attn") is False
                    _kv_bpt = _gmeta.get("kv_bytes_per_token_f16" if _fa_off else "kv_bytes_per_token_q4")
                except (ImportError, OSError, ValueError, TypeError, AttributeError):
                    _kv_bpt = None
            p["kv_bytes_per_token"] = int(_kv_bpt) if _kv_bpt else None
        except (AttributeError, KeyError, TypeError, ValueError, OSError) as _prof_err:
            logging.getLogger(__name__).debug("model profile enrichment failed for %s: %s", _mname, _prof_err)
            p.setdefault("mtp", False)
            p.setdefault("load_mode", "auto")
            p.setdefault("thinking_budget", None)
            p.setdefault("reasoning_effort", None)
            p.setdefault("reasoning_mode", "binary" if p.get("thinking") else "none")
            p.setdefault("reasoning_levels", ["none", "on"] if p.get("thinking") else [])
            p.setdefault("reasoning_template_source", "none")
            p.setdefault("source", "generic")
            p.setdefault("kv_bytes_per_token", None)
    return prof_list


@router.get("/models")
async def get_models():
    try:
        from backend.llama_models import list_available_models as _lam
        models = await asyncio.to_thread(_lam)
    except Exception:
        models = []

    if not models:
        try:
            models = await _bk_tags()
        except Exception:
            pass

    if not models and S_models_cache:
        models = list(S_models_cache)

    if not models:
        models = sorted(set(v for v in registry_all().values() if v))

    if models:
        # H-audit: mutate IN PLACE - a rebind starved every from-import
        # consumer (chat_run automap, /v1 agent pick) of the update.
        _update_models_cache(models)

    prof_list = await asyncio.to_thread(_build_models_profiles, models)
    return {
        "models": models,
        "profiles": prof_list,
        "vision_preprocessing_allowlist": list(_VISION_PREPROCESSING_ALLOWLIST),
    }


@router.get("/models/profile/{name:path}")
async def get_model_profile_override(name: str):
    try:
        clean, _ = _models_registry._validate_user_slug(name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return await asyncio.to_thread(_load_profile_payload, clean)


@router.put("/models/profile/{name:path}")
async def put_model_profile_override(name: str, req: Request):
    try:
        clean, _ = _models_registry._validate_user_slug(name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    try:
        body = await req.json()
    except (ValueError, TypeError, RuntimeError) as exc:
        return JSONResponse({"ok": False, "error": f"invalid JSON body: {exc}"}, status_code=400)

    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "error": "request body must be a JSON object"}, status_code=400)

    if "profile" in body:
        if not isinstance(body["profile"], dict):
            return JSONResponse({"ok": False, "error": "'profile' must be a JSON object"}, status_code=400)
        extra_wrapper = set(body.keys()) - {"profile", "model"}
        if extra_wrapper:
            return JSONResponse(
                {"ok": False, "error": f"unsupported wrapper keys: {', '.join(sorted(extra_wrapper))}"},
                status_code=400,
            )
        raw_profile = body["profile"]
    else:
        raw_profile = body

    clean_payload, err = _validate_user_profile_payload(raw_profile, clean)
    if err or clean_payload is None:
        return JSONResponse({"ok": False, "error": err or "invalid profile payload"}, status_code=400)

    def _save_and_load() -> dict:
        saved_path = _models_registry.save_user_profile(clean, clean_payload)
        return {
            "ok": True,
            "model": clean,
            "source": _models_registry.get_profile_source(clean),
            "profile": _models_registry.get_profile(clean),
            "file": saved_path.name,
        }

    try:
        return await asyncio.to_thread(_save_and_load)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except (OSError, TypeError) as exc:
        _logger.warning("save_user_profile failed for %s: %s", clean, exc, exc_info=True)
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)


@router.delete("/models/profile/{name:path}")
async def delete_model_profile_override(name: str):
    try:
        clean, _ = _models_registry._validate_user_slug(name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    def _delete_and_load() -> dict:
        deleted = _models_registry.delete_user_profile(clean)
        return {
            "ok": True,
            "model": clean,
            "deleted": bool(deleted),
            "source": _models_registry.get_profile_source(clean),
            "profile": _models_registry.get_profile(clean),
        }

    try:
        return await asyncio.to_thread(_delete_and_load)
    except OSError as exc:
        _logger.warning("delete_user_profile failed for %s: %s", clean, exc, exc_info=True)
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)


@router.post("/models/rescan")
async def rescan_models():
    def _do_rescan() -> list[str]:
        _llama_models.invalidate_caches()
        _gguf_meta.clear_cache()
        _models_registry.refresh()
        return list(_llama_models.list_available_models(True) or [])

    try:
        models = await asyncio.to_thread(_do_rescan)
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        _logger.warning("rescan_models failed: %s", exc, exc_info=True)
        return JSONResponse({"ok": False, "error": str(exc)[:120]}, status_code=500)

    _update_models_cache(models)
    return {"ok": True, "count": len(models), "models": models}


@router.get("/v1/models")
async def v1_models():
    if S_models_cache:
        models = list(S_models_cache)
    else:
        try:
            models = await _bk_tags()
        except Exception:
            models = list({a["model"] for a in settings.get("agents", {}).values() if a.get("model")})
    return {
        "object": "list",
        "data": [
            {"id": m, "object": "model", "created": 0, "owned_by": "ollama"}
            for m in models
        ]
    }
