"""Shared runtime state — set by server.py at startup, read by routers at request time."""
from __future__ import annotations
from pathlib import Path
from typing import Any

from utils.file import write_json_atomic

# HiveMind repo root — two levels up from core/state.py
_HIVEMIND_ROOT = Path(__file__).resolve().parent.parent

pipeline: Any = None
memory: Any = None
settings: dict = {}

_registry: dict = {}
REGISTRY_FILE: Path | None = None

_WEBSEARCH_AVAILABLE: bool = False
_websearch: Any = None
_safe_profile_state: dict = {}


def init_state(*, pipeline_obj=None, memory_obj=None, settings_dict=None,
               registry_dict=None, registry_file=None,
               websearch_available=False, websearch_obj=None,
               safe_profile_state=None):
    global pipeline, memory, settings, _registry, REGISTRY_FILE
    global _WEBSEARCH_AVAILABLE, _websearch, _safe_profile_state
    if pipeline_obj is not None:
        pipeline = pipeline_obj
    if memory_obj is not None:
        memory = memory_obj
    if settings_dict is not None:
        if settings_dict is not settings:
            settings.clear()
            settings.update(settings_dict)
    if registry_dict is not None:
        _registry = registry_dict
    if registry_file is not None:
        REGISTRY_FILE = Path(registry_file)
    if websearch_obj is not None:
        _websearch = websearch_obj
    _WEBSEARCH_AVAILABLE = websearch_available
    if safe_profile_state is not None:
        _safe_profile_state = safe_profile_state


# ── Registry ─────────────────────────────────────────────────────────

def registry_get(agent: str) -> str:
    if pipeline and agent in pipeline.agents:
        return _registry.get(agent, pipeline.agents[agent].model)
    return _registry.get(agent, "")


def registry_all() -> dict:
    base = {}
    if pipeline:
        base = {k: a.model for k, a in pipeline.agents.items()}
    base.update(_registry)
    return base


def _save_registry(reg: dict):
    if REGISTRY_FILE is None:
        return
    write_json_atomic(REGISTRY_FILE, reg)


def registry_set(agent: str, model: str):
    _registry[agent] = model
    if pipeline and agent in pipeline.agents:
        pipeline.agents[agent].model = model
    _save_registry(_registry)


def registry_sync_from_pipeline():
    if pipeline:
        for k, a in pipeline.agents.items():
            _registry[k] = a.model
        _save_registry(_registry)


# ── Settings → Pipeline ──────────────────────────────────────────────

def apply_settings_to_pipeline(s: dict):
    _changed = False
    for key, cfg in s.get("agents", {}).items():
        if pipeline and key in pipeline.agents:
            if "model" in cfg:
                if _registry.get(key) != cfg["model"]:
                    _changed = True
                pipeline.agents[key].model = cfg["model"]
                _registry[key] = cfg["model"]
            if "temperature" in cfg:
                pipeline.agents[key].temperature = float(cfg["temperature"])
            if "max_tokens" in cfg:
                pipeline.agents[key].max_tokens = int(cfg["max_tokens"])
            if "thinking" in cfg:
                pipeline.agents[key].thinking = bool(cfg["thinking"])
            if "thinking_budget" in cfg:
                pipeline.agents[key].thinking_budget = int(cfg["thinking_budget"] or 0)
    if _changed:
        _save_registry(_registry)
    if pipeline and s.get("vision_agent_model"):
        pipeline.agents["vision"].model = s["vision_agent_model"]


# ── Websearch ────────────────────────────────────────────────────────

def _ws_configure(**kwargs):
    if not _WEBSEARCH_AVAILABLE or not _websearch:
        return
    import inspect as _ins
    sig = _ins.signature(_websearch.configure)
    has_var_kw = any(
        p.kind == _ins.Parameter.VAR_KEYWORD
        for p in sig.parameters.values()
    )
    known = {p.name for p in sig.parameters.values()}
    if has_var_kw:
        _websearch.configure(**kwargs)
    else:
        filtered = {k: v for k, v in kwargs.items() if k in known}
        _websearch.configure(**filtered)


# ── Safe Profile Policy ──────────────────────────────────────────────

def _refresh_safe_profile_policy() -> dict:
    global _safe_profile_state
    try:
        from hive_functions.safe_profile_policy import apply_safe_profile_policy
        _safe_profile_state = apply_safe_profile_policy(
            settings,
            Path(REGISTRY_FILE).parent if REGISTRY_FILE else _HIVEMIND_ROOT,
        )
    except Exception as _sp_err:
        _safe_profile_state = {
            "applied": False,
            "reason": f"policy_error:{type(_sp_err).__name__}",
            "updated_keys": [],
        }
    return _safe_profile_state


# ── Git Tools ──────────────────────────────────────────────────────

_GIT_TOOLS_AVAILABLE: bool = False
exec_git_reset: Any = None
exec_git_checkout: Any = None
exec_git_stash: Any = None
exec_git_status_detailed: Any = None

_GIT_CONFIG_KEYS = [
    "git_repo_url", "git_username", "git_email", "git_token",
    "git_default_branch", "git_commit_prefix", "git_auto_push",
]


def _get_num_ctx(model: str, agent_role: str | None = None) -> int | None:


    override = settings.get("ctx_overrides")
    if not isinstance(override, dict):
        return None

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    model_name = str(model or "")
    base = model_name.rsplit("#", 1)[0] if "#" in model_name else model_name
    base_name = model_name.split(":")[0] if model_name else ""

    # Legacy flat shape
    if agent_role:
        _r = _int(override.get(agent_role))
        if _r:
            return _r
    for mk in (model_name, base, base_name):
        if mk:
            _m = _int(override.get(mk))
            if _m:
                return _m

    # Nested shape
    roles = override.get("roles")
    if agent_role and isinstance(roles, dict):
        _r = _int(roles.get(agent_role))
        if _r:
            return _r
    models = override.get("models")
    if isinstance(models, dict):
        for mk in (model_name, base_name):
            if mk:
                _m = _int(models.get(mk))
                if _m:
                    return _m

    return _int(override.get("default"))


def detect_vision_need(images, query):
    if not images:
        return None
    q = query.lower()
    ocr_kws = ["ocr", "text erkennen", "scan", "tabelle", "rechnung", "formular",
                "handschrift", "extract text", "read document", "bild zu text"]
    if any(k in q for k in ocr_kws):
        return "ocr"
    return "vision"


# ═══════════════════════════════════════════════════════════════════
#  S_models_cache (populated by server.py, read by routers)
# ═══════════════════════════════════════════════════════════════════

S_models_cache: list = []


# ═══════════════════════════════════════════════════════════════════
#  Chat State (populated by server.py, read by routers)
# ═══════════════════════════════════════════════════════════════════

_chats_cache: dict = {}
_cache_lock: Any = None
_cache_loaded: bool = False
_SESSIONS_DIR: Path | None = None
_safe_web_search: Any = None
_vision_cfg: dict = {}
