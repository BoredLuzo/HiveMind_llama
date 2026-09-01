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

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("hivemind.models_registry")

_THIS_DIR = Path(__file__).resolve().parent
_MODELS_DIR = _THIS_DIR / "models"

# Filename encoding: ':' is forbidden in Windows filenames. Canonical
# names are stored as  <base>_<tag>.json  (":" → "_"). An optional
# "model" field in the JSON overrides the name derived from the filename.
_UNDERSCORE_PLACEHOLDER = ":"

# ── Cache ──────────────────────────────────────────────────────────────────────
_profile_cache: dict[str, dict] = {}     # canonical/base → merged config
_profile_mtimes: dict[str, float] = {}
_cache_ts: float = 0.0
_CACHE_TTL: float = 30.0

_FILE_CACHE: dict[str, dict] = {}        # filename → raw config
_FILE_MTIMES: dict[str, float] = {}

# Canonical mapping: filename stem → (canonical name, base name)
_FILE_KEYS: dict[str, tuple[str, str]] = {}


def _load_file(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("models_registry: config %s not readable: %s", path.name, e)
        return {}


def _scan() -> dict[str, dict]:
    """Lade alle JSON-Dateien aus model_configs/models/ (mtime-cached)."""
    global _FILE_CACHE, _FILE_MTIMES, _FILE_KEYS
    if not _MODELS_DIR.is_dir():
        return {}

    out: dict[str, dict] = {}
    _new_keys: dict[str, tuple[str, str]] = {}

    for p in sorted(_MODELS_DIR.glob("*.json")):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if _FILE_CACHE.get(p.name) is not None and _FILE_MTIMES.get(p.name) == mtime:
            out[p.name] = _FILE_CACHE[p.name]
            _new_keys[p.name] = _FILE_KEYS[p.name]
            continue

        data = _load_file(p)
        if not data:
            continue
        _FILE_CACHE[p.name] = data
        _FILE_MTIMES[p.name] = mtime
        out[p.name] = data
        # Canonical name: explicit "model" field > filename ("_" = ":").
        _canonical = str(data.get("model") or "").strip()
        if not _canonical:
            _canonical = p.stem.replace("_", _UNDERSCORE_PLACEHOLDER)
        _new_keys[p.name] = (_canonical, _canonical.split(":")[0])

    # Remove orphaned cache entries (deleted files)
    for old in list(_FILE_CACHE):
        if old not in out:
            _FILE_CACHE.pop(old, None)
            _FILE_MTIMES.pop(old, None)
            _FILE_KEYS.pop(old, None)

    _FILE_KEYS = _new_keys
    return out


def refresh() -> None:
    """Discard the cache — the next access rescans."""
    global _profile_cache, _cache_ts
    _profile_cache = {}
    _cache_ts = 0.0


def _ensure_cache() -> dict[str, dict]:
    global _profile_cache, _cache_ts
    now = time.time()
    if not _profile_cache or (now - _cache_ts) > _CACHE_TTL:
        _files = _scan()
        tmp: dict[str, dict] = {}
        for fname, data in _files.items():
            canonical, base = _FILE_KEYS.get(fname, (fname, fname.split(":")[0]))
            tmp[canonical] = data
            tmp.setdefault(base, {})
        _profile_cache = tmp
        _cache_ts = now
    return _profile_cache


def _merge(base: dict, over: dict) -> dict:
    """Shallow merge with dict deep-copy for 'capabilities'."""
    result = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            merged = dict(result[k])
            merged.update(v)
            result[k] = merged
        else:
            result[k] = v
    return result


def get_profile(model_name: str) -> dict:
    """Returns the merged config for a model (canonical → base → {})."""
    model = (model_name or "").strip()
    if not model:
        return {}
    canonical = model.replace("#", "")
    base = canonical.split(":")[0] if ":" in canonical else canonical

    _reg = _ensure_cache()
    direct = _reg.get(canonical)
    fallback = _reg.get(base)

    if direct and fallback is not None and direct is not fallback:
        return _merge(fallback, direct)
    if direct is not None:
        return dict(direct)
    if fallback is not None:
        return dict(fallback)
    return {}


# ── Accessors ──────────────────────────────────────────────────────────────────

def get_capabilities(model_name: str) -> dict | None:
    caps = get_profile(model_name).get("capabilities")
    if isinstance(caps, dict):
        return {
            "thinking": bool(caps.get("thinking")),
            "vision": bool(caps.get("vision")),
            "tool_call": bool(caps.get("tool_call")),
        }
    return None


def is_vision_preprocessing(model_name: str) -> bool | None:
    v = get_profile(model_name).get("vision_preprocessing")
    return bool(v) if v is not None else None


def get_num_ctx(model_name: str, agent_role: str | None = None) -> int | None:
    prof = get_profile(model_name)
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
    v = get_profile(model_name).get("chat_template")
    return str(v).strip() if v else None


def is_jinja(model_name: str) -> bool | None:
    v = get_profile(model_name).get("jinja")
    return bool(v) if v is not None else None


def get_reasoning(model_name: str) -> str | None:
    v = get_profile(model_name).get("reasoning")
    if v in ("on", "off"):
        return v
    return None


def is_distilled(model_name: str) -> bool | None:
    v = get_profile(model_name).get("distilled")
    return bool(v) if v is not None else None


def get_moe_cpu_experts(model_name: str) -> int | None:
    v = get_profile(model_name).get("moe_cpu_experts")
    if v:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


def is_mtp(model_name: str) -> bool | None:
    v = get_profile(model_name).get("mtp")
    return bool(v) if v is not None else None


def get_gpu_layers(model_name: str) -> int | None:
    v = get_profile(model_name).get("gpu_layers")
    if v:
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


def get_mmproj_filename(model_name: str) -> str | None:
    v = get_profile(model_name).get("mmproj_filename")
    return str(v).strip() if v else None


def get_vram_gb_override(model_name: str) -> float | None:
    v = get_profile(model_name).get("vram_gb_override")
    if v:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


def save_profile(model_name: str, data: dict) -> Path:
    """Schreibt eine Config-Datei nach model_configs/models/<name>.json."""
    _MODELS_DIR.mkdir(parents=True, exist_ok=True)
    safe = (model_name or "model").replace("/", "_").replace("\\", "_")
    safe = "".join(c if c not in '<>:"|?*' else "_" for c in safe)
    path = _MODELS_DIR / f"{safe}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    refresh()
    return path
