


from __future__ import annotations

# ── Basis-Limits ──────────────────────────────────────────────────────────────
# Modelfile 21GB KV in RAM!). Faustregel: 4096 ≈ 0.5GB KV, 8192 ≈ 1.0GB.

MODEL_NUM_CTX: dict[str, int] = {
    "rnj-1":             4096,   # default 32768
    "ministral-3":       4096,   # :3b und :8b
    "omnicoder":         4096,
    "ternary-bonsai":    4096,
    # ── Granite-Familie ──
    "granite4":          4096,
    "granite3":          8192,   # granite3.x series
    "granite3.2-vision": 8192,
    # ── Gemma-Familie ──
    "gemma3":            8192,
    # gemma-4-E4B-it-OBLITERATED (4B Q5_K_M ~3GB) — Soul-Agent:
    "gemma-4-e4b-it":    4096,
    "qwen3.5":           4096,
    "qwen3.6":           8192,
    # ── Weitere ──
    "lfm2.5":            16384,
    "ling-3.0-tiny":     16384,
    "k2-horizon":        16384,  # dense 36-layer full-attention: KV-heavy
}

# ── Analyst-Override ──────────────────────────────────────────────────────────

MODEL_NUM_CTX_ANALYST: dict[str, int] = {
    "rnj-1":       12288,  # Code-STEM: 4k too tight for long pipeline contexts
    "ministral-3":  8192,
    "gemma3":      16384,  # gemma3:12b excellent long-ctx, 16k uncritical (sequential)
    "qwen3.5":      8192,  # :4b/:9b as analyst: 4k too tight for long sessions
    "qwen3.6":       8192,
}

# ── Duo-Coder-Override ────────────────────────────────────────────────────────
# Code-Generierung: Input = user_input + coder_out(~1500tok) + issues → leicht > 4096.
# qwen3.5:4b als Duo-Coder: 6144 ctx → ~5.1GB VRAM. 8192 = CPU-Split-Grenze!

MODEL_NUM_CTX_DUO_CODER: dict[str, int] = {
    "qwen3.5":      8192,
    "rnj-1":        8192,  # rnj-1:8b: Code-STEM, lange Outputs erwartet
    "omnicoder":    8192,  # OmniCoder-9B: ~5.5GB@ctx=6144
    "qwen3.6":      8192,
    "k2-horizon":      16384,
    "ling-3.0-tiny": 16384,
}


# ── Vision-Override ───────────────────────────────────────────────────────────

MODEL_NUM_CTX_VISION: dict[str, int] = {
    "qwen3.5":           8192,
    "granite3.2-vision": 8192,
    "gemma3":            8192,
}

def get_num_ctx(model: str, agent_role: str | None = None) -> int | None:


    base = model.split(":")[0]

    # User-config (model_configs/models/*.json) has highest priority.
    try:
        from model_configs.models_registry import get_num_ctx as _reg_ctx
        _user_ctx = _reg_ctx(model, agent_role)
        if _user_ctx:
            return _user_ctx
    except Exception:
        pass

    if agent_role == "vision":
        vision_ctx = MODEL_NUM_CTX_VISION.get(base)
        if vision_ctx:
            return vision_ctx
        base_val = MODEL_NUM_CTX.get(model) or MODEL_NUM_CTX.get(base)
        return max(base_val, 8192) if base_val else 8192

    if model in MODEL_NUM_CTX:
        base_val = MODEL_NUM_CTX[model]
        if agent_role == "analyst" and base in MODEL_NUM_CTX_ANALYST:
            return MODEL_NUM_CTX_ANALYST[base]
        if agent_role == "duo_coder" and base in MODEL_NUM_CTX_DUO_CODER:
            return MODEL_NUM_CTX_DUO_CODER[base]
        return base_val

    # 3. Role-spezifischer Override
    if agent_role == "analyst" and base in MODEL_NUM_CTX_ANALYST:
        return MODEL_NUM_CTX_ANALYST[base]
    if agent_role == "duo_coder" and base in MODEL_NUM_CTX_DUO_CODER:
        return MODEL_NUM_CTX_DUO_CODER[base]

    # 4. Basis-Lookup
    return MODEL_NUM_CTX.get(base)


# ── Agentic Tool-Loop Headroom ────────────────────────────────────────────────
AGENTIC_CTX_FLOOR = 16384

_CTX_LAST_RESORT = 8192

# Agent-card Context ("ctx_overrides") must be honoured in the duo/agentic
# paths too — a user-chosen context wins over every default, and the 16k floor
# is only a minimum, never a cap for models that support more.
_ROLE_TO_OVERRIDE_KEY = {
    "coder":    "duo_coder",
    "planner":  "duo_coder",
    "agentic":  "duo_coder",
    "critic":   "duo_critic",
}


def _live_ctx_override(model_name: str, role: str) -> int | None:
    """Read the user's per-agent/per-model context override from the running
    settings. RESOLVER-ONE (2026-10-28, audit): this was a full copy of the
    ctx_overrides lookup; the canonical one lives in
    core.state._ctx_override_lookup (legacy flat + nested shapes, '#' suffix
    handling). Only the duo-role -> override-key mapping stays here."""
    try:
        from core.state import _ctx_override_lookup
        from core import state as _st
        ov = getattr(_st, "settings", None)
        if not isinstance(ov, dict):
            return None
        ov = ov.get("ctx_overrides")
        if not isinstance(ov, dict):
            return None
        role_key = _ROLE_TO_OVERRIDE_KEY.get(role) or role
        return _ctx_override_lookup(ov, str(model_name or ""), role_key)
    except Exception:
        return None


def resolve_ctx(explicit_value, model_name: str, role: str = "coder") -> int:

    # 1. The per-agent/per-model override (agent card → ctx_overrides) is the
    #    most specific user intent and wins over generic duo_* keys — otherwise
    #    a persisted default (e.g. 16384) would silently beat a raised override.
    _user_ctx = _live_ctx_override(model_name, role)
    if _user_ctx:
        return _user_ctx
    # 2. explicit settings key (duo_coder_ctx_agentic / duo_planner_ctx_target …)
    if explicit_value is not None:
        try:
            _ev = int(explicit_value)
        except (TypeError, ValueError):
            _ev = 0
        if _ev > 0:
            return _ev
    # 3. agentic: at least the floor, but never clamp a bigger model default
    if role == "agentic":
        _gnc = get_num_ctx(model_name, "duo_coder")
        _mdef = int(_gnc) if _gnc else 0
        return max(AGENTIC_CTX_FLOOR, _mdef)
    # 4. role/model default
    _gnc_role = "duo_coder" if role in ("coder", "planner") else None
    _model_default = get_num_ctx(model_name, _gnc_role)
    return int(_model_default) if _model_default else _CTX_LAST_RESORT
