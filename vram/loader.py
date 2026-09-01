"""VRAM model loader (extracted from server.py)."""
from __future__ import annotations
import asyncio
import logging
import time

logger = logging.getLogger("hivemind.vram_loader")

_api_gen_load = None
_bk_ps = None
_settings = None
_registry = None
_get_num_ctx_fn = None

def _get_num_ctx(model):
    return _get_num_ctx_fn(model) if _get_num_ctx_fn else None

_VRAM_LOOKUP_GB = None
_loaded_models_cache = (0.0, set())
_VRAM_BUDGET_DEFAULT = 7.5  # overridable via init


def init_vram_loader(api_gen_load=None, bk_ps=None, settings=None,
                     registry=None, get_num_ctx=None, vram_lookup_gb=None,
                     vram_budget_default=7.5):
    global _api_gen_load, _bk_ps, _settings, _registry, _get_num_ctx_fn, _VRAM_LOOKUP_GB
    global _VRAM_BUDGET_DEFAULT
    if api_gen_load is not None:
        _api_gen_load = api_gen_load
    if bk_ps:
        _bk_ps = bk_ps
    if settings is not None:
        _settings = settings
    if registry is not None:
        _registry = registry
    if get_num_ctx:
        _get_num_ctx_fn = get_num_ctx
    if vram_lookup_gb is not None:
        _VRAM_LOOKUP_GB = vram_lookup_gb
    if vram_budget_default is not None:
        _VRAM_BUDGET_DEFAULT = float(vram_budget_default)


async def _bk_load(model: str, keep_alive: str, num_ctx: int | None = None):
    """keep_alive=Nm to load / keep_alive=-1 to pin / keep_alive=0 to evict."""
    if _api_gen_load is None:
        raise RuntimeError("VRAM loader not initialized — init_vram_loader() not called")
    await _api_gen_load(model, keep_alive=keep_alive, num_ctx=num_ctx)


async def _bk_pin(model: str, num_ctx: int | None = None):
    """keep_alive=-1 — model stays in VRAM permanently."""
    await _bk_load(model, keep_alive="-1", num_ctx=num_ctx)


async def _bk_evict(model: str):
    """keep_alive=0 — remove the model from VRAM."""
    await _bk_load(model, keep_alive="0")


async def _get_loaded_models_set(max_age: float = 15.0) -> set:


    global _loaded_models_cache
    now = time.time()
    if now - _loaded_models_cache[0] < max_age:
        return _loaded_models_cache[1]
    try:
        result = {m["name"] for m in await _bk_ps()}
        _loaded_models_cache = (now, result)
        return result
    except Exception:
        return _loaded_models_cache[1]


async def smart_preload_if_needed(model: str, loaded_set: set) -> tuple[bool, set]:


    def _base(m: str) -> str:
        return m[:-7] if m.endswith(":latest") else m

    _judge_model = _registry.get("judge", "")
    if _judge_model and (model == _judge_model or _base(model) == _base(_judge_model)):
        return False, loaded_set

    if model in loaded_set or _base(model) in {_base(m) for m in loaded_set}:
        return False, loaded_set

    # Grober VRAM-Filter vor Preload (ctx-aware).
    budget_gb    = float(_settings.get("vram_budget_gb") or _VRAM_BUDGET_DEFAULT)
    _BUFFER      = 0.6   # AMD HIP/ROCm overhead ~0.5GB
    try:
        from backend.llama_vram_table import vram_of_with_ctx as _vram_ctx
        already_used = 0.0
        for _m in loaded_set:
            _m_base = _m.rsplit("#", 1)[0] if "#" in _m else _m
            _m_ctx = _get_num_ctx(_m_base) or 4096
            already_used += float(_vram_ctx(_m_base, int(_m_ctx)))
        _target_ctx = _get_num_ctx(model) or 4096
        model_gb = float(_vram_ctx(model, int(_target_ctx)))
    except Exception:
        already_used = sum(_VRAM_LOOKUP_GB.get(m, 4.0) for m in loaded_set)
        model_gb     = _VRAM_LOOKUP_GB.get(model, 4.0)
    if already_used + model_gb + _BUFFER > budget_gb:
        return False, loaded_set

    try:
        ctx = _get_num_ctx(model)
        from backend import api_prefetch_next as _api_pf_next
        await _api_pf_next(model, num_ctx=ctx)
        return True, loaded_set | {model}
    except Exception:
        return False, loaded_set


async def _refresh_judge_keepalive():
    if _settings is None:
        return
    if not _settings.get("judge_keepalive_enabled", True):
        return
    _judge = _registry.get("judge", "")
    if not _judge:
        return

    budget_eff = float(_settings.get("vram_budget_gb") or _VRAM_BUDGET_DEFAULT)
    judge_gb   = _VRAM_LOOKUP_GB.get(_judge, 2.1)
    try:
        currently_loaded = await _get_loaded_models_set(max_age=5.0)
        judge_base = _judge.split(":")[0]
        other_gb = sum(
            _VRAM_LOOKUP_GB.get(m, 4.0) for m in currently_loaded
            if m.split(":")[0] != judge_base
        )
        if other_gb + judge_gb > budget_eff:
            return
    except Exception:
        pass

    _judge_keep = str(_settings.get("default_keep_alive", "30m"))
    _judge_ctx_r = _get_num_ctx(_judge)
    try:
        from backend.llama_server_manager import manager as _jkm
        _judge_was_loaded = await _jkm.touch_if_loaded(_judge)
        if not _judge_was_loaded:
            await _bk_load(_judge, keep_alive=_judge_keep, num_ctx=_judge_ctx_r)
    except Exception:
        pass

