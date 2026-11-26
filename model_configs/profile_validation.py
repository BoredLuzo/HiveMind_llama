# -*- coding: utf-8 -*-
"""Shared model-profile and sampling validation helpers for routers/config and routers/models."""
from __future__ import annotations

import math

SAMPLING_NUMERIC_BOUNDS: dict[str, tuple[float, float, bool]] = {
    "temperature": (0.0, 2.0, False),
    "top_p": (0.0, 1.0, False),
    "top_k": (0.0, 1000.0, True),
    "min_p": (0.0, 1.0, False),
    "presence_penalty": (-2.0, 2.0, False),
    "repetition_penalty": (0.5, 2.0, False),
    "repeat_penalty": (0.5, 2.0, False),
    "seed": (-1.0, 2147483647.0, True),
    "thinking_budget": (0.0, 131072.0, True),
}
SAMPLING_BOOL_KEYS = frozenset({"preserve_thinking", "cache_prompt"})
VALID_REASONING_EFFORT_KEYS = frozenset({
    "auto", "none", "on", "minimal", "low", "medium", "high", "xhigh", "max",
})
CAPABILITY_KEYS = frozenset({"thinking", "vision", "tool_call"})


def validate_sampling_block(block: dict, prefix: str = "sampling") -> str | None:
    """Validate nested or flat per-model sampling parameters."""
    for k, v in block.items():
        if v is None:
            continue
        if k in SAMPLING_BOOL_KEYS:
            if not isinstance(v, bool):
                return f"{prefix}.{k} requires boolean or null"
        elif k in SAMPLING_NUMERIC_BOUNDS:
            lo, hi, is_int = SAMPLING_NUMERIC_BOUNDS[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                return f"{prefix}.{k} requires finite number or null"
            if is_int and not isinstance(v, int):
                return f"{prefix}.{k} requires integer or null"
            if float(v) < lo or float(v) > hi:
                return f"{prefix}.{k} must be between {lo:g} and {hi:g}"
        elif k == "reasoning_effort":
            if not isinstance(v, str) or v.strip().lower() not in VALID_REASONING_EFFORT_KEYS:
                return f"{prefix}.{k} must be one of auto/none/on/minimal/low/medium/high/xhigh/max or null"
        elif isinstance(v, dict):
            err = validate_sampling_block(v, f"{prefix}.{k}")
            if err:
                return err
        else:
            return f"unsupported sampling key '{prefix}.{k}'"
    return None


def validate_capabilities_dict(
    val: object, *, drop_none: bool = False
) -> tuple[dict[str, bool] | None, str | None]:
    """Validate a model profile's capabilities mapping."""
    if val is None:
        return None, None
    if not isinstance(val, dict):
        return None, "key 'capabilities' requires an object or null"
    clean_caps: dict[str, bool] = {}
    for ck, cv in val.items():
        if ck not in CAPABILITY_KEYS:
            return None, f"unsupported capability '{ck}'"
        if cv is None:
            continue
        if not isinstance(cv, bool):
            return None, f"capabilities.{ck} requires boolean or null"
        clean_caps[ck] = cv
    if drop_none:
        return (clean_caps or None), None
    return clean_caps, None
