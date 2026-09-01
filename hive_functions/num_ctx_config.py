


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
}

# ── Analyst-Override ──────────────────────────────────────────────────────────

MODEL_NUM_CTX_ANALYST: dict[str, int] = {
    "rnj-1":       12288,  # Code-STEM: 4k zu knapp bei langen Pipeline-Kontexten
    "ministral-3":  8192,
    "gemma3":      16384,  # gemma3:12b exzellentes long-ctx, 16k unkritisch (sequenziell)
    "qwen3.5":      8192,  # :4b/:9b als Analyst: 4k zu knapp bei langen Sessions
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


def resolve_ctx(explicit_value, model_name: str, role: str = "coder") -> int:


    if explicit_value is not None:
        try:
            _ev = int(explicit_value)
        except (TypeError, ValueError):
            _ev = 0
        if _ev > 0:
            return _ev
    if role == "agentic":
        return AGENTIC_CTX_FLOOR
    _gnc_role = "duo_coder" if role in ("coder", "planner") else None
    _model_default = get_num_ctx(model_name, _gnc_role)
    return int(_model_default) if _model_default else _CTX_LAST_RESORT
