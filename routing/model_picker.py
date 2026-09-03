"""Model-Picker (aus server.py extrahiert)."""

# Phase A: Runtime-Dependencies
pipeline = None
settings = None

_SIMPLE_DIRECT_MODELS = {}
_COMPLEX_DIRECT_MODELS = {}


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

def _pick_direct_model(complexity: str, task_type: str, available: list) -> str:


    # Manuelles direct-Pinning respektieren.
    _configured_direct = pipeline.agents["direct"].model if "direct" in pipeline.agents else ""
    _direct_pinned = "direct" in settings.get("automap_excluded", [])
    if _direct_pinned and _configured_direct and _configured_direct in available:
        return _configured_direct

    if task_type in ("vision", "ocr"):
        _vision_priority = [
            "qwen3.5:4b", "qwen3.5:2b", "qwen3.5:0.8b", "qwen3.5:9b-ud",
            "qwen3.6:35b-a3b-ud", "qwen3.6:35b-a3b-uncensored",
            "hermes3.6:35b-a3b-uncensored-genesis-v7-mtp-apex-compact",
            "hermes3.6:35b-a3b-uncensored-genesis-v10-mtp-apex-compact",
            "gemma-4:e4b-it-obliterated", "gemma-4:e4b-it-qat-ud",
        ]
        for cand in _vision_priority:
            if cand in available:
                return cand
        _multimodal_bases = ("qwen3.5", "qwen3.6", "hermes", "gemma-4")
        for m in available:
            if m.split(":")[0] in _multimodal_bases:
                return m

    if complexity == "trivial":
        _has_prepro = task_type not in ("vision",)
        if _has_prepro:
            for candidate in ["qwen3.5:2b", "qwen3.5:0.8b", "gemma3:4b", "granite4:1b"]:
                if candidate in available:
                    return candidate
        else:
            for candidate in ["gemma3:4b", "granite4:1b", "qwen3.5:2b"]:
                if candidate in available:
                    return candidate
        return available[0] if available else "gemma3:4b"

    table = _COMPLEX_DIRECT_MODELS if complexity == "complex" else _SIMPLE_DIRECT_MODELS
    preferred = table.get(task_type, "gemma3:4b")
    if preferred in available:
        return preferred

    fallback_by_task = {
        "code":       ["qwen3.5:4b", "qwen3.5:2b", "qwen3.5:9b-ud"],
        "math":       ["qwen3.5:9b-ud", "qwen3.5:4b"],
        "factual":    ["qwen3.5:4b", "qwen3.5:2b", "gemma3:4b"],
        "reasoning":  ["qwen3.5:9b-ud", "qwen3.5:4b", "gemma3:4b"],
        "tool_use":   ["qwen3.5:4b", "qwen3.5:2b", "granite-4.1:3b"],
        "vision":     ["granite3.2-vision:2b", "gemma3:4b"],
        "multilingual": ["ministral-3:3b", "gemma3:4b"],
    }
    for fallback in fallback_by_task.get(task_type, ["gemma3:4b", "qwen3.5:2b"]):
        if fallback in available:
            return fallback

    return pipeline.agents["direct"].model if "direct" in pipeline.agents else preferred

