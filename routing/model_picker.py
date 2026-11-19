"""Model picker with dynamic capability and VRAM scoring over installed models."""
from __future__ import annotations

import logging

logger = logging.getLogger("hivemind.model_picker")

# Runtime-injected dependencies
pipeline = None
settings = None

_SIMPLE_DIRECT_MODELS: dict[str, str] = {}
_COMPLEX_DIRECT_MODELS: dict[str, str] = {}


def reload_compat_views() -> None:
    """Populate simple/complex direct fallback preferences from arch_compat.json."""
    try:
        from model_configs.models_registry import load_arch_compat as _load_compat
        compat = _load_compat()
    except (ImportError, OSError, ValueError, TypeError) as _e:
        logger.debug("model_picker compat reload skipped: %s", _e)
        return

    simple = compat.get("simple_direct_models") or {}
    comp = compat.get("complex_direct_models") or {}
    if isinstance(simple, dict):
        _SIMPLE_DIRECT_MODELS.clear()
        _SIMPLE_DIRECT_MODELS.update({str(k): str(v) for k, v in simple.items() if v})
    if isinstance(comp, dict):
        _COMPLEX_DIRECT_MODELS.clear()
        _COMPLEX_DIRECT_MODELS.update({str(k): str(v) for k, v in comp.items() if v})


reload_compat_views()


def init_model_picker(pipeline_obj=None, settings_dict=None,
                      simple_direct=None, complex_direct=None):
    global pipeline, settings, _SIMPLE_DIRECT_MODELS, _COMPLEX_DIRECT_MODELS
    if pipeline_obj:
        pipeline = pipeline_obj
    if settings_dict is not None:
        settings = settings_dict
    if simple_direct:
        _SIMPLE_DIRECT_MODELS = simple_direct
    if complex_direct:
        _COMPLEX_DIRECT_MODELS = complex_direct


def _vram_budget_gb() -> float:
    raw = (settings or {}).get("vram_budget_gb", 0.0) if isinstance(settings, dict) else 0.0
    try:
        val = float(raw or 0.0)
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return 0.0


def _score_installed_candidate(
    model: str,
    *,
    task_type: str,
    complexity: str,
    budget_gb: float,
    tie_list: list[str],
) -> tuple:
    from routing.model_automap import (
        _estimate_param_b,
        _estimate_vram_gb_for_model,
        get_model_capabilities,
    )
    caps = get_model_capabilities(model)
    est_vram = float(_estimate_vram_gb_for_model(model))
    pb = float(_estimate_param_b(model))
    score = 0

    if task_type in ("vision", "ocr"):
        score += 10 if caps.get("vision") else -10
    if task_type in ("code", "tool_use", "general") and caps.get("tool_call"):
        score += 4
    if task_type in ("reasoning", "math", "code") or complexity == "complex":
        if caps.get("thinking"):
            score += 3
    if complexity == "trivial" and pb <= 4.5:
        score += 3

    if budget_gb > 0:
        if est_vram <= budget_gb:
            score += 4
        else:
            score -= int((est_vram - budget_gb) * 4) + 1

    # Installed legacy tie-breaker preference only (higher bonus for earlier slot)
    if model in tie_list:
        score += max(1, len(tie_list) - tie_list.index(model))

    size_key = pb if complexity == "complex" else -est_vram
    return (score, size_key, model)


def _pick_direct_model(complexity: str, task_type: str, available: list) -> str:
    avail = [str(m) for m in (available or []) if m]
    agents = getattr(pipeline, "agents", None) or {}
    cfg_dict = settings if isinstance(settings, dict) else {}

    # Respect manual direct model selection unless running in AutoMap mode.
    _configured_direct = agents["direct"].model if "direct" in agents else ""
    _direct_pinned = "direct" in cfg_dict.get("automap_excluded", []) or cfg_dict.get("mode") != "automap"
    if _direct_pinned and _configured_direct and _configured_direct in avail:
        return _configured_direct

    budget_gb = _vram_budget_gb()

    if task_type in ("vision", "ocr") and avail:
        from routing.model_automap import get_model_capabilities, _estimate_vram_gb_for_model
        vision_avail = [m for m in avail if get_model_capabilities(m).get("vision")]
        if vision_avail:
            _vision_tie = [
                "qwen3.5:4b", "qwen3.5:2b", "qwen3.5:0.8b", "qwen3.5:9b-ud",
                "gemma-4:e4b-it-qat", "gemma-4:e4b-it-obliterated",
            ]
            return max(
                vision_avail,
                key=lambda m: _score_installed_candidate(
                    m,
                    task_type=task_type,
                    complexity=complexity,
                    budget_gb=budget_gb,
                    tie_list=_vision_tie,
                ) if complexity != "trivial" else (
                    1 if (budget_gb <= 0 or _estimate_vram_gb_for_model(m) <= budget_gb) else 0,
                    -_estimate_vram_gb_for_model(m),
                    m,
                ),
            )

    if complexity == "trivial":
        if not avail:
            return _configured_direct or "gemma3:4b"
        _trivial_tie = (
            ["qwen3.5:2b", "qwen3.5:0.8b", "gemma3:4b", "granite4:1b"]
            if task_type not in ("vision",)
            else ["gemma3:4b", "granite4:1b", "qwen3.5:2b"]
        )
        return max(
            avail,
            key=lambda m: _score_installed_candidate(
                m, task_type=task_type, complexity="trivial", budget_gb=budget_gb, tie_list=_trivial_tie
            ),
        )

    table = _COMPLEX_DIRECT_MODELS if complexity == "complex" else _SIMPLE_DIRECT_MODELS
    preferred = table.get(task_type, "")
    if preferred and preferred in avail:
        return preferred

    fallback_by_task = {
        "code":         ["qwen3.5:4b", "qwen3.5:2b", "qwen3.5:9b-ud"],
        "math":         ["qwen3.5:9b-ud", "qwen3.5:4b"],
        "factual":      ["qwen3.5:4b", "qwen3.5:2b", "gemma3:4b"],
        "reasoning":    ["qwen3.5:9b-ud", "qwen3.5:4b", "gemma3:4b"],
        "tool_use":     ["qwen3.5:4b", "qwen3.5:2b", "granite-4.1:3b"],
        "vision":       ["granite3.2-vision:2b", "gemma3:4b"],
        "multilingual": ["ministral-3:3b", "gemma3:4b"],
    }
    if avail:
        tie = fallback_by_task.get(task_type, [])
        return max(
            avail,
            key=lambda m: _score_installed_candidate(
                m, task_type=task_type, complexity=complexity, budget_gb=budget_gb, tie_list=tie
            ),
        )

    return _configured_direct or preferred or table.get("general", "")

