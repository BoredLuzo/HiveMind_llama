


from __future__ import annotations

import logging
import os
import platform
import re
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)
_REPO_ROOT = Path(__file__).resolve().parent.parent


_LLAMA_ROOT = _REPO_ROOT / "llama"


def _boot_settings() -> dict:
    try:
        from settings import load_settings as _ls
        return _ls() or {}
    except Exception:
        return {}


_BOOT_CFG = _boot_settings()


# ── GPU-Backend ───────────────────────────────────────────────────────────────
def _resolve_gpu_backend(cfg: dict | None = None) -> str:
    _env = os.environ.get("HIVEMIND_GPU_BACKEND", "").strip().lower()
    if _env in ("vulkan", "cuda", "rocm", "cpu"):
        return _env
    try:
        _src = _boot_settings() if cfg is None else cfg
        _g = str(_src.get("gpu_backend", "") or "").strip().lower()
        if _g in ("vulkan", "cuda", "rocm", "cpu"):
            return _g
    except Exception:
        pass
    return "vulkan"


GPU_BACKEND = _resolve_gpu_backend(_BOOT_CFG)


def _find_llama_server() -> Path:
    """Find llama-server (Windows: .exe, POSIX: no suffix): env > auto-discovery > fallback."""
    _env = os.environ.get("HIVEMIND_LLAMA_BIN", "").strip()
    if _env:
        return Path(_env)

    # Auto-Discovery: <repo>/llama/<folder>/llama-server[.exe].
    _bin_name = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    _backend_tag = ("cuda" if GPU_BACKEND == "cuda"
                    else "rocm" if GPU_BACKEND == "rocm"
                    else "vulkan" if GPU_BACKEND == "vulkan" else "cpu")
    _best: tuple[tuple, Path] | None = None
    if _LLAMA_ROOT.is_dir():
        # NESTED-LAYOUT (2026-09-13): the ubuntu tarballs carry a top-level
        # "llama-b<build>/" folder, so extraction yields
        # <root>/<target>/<llama-b<build>>/llama-server (double nesting).
        # A flat "*/llama-server" glob missed those entirely — search
        # recursively; flat layouts (Windows ZIPs) still match.
        for cand in _LLAMA_ROOT.rglob(_bin_name):
            # score over the FULL directory path: in the nested ubuntu layout
            # the backend tag sits in the OUTER folder (…-ubuntu-vulkan-x64),
            # the build number in the inner one (llama-b10941).
            _name = str(cand.parent).lower()
            m = re.search(r"b(\d{4,})", _name)
            build = int(m.group(1)) if m else 0
            if _backend_tag == "cpu":
                backend_match = 0 if ("vulkan" in _name or "cuda" in _name or "rocm" in _name) else 1
            else:
                backend_match = 1 if _backend_tag in _name else 0
            cuda_ver: tuple = (0,)
            if _backend_tag == "cuda":
                m2 = re.search(r"cuda-([\d.]+)", _name)
                if m2:
                    try:
                        cuda_ver = tuple(int(x) for x in m2.group(1).split("."))
                    except Exception:
                        cuda_ver = (0,)
            # Matching the configured GPU_BACKEND always outranks a higher build
            # from another backend (e.g. newer vulkan build must not shadow pinned cuda).
            key = (backend_match, build, cuda_ver)
            if _best is None or key > _best[0]:
                _best = (key, cand)
    if _best is not None:
        return _best[1]

    return _LLAMA_ROOT / _bin_name


LLAMA_BIN = _find_llama_server()


def sync_runtime_backend(gpu_backend: str = "", llama_bin: Path | None = None) -> tuple[str, Path]:
    """Update GPU_BACKEND and LLAMA_BIN across llama_config and value-importing modules."""
    import importlib
    import sys

    global GPU_BACKEND, LLAMA_BIN
    gb = str(gpu_backend or "").strip().lower()
    if gb in ("vulkan", "cuda", "rocm", "cpu"):
        GPU_BACKEND = gb
    LLAMA_BIN = Path(llama_bin) if llama_bin is not None else _find_llama_server()

    for mod_name in (
        "backend.llama_server_manager",
        "backend.manager_load",
        "backend.llama_start_phases",
    ):
        mod = sys.modules.get(mod_name)
        if mod is None:
            try:
                mod = importlib.import_module(mod_name)
            except ImportError:
                continue
        setattr(mod, "GPU_BACKEND", GPU_BACKEND)
        setattr(mod, "LLAMA_BIN", LLAMA_BIN)

    return GPU_BACKEND, LLAMA_BIN


def find_models_dir(cfg: dict | None = None) -> Path:
    """Models-Ordner finden: Env > settings.json "models_dir" > <repo>/models."""
    _env = os.environ.get("HIVEMIND_MODELS_DIR", "").strip()
    if _env:
        return Path(_env)
    # MODELS-DIR-SETTING (2026-09-01): der Installer schreibt einen benutzerdefinierten
    # models folder from settings.json — the server must find it even WITHOUT an env var.
    try:
        _src = _boot_settings() if cfg is None else cfg
        _cfg = _src.get("models_dir", "") or ""
        _cfg = str(_cfg).strip()
        if _cfg:
            return Path(_cfg)
    except Exception:
        pass
    return _REPO_ROOT / "models"


MODELS_DIR = find_models_dir(_BOOT_CFG)


# ── Server-Ports ──────────────────────────────────────────────────────────────

# Per-instance llama ports (2026-10-01): two HiveMind copies on one machine
# used to fight over the hardcoded 8101-8103 — startup_cleanup KILLS foreign
# llama-server processes on those ports. Cascade: env override ->
# settings.server_port + 100 -> legacy default. server_port comes from each
# instance's own settings.json (testing 8001 -> 8101-8103, dev 8011 -> 8111-8113).
BASE_PORT = int(os.environ.get("HIVEMIND_LLAMA_BASE_PORT", "").strip() or 0)
if not BASE_PORT:
    try:
        BASE_PORT = int(_BOOT_CFG.get("server_port", 0) or 0) + 100
    except Exception:
        BASE_PORT = 0
if not BASE_PORT or BASE_PORT < 1024:
    BASE_PORT = 8101

MAX_SLOTS = 3
try:
    _raw_slots = int(_BOOT_CFG.get("llama_max_slots", 3) or 3)
    MAX_SLOTS = max(1, min(16, _raw_slots))
except (ImportError, OSError, TypeError, ValueError):
    MAX_SLOTS = 3


# ── Vulkan / GPU ──────────────────────────────────────────────────────────────

GPU_LAYERS = 99

# llama-server Flag: --device VULKAN<N> / CUDA<N>
VULKAN_DEVICE = 0
GPU_DEVICE = 0


# ── KV-Cache Quantisierung ────────────────────────────────────────────────────

KV_CACHE_TYPE = "q4_0"

# Extra CLI flags appended globally when starting llama-server (space-separated string).
LLAMA_EXTRA_ARGS = ""

# Speculative decoding mode: "auto" (profile/sibling drafters active) | "off".
SPEC_DECODE_MODE = "auto"

# Optional VRAM cap override in MiB (<= 0 uses hardware detection / budget).
TOTAL_VRAM_MIB_OVERRIDE = 0

# 0 = aus, 256 = llama.cpp-Empfehlung. Senkt Re-Prefill-Kosten massiv bei hohen ctx.
CACHE_REUSE = 256

# HOST-RAM-CAP (2026-10-14): b11456-era context features default to
# host-RAM-heavy values — the prompt cache may grow to 8192 MiB and each
# slot keeps up to 32 context checkpoints in host RAM. With per-slot KV
# (--parallel 1) idle-slot snapshots are NOT cleared, so long installs
# filled RAM and then swap; a compression rewrites the prefix, making the
# cached old state dead weight anyway. Defaults below bound both.
#   cache-ram: 0 disables the prompt cache entirely, -1 = unlimited.
#   ctx-checkpoints: -1 keeps the binary default (32), 0 disables.
CACHE_RAM_MIB = int(os.environ.get("HIVEMIND_CACHE_RAM_MIB", "1024") or 1024)
CTX_CHECKPOINTS = int(os.environ.get("HIVEMIND_CTX_CHECKPOINTS", "4") or 4)

# VRAM pre-flight gate (2026-10-11): refuse loads that the estimate
# does not fit. False = attempt the load anyway and let real failures
# drive the ctx cascade. Synced from settings "vram_preflight_gate".
VRAM_PREFLIGHT_GATE = True

# Prompt-processing chunk (--ubatch-size). Higher = faster prefill for MoE with
# CPU expert offloading, but larger VRAM compute buffer. Runtime-overridable via
# settings key duo_ubatch (falls back to the older llama_ubatch_size).
# MEASURED 2026-10-30 (build b11889/b12001, RX 6600):
#   bench, pp 8192, fresh process: 256 -> 77.6 | 512 -> 119.96 |
#     1024 -> aborted (absolute shared 80 -> 340 MiB) | 2048 -> 193.6
#     (absolute shared peak 2348 MiB).
#   BUT the absolute shared number was the wrong yardstick (idle baseline is
#   ~470 MiB, and every real run sits 900-1600 MiB above it). Judged as a
#   DELTA to the idle baseline / to the neighbouring variant, on the REAL
#   server at 18 011 prompt tokens:
#     Hermes 512 -> 91.7 t/s, 1024 -> 113.6 t/s (+23.9 %),
#     shared delta vs the 512 run: +52 MiB only.
#   Groot (ub 512 vs 1024, 4.2k/10.5k depth): +27.7 % / +17.1 %, +58 MiB.
# Default 512 (2026-11-18): 512 lifts 4B/MoE prefill significantly (+72 MiB
# compute buffer vs 256); tight <6 GB GPUs can still override to 256.
LLAMA_UBATCH = 512
LLAMA_UBATCH_DEFAULT = 512
LLAMA_UBATCH_MIN = 64
LLAMA_UBATCH_MAX = 4096
LLAMA_BATCH_DEFAULT = 1024
# --fit on sizes unset args to free VRAM; target = per-device margin MiB.
LLAMA_FIT = True
LLAMA_FIT_TARGET_MIB = 0
# Windows Vulkan WDDM spill guard: forces pure DEVICE_LOCAL VRAM allocations
# in ggml-vulkan unless disabled via settings or GGML_VK_DISABLE_HOST_VISIBLE_VIDMEM.
VK_DISABLE_HOST_VISIBLE_VIDMEM = True


def resolve_ubatch_sizes(settings: dict | None = None) -> tuple[int, int]:
    """(ubatch, batch) from settings - pure, unit-tested.

    duo_ubatch wins; llama_ubatch_size is kept as the older alias; neither
    set -> 256. The value is clamped to [64, 4096] and rounded down to a
    multiple of 32 (llama.cpp prefers aligned ubatches). batch is always
    >= ubatch (llama.cpp rejects ubatch > batch) and never below 1024.
    """
    s = settings or {}
    raw = s.get("duo_ubatch")
    if raw in (None, "", 0, "0"):
        raw = s.get("llama_ubatch_size")
    try:
        ub = int(raw) if raw not in (None, "") else LLAMA_UBATCH_DEFAULT
    except (TypeError, ValueError):
        ub = LLAMA_UBATCH_DEFAULT
    ub = max(LLAMA_UBATCH_MIN, min(LLAMA_UBATCH_MAX, ub))
    ub = (ub // 32) * 32 or LLAMA_UBATCH_DEFAULT
    batch = max(LLAMA_BATCH_DEFAULT, ub)
    return ub, batch


# MOE-UBATCH-RAISE (35B lever 3, 2026-11-12): the settings baseline
# (resolve_ubatch_sizes -> duo_ubatch, shipped 256) stays the global
# default; the per-model profile key "ubatch" (MoE models only) or the
# duo_ubatch_moe setting may raise it per load. Pure + unit-tested; the
# loader applies the raise only when the VRAM pre-flight still fits
# (ubatch_extra_mib surcharge) and falls back to the baseline otherwise.
def _align_ubatch(ub: int) -> int:
    ub = max(LLAMA_UBATCH_MIN, min(LLAMA_UBATCH_MAX, int(ub)))
    return (ub // 32) * 32 or LLAMA_UBATCH_DEFAULT


def resolve_model_ubatch(model: str, base_ubatch: int,
                         moe_setting: int = 0) -> tuple[int, str]:
    """Effective --ubatch-size for ONE model load: (value, source).

    base_ubatch is the settings baseline (duo_ubatch). moe_setting is the
    duo_ubatch_moe setting:
        > 0  force this ubatch for every MoE model (never below the baseline)
        < 0  veto: never raise above the baseline, profile ignored
       == 0  auto: a MoE model's profile "ubatch" key applies when it is
              above the baseline; everything else keeps the baseline
    Non-MoE models always keep the baseline (small cards are unaffected).
    source is one of "base", "veto", "setting", "profile".
    """
    base = _align_ubatch(base_ubatch)
    if not is_moe_model(model):
        return base, "base"
    try:
        _moe_set = int(moe_setting or 0)
    except (TypeError, ValueError):
        _moe_set = 0
    if _moe_set < 0:
        return base, "veto"
    if _moe_set > 0:
        _raised = _align_ubatch(_moe_set)
        return (_raised, "setting") if _raised > base else (base, "base")
    try:
        from model_configs.models_registry import get_ubatch as _gu
        _prof_ub = _gu(model)
    except (ImportError, AttributeError, KeyError, TypeError, ValueError):
        _prof_ub = None
    if _prof_ub:
        _raised = _align_ubatch(_prof_ub)
        if _raised > base:
            return _raised, "profile"
    return base, "base"


# UBATCH-COMPUTE-BUFFER (2026-10-30, audit): the VRAM table (llama_vram_table)
# has no ubatch term. Measured on the real server (Groot, ncmoe 28, ctx
# 32768): dedicated peak 5188 MiB at ub 512 vs 5333 at ub 1024 = +145 MiB
# for +512 ubatch, i.e. ~0.28 MiB of compute buffer per ubatch unit above
# 256. ONE helper so the VRAM pre-flight gate (manager_evict.can_fit) and
# the eviction heuristic (manager_load) cannot drift apart.
def ubatch_extra_mib(ubatch: int) -> float:
    """VRAM compute-buffer surcharge (MiB) for a ubatch above 256.

    Shared by the load gate (can_fit — this can BLOCK a load) and the
    eviction heuristic; both must see the same number.
    """
    try:
        return max(0, int(ubatch) - 256) * 0.28
    except (TypeError, ValueError):
        return 0.0


# ── MoE (Mixture of Experts) ──────────────────────────────────────────────────

# Per-model CPU-experts override: {model_key: n_cpu_moe}. 0/missing = from _MOE_EXPERT_COUNTS.
MOE_CPU_EXPERTS = {}
# Opt-in llama.cpp expert cache (--moe-cache-mib, PR #29887). 0 = off.
# Read live by the loader; pushed from settings by runtime_sync.
MOE_CACHE_MIB = 0

#   Override (moe_cpu_experts) > _MOE_EXPERT_COUNTS (kalibriert) > Autodetect.
_MOE_AUTODETECT_MIN_TOTAL_B = 30


def is_moe_model(model_key: str) -> bool:
    import re as _re_moe

    _key = str(model_key or "").strip().lower()
    if not _key:
        return False
    _norm = _re_moe.sub(r"(?:#\d+|:latest)+$", "", _key)
    for _cand in _ud_candidates(_key):
        # MOE-PIN (2026-10-14): an explicit profile answer ("moe": true/false in
        # model_configs/models/*.json) beats every name heuristic — the 30B+
        # autodetect is a guess, the model config is the user's call.
        try:
            from model_configs.models_registry import get_moe_pin as _reg_pin
            _pin = _reg_pin(_cand)
            if _pin is not None:
                return _pin
        except Exception:
            pass
    _ud_stripped = _strip_ud_suffix(_norm)
    if _key in _MOE_EXPERT_COUNTS or _norm in _MOE_EXPERT_COUNTS or _ud_stripped in _MOE_EXPERT_COUNTS:
        return True
    if "moe" in _norm:
        return True
    if _re_moe.search(r"(^|[-._:])(a\d+(?:\.\d+)?b|\d+x\d+(?:\.\d+)?b)([-._:]|$)", _norm):
        return True
    try:
        from backend.gguf_meta import read_gguf_metadata as _gguf_meta
        if _gguf_meta(model_key).get("is_moe"):
            return True
    except (ImportError, OSError, ValueError, TypeError) as _e:
        _logger.debug("[MOE] gguf_meta fallback skipped for %s: %s", model_key, _e)
    return False


def _ud_candidates(model_key: str) -> tuple[str, ...]:
    """Return exact alias-stripped key first, then -ud-stripped fallback if different."""
    raw = re.sub(r"#\d+$", "", str(model_key or "").strip())
    raw = re.sub(r"(?i):latest$", "", raw)
    stripped = _strip_ud_suffix(raw)
    return (raw, stripped) if stripped != raw else (raw,)


def _try_registry_field(
    model_key: str,
    getter_name: str,
    *,
    predicate=None,
    transform=None,
    default=None,
) -> Any:
    """Look up a field across `_ud_candidates(model_key)` in `models_registry`."""
    try:
        import model_configs.models_registry as _mr
        getter = getattr(_mr, getter_name, None)
        if not callable(getter):
            return default
        for _cand in _ud_candidates(model_key):
            val = getter(_cand)
            matched = predicate(val) if predicate is not None else (val is not None)
            if matched:
                return transform(val) if transform is not None else val
    except (ImportError, OSError, ValueError, TypeError, KeyError, AttributeError) as _e:
        _logger.debug("[REGISTRY] %s lookup skipped for %s: %s", getter_name, model_key, _e)
    return default


def _try_registry_moe(model_key: str) -> int:
    """Per-model registry -> n_cpu_moe."""
    return _try_registry_field(
        model_key, "get_moe_cpu_experts", transform=lambda v: int(v or 0), default=0
    )


def _try_registry_gpu_layers(model_key: str) -> int | None:
    """Per-model registry -> --n-gpu-layers."""
    return _try_registry_field(model_key, "get_gpu_layers")


def _try_registry_mtp(model_key: str) -> bool | None:
    """Per-model registry -> MTP/Speculative-Decoding."""
    return _try_registry_field(model_key, "is_mtp")


def _try_registry_draft_attr(model_key: str, getter_name: str) -> str | None:
    return _try_registry_field(
        model_key, getter_name, predicate=bool, transform=lambda v: str(v).strip(), default=None
    )


def _try_registry_dspark_draft(model_key: str) -> str | None:
    """Per-model registry -> DSpark drafter GGUF filename."""
    return _try_registry_draft_attr(model_key, "get_dspark_draft")


def _try_registry_dflash_draft(model_key: str) -> str | None:
    """Per-model registry -> DFlash drafter GGUF filename."""
    return _try_registry_draft_attr(model_key, "get_dflash_draft")


def _try_registry_mtp_draft(model_key: str) -> str | None:
    """Per-model registry -> external MTP sidecar filename."""
    return _try_registry_draft_attr(model_key, "get_mtp_draft")


def _try_registry_flash_attn_off(model_key: str) -> bool:
    """True when the model profile explicitly sets flash_attn=false (requires f16 KV)."""
    return bool(_try_registry_field(
        model_key, "get_flash_attn", transform=lambda v: v is False, default=False
    ))


def detect_moe_count(model_key: str) -> int:
    import re as _re_moe2
    _key = _re_moe2.sub(r"#\d+$", "", str(model_key or "").strip().lower()).removesuffix(":latest")
    _key_no_ud = _strip_ud_suffix(_key)
    _tbl = _MOE_EXPERT_COUNTS.get(_key)
    if not _tbl and _key_no_ud != _key:
        _tbl = _MOE_EXPERT_COUNTS.get(_key_no_ud)
    if not _tbl:
        # Variant suffixes from GGUF filenames (e.g. lfm2.5:24b-a2b-qad) match
        # their calibrated catalog prefix row in _MOE_EXPERT_COUNTS (longest prefix first).
        for _tk, _tv in sorted(_MOE_EXPERT_COUNTS.items(), key=lambda kv: -len(kv[0])):
            if _key.startswith(f"{_tk}-"):
                _tbl = _tv
                break
    if _tbl:
        return int(_tbl)
    m = _re_moe2.search(r"(\d+)\s*b[-_.]?a(\d+)\s*b", _key)
    if m:
        total_b = int(m.group(1))
        if total_b >= _MOE_AUTODETECT_MIN_TOTAL_B:
            return total_b
    try:
        from backend.gguf_meta import read_gguf_metadata as _gguf_meta
        meta = _gguf_meta(model_key)
        if meta.get("is_moe") and float(meta.get("file_size_gb") or 0) >= 12.0:
            blocks = int(meta.get("block_count") or 0)
            if blocks > 0:
                return blocks
    except (ImportError, OSError, ValueError, TypeError) as _e:
        _logger.debug("[MOE] detect_moe_count gguf_meta skipped for %s: %s", model_key, _e)
    return 0

MLOCK_MODEL = True

_MOE_EXPERT_COUNTS: dict[str, int] = {}
_MOE_KV_CACHE_TYPES: dict[str, str] = {}


def _strip_ud_suffix(model_key: str) -> str:
    """Strip ONE trailing "-ud" (Unsloth Dynamic) quant suffix.

    SUBSTRING-BUG (2026-10-28, audit): the old replace("-ud", "") mangled
    every name CONTAINING "-ud" ("study-ud-model:7b" -> "study-model:7b")
    and pulled another model's profile. Only the suffix counts, and it
    counts for the family table too (live: "k2-horizon:3.7b-ud" never hit
    the table entry "k2-horizon:3.7b" because the raw name was searched)."""
    k = str(model_key or "").strip()
    return k[:-3] if k.lower().endswith("-ud") else k


def moe_kv_cache_types(model_key: str) -> str:
    """MoE KV quantization for --cache-type-k/-cache-type-v.

    Resolution: flash_attn=false guard ("f16") > manual dict (raw or -ud-stripped key)
    > model profile ("kv_cache_type") > global KV_CACHE_TYPE default.
    """
    if _try_registry_flash_attn_off(model_key):
        return "f16"
    cands = _ud_candidates(model_key)
    v = None
    for _cand in cands:
        v = _MOE_KV_CACHE_TYPES.get(_cand) or _MOE_KV_CACHE_TYPES.get(_cand.lower())
        if v:
            break
    if not v:
        try:
            from model_configs.models_registry import get_profile as _gp
            for _cand in cands:
                v = (_gp(_cand) or {}).get("kv_cache_type")
                if v:
                    break
        except (ImportError, OSError, AttributeError, TypeError, ValueError) as _e:
            _logger.debug("[KV] moe profile kv lookup skipped for %s: %s", model_key, _e)
    return str(v or KV_CACHE_TYPE or "q4_0").strip().lower()


def dense_kv_cache_type(model_key: str, global_default: str = "") -> str:
    """Dense KV quantization for --cache-type-k/-cache-type-v.

    Resolution: flash_attn=false guard ("f16") > model profile ("kv_cache_type") >
    static family table (full key, then base before the colon) > global_default.
    """
    if _try_registry_flash_attn_off(model_key):
        return "f16"
    cands = _ud_candidates(model_key)
    v = None
    try:
        from model_configs.models_registry import get_profile as _gp
        for _cand in cands:
            v = (_gp(_cand) or {}).get("kv_cache_type")
            if v:
                break
    except (ImportError, AttributeError, TypeError, ValueError):
        # registry unavailable/malformed -> the family table and the global
        # default still resolve (same fallback contract as moe_kv_cache_types)
        v = None
    if not v:
        for _cand in cands:
            v = _DENSE_KV_CACHE_TYPES.get(_cand.lower())
            if v:
                break
    if not v:
        for _cand in cands:
            v = _DENSE_KV_CACHE_TYPES.get(_cand.lower().split(":")[0])
            if v:
                break
    return str(v or global_default or "").strip().lower()


def llama_gpu_layers_auto() -> bool:
    """llama_gpu_layers_auto setting: omit --n-gpu-layers so llama.cpp
    auto-fits the layers to free VRAM (it refuses while the flag pins 99:
    "n_gpu_layers already set by user to 99, abort" — live log)."""
    try:
        from settings import load_settings as _ls
        return bool(_ls().get("llama_gpu_layers_auto", False))
    except Exception:
        return False


def resolve_gpu_layers(model: str, override: int | None, table: dict,
                       auto: bool) -> int | None:
    """--n-gpu-layers value; None = omit the flag (llama auto-fit).

    Precedence preserved from the inline code this replaces: explicit
    override > per-model table entry > global default (99) — except in auto
    mode, where a model without explicit entry/table value omits the flag.
    """
    if override is not None:
        return int(override)
    raw = str(model)
    if raw in table:
        return int(table[raw])
    for _cand in _ud_candidates(raw):
        if _cand in table:
            return int(table[_cand])
        if _cand.lower() in table:
            return int(table[_cand.lower()])
    if auto:
        return None
    return int(GPU_LAYERS)

# Dense per-model KV-cache override populated from arch_compat.json.
_DENSE_KV_CACHE_TYPES: dict[str, str] = {}


# ── MTP (Multi-Token Prediction / Speculative Decoding) ─────────────────────
# Trade-off: MTP provides ~+55% decode speed, but greedy output is NOT
# bit-identical with MTP enabled (near-tie token flips); set HIVEMIND_MTP_SPEC=0
# for deterministic byte-identical runs (or use n_max <= 2; >=3 can diverge).
MTP_SPEC_TYPE = "draft-mtp"

MTP_DRAFT_N_MAX = int(os.environ.get("HIVEMIND_MTP_DRAFT_N_MAX", "2") or 2)

MTP_DRAFT_N_MIN = 1

# Default 0.0 with n_max 2 (the live value); a profile may override both via
# "mtp_draft_n_max" / "mtp_draft_p_min". Only passed when the binary's help
# lists --spec-draft-p-min (b12001 does, b11889 does not).
MTP_DRAFT_P_MIN = float(os.environ.get("HIVEMIND_MTP_DRAFT_P_MIN", "0.0") or 0.0)


# ── DSpark / DFlash external-drafters (Speculative-Decoding Sidecars) ────────
DSPARK_SPEC_TYPE = "draft-dspark"
DSPARK_DRAFT_N_MAX = 10
DSPARK_DRAFT_N_MIN = 0
DSPARK_MIN_BUILD = 10173   # llama.cpp mainline #25173 (--spec-type …draft-dspark)

DFLASH_SPEC_TYPE = "draft-dflash"
DFLASH_DRAFT_N_MAX = 7
DFLASH_DRAFT_N_MIN = 0
DFLASH_DRAFT_P_MIN = float(os.environ.get("HIVEMIND_DFLASH_DRAFT_P_MIN", "0.4") or 0.4)
DFLASH_MIN_BUILD = 12001   # llama.cpp mainline (--spec-type …draft-dflash)

_MTP_MODELS: set[str] = set()
_GPU_LAYERS_TABLE: dict[str, int] = {}


# ── Default Context Size ──────────────────────────────────────────────────────

CONTEXT_SIZE_DEFAULT = 4096


# ── Startup ───────────────────────────────────────────────────────────────────

DEFAULT_IDLE_TIMEOUT_SECONDS = 600   # 10 Minuten


BINARY_MIN_BUILD: dict[str, int] = {}

# ── Windows-Vulkan mlock/mmap-Regression (b10300+) ───────────────────────────
#   --mlock (or --load-mode mlock) → GGML_ASSERT(addr) failed in
NO_MMAP_MIN_BUILD = 10300


def reload_compat_views() -> None:
    """Populate compatibility dicts/sets dynamically from arch_compat.json + registry."""
    try:
        from model_configs.models_registry import (
            iter_all_profiles as _iter_profiles,
            load_arch_compat as _load_compat,
        )
        compat = _load_compat()
        profiles = _iter_profiles()
    except (ImportError, OSError, ValueError, TypeError) as _e:
        _logger.debug("[COMPAT] llama_config compat reload skipped: %s", _e)
        return

    dense_kv = {
        str(k).lower(): str(v).strip().lower()
        for k, v in (compat.get("dense_kv_cache_types") or {}).items()
        if v
    }
    _DENSE_KV_CACHE_TYPES.clear()
    _DENSE_KV_CACHE_TYPES.update(dense_kv)

    moe_counts: dict[str, int] = {
        str(k).lower(): int(v)
        for k, v in (compat.get("moe_expert_counts") or {}).items()
        if v is not None
    }
    gpu_layers: dict[str, int] = {
        str(k).lower(): int(v)
        for k, v in (compat.get("gpu_layers_table") or {}).items()
        if v is not None
    }
    mtp_models: set[str] = {
        str(m).lower() for m in (compat.get("mtp_models") or []) if m
    }
    min_builds: dict[str, int] = {
        str(k).lower(): int(v)
        for k, v in (compat.get("binary_min_build") or {}).items()
        if v is not None
    }

    for key, prof in profiles.items():
        lk = str(key).lower()
        try:
            moe_val = int(prof.get("moe_cpu_experts") or 0)
            if moe_val > 0:
                moe_counts[lk] = moe_val
        except (TypeError, ValueError):
            pass
        if prof.get("gpu_layers") is not None:
            try:
                gpu_layers[lk] = int(prof["gpu_layers"])
            except (TypeError, ValueError):
                pass
        if prof.get("mtp") is True:
            mtp_models.add(lk)
        if prof.get("binary_min_build") is not None:
            try:
                min_builds[lk] = int(prof["binary_min_build"])
            except (TypeError, ValueError):
                pass

    _MOE_EXPERT_COUNTS.clear()
    _MOE_EXPERT_COUNTS.update(moe_counts)
    _GPU_LAYERS_TABLE.clear()
    _GPU_LAYERS_TABLE.update(gpu_layers)
    _MTP_MODELS.clear()
    _MTP_MODELS.update(mtp_models)
    BINARY_MIN_BUILD.clear()
    BINARY_MIN_BUILD.update(min_builds)


reload_compat_views()
