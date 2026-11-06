"""Pure ctx-override lookup (2026-11-05 cycle break 5/5).

Extracted from core/state.py: the function touches no state (it takes the
override dict as an argument), and hive_functions.num_ctx_config needed it —
a two-way lazy cycle state ↔ num_ctx_config. num_ctx_config imports this
leaf statically now; core.state keeps its lazy table import (pinned by
tests/test_profile_launch_flags.py).
"""
from __future__ import annotations


def _ctx_override_lookup(ov: dict, model_name: str,
                         agent_role: str | None) -> int | None:
    """ctx_overrides lookup: legacy flat shape (role key, model keys, base,
    default) and nested {"roles": {}, "models": {}} shape. Returns int|None."""
    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None

    base = model_name.rsplit("#", 1)[0] if "#" in model_name else model_name
    base_name = model_name.split(":")[0] if model_name else ""

    # Legacy flat shape
    if agent_role:
        _r = _int(ov.get(agent_role))
        if _r:
            return _r
    for mk in (model_name, base, base_name):
        if mk:
            _m = _int(ov.get(mk))
            if _m:
                return _m

    # Nested shape
    roles = ov.get("roles")
    if agent_role and isinstance(roles, dict):
        _r = _int(roles.get(agent_role))
        if _r:
            return _r
    models = ov.get("models")
    if isinstance(models, dict):
        for mk in (model_name, base_name):
            if mk:
                _m = _int(models.get(mk))
                if _m:
                    return _m
    return None
