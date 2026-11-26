


from __future__ import annotations

from collections.abc import Set as _AbstractSet
import logging
import math
import re

from .llama_config import _ud_candidates

_logger = logging.getLogger("llama_vram_table")

_INITIAL_KV_CACHE_TYPE: str = "q4_0"
KV_CACHE_TYPE: str = _INITIAL_KV_CACHE_TYPE
VRAM_PRE_FLIGHT_MARGIN_MIB: int = 768
TOTAL_VRAM_MIB_OVERRIDE: int = 0

# Compatibility views populated dynamically from model_configs/arch_compat.json
# and model_configs/{user,models}/*.json via reload_compat_views().
_VRAM_TABLE: dict[str, float] = {}
_MOE_TABLE: dict[str, dict] = {}
VRAM_GB: dict[str, float] = {}
_FINETUNE_TO_BASE: dict[str, str] = {}
_OVERFLOW_MODELS_SET: set[str] = set()
_CTX_KV_KB_PER_TOKEN: dict[str, float] = {}


def reload_compat_views() -> None:
    """Populate VRAM/MoE/KV compatibility tables dynamically from arch_compat.json + profiles."""
    try:
        from model_configs.models_registry import (
            iter_all_profiles as _iter_profiles,
            load_arch_compat as _load_compat,
        )
        compat = _load_compat()
        profiles = _iter_profiles()
    except (ImportError, OSError, ValueError, TypeError) as _e:
        _logger.debug("[VRAM] compat table reload skipped: %s", _e)
        return

    vram_table: dict[str, float] = {
        str(k).lower(): float(v)
        for k, v in (compat.get("vram_table") or {}).items()
        if v is not None
    }
    moe_table: dict[str, dict] = {
        str(k).lower(): dict(v)
        for k, v in (compat.get("moe_table") or {}).items()
        if isinstance(v, dict)
    }
    for alias, target in (compat.get("moe_table_aliases") or {}).items():
        tk = str(target).lower()
        if tk in moe_table:
            moe_table[str(alias).lower()] = moe_table[tk]

    for key, prof in profiles.items():
        lk = str(key).lower()
        try:
            ov = float(prof.get("vram_gb_override") or 0)
        except (TypeError, ValueError):
            ov = 0.0
        try:
            moe = int(prof.get("moe_cpu_experts") or 0)
        except (TypeError, ValueError):
            moe = 0
        if ov > 0 and moe > 0 and lk not in moe_table:
            entry: dict = {
                "active_gpu_gb": round(ov, 2),
                "source": "profile",
                "n_cpu_moe": moe,
                "calibrated_n_cpu_moe": moe,
            }
            try:
                kv_bpt = float(prof.get("kv_bytes_per_token") or 0)
            except (TypeError, ValueError):
                kv_bpt = 0.0
            if kv_bpt > 0:
                entry["measured_bytes_per_token"] = int(kv_bpt)
            moe_table[lk] = entry
        elif ov > 0 and moe <= 0 and lk not in vram_table:
            vram_table[lk] = round(ov, 2)

    _VRAM_TABLE.clear()
    _VRAM_TABLE.update(vram_table)

    _MOE_TABLE.clear()
    _MOE_TABLE.update(moe_table)

    VRAM_GB.clear()
    VRAM_GB.update(_VRAM_TABLE)
    VRAM_GB.update({k: float(v["active_gpu_gb"]) for k, v in _MOE_TABLE.items() if "active_gpu_gb" in v})

    _FINETUNE_TO_BASE.clear()
    _FINETUNE_TO_BASE.update({
        str(k).lower(): str(v).lower()
        for k, v in (compat.get("finetune_to_base") or {}).items()
        if v
    })

    _OVERFLOW_MODELS_SET.clear()
    _OVERFLOW_MODELS_SET.update(
        str(m).lower() for m in (compat.get("overflow_models") or []) if m
    )

    _CTX_KV_KB_PER_TOKEN.clear()
    _CTX_KV_KB_PER_TOKEN.update({
        str(k).lower(): float(v)
        for k, v in (compat.get("ctx_kv_kb_per_token") or {}).items()
        if v is not None
    })


reload_compat_views()
_OVERFLOW_MODELS_BASE: frozenset[str] = frozenset(_OVERFLOW_MODELS_SET)


def _read_profile(model_name: str) -> dict:
    try:
        from model_configs.models_registry import get_profile as _get_profile
        return _get_profile(model_name) or {}
    except (ImportError, OSError, ValueError, KeyError, TypeError):
        return {}


def _profile_moe_cfg(model_name: str) -> dict | None:
    """MoE sizing from the LOCAL model profile when the static table is silent.

    A profile carries `moe_cpu_experts` and, for models whose VRAM was measured
    on this box, `vram_gb_override`. Both come from the same JSON the loader
    reads, so a model that exists only locally (a test candidate, a fine-tune)
    gets a correct pre-flight estimate without editing this table.

    Without moe_cpu_experts the model is not MoE and the dense path stays
    authoritative, so this can never make a dense estimate worse.
    """
    prof = _read_profile(model_name)
    if not prof:
        return None
    try:
        moe = int(prof.get("moe_cpu_experts") or 0)
    except (TypeError, ValueError):
        moe = 0
    if moe <= 0:
        return None
    try:
        override = float(prof.get("vram_gb_override") or 0)
    except (TypeError, ValueError):
        override = 0.0
    if override <= 0:
        return None
    cfg: dict = {"active_gpu_gb": round(override, 2),
                 "source": "profile",
                 "n_cpu_moe": moe}
    try:
        kv_bpt = float(prof.get("kv_bytes_per_token") or 0)
    except (TypeError, ValueError):
        kv_bpt = 0.0
    if kv_bpt > 0:
        cfg["measured_bytes_per_token"] = int(kv_bpt)
    return cfg


def _profile_dense_vram(model_name: str) -> tuple[float | None, float | None]:
    """Optional `vram_gb_override` + `kv_bytes_per_token` for dense profiles."""
    prof: dict = {}
    for _cand in _ud_candidates(model_name):
        prof = _read_profile(_cand)
        if prof:
            break
    if not prof:
        return None, None
    try:
        ov = float(prof.get("vram_gb_override") or 0)
    except (TypeError, ValueError):
        ov = 0.0
    try:
        kv_bpt = float(prof.get("kv_bytes_per_token") or 0)
    except (TypeError, ValueError):
        kv_bpt = 0.0
    return (round(ov, 2) if ov > 0 else None), (kv_bpt if kv_bpt > 0 else None)


# Fallback KV cost for a MoE that carries no KV numbers anywhere (12 KB/token,
# the 35B-A3B q4_0 class measured on this box). Only used when neither the
# table nor the profile says anything better.
MOE_DEFAULT_BYTES_PER_TOKEN = 12288


# Rows whose name carries no MoE marker but that must keep prefix matching
# for quant/finetune suffixes (ling: hybrid KDA/MLA row).
_MOE_PREFIX_EXTRA = {"ling-3.0-tiny"}

_PROFILE_MOE_LOGGED: set[str] = set()


def _is_user_profile(model_name: str) -> bool:
    """Return True if a user layer override exists for `model_name`."""
    try:
        from model_configs.models_registry import get_profile_source as _gps
        for _cand in _ud_candidates(model_name):
            if _gps(_cand) == "user":
                return True
    except (ImportError, OSError, ValueError, TypeError, AttributeError):
        return False
    return False


def moe_cfg_for(model_name: str) -> dict | None:
    """_MOE_TABLE resolution shared by vram_of_moe and the /vram/estimate route:
    user override > exact, -ud-stripped, then prefix > shipped/local profile."""
    cands = [c.lower() for c in _ud_candidates(model_name) if c]
    if not cands:
        return None
    key = cands[0]

    if _is_user_profile(model_name):
        for _cand in cands:
            user_cfg = _profile_moe_cfg(_cand)
            if user_cfg is not None:
                return user_cfg

    cfg = None
    for _cand in cands:
        cfg = _MOE_TABLE.get(_cand)
        if cfg is not None:
            break
    if not cfg:
        for table_key in sorted(_MOE_TABLE, key=len, reverse=True):
            if table_key == key:
                continue
            if not (any(m in table_key for m in ("a3b", "a2b", "a2.5b", "35b"))
                    or table_key in _MOE_PREFIX_EXTRA):
                continue
            if key.startswith(table_key):
                return _MOE_TABLE[table_key]
    if cfg is None:
        for _cand in cands:
            cfg = _profile_moe_cfg(_cand)
            if cfg is not None:
                break
        if cfg:
            if key not in _PROFILE_MOE_LOGGED:
                _PROFILE_MOE_LOGGED.add(key)
                _logger.info("moe_cfg_for(%s): no table entry, using the profile "
                             "(active_gpu_gb=%s, n_cpu_moe=%s)",
                             key, cfg["active_gpu_gb"], cfg.get("n_cpu_moe"))
            else:
                _logger.debug("moe_cfg_for(%s): profile repeat", key)
    return cfg


def _explicit_moe_override(model_name: str) -> int | None:
    """Return user's explicit MOE_CPU_EXPERTS override for `model_name`, if any."""
    try:
        from backend import llama_config as _lc
        moe_map = getattr(_lc, "MOE_CPU_EXPERTS", None) or {}
    except (ImportError, AttributeError):
        return None
    if not isinstance(moe_map, dict) or not moe_map:
        return None
    raw = str(model_name or "").strip().lower()
    if not raw:
        return None
    norm = re.sub(r"#\d+$", "", raw).removesuffix(":latest")
    ud_stripped = norm[:-3] if norm.endswith("-ud") else norm
    lower_map = {str(k).strip().lower(): v for k, v in moe_map.items()}
    for cand in (raw, norm, ud_stripped):
        if cand in lower_map and lower_map[cand] is not None:
            try:
                return int(lower_map[cand])
            except (TypeError, ValueError):
                return None
    return None


def moe_base_gb_for_experts(
    model_name: str,
    cfg: dict,
    n_cpu_moe: int | None = None,
) -> float:
    """Estimate active GPU base GB at `n_cpu_moe`, scaling conservatively from
    the calibrated table/profile split and GGUF block count when overridden."""
    base = float(cfg["active_gpu_gb"])
    cal_n = int(cfg.get("calibrated_n_cpu_moe") or cfg.get("n_cpu_moe") or 0)
    eff_n = int(n_cpu_moe) if n_cpu_moe is not None else _explicit_moe_override(model_name)
    if eff_n is None or cal_n <= 0 or eff_n == cal_n:
        return base

    try:
        from backend.gguf_meta import read_gguf_metadata as _read_meta
        meta = _read_meta(model_name) or {}
    except (ImportError, OSError, RuntimeError, ValueError, TypeError):
        meta = {}
    file_gb = float(meta.get("file_size_gb") or 0.0)
    if file_gb <= base or file_gb <= 0.0:
        return base

    total_blocks = int(meta.get("block_count") or 0)
    eff_clamped = min(max(0, eff_n), total_blocks) if total_blocks > 0 else max(0, eff_n)
    if eff_clamped == cal_n:
        return base
    per_block_gb = (file_gb - base) / float(cal_n)
    if eff_clamped > cal_n:
        floor_gb = max(2.2, round(file_gb * 0.21, 2))
        adjusted = base - (eff_clamped - cal_n) * (per_block_gb * 0.80)
        return round(max(floor_gb, adjusted), 2)
    adjusted = base + (cal_n - eff_clamped) * per_block_gb
    return round(min(file_gb, adjusted), 2)


def vram_of_moe(
    model_name: str,
    num_ctx: int = 4096,
    *,
    n_cpu_moe: int | None = None,
) -> float:


    cfg = moe_cfg_for(model_name)
    if not cfg:
        return vram_of_with_ctx(model_name, num_ctx)  # dense fallback

    base = moe_base_gb_for_experts(model_name, cfg, n_cpu_moe=n_cpu_moe)
    if num_ctx <= 4096:
        return base

    if "measured_bytes_per_token" in cfg:
        overhead_gb = (num_ctx * cfg["measured_bytes_per_token"]) / (1024 ** 3)
        return round(base + overhead_gb, 2)

    if "kv_heads" in cfg and "layers" in cfg:
        kv_per_tok = 2 * cfg["kv_heads"] * cfg["layers"] * 128 * 0.5
        extra_ctx = num_ctx - 4096
        overhead_gb = (extra_ctx * kv_per_tok) / (1024 ** 3)
        return round(base + overhead_gb, 2)

    # No KV shape recorded (a sparse table entry, or a profile-derived one).
    # A dense ctx delta is meaningless for a MoE (the weights live on the CPU),
    # so use the 35B-A3B q4_0 class default of 12 KB/token. Conservative for
    # smaller MoE, honest for this box, and no KeyError on the load path.
    overhead_gb = (num_ctx * MOE_DEFAULT_BYTES_PER_TOKEN) / (1024 ** 3)
    return round(base + overhead_gb, 2)


_TIGHT_VRAM_OVERFLOW_THRESHOLD_MIB: int = 12288


def _current_gpu_backend() -> str:
    try:
        from . import llama_config as _lc
        return str(getattr(_lc, "GPU_BACKEND", "") or "").strip().lower()
    except (ImportError, AttributeError):
        return ""


def _is_tight_vram_gpu() -> bool:
    if _current_gpu_backend() == "cpu":
        return False
    return _effective_total_vram_mib() <= _TIGHT_VRAM_OVERFLOW_THRESHOLD_MIB


class _TightVramOverflowModels(_AbstractSet[str]):
    """Dynamic set view of VRAM_OVERFLOW_MODELS gated to tight-VRAM GPUs."""

    def _active(self) -> frozenset[str]:
        return frozenset(_OVERFLOW_MODELS_SET) if _is_tight_vram_gpu() else frozenset()

    def __contains__(self, item: object) -> bool:
        return item in self._active()

    def __iter__(self):
        return iter(self._active())

    def __len__(self) -> int:
        return len(self._active())

    def __bool__(self) -> bool:
        return bool(self._active())

    def __repr__(self) -> str:
        return repr(set(self._active()))

    def __eq__(self, other: object) -> bool:
        if isinstance(other, (set, frozenset, _AbstractSet)):
            return self._active() == other
        return NotImplemented

    def copy(self) -> set[str]:
        return set(self._active())


VRAM_OVERFLOW_MODELS: _AbstractSet[str] = _TightVramOverflowModels()

# ── ctx-Overhead ───────────────────────────────────────────────────────────────
# KV-Cache Overhead pro 1024 ctx-Tokens (q4_0 Quantisierung).
#   KV per token = 2 * 36 * 4 * 128 * 0.5 = 18432 bytes ≈ 18KB
#   Per 1024 extra tokens: 18KB * 1024 ≈ 18MB ≈ 0.018GB
#   falsche Evictions → exit=-1 → CPU-Fallback (alles im RAM).
_CTX_OVERHEAD_BASE_GB: float = 0.03
_CTX_OVERHEAD_REF_SIZE_B: float = 4.0  # Referenz-Groesse in Milliarden Parametern


def _extract_size_gb(model_name: str) -> float | None:
    """Extrahiert die Modellgroesse aus dem Namen (z.B. '4b' → 4.0, '1.5b' → 1.5)."""
    m = re.search(r'[:\-](0?\d{1,3}(?:\.\d+)?)[bB](?:[\-]|$)', model_name)
    if m:
        return float(m.group(1))
    # E-Size: E4B → 4B equivalent
    m = re.search(r'[:\-]E(\d+)[bB](?:[\-]|$)', model_name, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def vram_of(model_name: str) -> float:
    cands = [c.lower() for c in _ud_candidates(model_name) if c]
    if not cands:
        return 4.0
    name = cands[0]

    # 0. PROFILE-FIRST (2026-10-30, audit fix): a model whose profile carries
    #    moe_cpu_experts + vram_gb_override is sized by its own profile
    #    (active_gpu_gb = the measured non-KV resident part). Without this,
    #    profile-sized MoEs fell through to the dense heuristic (~0.6 GB/B),
    #    which overstates a 35B-A3B by gigabytes and blocked loads that fit.
    # Any MoE sizing (static table row OR local profile) is authoritative:
    # n_cpu_moe + active_gpu_gb store the CPU/GPU split; the dense heuristic
    # must never override it.
    _cfg = moe_cfg_for(model_name)
    if _cfg and _cfg.get("active_gpu_gb"):
        return round(moe_base_gb_for_experts(model_name, _cfg), 2)

    _dense_ov, _ = _profile_dense_vram(model_name)
    if _dense_ov is not None:
        return _dense_ov

    # 1. Exact match (exact -ud key first, then -ud-stripped fallback)
    for _cand in cands:
        if _cand in _VRAM_TABLE:
            return _VRAM_TABLE[_cand]

    for _cand in cands:
        if ":" not in _cand:
            continue
        base, tag = _cand.split(":", 1)
        if not base or not tag:
            continue
        for key, val in _VRAM_TABLE.items():
            if ":" not in key:
                continue
            key_base, key_tag = key.split(":", 1)
            if key_base == base and tag in key_tag:
                return val

    base = name.split(":")[0] if ":" in name else ""
    if base in _FINETUNE_TO_BASE:
        base_name = _FINETUNE_TO_BASE[base]
        for _cand in cands:
            tag = _cand.split(":")[1] if ":" in _cand else ""
            base_lookup = f"{base_name}:{tag}" if tag else base_name
            if base_lookup in _VRAM_TABLE:
                return _VRAM_TABLE[base_lookup]
        size = _extract_size_gb(name)
        if size is not None:
            for key, val in _VRAM_TABLE.items():
                if key.startswith(f"{base_name}:") and _extract_size_gb(key) == size:
                    return val

    # 3. GGUF metadata file size fallback (actual quant footprint for dropped-in GGUFs)
    try:
        from backend.gguf_meta import read_gguf_metadata as _read_gguf_meta
        _meta = _read_gguf_meta(model_name)
        _fsize = float(_meta.get("file_size_gb") or 0)
        if _fsize > 0:
            return round(_fsize * 1.08, 2)
    except (ImportError, OSError, TypeError, ValueError) as _e:
        _logger.debug("[VRAM] gguf_meta base size skipped for %r: %s", model_name, _e)

    # 4. size heuristic
    size = _extract_size_gb(name)
    if size is not None:
        # Q4_K_M Faustregel: ~0.6 GB pro Milliarde Parameter
        return round(size * 0.6, 1)

    # 4. Konservativer Fallback
    _logger.debug(f"VRAM estimate for '{model_name}': fallback 4.0GB")
    return 4.0


def vram_of_with_ctx(model_name: str, num_ctx: int = 4096) -> float:


    # MOE-DELEGATION (2026-10-30, audit fix): any model with a MoE sizing
    # (static table row or local profile) is sized by vram_of_moe — the SAME
    # function the load gate uses (manager_load "same resolution vram_of_moe
    # uses"). Without this, a profile-sized MoE that is missing from
    # _CTX_KV_KB_PER_TOKEN fell through to the DENSE ctx formula and produced
    # absurd estimates (Groot 19B: 29+ GiB @131k) that block loads that fit.
    # Sits ABOVE the ctx<=4096 early return so a table MoE answers its
    # active_gpu_gb at small ctx too (else 21.0@4096 vs 4.68@4097 — a
    # non-monotonic cliff manager_health sums into free_gb). The guard against
    # the dense fallback inside vram_of_moe avoids recursion.
    if moe_cfg_for(model_name):
        return vram_of_moe(model_name, num_ctx)

    base_vram = vram_of(model_name)

    if num_ctx <= 4096:
        return base_vram

    _, _kv_bpt = _profile_dense_vram(model_name)
    if _kv_bpt is not None:
        return round(base_vram + ((num_ctx - 4096) * _kv_bpt) / (1024 ** 3), 2)

    # QWEN35-HYBRID-KV (2026-10-10, gemessen): qwen3.5 traegt KV nur auf
    # 8 von 33 Layern (4 KV-Koepfe x 256 key_length, Rest SSM). Gemessen
    # (llama-server -v, q4_0/FA-on): 72 MiB @8192, 1062 MiB @120832 =
    # 9,08 KB/Token; f16 = 32 KB/Token. Die generische Formel unten
    # (36x4x128 -> 30 MB/1k) ueberschaetzt die qwen35-KV ~2x und blockte
    # Realruns am PRE-FLIGHT-Gate (VRAMPreFlightError, 09.10.). Nur der
    # belegte 4B-Wert; andere Groessen unbestaetigt (alter Pfad).
    _cands = [c.lower() for c in _ud_candidates(model_name) if c]
    _kb = None
    for _cand in _cands:
        for _key, _kv in _CTX_KV_KB_PER_TOKEN.items():
            if _cand.startswith(_key):
                _kb = _kv
                break
        if _kb is not None:
            break
    if _kb is not None:
        extra_ctx = num_ctx - 4096
        if extra_ctx > 0:
            return round(base_vram + (extra_ctx / 1024) * _kb / 1000, 2)

    try:
        from backend import llama_config as _lc
        from backend.gguf_meta import read_gguf_metadata as _read_gguf_meta
        _meta = _read_gguf_meta(model_name)
        _kv_q4 = _meta.get("kv_bytes_per_token_q4")
        _kv_f16 = _meta.get("kv_bytes_per_token_f16")
        if _kv_q4 and _kv_f16:
            _kv_default = (
                KV_CACHE_TYPE
                if KV_CACHE_TYPE != _INITIAL_KV_CACHE_TYPE
                else getattr(_lc, "KV_CACHE_TYPE", KV_CACHE_TYPE)
            )
            _kv_type = _lc.dense_kv_cache_type(model_name, _kv_default)
            if _kv_type in ("f16", "bf16"):
                _bpt = float(_kv_f16)
            elif _kv_type == "q8_0":
                _bpt = float(_kv_f16) * (34.0 / 64.0)
            else:
                _bpt = float(_kv_q4)
            return round(base_vram + ((num_ctx - 4096) * _bpt) / (1024 ** 3), 2)
    except (ImportError, OSError, TypeError, ValueError) as _e:
        _logger.debug("[VRAM] gguf_meta kv fallback skipped for %r: %s", model_name, _e)

    size = _extract_size_gb(model_name) or 4.0
    size_factor = size / _CTX_OVERHEAD_REF_SIZE_B
    extra_ctx = num_ctx - 4096
    ctx_overhead = (extra_ctx / 1024) * _CTX_OVERHEAD_BASE_GB * size_factor

    return round(base_vram + ctx_overhead, 2)


# ── Live-VRAM-Query (Windows WDDM Performance-Counter via pywin32/PDH) ──────────
_DETECTED_TOTAL_VRAM_CACHE: int | None = None
_DETECTED_TOTAL_VRAM_PROBED: bool = False


def _query_nvidia_smi_mib(field: str, caller_tag: str) -> list[float] | None:
    """Query one numeric GPU memory column (MiB) via nvidia-smi."""
    import shutil as _shutil
    import subprocess as _subprocess

    nsmi = _shutil.which("nvidia-smi")
    if not nsmi:
        return None
    try:
        cnf = int(getattr(_subprocess, "CREATE_NO_WINDOW", 0))
        res = _subprocess.run(
            [nsmi, f"--query-gpu={field}", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=5, creationflags=cnf,
        )
        if res.returncode != 0:
            return None
        vals: list[float] = []
        for tok in res.stdout.decode("ascii", "replace").replace(",", " ").split():
            try:
                fv = float(tok)
                if math.isfinite(fv) and fv >= 0.0:
                    vals.append(fv)
            except ValueError:
                continue
        return vals
    except (OSError, ValueError, TypeError, _subprocess.SubprocessError) as exc:
        _logger.warning("%s: nvidia-smi failed (%s: %s)", caller_tag, type(exc).__name__, exc)
        return None


def _probe_nvidia_smi_total_mib() -> int | None:
    """Query total GPU VRAM in MiB via nvidia-smi (None if unavailable)."""
    vals = [int(v) for v in (_query_nvidia_smi_mib("memory.total", "detect_gpu_total_vram_mib") or []) if v > 0]
    return max(vals) if vals else None


def _probe_linux_sysfs_cards_mib(*, need_used: bool = False) -> tuple[float, float]:
    """Scan /sys/class/drm cards, returning (max_total_bytes, matching_used_bytes)."""
    from pathlib import Path as _P
    best_total = 0.0
    best_used = 0.0
    for card in _P("/sys/class/drm").glob("card[0-9]*/device"):
        tf = card / "mem_info_vram_total"
        uf = (card / "mem_info_vram_used") if need_used else None
        if not tf.exists() or (uf is not None and not uf.exists()):
            continue
        try:
            tv = float(tf.read_text(encoding="utf-8", errors="replace").strip())
            uv = float(uf.read_text(encoding="utf-8", errors="replace").strip()) if uf is not None else 0.0
        except (OSError, ValueError):
            continue
        if tv > best_total or (need_used and tv == best_total and tv > 0 and uv < best_used):
            best_total = tv
            best_used = uv
    return best_total, best_used


def _probe_linux_sysfs_total_mib() -> int | None:
    """Query Linux DRM sysfs dedicated VRAM total in MiB (None if unavailable)."""
    try:
        sysfs_total, _ = _probe_linux_sysfs_cards_mib(need_used=False)
        if sysfs_total > 0:
            return int(sysfs_total / (1024 * 1024))
    except OSError as exc:
        _logger.debug("detect_gpu_total_vram_mib: sysfs probe failed: %s", exc)
    return None


def _probe_gpu_total_vram_mib() -> int | None:
    """Detect physical GPU total dedicated VRAM in MiB (None if unavailable)."""
    import platform as _platform

    if _platform.system() != "Windows":
        if _current_gpu_backend() == "cuda":
            cu_total = _probe_nvidia_smi_total_mib()
            if cu_total is not None:
                return cu_total
            return _probe_linux_sysfs_total_mib()
        sysfs_total = _probe_linux_sysfs_total_mib()
        if sysfs_total is not None:
            return sysfs_total
        return _probe_nvidia_smi_total_mib()

    # Windows: 1) PDH Dedicated Limit when exposed, 2) WDDM adapter registry
    # HardwareInformation.qwMemorySize, 3) nvidia-smi fallback.
    h = None
    try:
        import win32pdh
        pdh_err = getattr(win32pdh, "error", OSError)
        try:
            paths = win32pdh.ExpandCounterPath(r"\GPU Adapter Memory(*)\Dedicated Limit")
            if paths:
                h = win32pdh.OpenQuery()
                counters = [(p, win32pdh.AddCounter(h, p)) for p in paths]
                win32pdh.CollectQueryData(h)
                best_bytes = 0.0
                for _p, c in counters:
                    try:
                        _typ, val = win32pdh.GetFormattedCounterValue(c, win32pdh.PDH_FMT_LARGE)
                        if float(val) > best_bytes:
                            best_bytes = float(val)
                    except (OSError, ValueError, TypeError, pdh_err):
                        continue
                win32pdh.CloseQuery(h)
                h = None
                if best_bytes > 0:
                    return int(best_bytes / (1024 * 1024))
        except (OSError, ValueError, TypeError, RuntimeError, pdh_err) as exc:
            _logger.debug("detect_gpu_total_vram_mib: PDH Dedicated Limit unavailable: %s", exc)
            if h is not None:
                try:
                    win32pdh.CloseQuery(h)
                except (OSError, pdh_err):
                    pass
    except ImportError:
        pass

    try:
        import winreg
        base = r"SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        best_bytes = 0
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
            idx = 0
            while True:
                try:
                    sub = winreg.EnumKey(k, idx)
                    idx += 1
                except OSError:
                    break
                try:
                    with winreg.OpenKey(k, sub) as sk:
                        sz, _ = winreg.QueryValueEx(sk, "HardwareInformation.qwMemorySize")
                        raw = (
                            int.from_bytes(sz, "little")
                            if isinstance(sz, (bytes, bytearray))
                            else int(sz)
                        )
                        if raw > best_bytes:
                            best_bytes = raw
                except (OSError, ValueError, TypeError):
                    continue
        if best_bytes > 0:
            return int(best_bytes / (1024 * 1024))
    except (ImportError, OSError, ValueError, TypeError) as exc:
        _logger.debug("detect_gpu_total_vram_mib: registry VRAM probe skipped: %s", exc)

    return _probe_nvidia_smi_total_mib()


def detect_gpu_total_vram_mib(*, refresh: bool = False) -> int | None:
    """Cached physical GPU total dedicated VRAM in MiB (None if unavailable)."""
    global _DETECTED_TOTAL_VRAM_CACHE, _DETECTED_TOTAL_VRAM_PROBED
    if _DETECTED_TOTAL_VRAM_PROBED and not refresh:
        return _DETECTED_TOTAL_VRAM_CACHE
    _DETECTED_TOTAL_VRAM_CACHE = _probe_gpu_total_vram_mib()
    _DETECTED_TOTAL_VRAM_PROBED = True
    return _DETECTED_TOTAL_VRAM_CACHE


def _configured_vram_budget_gb() -> float:
    """Return configured vram_budget_gb from llama_manager_utils or settings."""
    try:
        import sys as _sys
        _lmu = _sys.modules.get("backend.llama_manager_utils")
        if _lmu is not None and hasattr(_lmu, "VRAM_BUDGET_GB"):
            return float(_lmu.VRAM_BUDGET_GB)
    except (TypeError, ValueError):
        pass
    try:
        from settings import load_settings as _ls
        _raw = _ls().get("vram_budget_gb", 7.5)
        return float(_raw if _raw is not None else 7.5)
    except (ImportError, OSError, TypeError, ValueError):
        return 7.5


def _buffered_total_vram_mib(detected_mib: int | None, vram_budget_gb: float) -> int:
    """Apply defensive rounding buffer near 8 GiB cards reporting ~8176 MiB."""
    buf_mib = int(float(vram_budget_gb) * 1024 + 512)
    if detected_mib is None or detected_mib <= 0:
        return max(8192, buf_mib)
    if detected_mib < buf_mib and (buf_mib - detected_mib) <= 512:
        return max(detected_mib, buf_mib)
    return int(detected_mib)


_INITIAL_TOTAL_VRAM_MIB: int = _buffered_total_vram_mib(
    detect_gpu_total_vram_mib(), _configured_vram_budget_gb()
)
TOTAL_VRAM_MIB: int = _INITIAL_TOTAL_VRAM_MIB


def _effective_total_vram_mib() -> int:
    """Effective GPU total VRAM (MiB).

    Honors explicit monkeypatches to module-level TOTAL_VRAM_MIB first, then
    runtime TOTAL_VRAM_MIB_OVERRIDE (> 0), before dynamic GPU detection.
    """
    if TOTAL_VRAM_MIB != _INITIAL_TOTAL_VRAM_MIB:
        return int(TOTAL_VRAM_MIB)
    if TOTAL_VRAM_MIB_OVERRIDE > 0:
        return int(TOTAL_VRAM_MIB_OVERRIDE)
    try:
        from backend import llama_config as _lc
        lc_ov = int(getattr(_lc, "TOTAL_VRAM_MIB_OVERRIDE", 0) or 0)
        if lc_ov > 0:
            return lc_ov
    except (ImportError, TypeError, ValueError) as _e:
        _logger.debug("[VRAM] TOTAL_VRAM_MIB_OVERRIDE read skipped: %s", _e)
    return _buffered_total_vram_mib(
        detect_gpu_total_vram_mib(), _configured_vram_budget_gb()
    )

_sel_path_cache: str | None = None


def _pdh_instance_name(counter_path: str) -> str:
    """Extract PDH instance identifier between the outermost parentheses."""
    if "(" in counter_path and ")" in counter_path:
        return counter_path.rsplit("(", 1)[-1].split(")", 1)[0]
    return counter_path


def _select_gpu_instance(
    by_path: dict[str, float],
    limits_by_inst: dict[str, float] | None = None,
) -> str:
    """Select the primary GPU PDH counter instance (highest Dedicated Limit, then Usage)."""
    global _sel_path_cache
    if _sel_path_cache is not None and _sel_path_cache in by_path:
        return _sel_path_cache
    if limits_by_inst:
        matched = [
            p for p in by_path
            if limits_by_inst.get(_pdh_instance_name(p), 0.0) > 0.0
        ]
        if matched:
            _sel = max(
                matched,
                key=lambda p: (limits_by_inst[_pdh_instance_name(p)], by_path[p]),
            )
            _sel_path_cache = _sel
            return _sel
    # Fallback when Dedicated Limit is unavailable: highest Dedicated Usage,
    # caching only on single-adapter hosts so an idle dGPU is not skipped.
    _max = max(by_path, key=lambda k: by_path[k])
    if len(by_path) == 1:
        _sel_path_cache = _max
    return _max


def _probe_linux_sysfs_free_mib() -> float | None:
    """Query Linux DRM sysfs free VRAM in MiB (None if unavailable)."""
    try:
        _sysfs_total, _sysfs_used = _probe_linux_sysfs_cards_mib(need_used=True)
        if _sysfs_total > 0:
            _free = max(0.0, (_sysfs_total - _sysfs_used) / (1024 * 1024))
            _logger.debug("get_live_gpu_free_mib: sysfs amdgpu %.0f MiB free", _free)
            return round(_free, 1)
    except OSError as _exc:
        _logger.debug("sysfs VRAM probe unavailable (%s), falling back", _exc)
    return None


def _probe_nvidia_smi_free_mib() -> float | None:
    """Query free GPU VRAM in MiB via nvidia-smi (None if unavailable)."""
    _vals = _query_nvidia_smi_mib("memory.free", "get_live_gpu_free_mib")
    if _vals is None:
        return None
    if not _vals:
        _logger.debug("get_live_gpu_free_mib: nvidia-smi returned no values")
        return None
    return round(max(_vals), 1)


def get_live_gpu_free_mib() -> float | None:


    import platform
    if platform.system() != "Windows":
        if _current_gpu_backend() == "cuda":
            _cu_free = _probe_nvidia_smi_free_mib()
            if _cu_free is not None:
                return _cu_free
            return _probe_linux_sysfs_free_mib()
        _sysfs_free = _probe_linux_sysfs_free_mib()
        if _sysfs_free is not None:
            return _sysfs_free
        return _probe_nvidia_smi_free_mib()
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
        _limits_by_inst: dict[str, float] | None = None
        if len(_by_path) > 1 and (_sel_path_cache is None or _sel_path_cache not in _by_path):
            _lh = None
            _pdh_err = getattr(win32pdh, "error", OSError)
            try:
                _lim_paths = win32pdh.ExpandCounterPath(r"\GPU Adapter Memory(*)\Dedicated Limit")
                if _lim_paths:
                    _lh = win32pdh.OpenQuery()
                    _lim_counters = [(lp, win32pdh.AddCounter(_lh, lp)) for lp in _lim_paths]
                    win32pdh.CollectQueryData(_lh)
                    _limits_by_inst = {}
                    for lp, lc in _lim_counters:
                        try:
                            _lt, _lv = win32pdh.GetFormattedCounterValue(lc, win32pdh.PDH_FMT_LARGE)
                            _limits_by_inst[_pdh_instance_name(lp)] = float(_lv)
                        except (OSError, ValueError, TypeError, _pdh_err):
                            continue
            except (OSError, ValueError, TypeError, RuntimeError, _pdh_err) as _le:
                _logger.debug("get_live_gpu_free_mib: Dedicated Limit query skipped: %s", _le)
                _limits_by_inst = None
            finally:
                if _lh is not None:
                    try:
                        win32pdh.CloseQuery(_lh)
                    except (OSError, _pdh_err):
                        pass
        # 1. Cached instance, 2. Highest Dedicated Limit (dGPU over iGPU).
        _sel_before = _sel_path_cache
        _sel_path = _select_gpu_instance(_by_path, _limits_by_inst)
        if _sel_before != _sel_path:
            _inst_desc = ", ".join(
                f"{_pdh_instance_name(p)}={_by_path[p]/(1024*1024):.0f}MiB"
                for p in sorted(_by_path)
            )
            _logger.info(
                "get_live_gpu_free_mib: selected GPU instance '%s' [%s]",
                _pdh_instance_name(_sel_path), _inst_desc,
            )
        used_mib = _by_path[_sel_path] / (1024 * 1024)
        free_mib = _effective_total_vram_mib() - used_mib
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
                                poll_interval: float = 0.5,
                                stall_abort_s: float = 0.0) -> bool:


    import asyncio as _asyncio
    import time as _time
    try:
        from .llama_config import GPU_BACKEND as _wvr_gb
        if _wvr_gb == "cpu":
            return True  # CPU-BACKEND: no VRAM to reclaim
    except Exception:
        pass
    # NO-LIVE-SOURCE SHORT-CIRCUIT (2026-09-19): without a live VRAM source
    # the loop below can never observe a reclaim — it spun the full timeout
    # and returned False on every eviction. Can't-measure -> don't block:
    # report OK once; a real OOM surfaces visibly at the llama-server load.
    try:
        if await _asyncio.to_thread(get_live_gpu_free_mib) is None:
            _logger.info(
                "[VRAM-RECLAIM] no live VRAM source — skipping wait (target: %d MiB)",
                target_mib,
            )
            return True
    except Exception:
        pass
    start = _time.time()
    _first_free: float | None = None
    _best_free: float | None = None
    while _time.time() - start < timeout_sec:
        try:
            free_mib = await _asyncio.to_thread(get_live_gpu_free_mib)
            if _first_free is None and free_mib is not None:
                _first_free = free_mib
                _best_free = free_mib
            elif free_mib is not None and _best_free is not None:
                _best_free = max(_best_free, free_mib)
            if free_mib is not None and free_mib >= target_mib:
                _logger.info(
                    "[VRAM-RECLAIM] %d MiB frei nach %.1fs — OK (target: %d)",
                    free_mib, _time.time() - start, target_mib,
                )
                return True
            # STALL-ABORT (2026-09-07): if free VRAM does not measurably rise over
            # ~stall_abort_s (no driver release in progress), the target is
            # structurally unreachable — abort early instead of waiting 45s
            # (e.g. external ~3.1 GB blocks hermes@256 even after eviction).
            if (stall_abort_s > 0.0 and _first_free is not None
                    and _best_free is not None
                    and _time.time() - start >= stall_abort_s
                    and _best_free < _first_free + 64):
                _logger.warning(
                    "[VRAM-RECLAIM] abort after %.0fs (no release progress: "
                    "free=%dMiB, target=%d, initially=%dMiB)",
                    stall_abort_s, int(_best_free), target_mib, int(_first_free),
                )
                return False
        except Exception as e:
            _logger.warning("[VRAM-RECLAIM] measurement failed: %s", e)
            return False
        await _asyncio.sleep(poll_interval)
    try:
        free_mib = await _asyncio.to_thread(get_live_gpu_free_mib)
    except Exception:
        free_mib = -1
    _logger.warning(
        "[VRAM-RECLAIM] timeout after %ds — only %d MiB free (target: %d, initially %s) — "
        "driver release can take 30-40s (AMD/Vulkan)",
        timeout_sec, free_mib, target_mib,
        f"{_first_free:.0f} MiB" if _first_free is not None else "unknown",
    )
    return False


_MMPROJ_COMPUTE_MULT = 1.15


def mmproj_surcharge_mib(mmproj_path: object) -> float:
    """Estimated VRAM footprint (MiB) for a vision projector plus CLIP compute buffer."""
    if not mmproj_path:
        return 0.0
    import os as _os
    p = (_os.fspath(mmproj_path)
         if isinstance(mmproj_path, (str, _os.PathLike))
         else str(mmproj_path))
    if isinstance(p, bytes):
        p = _os.fsdecode(p)
    try:
        if not p or not _os.path.isfile(p):
            return 0.0
        size_mib = _os.path.getsize(p) / (1024.0 * 1024.0)
        return round(size_mib * _MMPROJ_COMPUTE_MULT, 1) if size_mib > 0.0 else 0.0
    except OSError:
        return 0.0


def slot_mmproj_vram_mib(model_name: str, is_vision: bool) -> float:
    """Return estimated mmproj VRAM (MiB) for an active vision slot/peer."""
    if not is_vision or not model_name:
        return 0.0
    try:
        from .llama_models import resolve_mmproj_path as _rmp
        return mmproj_surcharge_mib(_rmp(model_name))
    except (ImportError, OSError, RuntimeError, ValueError, TypeError, AttributeError):
        return 0.0


def slot_effective_vram_gb(slot: object, *, vram_fn=None) -> float:
    """Return effective slot VRAM (GB), honoring runtime all-GPU MoE lifts."""
    model = getattr(slot, "model", None)
    if not model:
        return 0.0
    ctx = int(getattr(slot, "_num_ctx", 0) or 0)
    if ctx <= 0:
        ctx = 4096
    key = re.sub(r"#\d+$", "", str(model)).removesuffix(":latest")
    est = float(vram_fn(model, ctx)) if vram_fn is not None else float(vram_of_moe(key, ctx))
    lifted = float(getattr(slot, "_moe_all_gpu_vram_gb", 0.0) or 0.0)
    return max(est, lifted)
