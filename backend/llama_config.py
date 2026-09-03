


from __future__ import annotations

import os
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


_LLAMA_ROOT = _REPO_ROOT / "llama"


# ── GPU-Backend ───────────────────────────────────────────────────────────────
def _resolve_gpu_backend() -> str:


    _env = os.environ.get("HIVEMIND_GPU_BACKEND", "").strip().lower()
    if _env in ("vulkan", "cuda"):
        return _env
    try:
        from settings import load_settings as _ls
        _g = str(_ls().get("gpu_backend", "") or "").strip().lower()
        if _g in ("vulkan", "cuda"):
            return _g
    except Exception:
        pass
    return "vulkan"


GPU_BACKEND = _resolve_gpu_backend()


def _find_llama_server() -> Path:
    """llama-server.exe finden: Env > Auto-Discovery (Build + Backend + CUDA-Version) > Fallback."""
    _env = os.environ.get("HIVEMIND_LLAMA_BIN", "").strip()
    if _env:
        return Path(_env)

    # Auto-Discovery: <repo>/llama/<folder>/llama-server.exe.
    _backend_tag = "cuda" if GPU_BACKEND == "cuda" else "vulkan"
    _best: tuple[tuple, Path] | None = None
    if _LLAMA_ROOT.is_dir():
        for cand in _LLAMA_ROOT.glob("*/llama-server.exe"):
            _name = cand.parent.name.lower()
            m = re.search(r"b(\d{4,})", _name)
            build = int(m.group(1)) if m else 0
            backend_match = 1 if _backend_tag in _name else 0
            cuda_ver: tuple = (0,)
            if _backend_tag == "cuda":
                m2 = re.search(r"cuda-([\d.]+)", _name)
                if m2:
                    try:
                        cuda_ver = tuple(int(x) for x in m2.group(1).split("."))
                    except Exception:
                        cuda_ver = (0,)
            key = (build, backend_match, cuda_ver)
            if _best is None or key > _best[0]:
                _best = (key, cand)
    if _best is not None:
        return _best[1]

    return _LLAMA_ROOT / "llama-server.exe"


LLAMA_BIN = _find_llama_server()


def find_models_dir() -> Path:
    """Models-Ordner finden: Env > settings.json "models_dir" > <repo>/models."""
    _env = os.environ.get("HIVEMIND_MODELS_DIR", "").strip()
    if _env:
        return Path(_env)
    # MODELS-DIR-SETTING (2026-09-01): der Installer schreibt einen benutzerdefinierten
    # models folder from settings.json — the server must find it even WITHOUT an env var.
    try:
        from settings import load_settings as _ls
        _cfg = _ls().get("models_dir", "") or ""
        _cfg = str(_cfg).strip()
        if _cfg:
            return Path(_cfg)
    except Exception:
        pass
    return _REPO_ROOT / "models"


MODELS_DIR = find_models_dir()


# ── Server-Ports ──────────────────────────────────────────────────────────────

BASE_PORT = 8101

MAX_SLOTS = 3


# ── Vulkan / GPU ──────────────────────────────────────────────────────────────

GPU_LAYERS = 99

# llama-server Flag: --device VULKAN<N>
VULKAN_DEVICE = 0


# ── KV-Cache Quantisierung ────────────────────────────────────────────────────

KV_CACHE_TYPE = "q4_0"

# 0 = aus, 256 = llama.cpp-Empfehlung. Senkt Re-Prefill-Kosten massiv bei hohen ctx.
CACHE_REUSE = 256


# ── MoE (Mixture of Experts) ──────────────────────────────────────────────────

# Per-Model-CPU-Experts-Override: {model_key: n_cpu_moe}. 0/fehlt = aus _MOE_EXPERT_COUNTS.
MOE_CPU_EXPERTS = {}

#   Override (moe_cpu_experts) > _MOE_EXPERT_COUNTS (kalibriert) > Autodetect.
_MOE_AUTODETECT_MIN_TOTAL_B = 30


def is_moe_model(model_key: str) -> bool:


    _key = str(model_key or "").strip().lower()
    if not _key:
        return False
    if _key in _MOE_EXPERT_COUNTS:
        return True
    if "moe" in _key:
        return True
    import re as _re_moe
    return bool(_re_moe.search(r"(^|[-._:])a\d+b([-._]|$)", _key))


def _try_registry_moe(model_key: str) -> int:
    """Per-Model-Registry (model_configs/models/*.json) → n_cpu_moe."""
    try:
        from model_configs.models_registry import get_moe_cpu_experts as _reg_moe
        return int(_reg_moe(model_key) or 0)
    except Exception:
        return 0


def _try_registry_gpu_layers(model_key: str) -> int | None:
    """Per-Model-Registry (model_configs/models/*.json) → --n-gpu-layers."""
    try:
        from model_configs.models_registry import get_gpu_layers as _reg_gl
        return _reg_gl(model_key)
    except Exception:
        return None


def _try_registry_mtp(model_key: str) -> bool | None:
    """Per-Model-Registry (model_configs/models/*.json) → MTP/Speculative-Decoding."""
    try:
        from model_configs.models_registry import is_mtp as _reg_mtp
        return _reg_mtp(model_key)
    except Exception:
        return None


def _try_registry_dspark_draft(model_key: str) -> str | None:
    """Per-Model-Registry (model_configs/models/*.json) → DSpark drafter GGUF filename."""
    try:
        from model_configs.models_registry import get_dspark_draft as _reg_dd
        v = _reg_dd(model_key)
        if v:
            return str(v).strip()
    except Exception:
        pass
    return None


def detect_moe_count(model_key: str) -> int:


    _tbl = _MOE_EXPERT_COUNTS.get(str(model_key or ""))
    if _tbl:
        return int(_tbl)
    import re as _re_moe2
    _key = str(model_key or "").strip().lower()
    m = _re_moe2.search(r"(\d+)\s*b[-_.]?a(\d+)\s*b", _key)
    if m:
        total_b = int(m.group(1))
        if total_b >= _MOE_AUTODETECT_MIN_TOTAL_B:
            return total_b
    return 0

MLOCK_MODEL = True

_MOE_EXPERT_COUNTS = {
    "qwen3.6:35b-a3b": 35,
    "qwen3.6:35b-a3b-uncensored": 35,
}

_HERMES_V7_MTP = "hermes3.6:35b-a3b-uncensored-genesis-v7-mtp-apex-compact"

_MOE_EXPERT_COUNTS[_HERMES_V7_MTP] = 35

# Hermes3.6 Genesis V10 MTP APEX (2026-08-25): identische Launchsettings wie
# V7 (n-cpu-moe 35, GPU-Offload 99, KV q4_0) — parallel zu V7 nutzbar.
_HERMES_V10_MTP = "hermes3.6:35b-a3b-uncensored-genesis-v10-mtp-apex-compact"
_MOE_EXPERT_COUNTS[_HERMES_V10_MTP] = 35

_MOE_KV_CACHE_TYPES: dict[str, str] = {}


# ── MTP (Multi-Token Prediction / Speculative Decoding) ─────────────────────

MTP_SPEC_TYPE = "draft-mtp"

MTP_DRAFT_N_MAX = 3

MTP_DRAFT_N_MIN = 1


# ── DSpark external-drafter (Speculative-Decoding Sidecar) ────────────────────
# LFM2.5-DSpark: standalone draft GGUF paired with the target model. Enabled per
# model via model_configs/models/*.json → "dspark_draft_filename".
DSPARK_SPEC_TYPE = "draft-dspark"
DSPARK_DRAFT_N_MAX = 10
DSPARK_DRAFT_N_MIN = 0
DSPARK_MIN_BUILD = 10173   # llama.cpp mainline #25173 (--spec-type …draft-dspark)

#   _MTP_MODELS = {"qwen3.6:35b-a3b-mtp"}
_MTP_MODELS: set[str] = {
    "qwen3.5:4b-mtp", _HERMES_V7_MTP, _HERMES_V10_MTP,
}


_GPU_LAYERS_TABLE: dict[str, int] = {
    "lfm2.5:8b-a1b": 24,   # full GPU offload (1B active MoE, ~5GB VRAM)
    "lfm2.5:2.6b": 99,     # dense 2.6B — passt komplett auf GPU (~2GB VRAM)
    "lfm2.5:2.6b-instruct": 99,
}


# ── Default Context Size ──────────────────────────────────────────────────────

CONTEXT_SIZE_DEFAULT = 4096


# ── Startup ───────────────────────────────────────────────────────────────────

PRELOAD_JUDGE_ON_START = True

DEFAULT_IDLE_TIMEOUT_SECONDS = 600   # 10 Minuten


BINARY_MIN_BUILD: dict[str, int] = {
    # b8340 → b8460: thinking_budget per-Request, AMD Vulkan Performance-Verbesserungen.
    "gemma3":    8278,   # gemma3.attention.layer_norm_rms_epsilon — b8250 zu alt
    "gemma2":    8250,
    "gemma-4":   8278,   # gemma-4-E4B-it Fine-Tunes (basieren auf gemma2/gemma3 Arch) — b8278+
    "qwen3.5":   8300,
    "qwen3.5": 8300, 
    "qwen3-vl":  8278,   # GGUF-Architektur "qwen3vl" — rope.dimension_sections
    "qwen3vl":   8278,   # interner Architektur-Name im GGUF
    "qwen35":    8300,
    "qwen3":     8260,
    "lfm2.5":    9518,
    "ling-3.0-tiny": 10488,
}

# ── Windows-Vulkan mlock/mmap-Regression (b10300+) ───────────────────────────
#   --mlock (or --load-mode mlock) → GGML_ASSERT(addr) failed in
NO_MMAP_MIN_BUILD = 10300
