


from __future__ import annotations


import logging
import re

_logger = logging.getLogger("llama_vram_table")

# ── VRAM-Tabelle: kanonischer_name → GB bei ctx=4096 ──────────────────────────
# Basis: echte Messungen (Vulkan, 99 GPU-Layers).
_VRAM_TABLE: dict[str, float] = {
    # qwen3.5 Familie
    "qwen3.5:0.8b":      0.6,
    "qwen3.5:0.8b-ud":   0.7,
    "qwen3.5:2b":        1.5,
    "qwen3.5:2b-ud":     1.7,
    # LFM2.5 — 8B total, 1B aktiv, Q4_K_M ~2.5GB weights + 0.2GB KV@4096
    "lfm2.5:8b-a1b":     2.7,
    # LFM2.5 dense 2.6B (Q4_K_M ~1.6GB weights + KV@4096) — Subagent-Ladder Default
    "lfm2.5:2.6b":       1.9,
    "lfm2.5:2.6b-instruct": 1.9,
    "qwen3.5:4b":        2.8,
    "qwen3.5:4b-ud":     3.0,
    "qwen3.5:4b-d":      3.2,
    "qwen3.5:4b-d-q6-k-1": 3.8,
    "qwen3.5:9b":        5.5,
    "qwen3.5:9b-ud":     5.8,


    # Qwen3-VL
    "qwen3-vl:2b-instruct": 1.6,

    # Gemma
    "gemma-4:e4b-it-obliterated": 3.0,
    "gemma-4:e4b-it":         3.0,
    "google-gemma-3:4b-it":   2.8,

    # Ministral
    "ministral:3b-instruct-2410": 2.0,
    "ministral:8b-instruct-2410": 4.8,

    # OmniCoder
    "omnicoder:9b":       5.5,

    # Granite
    "granite4:1b":       1.0,
    "granite-4.1:3b":       2.1,

    # Ternary-Bonsai
    "ternary-bonsai:8b": 4.8,

    # Ling-3.0-tiny (InclusionAI, 2026-08-19): MoE ~5.5B total / 1.4B aktiv,
    # — passt in 8GB, schneller als Expert-Offloading via PCIe.
    "ling-3.0-tiny":     4.8,

    # Qwen3.6
    "qwen3-6:27b":       7.0,

    # Qwen3
    "qwen-qwen3:14b-iq4-nl": 7.5,
}

# ── MoE (Mixture of Experts) VRAM-Tabelle ──────────────────────────────────────
_MOE_TABLE: dict[str, dict] = {
    # (d.h. moe_cpu_experts-Override=0 → Tabellenwert 35 aus llama_config._MOE_EXPERT_COUNTS).
    #   base   = load_tensors "Vulkan0 model buffer size" + llama_memory_recurrent "RS buffer size"
    #            + llama_context "output buffer size" + FIXER Anteil von sched_reserve "compute buffer size"
    # Messung @--n-cpu-moe=35, q4_0-KV:
    #   model 4309.34 + RS 62.81 + output 0.95 + compute-Intercept 395.00 MiB = 4768.10 MiB ≈ 4.66 GB
    "qwen3.6:35b-a3b": {
        "active_gpu_gb":            4.66,
        "measured_bytes_per_token": 6272,
        "calibrated_n_cpu_moe":     35,
    },
    "qwen3.6:35b-a3b-ud": {
        "active_gpu_gb":            4.66,
        "measured_bytes_per_token": 6272,
        "calibrated_n_cpu_moe":     35,
    },
    "qwen3.6:35b-a3b-uncensored": {
        "active_gpu_gb":            4.66,
        "measured_bytes_per_token": 6272,
        "calibrated_n_cpu_moe":     35,
    },
    "hermes3.6:35b-a3b": {
        "active_gpu_gb":            4.66,
        "measured_bytes_per_token": 6272,
        "calibrated_n_cpu_moe":     35,
    },
}


def vram_of_moe(model_name: str, num_ctx: int = 4096) -> float:


    key = model_name.strip().lower()
    cfg = _MOE_TABLE.get(key) or _MOE_TABLE.get(key.replace("-ud", ""))
    if not cfg:
        # Prefix-Match: GGUF auto-discovery can append suffixes like -hauhaucs-aggressive
        # that are not in _MOE_TABLE. Match "qwen3.6:35b-a3b-uncensored" as prefix of
        # "qwen3.6:35b-a3b-uncensored-hauhaucs-aggressive".
        for table_key in _MOE_TABLE:
            if key.startswith(table_key):
                cfg = _MOE_TABLE[table_key]
                break
    if not cfg:
        return vram_of_with_ctx(model_name, num_ctx)  # dense fallback

    base = cfg["active_gpu_gb"]
    if num_ctx <= 4096:
        return base

    if "measured_bytes_per_token" in cfg:
        overhead_gb = (num_ctx * cfg["measured_bytes_per_token"]) / (1024 ** 3)
        return round(base + overhead_gb, 2)

    kv_per_tok = 2 * cfg["kv_heads"] * cfg["layers"] * 128 * 0.5
    extra_ctx = num_ctx - 4096
    overhead_gb = (extra_ctx * kv_per_tok) / (1024 ** 3)
    return round(base + overhead_gb, 2)


VRAM_GB: dict[str, float] = dict(_VRAM_TABLE)
VRAM_GB.update({k: v["active_gpu_gb"] for k, v in _MOE_TABLE.items()})

_FINETUNE_TO_BASE: dict[str, str] = {
    "omnicoder":  "qwen3.5",    # OmniCoder = Qwen3.5-based
}

VRAM_OVERFLOW_MODELS: set[str] = {
    "qwen-qwen3:14b-iq4-nl",
    "qwen3.6:27b",
    "qwen3-6:27b",
}

# ── ctx-Overhead ───────────────────────────────────────────────────────────────
# KV-Cache Overhead pro 1024 ctx-Tokens (q4_0 Quantisierung).
#   KV per token = 2 * 36 * 4 * 128 * 0.5 = 18432 bytes ≈ 18KB
#   Per 1024 extra tokens: 18KB * 1024 ≈ 18MB ≈ 0.018GB
#   falsche Evictions → exit=-1 → CPU-Fallback (alles im RAM).
_CTX_OVERHEAD_BASE_GB: float = 0.03
_CTX_OVERHEAD_REF_SIZE_B: float = 4.0  # Referenz-Groesse in Milliarden Parametern


def _extract_size_gb(model_name: str) -> float | None:
    """Extrahiert die Modellgroesse aus dem Namen (z.B. '4b' → 4.0, '1.5b' → 1.5)."""
    m = re.search(r'[:\-](0?\d{1,2}(?:\.\d+)?)[bB](?:[\-]|$)', model_name)
    if m:
        return float(m.group(1))
    # E-Size: E4B → 4B equivalent
    m = re.search(r'[:\-]E(\d+)[bB](?:[\-]|$)', model_name, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def vram_of(model_name: str) -> float:


    name = model_name.strip().lower()
    if not name:
        return 4.0

    # 1. Exakter Match
    if name in _VRAM_TABLE:
        return _VRAM_TABLE[name]

    base = name.split(":")[0] if ":" in name else ""
    tag = name.split(":")[1] if ":" in name else ""
    for key, val in _VRAM_TABLE.items():
        key_base = key.split(":")[0] if ":" in key else ""
        key_tag = key.split(":")[1] if ":" in key else ""
        if key_base == base and tag in key_tag:
            return val

    if base in _FINETUNE_TO_BASE:
        base_name = _FINETUNE_TO_BASE[base]
        base_lookup = f"{base_name}:{tag}" if tag else base_name
        if base_lookup in _VRAM_TABLE:
            return _VRAM_TABLE[base_lookup]
        size = _extract_size_gb(name)
        if size is not None:
            for key, val in _VRAM_TABLE.items():
                if key.startswith(f"{base_name}:") and _extract_size_gb(key) == size:
                    return val

    # 3. size heuristic
    size = _extract_size_gb(name)
    if size is not None:
        # Q4_K_M Faustregel: ~0.6 GB pro Milliarde Parameter
        return round(size * 0.6, 1)

    # 4. Konservativer Fallback
    _logger.debug(f"VRAM estimate for '{model_name}': fallback 4.0GB")
    return 4.0


def vram_of_with_ctx(model_name: str, num_ctx: int = 4096) -> float:


    base_vram = vram_of(model_name)

    if num_ctx <= 4096:
        return base_vram

    size = _extract_size_gb(model_name) or 4.0
    size_factor = size / _CTX_OVERHEAD_REF_SIZE_B
    extra_ctx = num_ctx - 4096
    ctx_overhead = (extra_ctx / 1024) * _CTX_OVERHEAD_BASE_GB * size_factor

    return round(base_vram + ctx_overhead, 2)


# ── Live-VRAM-Query (Windows WDDM Performance-Counter via pywin32/PDH) ──────────
# Generic default (no device-specific measurement). Adjusted per system as
# needed or calibrated to the real card by the pre-flight check.
TOTAL_VRAM_MIB: int = 8192

_sel_path_cache: str | None = None


def _select_gpu_instance(by_path: dict[str, float]) -> str:


    global _sel_path_cache
    if _sel_path_cache is not None and _sel_path_cache in by_path:
        return _sel_path_cache
    # No device-specific LUID hint — always the highest dedicated occupancy.
    _max = max(by_path, key=lambda k: by_path[k])
    _sel_path_cache = _max
    return _max


def get_live_gpu_free_mib() -> float | None:


    import platform
    if platform.system() != "Windows":
        _logger.warning("get_live_gpu_free_mib: not Windows - live query unavailable")
        return None
    h = None
    try:
        import win32pdh
        h = win32pdh.OpenQuery()
        paths = win32pdh.ExpandCounterPath(r"\GPU Adapter Memory(*)\Dedicated Usage")
        if not paths:
            _logger.warning("get_live_gpu_free_mib: counter '\\GPU Adapter Memory(*)\\Dedicated Usage' returns no instances")
            win32pdh.CloseQuery(h)
            return None
        counters = [(p, win32pdh.AddCounter(h, p)) for p in paths]
        win32pdh.CollectQueryData(h)
        _by_path: dict[str, float] = {}
        for p, c in counters:
            try:
                _typ, _val = win32pdh.GetFormattedCounterValue(c, win32pdh.PDH_FMT_LARGE)
                _by_path[p] = float(_val)
            except Exception:
                pass
        win32pdh.CloseQuery(h)
        h = None
        if not _by_path:
            _logger.warning("get_live_gpu_free_mib: no formatted counter values received")
            return None
        # 1. Stabilitaets-Cache, 2. hoechste Dedicated-Belegung.
        _sel_before = _sel_path_cache
        _sel_path = _select_gpu_instance(_by_path)
        if _sel_before != _sel_path:
            _inst_desc = ", ".join(
                f"{p.rsplit('(', 1)[-1].split(')')[0]}={_by_path[p]/(1024*1024):.0f}MiB"
                for p in sorted(_by_path)
            )
            _logger.info(
                "get_live_gpu_free_mib: Instanz '%s' gewaehlt [%s] — "
                "hoechste Dedicated-Belegung",
                _sel_path.rsplit("(", 1)[-1].split(")")[0], _inst_desc,
            )
        used_mib = _by_path[_sel_path] / (1024 * 1024)
        free_mib = TOTAL_VRAM_MIB - used_mib
        if free_mib < 0:
            free_mib = 0.0
        return round(free_mib, 1)
    except Exception as e:
        _logger.warning("get_live_gpu_free_mib: live query failed (%s: %s) - fallback", type(e).__name__, e)
        try:
            if h is not None:
                import win32pdh as _w
                _w.CloseQuery(h)
        except Exception:
            pass
        return None


async def wait_for_vram_reclaim(target_mib: int, timeout_sec: int = 45,
                                poll_interval: float = 0.5) -> bool:


    import asyncio as _asyncio
    import time as _time
    start = _time.time()
    _first_free: float | None = None
    while _time.time() - start < timeout_sec:
        try:
            free_mib = get_live_gpu_free_mib()
            if _first_free is None and free_mib is not None:
                _first_free = free_mib
            if free_mib is not None and free_mib >= target_mib:
                _logger.info(
                    "[VRAM-RECLAIM] %d MiB frei nach %.1fs — OK (target: %d)",
                    free_mib, _time.time() - start, target_mib,
                )
                return True
        except Exception as e:
            _logger.warning("[VRAM-RECLAIM] measurement failed: %s", e)
            return False
        await _asyncio.sleep(poll_interval)
    try:
        free_mib = get_live_gpu_free_mib()
    except Exception:
        free_mib = -1
    _logger.warning(
        "[VRAM-RECLAIM] Timeout nach %ds — nur %d MiB frei (target: %d, anfangs %s) — "
        "Treiber-Freigabe kann 30-40s dauern (AMD/Vulkan)",
        timeout_sec, free_mib, target_mib,
        f"{_first_free:.0f} MiB" if _first_free is not None else "unbekannt",
    )
    return False
