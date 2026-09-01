"""routing/model_automap.py - Automatic model selection, mapping, and profiles.

Provides MODEL_PROFILES (capability dict per model), automap routing,
display mapping, and vision/tool-call feature detection.

v0.96 FIXES:
  - VRAM-aware scoring: oversized models (27b+ on 8GB) get penalty
  - Cleaned MODEL_PROFILES: removed exotic/unlikely models
  - qwen3.6 profile corrected: tool_call=True (supports function calling via chat-template)
"""
from __future__ import annotations
import logging, os, json, re

from settings import load_settings as _load_settings
settings = _load_settings()  # Runtime settings dict
from pathlib import Path

logger = logging.getLogger("hivemind.model_automap")

#         vision (image input), tool_call (function calling).
MODEL_PROFILES: dict[str, dict] = {
    "qwen3.5":     {"thinking": True,  "vision": True, "tool_call": True},
    "qwen3-d":     {"thinking": True,  "vision": False, "tool_call": False},
    "qwen3.6":     {"thinking": True,  "vision": True, "tool_call": True},  # embedded encoder / mmproj
    "hermes3.6":   {"thinking": True,  "vision": True, "tool_call": True},
    "hermes":      {"thinking": True,  "vision": True, "tool_call": True},
    # Tiel-Coder (2026-08-31): vision tower from Ornith-1.5 (mmproj-BF16.gguf),
    # Tool calls + thinking like hermes3.6. EXPLICITLY needed — the generic
    # the "coder" heuristic below would wrongly set vision/thinking to False.
    "tiel-coder":  {"thinking": True,  "vision": True, "tool_call": True},
    "lfm2.5":      {"thinking": False, "vision": False, "tool_call": True},
    # Ling-3.0-tiny (InclusionAI, 2026-08-19): MoE, Thinking per Default an.
    "ling-3.0-tiny": {"thinking": True, "vision": False, "tool_call": True},
    "qwq":         {"thinking": True,  "vision": False, "tool_call": True},
    "bonsai":      {"thinking": True,  "vision": False, "tool_call": False},
    "magistral":   {"thinking": True,  "vision": False, "tool_call": True},
    "thinker":     {"thinking": True,  "vision": False, "tool_call": False},
    # ── IBM Granite ──────────────────────────────────────────────────────
    "granite4":    {"thinking": False, "vision": False, "tool_call": True},
    "granite3":    {"thinking": False, "vision": False, "tool_call": True},
    "granite":     {"thinking": False, "vision": False, "tool_call": True},
    # ── Mistral / Ministral / Devstral ───────────────────────────────────
    "ministral":   {"thinking": False, "vision": False, "tool_call": True},
    "mistral-small":{"thinking": False, "vision": False, "tool_call": True},
    "devstral":    {"thinking": False, "vision": False, "tool_call": True},
    "mistral":     {"thinking": False, "vision": False, "tool_call": True},
    "mixtral":     {"thinking": False, "vision": False, "tool_call": True},
    "qwen3-coder":   {"thinking": False, "vision": False, "tool_call": True},
    "codestral":     {"thinking": False, "vision": False, "tool_call": True},
    "llava":         {"thinking": False, "vision": True,  "tool_call": False},
    "llava-next":    {"thinking": False, "vision": True,  "tool_call": False},
    "minicpm-v":     {"thinking": False, "vision": True,  "tool_call": False},
    "llama3.2-vision":{"thinking": False, "vision": True,  "tool_call": False},
    "pixtral":       {"thinking": False, "vision": True,  "tool_call": False},
    "gemma3-vl":     {"thinking": False, "vision": True,  "tool_call": False},
    "llama3.1":      {"thinking": False, "vision": False, "tool_call": True},
    "llama3":        {"thinking": False, "vision": False, "tool_call": True},
    "llama3.2":      {"thinking": False, "vision": False, "tool_call": True},
    "llama3.3":      {"thinking": False, "vision": False, "tool_call": True},
    "phi3":          {"thinking": False, "vision": False, "tool_call": True},
    "phi3.5":        {"thinking": False, "vision": False, "tool_call": True},
    "phi4":          {"thinking": False, "vision": False, "tool_call": True},
    "gemma3":        {"thinking": False, "vision": False, "tool_call": True},
    # VISION-FIX (2026-08-19): gemma-4 is based on gemma2/3 arch → embedded
    "gemma-4-e4b-it":{"thinking": False, "vision": True,  "tool_call": True},  # Obliterated Fine-Tune
    "gemma-4":       {"thinking": False, "vision": True,  "tool_call": True},
    "gemma-4-e4b-it-qat": {"thinking": False, "vision": True, "tool_call": True},
    "qwen":          {"thinking": False, "vision": False, "tool_call": False},
    # ── Command-R / Hermes / Dolphin ────────────────────────────────────
    "command-r":     {"thinking": False, "vision": False, "tool_call": True},
    "hermes":        {"thinking": False, "vision": False, "tool_call": True},
    "hermes3":       {"thinking": True,  "vision": False, "tool_call": True},
    "dolphin":       {"thinking": False, "vision": False, "tool_call": True},
    "dolphin3":      {"thinking": True,  "vision": False, "tool_call": True},
    # ── RNJ / Custom Models ──────────────────────────────────────────────
    "rnj":           {"thinking": False, "vision": False, "tool_call": True},
    # ── InternLM ────────────────────────────────────────────────────────
    "internlm2":     {"thinking": False, "vision": False, "tool_call": True},
    "internlm3":     {"thinking": True,  "vision": False, "tool_call": True},
}

# ── VRAM-Estimation: Modellname → geschaetzte Parameterzahl (Milliarden) ────
_SIZE_PATTERNS: list[tuple[str, float]] = [
    ("1.5b", 1.5), ("70b", 70), ("32b", 32), ("27b", 27),
    ("15b", 15), ("14b", 14), ("12b", 12), ("9b", 9),
    ("8b", 8), ("7b", 7), ("5b", 5), ("4b", 4),
    ("3b", 3), ("2b", 2), ("1b", 1),
]


def _estimate_param_b(model_name: str) -> float:
    """Estimate parameter count from model name (e.g. 'qwen3.5:4b' → 4.0)."""
    lo = model_name.lower()
    tag = lo.split(":")[-1] if ":" in lo else lo
    for pattern, param_b in _SIZE_PATTERNS:
        if pattern in tag:
            return param_b
    return 4.0  # Default: medium


_QUANT_FACTORS: dict[str, float] = {
    "q4": 0.6, "q4_k_s": 0.55, "q4_k_m": 0.6, "q5": 0.7,
    "q5_k_m": 0.7, "q6": 0.75, "q8": 0.9, "f16": 1.9,
}


def _is_moe_model(model_name: str) -> bool:
    """Detect MoE architecture from model name/tag."""
    _lo = model_name.lower()
    _tag = _lo.split(":")[-1] if ":" in _lo else _lo
    return bool(re.search(r"\d+x\d+b|\d+b-a\d+b|mixtral|moe", _lo))


def _moe_active_b(model_name: str, total_b: float) -> float | None:
    """Extract active parameter count from MoE model tag (e.g. '35b-a3b' → 3.0)."""
    _tag = model_name.lower().split(":")[-1] if ":" in model_name.lower() else model_name.lower()
    m = re.search(r"a(\d+(?:\.\d+)?)b", _tag)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)x(\d+(?:\.\d+)?)b", _tag)
    if m:
        return float(m.group(2))
    return None


def _estimate_vram_gb(param_b: float, quant: str = "q4", is_moe: bool = False, active_b: float | None = None) -> float:


    base = param_b * _QUANT_FACTORS.get(quant, 0.6)
    if is_moe and active_b is not None and active_b > 0 and active_b < param_b:
        active_fraction = active_b / param_b
        base = base * active_fraction + (base * (1 - active_fraction)) * 0.15
    return base


def _estimate_vram_gb_for_model(model_name: str, quant: str = "q4") -> float:
    """Convenience wrapper: detects MoE architecture and applies correction automatically."""
    try:
        from model_configs.models_registry import get_vram_gb_override as _reg_vram
        _ovr = _reg_vram(model_name)
        if _ovr:
            return _ovr
    except Exception:
        pass
    _pb = _estimate_param_b(model_name)
    _moe = _is_moe_model(model_name)
    _ab = _moe_active_b(model_name, _pb) if _moe else None
    return _estimate_vram_gb(_pb, quant=quant, is_moe=_moe, active_b=_ab)


# ── Vision preprocessing allowlist ──────────────────────────────────────────
_VISION_PREPROCESSING_ALLOWLIST: list[str] = [
    "minicpm-v", "llava", "qwen2-vl", "qwen2.5-vl", "pixtral",
    "llava-next", "gemma3-vl", "internvl",
    "qwen3.5", "qwen3.6", "hermes3.6", "hermes", "gemma-4", "tiel-coder",
]

# ── Automap-State ──────────────────────────────────────────────────────────
_automap_data: dict = {}
_routing_weights: dict = {}

def load_routing_weights(base_path: str = "") -> dict:
    """Load routing weights from disk into the canonical flat format."""
    global _routing_weights
    try:
        p = Path(__file__).parent.parent / "routing_weights.json"
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                # Detect format: nested has sub-dicts, flat has "model|task_type" keys with wins/losses
                _first_key = next(iter(raw), "")
                if "|" in _first_key:
                    # Already flat format — use directly
                    _routing_weights = raw
                else:
                    # Old nested format — start fresh, it'll be overwritten on next save
                    _routing_weights = {}
    except Exception as e:
        logger.warning("routing_weights load failed: %s", e)
    return _routing_weights

load_routing_weights()
_automap_path: Path = Path(__file__).parent.parent / "automap.json"


def load_automap() -> dict:
    """Load automap data from disk."""
    global _automap_data
    try:
        if _automap_path.exists():
            with open(_automap_path, "r", encoding="utf-8") as f:
                _automap_data = json.load(f)
    except Exception as e:
        logger.warning("automap.json load failed: %s", e)
    return _automap_data


def save_automap(data: dict) -> None:
    """Save automap data to disk."""
    global _automap_data
    _automap_data = data
    try:
        with open(_automap_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("automap.json save failed: %s", e)


def get_automap(query: str = "", available: list | None = None, *,
                has_images: bool = False, task_type_override: str = "",
                base_path: str = "", vram_budget_gb: float = 0.0) -> dict:
    """Return automap assignments for the given query and available models.

    Args:
        query: User input text for task-type detection.
        available: List of available model names (from llama.cpp/Ollama).
        has_images: Whether images are attached.
        task_type_override: Force a specific task type (e.g. "vision").
        base_path: Project base path (for config file lookups).
        vram_budget_gb: VRAM budget in GB (e.g. 7.5 for a 8GB GPU).
                       If 0.0, no VRAM filter (backward compat).

    Returns:
        Dict with keys: task_type, assignments, reasoning, display_map.
    """
    # Lazy-load persisted automap data
    if not _automap_data:
        load_automap()

    # Determine task type from query
    task_type = task_type_override or detect_task_type(query or "", has_images=has_images)

    # Build assignments: prefer persisted, fall back to available models
    available = available or []
    assignments = {}

    # Role → capability requirements for automap
    ROLE_REQUIREMENTS = {
        "analyst":     {"prefer_thinking": True,  "prefer_tool_call": True,  "size": "medium"},
        "refiner":     {"prefer_thinking": False, "prefer_tool_call": True,  "size": "small"},
        "critic":      {"prefer_thinking": True,  "prefer_tool_call": True,  "size": "medium"},
        "synthesizer": {"prefer_thinking": True,  "prefer_tool_call": True,  "size": "medium"},
        "direct":      {"prefer_thinking": False, "prefer_tool_call": True,  "size": "large"},
        "judge":       {"prefer_thinking": False, "prefer_tool_call": True,  "size": "small"},
        "duo_coder":   {"prefer_thinking": True,  "prefer_tool_call": True,  "size": "large"},
        "duo_critic":  {"prefer_thinking": True,  "prefer_tool_call": True,  "size": "medium"},
    }

    _vram_per_model = 0.0
    if vram_budget_gb and vram_budget_gb > 0:
        _vram_per_model = (vram_budget_gb - 0.5) / 2.0

    # Score available models for each role
    for role, reqs in ROLE_REQUIREMENTS.items():
        # Check persisted automap first
        _persisted = _automap_data.get("assignments", {}).get(role)
        if _persisted and _persisted in available:
            assignments[role] = _persisted
            continue

        best_model = ""
        best_score = -999
        for m in available:
            caps = get_model_capabilities(m)
            score = 0
            # Capability scoring
            if reqs["prefer_thinking"] and caps.get("thinking"):
                score += 3
            if reqs["prefer_tool_call"] and caps.get("tool_call"):
                score += 3
            if not reqs["prefer_tool_call"] and not caps.get("tool_call"):
                score += 1  # No penalty for no TC when not needed
            if reqs["prefer_tool_call"] and not caps.get("tool_call"):
                score -= 2
            # Size heuristic from model name
            _name_lower = m.lower()
            if reqs["size"] == "small" and any(s in _name_lower for s in ("2b", "1.5b", "3b", "1b")):
                score += 2
            elif reqs["size"] == "medium" and any(s in _name_lower for s in ("4b", "5b", "7b", "8b")):
                score += 2
            elif reqs["size"] == "large" and any(s in _name_lower for s in ("9b", "12b", "14b", "15b")):
                score += 2
            # z.B. qwen3.6:27b auf 8GB GPU → -8 Penalty
            if _vram_per_model > 0:
                _model_vram = _estimate_vram_gb_for_model(m)
                if _model_vram > _vram_per_model:
                    _overshoot = _model_vram - _vram_per_model
                    score -= int(_overshoot * 4)  # 4 Punkte pro GB Overshoot
                elif _model_vram > _vram_per_model * 0.85:
                    # Knapp dran: leichter Malus
                    score -= 1
            if score > best_score:
                best_score = score
                best_model = m
        if best_model:
            assignments[role] = best_model

    # Build display map
    display_map = {}
    for m in available:
        caps = get_model_capabilities(m)
        display_map[m] = {
            "tags": [],
            "vram_gb": round(_estimate_vram_gb_for_model(m), 1),
            "thinking": caps.get("thinking", False),
            "vision": caps.get("vision", False),
            "tool_call": caps.get("tool_call", False),
        }

    return {
        "task_type":   task_type,
        "assignments": assignments,
        "reasoning":   f"Auto-mapped {len(assignments)} agents for task type '{task_type}'"
                       + (f" (VRAM budget: {vram_budget_gb}GB)" if vram_budget_gb else ""),
        "display_map": display_map,
    }


def get_model_display_map(models: list | None = None) -> dict:
    """Return model → display-name mapping from automap.

    Args:
        models: Optional list of model names to build display map for.
                If provided, generates capability-based display map.
                If None, returns persisted display_map from automap.json.
    """
    if models:
        display_map = {}
        for m in models:
            caps = get_model_capabilities(m)
            display_map[m] = {
                "tags": [],
                "vram_gb": round(_estimate_vram_gb_for_model(m), 1),
                "thinking": caps.get("thinking", False),
                "vision":   caps.get("vision", False),
                "tool_call": caps.get("tool_call", False),
            }
        return display_map
    # Fallback: return persisted data
    if not _automap_data:
        load_automap()
    return _automap_data.get("display_map", {})


def detect_task_type(query: str, files: list | None = None, *, has_images: bool = False) -> str:
    """Heuristic task-type detection from query, file list, and image presence."""
    q = (query or "").lower()
    if files:
        return "code"
    if has_images:
        return "vision"
    if any(kw in q for kw in ("code", "program", "function", "class", "debug", "fix", "implement")):
        return "code"
    if any(kw in q for kw in ("explain", "what", "why", "how", "describe", "summarize")):
        return "analysis"
    if any(kw in q for kw in ("write", "create", "draft", "compose", "generate")):
        return "creative"
    return "general"


def record_run_outcome(model: str, task_type: str, success: bool, latency_s: float = 0.0, mode: str = "unknown") -> None:
    """Record a run outcome for routing weight adjustment."""
    global _routing_weights
    key = f"{model}|{task_type}|{mode}"
    if key not in _routing_weights:
        _routing_weights[key] = {"wins": 0, "losses": 0, "total_latency": 0.0, "runs": 0}
    w = _routing_weights[key]
    w["runs"] += 1
    if success:
        w["wins"] += 1
    else:
        w["losses"] += 1
    w["total_latency"] += latency_s
    save_routing_weights()


def get_routing_suggestion(task_type: str, min_runs: int = 20) -> dict | None:
    """
    Returns {"preferred_mode": "duo"|"pipeline"|"direct",
             "confidence": float} if enough learned data exists,
    or None if below confidence threshold.

    Filters _routing_weights for keys matching task_type with
    runs >= min_runs, aggregates win_rate per mode, and returns
    the best mode if average win_rate > 0.65.
    """
    mode_stats: dict[str, dict[str, float]] = {}
    for key, w in _routing_weights.items():
        parts = key.split("|")
        if len(parts) < 3:
            continue
        _model, _tt, _mode = parts[0], parts[1], parts[2]
        if _tt != task_type:
            continue
        runs = int(w.get("runs", 0))
        if runs < min_runs:
            continue
        wins = int(w.get("wins", 0))
        losses = int(w.get("losses", 0))
        total = wins + losses
        if total == 0:
            continue
        win_rate = wins / total
        if _mode not in mode_stats:
            mode_stats[_mode] = {"total_win_rate": 0.0, "count": 0}
        mode_stats[_mode]["total_win_rate"] += win_rate
        mode_stats[_mode]["count"] += 1

    if not mode_stats:
        return None

    best_mode = None
    best_confidence = 0.0
    for _mode, stats in mode_stats.items():
        avg_win_rate = stats["total_win_rate"] / stats["count"] if stats["count"] else 0.0
        if avg_win_rate > 0.65 and avg_win_rate > best_confidence:
            best_mode = _mode
            best_confidence = avg_win_rate

    if best_mode is None:
        return None

    return {"preferred_mode": best_mode, "confidence": round(best_confidence, 3)}


def get_routing_weights_summary(base_path: str = "") -> dict:
    """Return routing weights summary."""
    return dict(_routing_weights)


def save_routing_weights(base_path: str = "", data: dict | None = None) -> None:
    """Persist routing weights to disk."""
    global _routing_weights
    if data is not None:
        _routing_weights = data
    try:
        p = Path(__file__).parent.parent / "routing_weights.json"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(_routing_weights, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.warning("routing_weights save failed: %s", e)


def is_valid_preprocessing_model(model: str) -> bool:
    """Check if a model is suitable for vision preprocessing."""
    try:
        from model_configs.models_registry import is_vision_preprocessing as _reg_vis
        _ovr = _reg_vis(model)
        if _ovr is not None:
            return _ovr
    except Exception:
        pass
    base = model.split(":")[0]
    return base in _VISION_PREPROCESSING_ALLOWLIST


def get_best_preprocessing_model(models: list[str] | None = None) -> str | None:
    """Pick the best vision preprocessing model from available models."""
    if not models:
        return None
    for m in models:
        if is_valid_preprocessing_model(m):
            return m
    return None


def auto_resolve_model(task_type: str = "", settings: dict = None) -> str:
    """Auto-resolve model name based on task type and settings."""
    if settings:
        return settings.get("model", "")
    return ""


def get_model_capabilities(model_name: str) -> dict:
    """Return capability dict for a model, checking MODEL_PROFILES."""
    base = model_name.split(":")[0]
    # User-config (model_configs/models/*.json) has highest priority.
    try:
        from model_configs.models_registry import get_capabilities as _reg_caps
        _user_caps = _reg_caps(model_name)
        if _user_caps is not None:
            return _user_caps
    except Exception:
        pass
    # Exact prefix match first, then full name, then generic defaults
    profile = MODEL_PROFILES.get(base, MODEL_PROFILES.get(model_name, {}))
    if profile:
        return dict(profile)
    # Heuristic: known thinking model patterns
    lo = model_name.lower()
    if any(x in lo for x in ("qwen3", "bonsai",
                              "magistral", "qwq", "hermes3", "internlm3", "dolphin3", "thinker")):
        _vis = any(x in lo for x in ("qwen3.5", "qwen3.6", "hermes3.6"))
        return {"thinking": True, "vision": _vis, "tool_call": True}
    if any(x in lo for x in ("granite",)):
        return {"thinking": False, "vision": False, "tool_call": True}
    if any(x in lo for x in ("gemma-4", "gemma_4", "gemma4")):
        # VISION-FIX: gemma-4 = embedded vision encoder
        return {"thinking": False, "vision": True, "tool_call": True}
    if any(x in lo for x in ("vl", "vision", "llava", "pixtral", "internvl", "idefics", "cogvlm", "fuyu")):
        return {"thinking": False, "vision": True, "tool_call": False}
    if any(x in lo for x in ("coder", "code", "instruct", "devstral", "command-r")):
        return {"thinking": False, "vision": False, "tool_call": True}
    return {"thinking": False, "vision": False, "tool_call": False}
