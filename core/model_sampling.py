# -*- coding: utf-8 -*-
"""Per-model sampling resolution (model JSON -> optional UI/runtime override -> DEFAULT_PROFILE)."""

from __future__ import annotations

import logging
import math

_smp_log = logging.getLogger("hivemind.sampling")


DEFAULT_PROFILE = {
    # Generic fallback for unprofiled GGUFs without a model_configs/models/*.json entry:
    # keep presence_penalty=0.0 so unknown models do not break JSON/tool-call syntax.
    "thinking": {
        "temperature": 0.8, "top_p": 0.9, "top_k": 40,
        "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0,
        "preserve_thinking": False, "cache_prompt": True,
    },
    "non_thinking": {
        "temperature": 0.8, "top_p": 0.9, "top_k": 40,
        "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0,
        "cache_prompt": True,
    },
}

_FALLBACK_UNPROFILED_SAMPLING: dict[str, float | int] = {
    "temperature": 0.7,
    "top_p": 0.9,
    "top_k": 40,
    "min_p": 0.05,
    "repeat_penalty": 1.05,
    "presence_penalty": 0.0,
}

# Synced at boot and on POST /settings via backend.runtime_flags.apply_runtime_flags.
DEFAULT_UNPROFILED_SAMPLING: dict[str, float | int] = dict(_FALLBACK_UNPROFILED_SAMPLING)


_FLAT_SAMPLING_KEYS = (
    "temperature", "top_p", "top_k", "min_p",
    "presence_penalty", "repeat_penalty", "repetition_penalty", "seed",
)

VALID_REASONING_EFFORTS = ("auto", "none", "low", "medium", "high", "max")
REASONING_EFFORT_BUDGETS: dict[str, int] = {
    "none": 0,
    "low": 1024,
    "medium": 3072,
    "high": 8192,
    "max": 16384,
}
_EFFORT_USER_ROLES = frozenset({"direct", "duo_coder", "duo_planner", "synthesizer"})


def normalize_reasoning_effort(raw: object) -> str:
    """Normalize user or profile reasoning_effort strings to VALID_REASONING_EFFORTS."""
    val = str(raw or "").strip().lower()
    if val in ("off", "disabled", "false", "0"):
        return "none"
    if val == "med":
        return "medium"
    if val in ("xhigh", "maximum"):
        return "max"
    if val == "minimal":
        return "low"
    return val if val in VALID_REASONING_EFFORTS else "auto"


def resolve_reasoning_effort(
    settings: dict | None = None,
    profile: dict | None = None,
    thinking: bool | None = None,
    model_name: str = "",
) -> str:
    """Resolve effective reasoning_effort ('none'|'low'|'medium'|'high'|'max' or '')."""
    user_eff = normalize_reasoning_effort((settings or {}).get("reasoning_effort"))
    if user_eff == "none":
        return "none"
    if user_eff != "auto":
        if model_name and not model_supports_thinking(model_name):
            return ""
        return user_eff
    if thinking is False:
        return "none"
    prof_eff = normalize_reasoning_effort((profile or {}).get("reasoning_effort"))
    if prof_eff != "auto":
        return prof_eff
    return ""


def _sync_repeat_penalty(target: dict, source: dict) -> None:
    """Keep repeat_penalty / repetition_penalty aligned across layers."""
    has_rep = "repeat_penalty" in source
    has_repi = "repetition_penalty" in source
    if has_rep and not has_repi:
        target["repetition_penalty"] = source["repeat_penalty"]
    elif has_repi and not has_rep:
        target["repeat_penalty"] = source["repetition_penalty"]


def _settings_sampling_override(model_name: str, settings: dict | None = None) -> dict:
    if not (settings and isinstance(settings, dict)):
        return {}
    ovr = settings.get("_model_sampling_overrides")
    if not isinstance(ovr, dict):
        return {}
    entry = ovr.get(model_name, ovr.get(model_name.split(":")[0]))
    return entry if isinstance(entry, dict) else {}


def _registry_sampling(model_name: str) -> dict:
    try:
        from model_configs.models_registry import get_sampling as _reg_sampling
        entry = _reg_sampling(model_name)
        return entry if isinstance(entry, dict) else {}
    except Exception:
        return {}


def _select_override_mode_dict(model_override: dict, _mode_key: str, thinking: bool) -> dict | None:
    """Pick the matching mode sub-dict inside a per-model sampling block."""
    if not model_override:
        return None
    mode_dict = model_override.get(_mode_key)
    if not mode_dict or not isinstance(mode_dict, dict):
        if thinking or _mode_key in ("thinking", "sampling_thinking_text"):
            mode_dict = model_override.get("thinking", model_override.get("sampling_thinking_text"))
        else:
            mode_dict = model_override.get("non_thinking", model_override.get("sampling_text"))
    return mode_dict if isinstance(mode_dict, dict) else None


def _has_explicit_presence(layer: dict, _mode_key: str, thinking: bool) -> bool:
    if not layer:
        return False
    mode_dict = _select_override_mode_dict(layer, _mode_key, thinking)
    if mode_dict and "presence_penalty" in mode_dict:
        return True
    return "presence_penalty" in layer and not isinstance(layer["presence_penalty"], dict)


def _apply_override_layer(base: dict, layer: dict, _mode_key: str, thinking: bool) -> None:
    if not layer:
        return
    flat = {
        k: layer[k]
        for k in _FLAT_SAMPLING_KEYS
        if k in layer and not isinstance(layer[k], dict)
    }
    if flat:
        base.update(flat)
        _sync_repeat_penalty(base, flat)
    mode_dict = _select_override_mode_dict(layer, _mode_key, thinking)
    if isinstance(mode_dict, dict):
        base.update(mode_dict)
        _sync_repeat_penalty(base, mode_dict)


def resolve_presence_penalty(
    profile: dict,
    model_name: str,
    thinking: bool = False,
    settings: dict | None = None,
    *,
    mode: str = "",
    default_cap: float = 0.2,
) -> float:
    """Return presence_penalty, honoring explicit model JSON or UI overrides."""
    raw = float((profile or {}).get("presence_penalty", 0.0))
    _mode_key = mode or ("thinking" if thinking else "non_thinking")
    explicit = (
        _has_explicit_presence(_settings_sampling_override(model_name, settings), _mode_key, thinking)
        or _has_explicit_presence(_registry_sampling(model_name), _mode_key, thinking)
    )
    return raw if explicit else min(raw, default_cap)


def _pick_profile_subdict(profile: dict, _mode_key: str, thinking: bool) -> dict:
    if _mode_key in profile:
        return dict(profile[_mode_key])
    if thinking or _mode_key in ("thinking", "sampling_thinking_text"):
        for k in ("thinking", "sampling_thinking_text", "non_thinking", "sampling_text"):
            if k in profile:
                return dict(profile[k])
    else:
        for k in ("non_thinking", "sampling_text"):
            if k in profile:
                return dict(profile[k])
    return {}


def _sanitize_unprofiled_sampling(raw: object) -> dict[str, float | int]:
    """Extract valid numeric sampling fields from settings or runtime globals."""
    if not isinstance(raw, dict):
        return {}
    clean: dict[str, float | int] = {}
    for k in _FLAT_SAMPLING_KEYS:
        if k not in raw:
            continue
        v = raw[k]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if not math.isfinite(float(v)):
            continue
        clean[k] = int(v) if k in ("top_k", "seed") else float(v)
    return clean


def _apply_unprofiled_layer(target: dict[str, float | int], layer: dict[str, float | int]) -> None:
    if not layer:
        return
    if "repetition_penalty" in layer and "repeat_penalty" not in layer:
        target["repeat_penalty"] = layer["repetition_penalty"]
    elif "repeat_penalty" in layer and "repetition_penalty" not in layer:
        target["repetition_penalty"] = layer["repeat_penalty"]
    target.update(layer)
    _sync_repeat_penalty(target, target)


def _resolve_unprofiled_sampling_layer(settings: dict | None = None) -> dict[str, float | int]:
    """Merge fallback -> runtime DEFAULT_UNPROFILED_SAMPLING -> settings override."""
    resolved: dict[str, float | int] = dict(_FALLBACK_UNPROFILED_SAMPLING)
    _apply_unprofiled_layer(resolved, _sanitize_unprofiled_sampling(DEFAULT_UNPROFILED_SAMPLING))
    if isinstance(settings, dict):
        _apply_unprofiled_layer(
            resolved, _sanitize_unprofiled_sampling(settings.get("default_unprofiled_sampling")),
        )
    return resolved


def get_sampling_profile(model_name: str, thinking: bool = False, settings: dict | None = None, *, mode: str = "") -> dict:
    """Resolve sampling for model_name: DEFAULT_PROFILE (+ unprofiled defaults) <- model JSON <- UI/runtime override."""
    _mode_key = mode or ("thinking" if thinking else "non_thinking")
    base = _pick_profile_subdict(DEFAULT_PROFILE, _mode_key, thinking)
    _sync_repeat_penalty(base, base)

    reg_sampling = _registry_sampling(model_name)
    if not reg_sampling:
        _apply_override_layer(
            base, _resolve_unprofiled_sampling_layer(settings), _mode_key, thinking,
        )
    else:
        _apply_override_layer(base, reg_sampling, _mode_key, thinking)
    _apply_override_layer(base, _settings_sampling_override(model_name, settings), _mode_key, thinking)

    _sync_repeat_penalty(base, base)
    return base


from routing.model_automap import MODEL_PROFILES as _MODEL_PROFILES_CACHE

_model_cap_overrides: dict = {}


def update_model_capability_overrides(overrides: dict | None = None):
    _model_cap_overrides.clear()
    _model_cap_overrides.update(overrides or {})


def _model_profile(model: str) -> dict:
    base = _MODEL_PROFILES_CACHE.get(model.split(":")[0], _MODEL_PROFILES_CACHE.get(model, {}))
    result = dict(base)
    try:
        from model_configs.models_registry import get_capabilities as _reg_caps
        _user_caps = _reg_caps(model)
        if _user_caps:
            result.update(_user_caps)
    except Exception:
        pass
    _prefix = model.split(":")[0]
    _ovr = _model_cap_overrides.get(_prefix, _model_cap_overrides.get(model))
    if _ovr:
        for _k in ("thinking", "tool_call", "vision"):
            if _k in _ovr:
                result[_k] = bool(_ovr[_k])
    return result


def _model_thinking_default(model_name: str) -> bool:
    """Resolve default thinking capability for model_name (profile -> gguf_meta)."""
    prof = _model_profile(model_name)
    if "thinking" in prof:
        return bool(prof["thinking"])
    try:
        from routing.model_automap import get_model_capabilities
        caps = get_model_capabilities(model_name)
        if "thinking" in caps:
            return bool(caps["thinking"])
    except (ImportError, AttributeError, TypeError, ValueError, OSError) as _exc:
        _smp_log.debug("thinking capability fallback for %s failed: %s", model_name, _exc)
    return False


def model_supports_thinking(model_name: str) -> bool:
    """Return True when model_name natively supports thinking/reasoning."""
    return _model_thinking_default(model_name)


def resolve_agent_sampling(
    model_name: str,
    *,
    agent: object | None = None,
    temperature_override: float | None = None,
    thinking_override: bool | None = None,
    thinking_budget_override: int | None = None,
    settings: dict | None = None,
    role: str = "",
    mode: str = "",
    fallback_temp: float = 0.3,
    default_cap: float = 0.2,
) -> tuple[float, bool, int, dict]:
    """Resolve (temperature, thinking, thinking_budget, sampler) for an agent call.

    - thinking=None inherits the model capability (_model_profile / gguf_meta).
    - temperature=None ("Model Default") resolves from get_sampling_profile().
    - Returns a sampler dict with resolved presence_penalty and aligned repeat keys.
    """
    raw_think = thinking_override
    if raw_think is None and agent is not None:
        raw_think = getattr(agent, "thinking", None)
    _supports_think = model_supports_thinking(model_name)
    eff_think = _supports_think if raw_think is None else bool(raw_think)

    raw_budget = thinking_budget_override
    if raw_budget is None and agent is not None:
        raw_budget = getattr(agent, "thinking_budget", 0)
    try:
        eff_budget = max(0, int(raw_budget or 0))
    except (TypeError, ValueError):
        eff_budget = 0

    _effort_settings = settings if role in _EFFORT_USER_ROLES else None
    _user_eff = normalize_reasoning_effort((_effort_settings or {}).get("reasoning_effort"))
    if _user_eff == "none":
        eff_think = False
        eff_budget = 0
    elif _user_eff in ("low", "medium", "high", "max") and _supports_think:
        eff_think = True
        eff_budget = REASONING_EFFORT_BUDGETS[_user_eff]

    smp = get_sampling_profile(model_name, thinking=eff_think, settings=settings, mode=mode)
    _resolved_eff = resolve_reasoning_effort(
        _effort_settings, smp, thinking=eff_think, model_name=model_name,
    )
    if _resolved_eff:
        smp["reasoning_effort"] = _resolved_eff
    else:
        smp.pop("reasoning_effort", None)
    smp["presence_penalty"] = resolve_presence_penalty(
        smp, model_name, thinking=eff_think, settings=settings, mode=mode, default_cap=default_cap,
    )

    raw_temp = temperature_override
    if raw_temp is None and agent is not None:
        raw_temp = getattr(agent, "temperature", None)
    if raw_temp is not None:
        try:
            eff_temp = float(raw_temp)
        except (TypeError, ValueError):
            eff_temp = float(smp.get("temperature", fallback_temp))
    else:
        eff_temp = float(smp.get("temperature", fallback_temp))
    smp["temperature"] = eff_temp
    return eff_temp, eff_think, eff_budget, smp
