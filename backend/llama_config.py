


from __future__ import annotations

import os
import platform
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


_LLAMA_ROOT = _REPO_ROOT / "llama"


# ── GPU-Backend ───────────────────────────────────────────────────────────────
def _resolve_gpu_backend() -> str:


    _env = os.environ.get("HIVEMIND_GPU_BACKEND", "").strip().lower()
    if _env in ("vulkan", "cuda", "cpu"):
        return _env
    try:
        from settings import load_settings as _ls
        _g = str(_ls().get("gpu_backend", "") or "").strip().lower()
        if _g in ("vulkan", "cuda", "cpu"):
            return _g
    except Exception:
        pass
    return "vulkan"


GPU_BACKEND = _resolve_gpu_backend()


def _find_llama_server() -> Path:
    """Find llama-server (Windows: .exe, POSIX: no suffix): env > auto-discovery > fallback."""
    _env = os.environ.get("HIVEMIND_LLAMA_BIN", "").strip()
    if _env:
        return Path(_env)

    # Auto-Discovery: <repo>/llama/<folder>/llama-server[.exe].
    _bin_name = "llama-server.exe" if platform.system() == "Windows" else "llama-server"
    _backend_tag = "cuda" if GPU_BACKEND == "cuda" else ("vulkan" if GPU_BACKEND == "vulkan" else "cpu")
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
            if _backend_tag == "cpu":
                # CPU backend: a vulkan/cuda binary won't start on a host
                # without the backend loader — backend fit outranks build age.
                key = (backend_match, build, cuda_ver)
            else:
                key = (build, backend_match, cuda_ver)
            if _best is None or key > _best[0]:
                _best = (key, cand)
    if _best is not None:
        return _best[1]

    return _LLAMA_ROOT / _bin_name


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

# Per-instance llama ports (2026-10-01): two HiveMind copies on one machine
# used to fight over the hardcoded 8101-8103 — startup_cleanup KILLS foreign
# llama-server processes on those ports. Cascade: env override ->
# settings.server_port + 100 -> legacy default. server_port comes from each
# instance's own settings.json (testing 8001 -> 8101-8103, dev 8011 -> 8111-8113).
BASE_PORT = int(os.environ.get("HIVEMIND_LLAMA_BASE_PORT", "").strip() or 0)
if not BASE_PORT:
    try:
        from settings import load_settings as _ls_port
        BASE_PORT = int(_ls_port().get("server_port", 0) or 0) + 100
    except Exception:
        BASE_PORT = 0
if not BASE_PORT or BASE_PORT < 1024:
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
# settings key llama_ubatch_size (server._sync_backend_runtime_config).
LLAMA_UBATCH = 256


# ── MoE (Mixture of Experts) ──────────────────────────────────────────────────

# Per-model CPU-experts override: {model_key: n_cpu_moe}. 0/missing = from _MOE_EXPERT_COUNTS.
MOE_CPU_EXPERTS = {}

#   Override (moe_cpu_experts) > _MOE_EXPERT_COUNTS (kalibriert) > Autodetect.
_MOE_AUTODETECT_MIN_TOTAL_B = 30


def is_moe_model(model_key: str) -> bool:


    _key = str(model_key or "").strip().lower()
    if not _key:
        return False
    # MOE-PIN (2026-10-14): an explicit profile answer ("moe": true/false in
    # model_configs/models/*.json) beats every name heuristic — the 30B+
    # autodetect is a guess, the model config is the user's call.
    try:
        from model_configs.models_registry import get_moe_pin as _reg_pin
        _pin = _reg_pin(_key)
        if _pin is not None:
            return _pin
    except Exception:
        pass
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

# MiniCPM-V-4.7 35B-A3B (2026-10-13): same MoE offload as hermes3.6/qwen3.6
# (n-cpu-moe 35, GPU-Offload 99, KV q4_0) — same 35B-A3B architecture class.
_MOE_EXPERT_COUNTS["minicpm-v-4.7:35b-a3b"] = 35

# LFM2.5-24B-A2B (2026-10-13): 24B total / 2.45B active MoE (64 experts,
# top-4). Measured 2026-10-14 (-lv 5 fit projection): 24 is the MAXIMUM that
# fits — 23 missed the 1024-MiB-free target by 58 MiB, 20 projected over
# budget. The generic autodetect only fires at >=30B total, so this needs
# the entry.
_MOE_EXPERT_COUNTS["lfm2.5:24b-a2b"] = 24

_HERMES_V7_MTP = "hermes3.6:35b-a3b-uncensored-genesis-v7-mtp-apex-compact"

_MOE_EXPERT_COUNTS[_HERMES_V7_MTP] = 35

# Hermes3.6 Genesis V10 MTP APEX (2026-08-25): identische Launchsettings wie
# V7 (n-cpu-moe 35, GPU-Offload 99, KV q4_0) — parallel zu V7 nutzbar.
_HERMES_V10_MTP = "hermes3.6:35b-a3b-uncensored-genesis-v10-mtp-apex-compact"
# APEX-Mini (2026-10-27): same MTP head as the Compact line.
_HERMES_APEX_MINI = "hermes3.6:35b-a3b-uncensored-genesis-apex-mini"
_MOE_EXPERT_COUNTS[_HERMES_V10_MTP] = 35

_MOE_KV_CACHE_TYPES: dict[str, str] = {}


def moe_kv_cache_types(model_key: str) -> str:
    """MoE KV quantization for --cache-type-k/-cache-type-v.

    Resolution: manual dict (raw or -ud-stripped key) > model profile
    ("kv_cache_type") > q4_0 default. The profile key is the escape hatch
    for models that degrade on q4_0 KV at long context; the aggressive
    default stays."""
    k = str(model_key or "").replace("-ud", "")
    v = _MOE_KV_CACHE_TYPES.get(str(model_key or "")) or _MOE_KV_CACHE_TYPES.get(k)
    if not v:
        try:
            from model_configs.models_registry import get_profile as _gp
            v = (_gp(k) or {}).get("kv_cache_type")
        except Exception:
            v = None
    return str(v or "q4_0").strip().lower()


def dense_kv_cache_type(model_key: str, global_default: str = "") -> str:
    """Dense KV quantization for --cache-type-k/-cache-type-v.

    Resolution: model profile ("kv_cache_type") > static family table
    (full key, then base before the colon) > global_default. Mirrors
    moe_kv_cache_types so the profile key applies to dense models too -
    previously only the MoE path consulted it (live 2026-10-27: a
    spark-x2.6:4b profile with q8_0 silently ran the global q4_0).
    global_default="" (an f16/empty global) means "no quantization flags"
    unless the profile says otherwise."""
    k = str(model_key or "").replace("-ud", "")
    low = str(model_key or "").lower()
    v = None
    try:
        from model_configs.models_registry import get_profile as _gp
        v = (_gp(k) or {}).get("kv_cache_type")
    except (ImportError, AttributeError, TypeError, ValueError):
        # registry unavailable/malformed -> the family table and the global
        # default still resolve (same fallback contract as moe_kv_cache_types)
        v = None
    if not v:
        v = _DENSE_KV_CACHE_TYPES.get(low) or _DENSE_KV_CACHE_TYPES.get(low.split(":")[0])
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
    if str(model) in table:
        return int(table[model])
    if auto:
        return None
    return int(GPU_LAYERS)

# Dense per-model KV-cache override (2026-09-26): MiniCPM5's 42 full-attention
# layers hold a full-length cache and the Sharp-MiniCPM5 card's memory table
# assumes -ctk/-ctv q8_0 — the global q4_0 default is too aggressive for this
# family. Matched on full key first, then on the base before the colon.
_DENSE_KV_CACHE_TYPES: dict[str, str] = {
    "minicpm5": "q8_0",
    "k2-horizon:3.7b": "q8_0",
}


# ── MTP (Multi-Token Prediction / Speculative Decoding) ─────────────────────
# DRAFT-N-MAX 2 (2026-09-15): draft-mtp with n-max 3 has a documented
# determinism/quality bug for the Qwen3.x MTP family (llama.cpp issue #23302,
# Qwen3.6-27B dense: output diverges from non-speculative greedy at n-max 3;
# 35B-A3B uses the same MTP implementation).
# NOT BIT-LOSSLESS AT 2 EITHER (measured 2026-10-25, b11802/Vulkan/q4_0, live
# box): greedy (temp 0, fixed seed) mtp=on n=2 diverged from mtp=off on 3/3
# prompts (near-tie token flips, coherent paraphrases - no visible corruption),
# while mtp=off was byte-identical across two engine restarts (3/3). The spec
# path changes the target-logit computation enough to flip near-ties: the
# "speculative decoding is lossless" guarantee does NOT hold here.
# Decision (2026-10-27, revised): mtp is ON for the coder profiles, n-max 2.
# The reason is a measured speed win that dwarfs the old prefill worry:
# +55 % decode for -1.6 % prefill (hermes 35B) and +54 % decode for -6.9 %
# prefill (qwen3.5:4b-mtp), same flags, 256-token answers, b11889, live box.
# The 2026-10-24 "off" decision rested on two legs; the prefill leg does not
# reproduce with llama_mlock=false (mmap prefill is fast), the determinism leg
# stands: greedy output is NOT bit-identical with mtp (0/3 at n-max 2 on
# b11802 AND b11889, near-tie token flips). So the trade is explicit:
# roughly half again the decode speed against bit-exact reproducibility when
# temperature is 0. Anyone who needs byte-identical runs sets
# HIVEMIND_MTP_SPEC=0. Never ship n-max 3+ (documented corruption, issue
# #23302). Re-check anchors: scripts/HiveMind_dev/mtp_quality.py +
# mtp_control.py, or /completion with ignore_eos.
# SPEC OFF-SWITCH (2026-09-17): HIVEMIND_MTP_SPEC=0 disables the spec-decode
# flags entirely — draft-mtp also disables llama-server's prompt cache-reuse
# and has reported heavy prefill regressions on some backends (Vulkan/Metal,
# 35B-A3B). A/B via env before pinning per-model config.
MTP_SPEC_TYPE = "draft-mtp"

MTP_ENABLED = os.environ.get("HIVEMIND_MTP_SPEC", "1") not in ("0", "false", "off")

MTP_DRAFT_N_MAX = int(os.environ.get("HIVEMIND_MTP_DRAFT_N_MAX", "2") or 2)

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
    "qwen3-vl":  8278,   # GGUF-Architektur "qwen3vl" — rope.dimension_sections
    "qwen3vl":   8278,   # interner Architektur-Name im GGUF
    "qwen35":    8300,
    "qwen3":     8260,
    "lfm2.5":    9518,
    "ling-3.0-tiny": 10488,
    # minicpmv4_7 VISION projector needs b11574+ (b11456 failed the mmproj
    # load with "unknown projector type" and ran TEXT fine — verified
    # 2026-10-14). Restored 2026-10-28 on b11889: the model is vision-capable
    # again (mmproj_filename + vision in the model profile, both base sets in
    # llama_manager_utils).
    "minicpm-v-4-7": 11574,
}

# ── Windows-Vulkan mlock/mmap-Regression (b10300+) ───────────────────────────
#   --mlock (or --load-mode mlock) → GGML_ASSERT(addr) failed in
NO_MMAP_MIN_BUILD = 10300

# ── Physical cores (2026-10-19) ──────────────────────────────────────────────
# MoE decode with experts in RAM is memory-bound; on an SMT CPU the logical count
# oversubscribes the physical pipelines (measured: 16 threads 12.6 tok/s vs 8
# threads 16.5 tok/s on an 8c/16t Ryzen). Cached - one query per process.
_PHYS_CORES: int | None = None


def physical_cores() -> int | None:
    """Physical core count, or None when it cannot be determined."""
    global _PHYS_CORES
    if _PHYS_CORES is not None:
        return _PHYS_CORES or None
    import os as _os
    import re as _re
    import subprocess as _sp
    try:
        if _os.name == "nt":
            _cnf = getattr(_sp, "CREATE_NO_WINDOW", 0)
            r = _sp.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_Processor | Measure-Object -Property "
                 "NumberOfCores -Sum).Sum"],
                capture_output=True, text=True, timeout=8,
                encoding="utf-8", errors="replace", creationflags=_cnf,
            )
            _n = int((r.stdout or "").strip() or 0)
        else:
            try:
                r = _sp.run(["sysctl", "-n", "hw.physicalcpu"], capture_output=True,
                            text=True, timeout=5)
                _n = int((r.stdout or "").strip() or 0)
            except Exception:
                with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as _f:
                    _pairs = set()
                    _phys_id = _core_id = None
                    for _ln in _f:
                        if _ln.startswith("physical id"):
                            _phys_id = _ln.split(":")[1].strip()
                        elif _ln.startswith("core id"):
                            _core_id = _ln.split(":")[1].strip()
                        elif not _ln.strip() and _phys_id and _core_id:
                            _pairs.add((_phys_id, _core_id))
                            _phys_id = _core_id = None
                    _n = len(_pairs)
    except Exception:
        _n = 0
    _PHYS_CORES = _n if 0 < _n < 1024 else 0
    return _PHYS_CORES or None
