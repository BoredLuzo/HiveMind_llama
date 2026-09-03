


from __future__ import annotations


import json
import logging
import os
import re
import time as _time_module
from pathlib import Path

from .llama_config import MODELS_DIR

_logger = logging.getLogger("llama_models")

_THIS_DIR = Path(__file__).parent.parent
_MODELS_JSON = _THIS_DIR / "models.json"

# ── Cache ──────────────────────────────────────────────────────────────────────
_index_cache: dict[str, Path] = {}     # canonical_name → Path
_index_ts: float = 0.0
_INDEX_TTL: float = 30.0

_names_cache: list[str] = []
_names_cache_ts: float = 0.0
_NAMES_TTL: float = 60.0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_alias(model_name: str) -> str:
    """Removes slot aliases like '#2' from model names."""
    if not isinstance(model_name, str):
        return ""
    return re.sub(r"#\d+$", "", model_name.strip())


def _normalize(text: str) -> str:

    t = text.lower()
    t = t.replace(".", "-").replace("_", "-")
    t = re.sub(r"-+", "-", t)
    return t.strip("-")


def _canonicalize(text: str) -> str:


    t = text.lower()
    t = t.replace("_", "-")
    t = re.sub(r"-+", "-", t)
    t = t.lstrip(".-")
    return t


# ── Dateiname → kanonischer Modellname ─────────────────────────────────────────

# Erkennt: Q4_K_M, Q5_K_M, Q8_0, Q6_K.1, IQ1_S, IQ4_XS, F16, F32, BF16
_QUANT_PRE = re.compile(
    r'[-._](?:'
    r'[Qq][0-9]+[A-Za-z0-9_.]*'       # Q4_K_M, Q5_K_M, Q8_0, Q6_K.1
    r'|[Ii][Qq][0-9]+[A-Za-z0-9_.]*'   # IQ1_S, IQ4_XS
    r'|[Bb][Ff][0-9]+[A-Za-z0-9_.]*'   # BF16
    r'|[Ff][0-9]+[A-Za-z0-9_.]*'       # F16, F32
    r')$',
    re.IGNORECASE
)

_SIZE_PRE = re.compile(r'[-._](0?\d{1,2}(?:\.\d+)?[bB])')
_ESIZE_PRE = re.compile(r'[-._](E\d+B)', re.IGNORECASE)


def _parse_gguf_filename(filename: str) -> list[str]:


    stem = Path(filename).stem

    cleaned = _QUANT_PRE.sub('', stem)

    m_esize = _ESIZE_PRE.search(cleaned)
    m_size = _SIZE_PRE.search(cleaned)

    size_raw = ""   # z.B. "2B", "E4B", "1.5b"
    pre_part = ""
    post_part = ""

    if m_esize:
        size_raw = m_esize.group(1).lower()
        pre_part = cleaned[:m_esize.start()]
        post_part = cleaned[m_esize.end():]
    elif m_size:
        size_raw = m_size.group(1).lower()
        pre_part = cleaned[:m_size.start()]
        post_part = cleaned[m_size.end():]
    else:
        return [_canonicalize(cleaned)]

    size_canonical = re.sub(r'^0+(\d)', r'\1', size_raw)

    # 4. Base kanonisieren — Punkte bleiben erhalten!
    base = _canonicalize(pre_part)

    # 5. canonicalize and append the post part
    post_canon = _canonicalize(post_part) if post_part.strip("._-") else ""

    if not base:
        return []

    # Kanonische Namen generieren
    names = []
    if post_canon:
        names.append(f"{base}:{size_canonical}-{post_canon}")
        names.append(f"{base}:{size_canonical}")
    else:
        names.append(f"{base}:{size_canonical}")

    return names


# ── GGUF-Index ─────────────────────────────────────────────────────────────────

_NON_MODEL_PATTERNS = {"mmproj", "projector", "vision_encoder", "vl-encoder", "dspark"}

# ── Ollama-Familien-Exclusion ─────────────────────────────────────────────────
#
_OLLAMA_EXCLUDE_FAMILIES: set[str] = {
    "deepseek-r1",
    "glm-ocr",
    "lfm2.5-thinking",
    "olmo-3",
    "qwen2.5",
    "qwen3-vl",
    "gemma3",
    "granite3.2-vision",
    "ministral-3",
    "qwen3",
}

_FOLDER_FAMILY_OVERRIDES: dict[str, str] = {
        "omnicoder": "omnicoder",  # OmniCoder = Qwen3.5-based
}


def _apply_folder_override(canonical_names: list[str], gguf_path: Path) -> list[str]:


    try:
        relative = gguf_path.relative_to(MODELS_DIR)
        folder_parts = [p.lower() for p in relative.parts[:-1]]
    except (ValueError, IndexError):
        return canonical_names

    folder_base = None
    for part in reversed(folder_parts):
        if part in _FOLDER_FAMILY_OVERRIDES:
            folder_base = _FOLDER_FAMILY_OVERRIDES[part]
            break

    if folder_base is None:
        return canonical_names

    file_base = canonical_names[0].split(":")[0] if canonical_names else ""
    if file_base == folder_base:
        return canonical_names

    result = []
    for name in canonical_names:
        _, _, tag = name.partition(":")
        result.append(f"{folder_base}:{tag}" if tag else folder_base)

    _logger.debug(f"Folder override: {gguf_path.name} -> {file_base} -> {folder_base} ({result})")
    return result


def _build_index() -> dict[str, Path]:


    index: dict[str, Path] = {}

    if not MODELS_DIR.exists():
        _logger.warning(f"MODELS_DIR does not exist: {MODELS_DIR}")
        return index

    try:
        gguf_files = sorted(MODELS_DIR.rglob("*.gguf"))
    except Exception as e:
        _logger.error(f"Error scanning {MODELS_DIR}: {e}")
        return index

    if not gguf_files:
        _logger.info(f"No .gguf files in {MODELS_DIR}")
        return index

    for gguf in gguf_files:
        fname_lower = gguf.name.lower()
        if any(pat in fname_lower for pat in _NON_MODEL_PATTERNS):
            _logger.debug(f"Skipping non-model: {gguf.name}")
            continue

        canonical_names = _parse_gguf_filename(gguf.name)
        if not canonical_names:
            _logger.debug(f"Cannot parse filename: {gguf.name}")
            continue

        # Unterordner-Override anwenden (z.B. Qwen3.5/4B/Qwen3.5-4B-UD.Q6_K.gguf → qwen3.5:4b-ud)
        canonical_names = _apply_folder_override(canonical_names, gguf)

        for name in canonical_names:
            if name in index:
                try:
                    existing_size = index[name].stat().st_size
                    new_size = gguf.stat().st_size
                    if new_size < existing_size:
                        index[name] = gguf
                except Exception:
                    pass
            else:
                index[name] = gguf

    _logger.info(f"GGUF index: {len(index)} entries from {len(gguf_files)} files in {MODELS_DIR}")
    for name, path in sorted(index.items()):
        _logger.debug(f"  {name} → {path.name}")

    return index


def _get_index() -> dict[str, Path]:
    global _index_cache, _index_ts
    now = _time_module.time()
    if not _index_cache or (now - _index_ts) > _INDEX_TTL:
        _index_cache = _build_index()
        _index_ts = _time_module.time()
    return _index_cache


def refresh_index() -> None:
    global _index_cache, _index_ts
    _index_cache = _build_index()
    _index_ts = _time_module.time()


def _find_ollama_root() -> Path | None:
    env_root = os.environ.get("OLLAMA_MODELS", "").strip()
    if env_root:
        p = Path(env_root)
        if (p / "manifests").exists() and (p / "blobs").exists():
            return p
    candidates = [
        Path.home() / ".ollama" / "models",
        Path(os.environ.get("USERPROFILE", "")) / ".ollama" / "models",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "models",
        Path("/usr/share/ollama/.ollama/models"),
    ]
    for c in candidates:
        try:
            if c and (c / "manifests").exists() and (c / "blobs").exists():
                return c
        except Exception:
            continue
    return None


def _resolve_ollama_blob(model_name: str) -> Path | None:
    ollama_root = _find_ollama_root()
    if not ollama_root or ":" not in model_name:
        return None

    base, tag = model_name.split(":", 1)
    manifest_path = ollama_root / "manifests" / "registry.ollama.ai" / "library" / base / tag
    if not manifest_path.exists():
        return None

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    layers = manifest.get("layers") or []
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        media_type = str(layer.get("mediaType", ""))
        if "model" not in media_type:
            continue
        digest = str(layer.get("digest", ""))
        if not digest:
            continue
        digest_name = digest.replace(":", "-")
        blob = ollama_root / "blobs" / digest_name
        if blob.exists():
            return blob
    return None


def _load_overrides() -> dict[str, str]:
    if not _MODELS_JSON.exists():
        return {}
    try:
        data = json.loads(_MODELS_JSON.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k.startswith("_"):
            continue
        out[k] = v
    return out


def _resolve_from_overrides(model_name: str, overrides: dict[str, str]) -> Path | None:
    candidates = [model_name]
    if ":" in model_name:
        base, _tag = model_name.split(":", 1)
        candidates.extend([f"{base}:latest", base])

    for key in candidates:
        if key not in overrides:
            continue
        raw = overrides[key]
        if raw.strip().upper().startswith("TODO:"):
            continue
        p = Path(raw.strip())
        if p.exists():
            return p
    return None


def resolve_model_path(model_name: str) -> Path | None:


    model = _strip_alias(model_name)
    if not model:
        return None

    # 1. models.json explicit path — user override has highest priority
    overrides = _load_overrides()
    _mmproj_keys = {k for k in overrides if k.endswith("_mmproj")}
    if model in overrides and model not in _mmproj_keys:
        p = _resolve_from_overrides(model, overrides)
        if p:
            return p

    # 2. GGUF-Index (MODELS_DIR scan)
    index = _get_index()

    # 2a. direct lookup: "qwen3.5:2b" → exact hit
    if model in index:
        return index[model]

    # 2b. Normalisierter Lookup: Punkte/Striche normalisieren
    model_norm = _normalize(model.replace(":", "-"))
    for idx_name, idx_path in index.items():
        idx_norm = _normalize(idx_name.replace(":", "-"))
        if model_norm == idx_norm:
            return idx_path

    # 3. models.json fallback (remaining overrides like base-name aliases)
    p = _resolve_from_overrides(model, overrides)
    if p:
        return p

    p = _resolve_ollama_blob(model)
    if p:
        _logger.warning(
            f"NO local GGUF for '{model}' - loading from Ollama blob ({p}). "
            f"Ollama blobs are often VL variants and can be incompatible with llama.cpp. "
            f"FIX: place a compatible GGUF in {MODELS_DIR}."
        )
        return p

    _logger.error(
        f"Model '{model}' not found. "
        f"Available models in the index: {sorted(index.keys())}"
    )
    if not Path(MODELS_DIR).exists():
        _logger.error(
            f"MODELS_DIR missing: {MODELS_DIR} — set HIVEMIND_MODELS_DIR "
            f"to your models folder or run setup_models.bat."
        )
    return None


def resolve_mmproj_path(model_name: str) -> Path | None:
    """Finds the mmproj GGUF for multimodal models."""
    model = _strip_alias(model_name)
    if not model:
        return None

    overrides = _load_overrides()
    keys = [f"{model}_mmproj"]
    if ":" in model:
        base, _tag = model.split(":", 1)
        keys.extend([f"{base}:latest_mmproj", f"{base}_mmproj"])

    for k in keys:
        raw = overrides.get(k, "")
        if not raw or raw.strip().upper().startswith("TODO:"):
            continue
        p = Path(raw.strip())
        if p.exists():
            return p

    # User-config (model_configs/models/*.json): mmproj_filename hat Vorrang.
    try:
        from model_configs.models_registry import get_mmproj_filename as _reg_mmproj
        _reg_fn = _reg_mmproj(model)
        if _reg_fn:
            _reg_p = Path(_reg_fn)
            if not _reg_p.is_absolute():
                for _base_dir in (MODELS_DIR,):
                    _cand = _base_dir / _reg_fn
                    if _cand.exists():
                        return _cand
                for _cand in MODELS_DIR.rglob(_reg_fn):
                    if _cand.is_file():
                        return _cand
            elif _reg_p.exists():
                return _reg_p
    except Exception:
        pass

    # mmproj im Models-Dir suchen
    if MODELS_DIR.exists():
        base = model.split(":")[0].lower().replace(".", "")
        _MMPROJ_FILENAME_MATCH: dict[str, str] = {
            "qwen3.6":   "mmprojbf16",
            "qwen35":    "mmprojqwen35",
            "hermes36":  "hermes36",
            "hermes":    "hermes",
        }
        _wanted = _MMPROJ_FILENAME_MATCH.get(base, "")
        for p in MODELS_DIR.rglob("*mmproj*.gguf"):
            n = p.name.lower().replace(".", "")
            if base in n:
                return p
            if _wanted and _wanted in n:
                return p
        if base in ("qwen3.6", "hermes3.6"):
            for p in MODELS_DIR.rglob("*mmproj*.gguf"):
                if "bf16" in p.name.lower().replace(".", ""):
                    return p
    return None


def list_available_models(force_refresh: bool = False) -> list[str]:
    global _names_cache, _names_cache_ts
    now = _time_module.time()
    if not force_refresh and _names_cache and (now - _names_cache_ts) < _NAMES_TTL:
        return _names_cache

    names: set[str] = set()

    index = _get_index()
    names.update(index.keys())

    overrides = _load_overrides()
    for k, v in overrides.items():
        if "mmproj" in k.lower():
            continue
        if v.strip().upper().startswith("TODO:"):
            continue
        names.add(_strip_alias(k))

    ollama_root = _find_ollama_root()
    if ollama_root:
        manifest_base = ollama_root / "manifests" / "registry.ollama.ai" / "library"
        if manifest_base.exists():
            try:
                for model_dir in manifest_base.iterdir():
                    if not model_dir.is_dir():
                        continue
                    # EXCLUSION: Familie in _OLLAMA_EXCLUDE_FAMILIES → komplett skippen
                    if model_dir.name.lower() in _OLLAMA_EXCLUDE_FAMILIES:
                        _logger.debug(f"Ollama exclusion: skipping '{model_dir.name}' (no local GGUF, incompatible)")
                        continue
                    for tag_file in model_dir.iterdir():
                        if tag_file.is_file():
                            names.add(f"{model_dir.name}:{tag_file.name}")
            except Exception:
                pass

    try:
        settings_path = _THIS_DIR / "settings.json"
        if settings_path.exists():
            sd = json.loads(settings_path.read_text(encoding="utf-8"))
            for ag in sd.get("agents", {}).values():
                m = ag.get("model", "") if isinstance(ag, dict) else ""
                if m:
                    names.add(_strip_alias(m))
    except Exception:
        pass

    result = sorted(n for n in names if n and "#" not in n)

    _override_names = {_strip_alias(k) for k in overrides if not k.endswith("_mmproj") and not k.startswith("_")}
    file_to_names: dict[str, list[str]] = {}
    for name, path in index.items():
        key = str(path)
        if key not in file_to_names:
            file_to_names[key] = []
        file_to_names[key].append(name)
    for name_list in file_to_names.values():
        if len(name_list) > 1:
            name_list.sort(key=len, reverse=True)
            for dup in name_list[1:]:
                if dup in _override_names:
                    continue
                try:
                    result.remove(dup)
                except ValueError:
                    pass

    _names_cache = result
    _names_cache_ts = _time_module.time()
    return result


def list_available_models_sync(force_refresh: bool = False) -> list[str]:
    """Compatibility alias."""
    return list_available_models(force_refresh=force_refresh)


def save_model_mapping(model_name: str, gguf_path: str):
    model = _strip_alias(model_name)
    if not model:
        return
    data = _load_overrides()
    data[model] = str(gguf_path)
    payload = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    _MODELS_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
