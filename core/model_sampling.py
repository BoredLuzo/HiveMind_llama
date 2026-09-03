# -*- coding: utf-8 -*-


from __future__ import annotations

QWEN36_PROFILE = {
    # Thinking-Mode (Planner) — Qwen3 Thinking Text-Empfehlung
    "sampling_thinking_text": {
        "temperature": 1.0, "top_p": 0.95, "top_k": 20,
        "min_p": 0.0, "presence_penalty": 1.5, "repetition_penalty": 1.0,
        "preserve_thinking": True, "cache_prompt": True,
    },
    # Non-Thinking Coding (Coder) — Unsloth Qwen3.6 Agent-Loop-Empfehlung
    # (docs/models/qwen3.6.md): presence_penalty=1.5 als Anti-Repetition im
    "sampling_thinking_code": {
        "temperature": 0.6, "top_p": 0.95, "top_k": 20,
        "min_p": 0.0, "presence_penalty": 1.5, "repetition_penalty": 1.0,
        "cache_prompt": True,
    },
    # Non-thinking Text (default non-thinking)
    "sampling_text": {
        "temperature": 0.6, "top_p": 0.95, "top_k": 20,
        "min_p": 0.0, "presence_penalty": 1.5, "repetition_penalty": 1.0,
        "cache_prompt": True,
    },
    # Legacy aliases: thinking=True → sampling_thinking_text (Planner)
    #                  thinking=False → sampling_text (Coder)
    "thinking":     {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5, "repetition_penalty": 1.0, "preserve_thinking": True, "cache_prompt": True},
    "non_thinking": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 1.5, "repetition_penalty": 1.0, "cache_prompt": True},
}

QWEN35_PROFILE = {
    # Primary use case: Non-thinking, Text (Pre-Explore, Contract-Generierung)
    "sampling_text": {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    },
    "sampling_vl": {
        "temperature": 0.7,
        "top_p": 0.80,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
    },
    # Thinking, Text
    "sampling_thinking_text": {
        "temperature": 1.0,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 1.5,
        "repetition_penalty": 1.0,
    },
    # Thinking, VL/Coding
    "sampling_thinking_code": {
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "min_p": 0.0,
        "presence_penalty": 0.0,
        "repetition_penalty": 1.0,
    },
    # Legacy aliases: thinking=True → sampling_thinking_code, thinking=False → sampling_text
    "thinking":     {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0, "preserve_thinking": True, "cache_prompt": True},
    "non_thinking": {"temperature": 1.0, "top_p": 1.0, "top_k": 20, "min_p": 0.0, "presence_penalty": 2.0, "repetition_penalty": 1.0, "cache_prompt": True},
}

LFM25_PROFILE = {
    "sampling_text": {
        "temperature": 0.2, "top_p": 1.0, "top_k": 80,
        "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.05,
        "cache_prompt": True,
    },
    "non_thinking": {"temperature": 0.2, "top_p": 1.0, "top_k": 80, "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.05, "cache_prompt": True},
}

# Ling-3.0-tiny (InclusionAI, 2026-08-19): 1.4B aktiver MoE, Thinking per Default
LING3_PROFILE = {
    "sampling_text": {
        "temperature": 1.0, "top_p": 0.95, "top_k": 20,
        "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0,
        "cache_prompt": True,
    },
    "sampling_thinking_text": {
        "temperature": 1.0, "top_p": 0.95, "top_k": 20,
        "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0,
        "preserve_thinking": True, "cache_prompt": True,
    },
    "sampling_thinking_code": {
        "temperature": 1.0, "top_p": 0.95, "top_k": 20,
        "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0,
        "cache_prompt": True,
    },
    "thinking":     {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0, "preserve_thinking": True, "cache_prompt": True},
    "non_thinking": {"temperature": 1.0, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "repetition_penalty": 1.0, "cache_prompt": True},
}

DEFAULT_PROFILE = {
    #   Qwen (offiziell, Agent/Non-Thinking): temp 0.7, top_p 0.8, top_k 20, presence 1.5
    #   Gemma 3 Instruct (offiziell):         temp 1.0, top_p 0.95, top_k 64, presence k.A.
    # Mittelwerte: temp 0.8, top_p 0.9, top_k 40, presence 1.0 (moderate
    "thinking": {
        "temperature": 0.8, "top_p": 0.9, "top_k": 40,
        "min_p": 0.0, "presence_penalty": 1.0, "repetition_penalty": 1.0,
        "preserve_thinking": False, "cache_prompt": False,
    },
    "non_thinking": {
        "temperature": 0.8, "top_p": 0.9, "top_k": 40,
        "min_p": 0.0, "presence_penalty": 1.0, "repetition_penalty": 1.0,
        "cache_prompt": False,
    },
}

FAMILY_PROFILES: dict[str, dict] = {
    "qwen3.6": QWEN36_PROFILE,
    "qwen3.5": QWEN35_PROFILE,
    "lfm2.5":  LFM25_PROFILE,
    "ling-3.0-tiny": LING3_PROFILE,
    "hermes3.6": QWEN36_PROFILE,
    # "qwen4.0": QWEN40_PROFILE,
    # "deepseek-v4": DEEPSEEK_PROFILE,
}


def get_sampling_profile(model_name: str, thinking: bool = False, settings: dict | None = None, *, mode: str = "") -> dict:


    if mode:
        _mode_key = mode
    else:
        _mode_key = "thinking" if thinking else "non_thinking"
    for family, profile in FAMILY_PROFILES.items():
        if model_name.lower().split(":")[0].startswith(family):
            base = dict(profile.get(_mode_key, profile.get("non_thinking", profile.get("sampling_text", {}))))
            break
    else:
        base = dict(DEFAULT_PROFILE.get(_mode_key, DEFAULT_PROFILE.get("non_thinking", DEFAULT_PROFILE.get("sampling_text", {}))))
    overrides = (settings or {}).get("_model_sampling_overrides", {})
    model_key = model_name.split(":")[0]
    model_override = overrides.get(model_name, overrides.get(model_key, {}))
    if model_override:
        base.update(model_override.get(_mode_key, {}))
    return base


from routing.model_automap import MODEL_PROFILES as _MODEL_PROFILES_CACHE
from core.duo_helpers import _model_cap_overrides


def _model_profile(model: str) -> dict:
    base = _MODEL_PROFILES_CACHE.get(model.split(":")[0], _MODEL_PROFILES_CACHE.get(model, {}))
    result = dict(base)
    # user config (model_configs/models/*.json) has highest priority.
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
