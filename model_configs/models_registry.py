"""
models_registry.py - per-model config files for HiveMind
============================================================
Loads custom model configurations from
`model_configs/models/<canonical-or-base>.json` and merges them over
the hardcoded defaults in the code tables.

One config file per model (or per base like "qwen3.5") makes it possible
to add fully configured models without Python edits:

    model_configs/models/my-model:7b.json
    {
      "capabilities": {"thinking": true, "vision": false, "tool_call": true},
      "vision_preprocessing": false,
      "num_ctx": 8192,
      "num_ctx_analyst": null,
      "num_ctx_duo_coder": null,
      "num_ctx_vision": null,
      "chat_template": null,
      "jinja": false,
      "reasoning": null,
      "distilled": false,
      "moe_cpu_experts": 0,
      "mtp": false,
      "gpu_layers": null,
      "mmproj_filename": null,
      "vram_gb_override": null
    }

Lookup order (get_profile):
  1. Exact canonical name   ("qwen3.5:9b-ud")
  2. Base name              ("qwen3.5")
  3. Empty defaults → code fallbacks apply unchanged.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import time
from pathlib import Path

logger = logging.getLogger("hivemind.models_registry")

_THIS_DIR = Path(__file__).resolve().parent
_MODELS_DIR = _THIS_DIR / "models"
USER_DIR = _THIS_DIR / "user"
_ARCH_COMPAT_FILE = _THIS_DIR / "arch_compat.json"

# Filename encoding: ':' is forbidden in Windows filenames. Canonical
# names are stored as  <base>_<tag>.json  (":" → "_"). An optional
# "model" field in the JSON overrides the name derived from the filename.
_UNDERSCORE_PLACEHOLDER = ":"

# ── Cache ──────────────────────────────────────────────────────────────────────
_profile_cache: dict[str, dict] = {}     # canonical/base → merged config
_shipped_cache: dict[str, dict] = {}     # canonical/base → shipped config
_user_cache: dict[str, dict] = {}        # canonical/base → user override config
_resolved_profile_cache: dict[str, dict] = {}
_resolved_shipped_ref: dict | None = None
_resolved_user_ref: dict | None = None
_profile_mtimes: dict[str, float] = {}
_cache_ts: float = 0.0
_CACHE_TTL: float = 30.0

_FILE_CACHE: dict[str, dict] = {}        # cache key → raw config
_FILE_MTIMES: dict[str, float] = {}

# Canonical mapping: cache key → (canonical name, base name)
_FILE_KEYS: dict[str, tuple[str, str]] = {}


def _canonical_from_file(path: Path, data: dict) -> str:
    """Canonical model name: explicit 'model' field > filename stem (last '_' -> ':')."""
    explicit = str((data or {}).get("model") or "").strip()
    if explicit:
        return explicit
    stem = path.stem
    if "_" in stem:
        base, tag = stem.rsplit("_", 1)
        return f"{base}{_UNDERSCORE_PLACEHOLDER}{tag}"
    return stem


def _load_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("models_registry: config %s not readable: %s", path.name, e)
        return {}


def _scan_dir(directory: Path, layer: str = "shipped") -> dict[str, dict]:
    """Load all JSON files from `directory` (mtime-cached per layer)."""
    if not directory or not directory.is_dir():
        prefix = f"{layer}:" if layer else ""
        for old in [k for k in _FILE_CACHE if (not prefix or k.startswith(prefix))]:
            _FILE_CACHE.pop(old, None)
            _FILE_MTIMES.pop(old, None)
            _FILE_KEYS.pop(old, None)
        return {}

    prefix = f"{layer}:" if layer else ""
    out: dict[str, dict] = {}

    for p in sorted(directory.glob("*.json")):
        ckey = f"{prefix}{p.name}"
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if _FILE_CACHE.get(ckey) is not None and _FILE_MTIMES.get(ckey) == mtime:
            out[ckey] = _FILE_CACHE[ckey]
            continue

        data = _load_file(p)
        if not isinstance(data, dict) or not data:
            continue
        _FILE_CACHE[ckey] = data
        _FILE_MTIMES[ckey] = mtime
        out[ckey] = data
        canonical = _canonical_from_file(p, data)
        _FILE_KEYS[ckey] = (canonical, canonical.split(":")[0])

    # Remove orphaned cache entries (deleted files in this layer)
    for old in list(_FILE_CACHE):
        if (not prefix or old.startswith(prefix)) and old not in out:
            _FILE_CACHE.pop(old, None)
            _FILE_MTIMES.pop(old, None)
            _FILE_KEYS.pop(old, None)

    return out


def _scan() -> dict[str, dict]:
    """Load all shipped JSON files from model_configs/models/ (mtime-cached)."""
    return _scan_dir(_MODELS_DIR, "")


def refresh() -> None:
    """Discard the cache — the next access rescans both shipped and user dirs."""
    global _profile_cache, _shipped_cache, _user_cache, _resolved_shipped_ref, _resolved_user_ref, _cache_ts
    _profile_cache = {}
    _shipped_cache = {}
    _user_cache = {}
    _resolved_profile_cache.clear()
    _resolved_shipped_ref = None
    _resolved_user_ref = None
    _profile_mtimes.clear()
    _FILE_CACHE.clear()
    _FILE_MTIMES.clear()
    _FILE_KEYS.clear()
    _cache_ts = 0.0


def _ensure_cache() -> dict[str, dict]:
    global _profile_cache, _shipped_cache, _user_cache, _resolved_shipped_ref, _resolved_user_ref, _cache_ts
    now = time.time()
    if not _profile_cache or (now - _cache_ts) > _CACHE_TTL:
        shipped_files = _scan_dir(_MODELS_DIR, "shipped")
        user_files = _scan_dir(USER_DIR, "user")

        shipped_tmp: dict[str, dict] = {}
        for ckey, data in shipped_files.items():
            canonical, base = _FILE_KEYS.get(ckey, (ckey, ckey.split(":")[0]))
            shipped_tmp[canonical] = data
            shipped_tmp.setdefault(base, {})

        user_tmp: dict[str, dict] = {}
        for ckey, data in user_files.items():
            canonical, base = _FILE_KEYS.get(ckey, (ckey, ckey.split(":")[0]))
            user_tmp[canonical] = data
            user_tmp.setdefault(base, {})

        merged_tmp: dict[str, dict] = dict(shipped_tmp)
        for k, uval in user_tmp.items():
            existing = merged_tmp.get(k)
            if isinstance(existing, dict) and existing and isinstance(uval, dict) and uval:
                merged_tmp[k] = _merge(existing, uval)
            elif uval or k not in merged_tmp:
                merged_tmp[k] = uval

        _shipped_cache = shipped_tmp
        _user_cache = user_tmp
        _profile_cache = merged_tmp
        _resolved_profile_cache.clear()
        _resolved_shipped_ref = _shipped_cache
        _resolved_user_ref = _user_cache
        _cache_ts = now
    return _profile_cache


def _merge(base: dict, over: dict) -> dict:
    """Recursive dict merge (preserves nested 'sampling' / 'capabilities' keys; None inherits base)."""
    result = dict(base)
    for k, v in over.items():
        if v is None:
            continue
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _merge(result[k], v)
        else:
            result[k] = v
    return result


def _registry_key(reg: dict[str, dict], key: str) -> str | None:
    """Exact first, then a case-insensitive lookup.

    GGUF/model keys have no case-significant distinction, but role configs
    and UI callers hand over mixed casing ("Qwen3.6:35B..."). Returning the
    stored key keeps the per-file base/canonical merge intact.
    """
    if key in reg:
        return key
    low = key.casefold()
    for stored in reg:
        if stored.casefold() == low:
            return stored
    return None


# Trailing quant / variant tokens parsed out of GGUF filenames when a suffix
# (e.g. "-verbose-heretic") follows the quant tag; arbitrary suffixes such as
# "caps-probe:35b-a3b-x" must NOT match the shorter tag.
_QUANT_VARIANT_SEGMENT_RE = re.compile(
    r"^(?:"
    r"i?q\d+(?:_(?:[kmsl01]|xs|xl|xxl|nl|xxs|im))*|mxfp\d+|ptq\d+(?:_\d+)?|bf16|fp?16|fp?32|"
    r"ud|qat|qad|im|imatrix|moe|gsq|rco|\d+gb|"
    r"[kmsl]|xs|xl|xxl|nl|[01]|"
    r"verbose|balanced|heretic|abliterated|obliterated|uncensored"
    r")$",
    re.IGNORECASE,
)


def _walkback_variant_profile(reg: dict[str, dict], canonical: str):
    """Walk back trailing quant/variant tokens to match a catalog profile."""
    if ":" in canonical:
        fam, _, tag = canonical.partition(":")
        parts = tag.split("-")
        for end in range(len(parts) - 1, 0, -1):
            if not _QUANT_VARIANT_SEGMENT_RE.match(parts[end]):
                break
            cand_key = _registry_key(reg, f"{fam}:{'-'.join(parts[:end])}")
            if cand_key is not None:
                return reg.get(cand_key)
        return None
    parts = canonical.split("-")
    for end in range(len(parts) - 1, 0, -1):
        if not _QUANT_VARIANT_SEGMENT_RE.match(parts[end]):
            break
        cand_key = _registry_key(reg, "-".join(parts[:end]))
        if cand_key is not None:
            return reg.get(cand_key)
    return None


def _normalize_model_key(model_name: str) -> tuple[str, str]:
    """Return normalized (canonical, base) pair with #N and :latest stripped."""
    model = (model_name or "").strip()
    if not model:
        return "", ""
    canonical = re.sub(r"#\d+$", "", model)
    canonical = re.sub(r":latest$", "", canonical, flags=re.IGNORECASE)
    base = canonical.split(":")[0] if ":" in canonical else canonical
    return canonical, base


def _resolve_layer(reg: dict[str, dict], canonical: str, base: str) -> tuple[dict, bool]:
    """Resolve (merged_profile, matched) within a single registry layer dict."""
    if not reg:
        return {}, False

    def _resolve_pair(canon_name: str, base_name: str):
        ck = _registry_key(reg, canon_name)
        bk = _registry_key(reg, base_name)
        d = reg.get(ck) if ck is not None else None
        if d is None:
            d = _walkback_variant_profile(reg, canon_name)
        f = reg.get(bk) if bk is not None else None
        return d, f

    direct, fallback = _resolve_pair(canonical, base)
    if direct is None and not fallback and base.lower().startswith("nemotron-"):
        direct, fallback = _resolve_pair(f"nvidia-{canonical}", f"nvidia-{base}")
    if direct is None and not fallback and base.lower().startswith("sharp-minicpm"):
        _norm_sc = canonical.lower().replace("sharp-minicpm", "minicpm")
        if not _norm_sc.endswith("-sharp"):
            _norm_sc += "-sharp"
        _norm_sb = _norm_sc.split(":")[0]
        direct, fallback = _resolve_pair(_norm_sc, _norm_sb)

    if direct and fallback is not None and direct is not fallback:
        return _merge(fallback, direct), True
    if direct is not None:
        return (direct, bool(direct))
    if fallback is not None and fallback:
        return fallback, True
    return {}, False


def _get_profile_readonly(model_name: str) -> dict:
    """Return cached merged profile dict for read-only internal accessors."""
    global _resolved_shipped_ref, _resolved_user_ref
    canonical, base = _normalize_model_key(model_name)
    if not canonical:
        return {}

    _ensure_cache()
    if _shipped_cache is not _resolved_shipped_ref or _user_cache is not _resolved_user_ref:
        _resolved_profile_cache.clear()
        _resolved_shipped_ref = _shipped_cache
        _resolved_user_ref = _user_cache

    ckey = canonical.casefold()
    cached = _resolved_profile_cache.get(ckey)
    if cached is not None:
        return cached

    shipped_prof, has_shipped = _resolve_layer(_shipped_cache, canonical, base)
    user_prof, has_user = _resolve_layer(_user_cache, canonical, base)

    if has_user and has_shipped:
        merged = _merge(shipped_prof, user_prof)
    elif has_user:
        merged = user_prof
    elif has_shipped:
        merged = shipped_prof
    else:
        merged = {}

    _resolved_profile_cache[ckey] = merged
    return merged


def get_profile(model_name: str) -> dict:
    """Return merged config for a model (user > shipped > base > {})."""
    prof = _get_profile_readonly(model_name)
    return copy.deepcopy(prof) if prof else {}


_ORIG_GET_PROFILE = get_profile


def _read_profile(model_name: str) -> dict:
    """Return cached read-only view, or delegate to `get_profile` if monkeypatched."""
    if get_profile is not _ORIG_GET_PROFILE:
        return get_profile(model_name) or {}
    return _get_profile_readonly(model_name)


def has_user_profile_key(model_name: str, key: str) -> bool:
    """Return True iff the user override layer explicitly sets `key` (non-None)."""
    canonical, base = _normalize_model_key(model_name)
    if not canonical or not key:
        return False
    _ensure_cache()
    user_prof, has_user = _resolve_layer(_user_cache, canonical, base)
    return bool(has_user and key in user_prof and user_prof[key] is not None)


def get_profile_source(model_name: str) -> str:
    """Return profile resolution source: 'user', 'shipped', 'gguf_auto', or 'generic'."""
    canonical, base = _normalize_model_key(model_name)
    if not canonical:
        return "generic"

    _ensure_cache()
    _, has_user = _resolve_layer(_user_cache, canonical, base)
    if has_user:
        return "user"
    _, has_shipped = _resolve_layer(_shipped_cache, canonical, base)
    if has_shipped:
        return "shipped"

    try:
        from backend.gguf_meta import read_gguf_metadata as _read_gguf_meta
        meta = _read_gguf_meta(model_name)
        if meta:
            return "gguf_auto"
    except (ImportError, OSError, ValueError, TypeError, AttributeError) as _e:
        logger.debug("models_registry.get_profile_source gguf_meta skipped for %s: %s", model_name, _e)
    return "generic"


def iter_all_profiles() -> dict[str, dict]:
    """Return a deep copy of all active non-empty profiles (user overriding shipped)."""
    reg = _ensure_cache()
    return {
        k: copy.deepcopy(v)
        for k, v in reg.items()
        if isinstance(v, dict) and v
    }


def load_arch_compat() -> dict:
    """Load architecture compatibility defaults from model_configs/arch_compat.json."""
    if not _ARCH_COMPAT_FILE.is_file():
        return {}
    try:
        data = json.loads(_ARCH_COMPAT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError, ValueError) as _e:
        logger.debug("models_registry: failed reading arch_compat.json: %s", _e)
        return {}


# ── Accessors ──────────────────────────────────────────────────────────────────

def get_capabilities(model_name: str) -> dict | None:
    caps = _read_profile(model_name).get("capabilities")
    if isinstance(caps, dict):
        return {
            "thinking": bool(caps.get("thinking")),
            "vision": bool(caps.get("vision")),
            "tool_call": bool(caps.get("tool_call")),
        }
    return None


def is_vision_preprocessing(model_name: str) -> bool | None:
    v = _read_profile(model_name).get("vision_preprocessing")
    return bool(v) if v is not None else None


def get_num_ctx(model_name: str, agent_role: str | None = None) -> int | None:
    prof = _read_profile(model_name)
    if agent_role == "analyst":
        v = prof.get("num_ctx_analyst")
        if v:
            return int(v)
    if agent_role == "duo_coder":
        v = prof.get("num_ctx_duo_coder")
        if v:
            return int(v)
    if agent_role == "vision":
        v = prof.get("num_ctx_vision")
        if v:
            return int(v)
    v = prof.get("num_ctx")
    if v:
        return int(v)
    return None


def get_chat_template(model_name: str) -> str | None:
    v = _read_profile(model_name).get("chat_template")
    return str(v).strip() if v else None


def is_jinja(model_name: str) -> bool | None:
    v = _read_profile(model_name).get("jinja")
    return bool(v) if v is not None else None


def get_reasoning(model_name: str) -> str | None:
    v = _read_profile(model_name).get("reasoning")
    if v in ("on", "off"):
        return v
    return None


def is_distilled(model_name: str) -> bool | None:
    v = _read_profile(model_name).get("distilled")
    return bool(v) if v is not None else None


def get_moe_cpu_experts(model_name: str) -> int | None:
    prof = _read_profile(model_name)
    v = prof.get("moe_cpu_experts")
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    # Dense model configs (e.g. qwen3.5:4b-mtp) ship moe_cpu_experts=0; honor 0
    # as an active override only when the profile explicitly sets moe=True.
    if n == 0 and prof.get("moe") is not True:
        return None
    return n


def is_mtp(model_name: str) -> bool | None:
    v = _read_profile(model_name).get("mtp")
    return bool(v) if v is not None else None


def _get_str_key(model_name: str, key: str) -> str | None:
    v = _read_profile(model_name).get(key)
    return str(v).strip() if v else None


def _get_pos_int_key(model_name: str, key: str) -> int | None:
    v = _read_profile(model_name).get(key)
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def get_dspark_draft(model_name: str) -> str | None:
    return _get_str_key(model_name, "dspark_draft_filename")


def get_dspark_draft_n_max(model_name: str) -> int | None:
    """Per-model draft budget for DSpark. None = use the engine default."""
    return _get_pos_int_key(model_name, "dspark_draft_n_max")


def get_dflash_draft(model_name: str) -> str | None:
    return _get_str_key(model_name, "dflash_draft_filename")


def get_dflash_draft_alternates(model_name: str) -> list[str]:
    """Optional fallback filenames when multiple quantised DFlash drafters exist."""
    v = _read_profile(model_name).get("dflash_draft_alternates")
    if isinstance(v, list):
        return [str(tok).strip() for tok in v if str(tok).strip()]
    return []


def get_mtp_draft(model_name: str) -> str | None:
    """Optional external MTP predictor sidecar filename (e.g. mtp-gemma-4-E4B-it.gguf)."""
    return _get_str_key(model_name, "mtp_draft_filename")


def get_flash_attn(model_name: str) -> bool | None:
    """Optional per-model --flash-attn override (True='on', False='off', None=default on)."""
    v = _read_profile(model_name).get("flash_attn")
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        low = v.strip().lower()
        if low in ("on", "true", "1", "yes"):
            return True
        if low in ("off", "false", "0", "no"):
            return False
    return None


def get_dflash_draft_n_max(model_name: str) -> int | None:
    """Per-model draft budget for DFlash. None = use the engine default."""
    return _get_pos_int_key(model_name, "dflash_draft_n_max")


def get_dflash_draft_p_min(model_name: str) -> float | None:
    """Per-model minimum draft probability for DFlash. None = use engine default."""
    v = _read_profile(model_name).get("dflash_draft_p_min")
    if v is None:
        return None
    try:
        p = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(0.95, p))


def get_draft_overhead_mib(model_name: str) -> float | None:
    """Calibrated external drafter VRAM overhead (MiB) for models without vram_gb_override."""
    v = _read_profile(model_name).get("draft_overhead_mib")
    if v is None:
        return None
    try:
        mib = float(v)
    except (TypeError, ValueError):
        return None
    return min(16384.0, mib) if mib > 0.0 else None


def get_cache_ram_mib(model_name: str) -> int | None:
    """Optional per-model --cache-ram MiB override (e.g. 2048 for high-ctx MoEs)."""
    v = _get_pos_int_key(model_name, "cache_ram_mib")
    return min(32768, v) if v is not None else None


def get_moe_cache_mib(model_name: str) -> int | None:
    """Optional per-model --moe-cache-mib override (0 = explicitly disabled)."""
    v = _read_profile(model_name).get("moe_cache_mib")
    if v is None or isinstance(v, bool):
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return max(0, min(16384, n))


def get_moe_cache_cpu_share(model_name: str) -> float | None:
    """Optional per-model --moe-cache-cpu-share override in [0.0, 1.0]."""
    v = _read_profile(model_name).get("moe_cache_cpu_share")
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, f))


def get_lazy_mode(model_name: str) -> str | None:
    v = (_get_str_key(model_name, "lazy_mode") or "").lower()
    return v if v in ("on", "off", "auto") else None


def get_moe_pin(model_name: str) -> bool | None:
    """MOE-PIN (2026-10-14): explicit "moe": true/false from the model's own
    profile — the user's answer to "is this a MoE?" — or None when unset
    (name heuristics decide, e.g. the 30B+ autodetect)."""
    v = _read_profile(model_name).get("moe")
    return bool(v) if v is not None else None


def _find_in_dir(directory: Path, model_name: str) -> Path | None:
    """Locate the JSON profile file matching `model_name` inside `directory`."""
    want = (model_name or "").strip().casefold()
    if not want or not directory or not directory.is_dir():
        return None
    for p in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(raw, dict) and _canonical_from_file(p, raw).casefold() == want:
            return p
    return None


def _atomic_write_json(path: Path, data: dict) -> None:
    """Atomically write JSON dict to `path` via a unique sibling temp file."""
    tmp = path.parent / f".{path.name}.tmp.{os.getpid()}"
    try:
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError as _cleanup_err:
                logger.debug("models_registry: failed cleaning temp file %s: %s", tmp, _cleanup_err)
        raise


_SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_user_slug(model_name: str) -> tuple[str, str]:
    """Validate `model_name` for safe user-profile storage and return (clean_name, slug)."""
    clean = (model_name or "").strip()
    if not clean:
        raise ValueError("model_name must not be empty")
    if "/" in clean or "\\" in clean or ".." in clean or clean.startswith("."):
        raise ValueError(f"Unsafe model_name for user profile: {model_name!r}")
    if any(ord(c) < 32 or c in '<>"|?*' for c in clean):
        raise ValueError(f"Invalid control/filename characters in model_name: {model_name!r}")
    slug = clean.replace(":", "_")
    if ".." in slug or not _SAFE_SLUG_RE.fullmatch(slug):
        raise ValueError(f"Invalid slug derived from model_name: {model_name!r}")
    return clean, slug


def set_profile_key(model_name: str, key: str, value) -> Path | None:
    """Patch a single key in the model's OWN profile file (not the merged
    view). value=None removes the key. Searches USER_DIR first, then _MODELS_DIR.
    Returns the file path, or None when no profile file exists for this exact model name."""
    want = (model_name or "").strip()
    if not want:
        return None
    p = _find_in_dir(USER_DIR, want) or _find_in_dir(_MODELS_DIR, want)
    if p is None:
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    if value is None:
        raw.pop(key, None)
    else:
        raw[key] = value
    _atomic_write_json(p, raw)
    refresh()
    return p


def get_gpu_layers(model_name: str) -> int | None:
    v = _read_profile(model_name).get("gpu_layers")
    if v is not None:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


def get_kv_cache_type(model_name: str) -> str | None:
    v = _read_profile(model_name).get("kv_cache_type")
    s = str(v).strip().lower() if v else ""
    return s or None


def get_extra_args(model_name: str) -> list[str]:
    v = _read_profile(model_name).get("extra_args")
    if isinstance(v, str):
        return [tok for tok in v.split() if tok]
    if isinstance(v, list):
        return [str(tok).strip() for tok in v if str(tok).strip()]
    return []


def get_ubatch(model_name: str) -> int | None:
    """Per-model prompt-processing chunk ("ubatch", llama.cpp --ubatch-size).

    MoE models with CPU expert offload prefill substantially faster at
    512/1024 than at the shipped 256 baseline (measured on the 35B-A3B:
    77.6 -> 120.0 t/s pp8192), at the cost of a larger VRAM compute buffer
    (~0.28 MiB per ubatch unit above 256, see llama_config.ubatch_extra_mib).
    The profile key is the per-model opt-in; the loader applies it only when
    the VRAM pre-flight still fits (see llama_config.resolve_model_ubatch).
    None = no profile value (keep the settings baseline). Clamped to the
    loader's [64, 4096] band; invalid values are ignored (None), never an
    abort.
    """
    v = _read_profile(model_name).get("ubatch")
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    if n < 64 or n > 4096:
        return None
    return n


def get_mmproj_filename(model_name: str) -> str | None:
    v = _read_profile(model_name).get("mmproj_filename")
    return str(v).strip() if v else None


def get_vram_gb_override(model_name: str) -> float | None:
    v = _read_profile(model_name).get("vram_gb_override")
    if v:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


def get_sampling(model_name: str) -> dict:
    """Per-model sampling overrides from the config file (mode key such as
    "thinking" / "non_thinking" -> llama.cpp sampling dict)."""
    v = _read_profile(model_name).get("sampling")
    return copy.deepcopy(v) if isinstance(v, dict) and v else {}


def get_load_mode(model_name: str) -> str | None:
    """Per-model load_mode override ("none" | "mmap" | "mmap+mlock"). None = auto."""
    v = _read_profile(model_name).get("load_mode")
    return str(v).strip().lower() if v else None


def get_thinking_budget(model_name: str) -> int | None:
    """Per-model thinking budget (from profile top-level or sampling sub-block)."""
    prof = _read_profile(model_name)
    v = prof.get("thinking_budget")
    if v is not None:
        try:
            return int(v)
        except (ValueError, TypeError):
            pass
    samp = prof.get("sampling", {})
    if isinstance(samp, dict):
        for block_name in ("thinking", "sampling_thinking_text", "sampling_thinking_code"):
            blk = samp.get(block_name)
            if isinstance(blk, dict) and blk.get("thinking_budget") is not None:
                try:
                    return int(blk["thinking_budget"])
                except (ValueError, TypeError):
                    pass
    return None


def find_profile_file(model_name: str) -> Path | None:
    """Find profile JSON file for `model_name`, checking USER_DIR before _MODELS_DIR."""
    return _find_in_dir(USER_DIR, model_name) or _find_in_dir(_MODELS_DIR, model_name)


def save_profile(model_name: str, data: dict) -> Path:
    """Write a shipped/local profile file to model_configs/models/<name>.json."""
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    safe = (model_name or "model").replace("/", "_").replace("\\", "_")
    safe = "".join(c if c not in '<>:"|?*' else "_" for c in safe)
    path = _find_in_dir(_MODELS_DIR, model_name) or (_MODELS_DIR / f"{safe}.json")
    _atomic_write_json(path, data)
    refresh()
    _ensure_cache()
    return path


def save_user_profile(model_name: str, profile_dict: dict) -> Path:
    """Validate slug, atomically write user override to USER_DIR, and refresh index."""
    if not isinstance(profile_dict, dict):
        raise TypeError("profile_dict must be a dict")
    clean, slug = _validate_user_slug(model_name)
    USER_DIR.mkdir(parents=True, exist_ok=True)

    existing = _find_in_dir(USER_DIR, clean)
    target = existing if existing is not None else (USER_DIR / f"{slug}.json")
    resolved_target = target.resolve()
    resolved_target.relative_to(USER_DIR.resolve())

    payload = copy.deepcopy(profile_dict)
    if not payload.get("model"):
        payload["model"] = clean

    _atomic_write_json(target, payload)
    refresh()
    _ensure_cache()
    return target


def delete_user_profile(model_name: str) -> bool:
    """Remove a user override profile from USER_DIR and refresh index."""
    clean = (model_name or "").strip()
    if not clean or not USER_DIR.is_dir():
        return False

    target = _find_in_dir(USER_DIR, clean)
    if target is None:
        try:
            _, slug = _validate_user_slug(clean)
        except ValueError:
            return False
        candidate = USER_DIR / f"{slug}.json"
        if candidate.is_file():
            target = candidate

    if target is None or not target.is_file():
        return False

    try:
        target.resolve().relative_to(USER_DIR.resolve())
    except ValueError:
        return False

    target.unlink()
    refresh()
    _ensure_cache()
    return True


def upsert_profile(model_name: str, patch: dict) -> Path:
    """Create or deep-merge a per-model defaults JSON file atomically."""
    clean = (model_name or "").strip()
    existing_path = _find_in_dir(_MODELS_DIR, clean)
    if existing_path and existing_path.is_file():
        try:
            base = json.loads(existing_path.read_text(encoding="utf-8"))
            if not isinstance(base, dict):
                base = {}
        except (OSError, json.JSONDecodeError, ValueError):
            base = {}
    else:
        base = {"model": clean}
    merged = _merge(base, patch)
    if not merged.get("model"):
        merged["model"] = clean
    return save_profile(clean, merged)
