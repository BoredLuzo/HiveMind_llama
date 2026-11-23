


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
_index_cache: dict[str, Path] | None = {}     # canonical_name → Path (None = invalidated)
_index_ts: float = 0.0
_index_stamp: tuple | None = None
_INDEX_TTL: float = 30.0
# NAMES_TTL must never exceed INDEX_TTL (2026-11-05 audit): the name
# list is DERIVED from the index — a longer names TTL kept listing a
# renamed/deleted model up to 30 s after the index had moved on.


_names_cache: list[str] | None = []
_names_cache_ts: float = 0.0
_NAMES_TTL: float = min(60.0, _INDEX_TTL)


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

_SPLIT_SHARD_RE = re.compile(r'-(\d{5})-of-(\d{5})$', re.IGNORECASE)
_SIZE_PRE = re.compile(r'[-._](0?\d{1,3}(?:\.\d+)?[bB])')
_ESIZE_PRE = re.compile(r'[-._](E\d+B)', re.IGNORECASE)


def _strip_shard_suffix(stem: str) -> str:
    return _SPLIT_SHARD_RE.sub('', stem)


def _parse_gguf_filename(filename: str) -> list[str]:


    stem = _strip_shard_suffix(Path(filename).stem)

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

_NON_MODEL_PATTERNS = {"mmproj", "projector", "vision_encoder", "vl-encoder", "dspark", "dflash"}


def is_non_model_gguf(filename: str) -> bool:
    """Shared non-model rule (2026-11-05 audit): the backend scan and
    fetch_models used to carry DIVERGENT copies of this check (the deploy
    copy missed 'projector'/'vision_encoder'/'vl-encoder'), so a projector
    could land in models.json depending on which writer ran last."""
    low = str(filename).lower()
    if any(pat in low for pat in _NON_MODEL_PATTERNS):
        return True
    return low.startswith("mtp-")

# ── Known-GGUF filename → tag manifest (2026-09-12) ──────────────────────────
# Filename parsing alone cannot always reconstruct the canonical tag: unsloth
# names MTP quants WITHOUT "mtp" (Qwen3.5-4B-Q4_K_M.gguf parsed as
# qwen3.5:4b), so the settings default qwen3.5:4b-mtp found no GGUF. The
# manifest (model_configs/gguf_filename_tags.json, shipped + extended by
# fetch_models.py) wins over parsing; parsed aliases stay available.
_FILENAME_TAG_MAP_FILE = _THIS_DIR / "model_configs" / "gguf_filename_tags.json"
_filename_tag_map: dict[str, str] | None = None

# Built-in fallback for clean installs/clones before fetch_models writes
# model_configs/gguf_filename_tags.json (on-disk manifest entries override).
_BUILTIN_FILENAME_TAGS: dict[str, str] = {
    "qwen3.5-4b-q4_k_m.gguf": "qwen3.5:4b-mtp",
    "qwen3.5-2b-q4_k_m.gguf": "qwen3.5:2b-mtp",
    "qwen3.5-9b-q4_k_m.gguf": "qwen3.5:9b-mtp",
    "gemma-4-e4b-it-qat-ud-q4_k_xl.gguf": "gemma-4:e4b-it-qat",
    "gemma-4-e2b-it-qat-ud-q4_k_xl.gguf": "gemma-4:e2b-it-qat",
    "gemma-4-e4b-uncensored-hauhaucs-aggressive-q4_k_p.gguf": "gemma-4:e4b-uncensored-hauhaucs-aggressive",
    "gemma-4-e4b-it-obliterated-q5_k_m.gguf": "gemma-4:e4b-it-obliterated",
    "sharp-minicpm5-2b-q6_k_xl.gguf": "minicpm5:2b-sharp",
    "lfm3-1.7b-qad-q4_0.gguf": "lfm3:1.7b-qad",
    "lfm3-1.7b-q4_k_m.gguf": "lfm3:1.7b-qad",
    "lfm3-1.7b-uncensored-ad-q6_k.gguf": "lfm3:1.7b-uncensored-ad",
    "lfm3-1.7b-uncensored-ad-q4_k_m.gguf": "lfm3:1.7b-uncensored-ad",
    "lfm3-omni-2b-q4_0.gguf": "lfm3-omni:2b",
    "lfm3-omni-2b-q4_k_m.gguf": "lfm3-omni:2b",
    "lfm2.5-24b-a2b-q4_k_m.gguf": "lfm2.5:24b-a2b",
    "spark-x2.6-4b-q4_k_m.gguf": "spark-x2.6:4b",
    "air-26b-a4b.gguf": "air:26b-a4b",
    "groot-19b-a3b-261023-q4_k_m.gguf": "groot:19b-a3b-261023",
    "qwen3.6-35b-a3b-ud-q4_k_xl.gguf": "qwen3.6:35b-a3b-ud",
    "qwen3.6-35b-a3b-mtp-ud-iq4_xs.gguf": "qwen3.6:35b-a3b-mtp",
    "qwen3.6-35b-a3b-ud-iq4_xs.gguf": "qwen3.6:35b-a3b-mtp",
    "qwen3.5-0.8b-ud-q4_k_xl.gguf": "qwen3.5:0.8b-ud",
    "nvidia-nemotron-3.5-bolt-e4b-ud-q4_k_xl.gguf": "nvidia-nemotron-3.5-bolt:e4b",
    "nemotron-3.5-bolt-e4b-q4_k_m.gguf": "nvidia-nemotron-3.5-bolt:e4b",
    "twil-8b-q4_k_m-balanced.gguf": "twil:8b",
    "cyber-tiel-coder-35b-a3b-mtp-ud-iq4_xs.gguf": "cyber-tiel-coder:35b-a3b-mtp-ud",
}


def _known_filename_tags() -> dict[str, str]:
    global _filename_tag_map
    if _filename_tag_map is not None:
        return _filename_tag_map
    merged = dict(_BUILTIN_FILENAME_TAGS)
    try:
        data = json.loads(_FILENAME_TAG_MAP_FILE.read_text(encoding="utf-8"))
        for k, v in data.items():
            if not k.startswith("_") and isinstance(v, str) and v:
                merged[str(k).lower()] = str(v)
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    _filename_tag_map = merged
    return _filename_tag_map

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


    _t0 = _time_module.perf_counter()
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

    # MULTI-QUANT (2026-11-05, live): several quants of one model map to
    # the SAME canonical names. Collisions are resolved once, after the
    # scan (see _resolve_index_collisions) — the old inline rule silently
    # kept the SMALLER file, which replaced an already-registered quant
    # with a worse one the moment a second file appeared.
    _name_cands: dict[str, list[Path]] = {}

    for gguf in gguf_files:
        fname_lower = gguf.name.lower()
        if is_non_model_gguf(gguf.name):
            _logger.debug(f"Skipping non-model: {gguf.name}")
            continue

        # Skip secondary shards of split GGUFs (only shard 00001 loads the full model)
        _shard_m = _SPLIT_SHARD_RE.search(gguf.stem)
        if _shard_m and int(_shard_m.group(1)) > 1:
            _logger.debug(f"Skipping split-GGUF shard >1: {gguf.name}")
            continue

        canonical_names = _parse_gguf_filename(gguf.name)
        if not canonical_names:
            fallback_canon = _canonicalize(gguf.stem)
            canonical_names = [fallback_canon] if fallback_canon else []
        if not canonical_names:
            _logger.debug(f"Cannot parse filename: {gguf.name}")
            continue

        # Unterordner-Override anwenden (z.B. Qwen3.5/4B/Qwen3.5-4B-UD.Q6_K.gguf → qwen3.5:4b-ud)
        canonical_names = _apply_folder_override(canonical_names, gguf)

        # KNOWN-FILENAME (2026-09-12): manifest tag wins, parsed names stay as
        # aliases. Insert at the front so the dedup in list_available_models
        # (keeps the longest name per file) never drops the canonical tag.
        _known_tag = _known_filename_tags().get(fname_lower)
        if _known_tag and _known_tag not in canonical_names:
            canonical_names = [_known_tag] + [n for n in canonical_names if n != _known_tag]

        for name in canonical_names:
            _name_cands.setdefault(name, []).append(gguf)

    _resolve_index_collisions(index, _name_cands)

    _logger.info(f"GGUF index: {len(index)} entries from {len(gguf_files)} files in {MODELS_DIR} (build took {(_time_module.perf_counter() - _t0) * 1000:.0f}ms)")
    for name, path in sorted(index.items()):
        _logger.debug(f"  {name} → {path.name}")

    return index


def _quant_token(filename: str) -> str:
    """Trailing quant token of a GGUF filename, lowercase ('q4_k_m')."""
    m = _QUANT_PRE.search(_strip_shard_suffix(Path(filename).stem))
    return m.group(0).lstrip("-._ ").lower() if m else ""


_logged_quant_collisions: set[tuple[str, tuple[str, ...]]] = set()


def _resolve_index_collisions(index: dict[str, Path],
                              cands: dict[str, list[Path]]) -> None:
    """MULTI-QUANT (2026-11-05, live): one model in several quantizations.

    Old rule: the SMALLER file silently replaced the registered one — a
    user who added an IQ1_M alongside his Q4_K_M lost the Q4_K_M alias
    without any notice. Resolution now:
      1. a file the user explicitly registered (models.json override)
         or that the known-filename manifest claims for this name wins;
      2. otherwise the LARGER file wins (higher quant ≈ better quality);
      3. every losing file stays reachable under an explicit
         '<name>-<quant>' alias, so no quant ever disappears from the
         picker — it just no longer owns the canonical name.
    """
    try:
        overrides = _load_overrides()
    except Exception:
        overrides = {}
    _claimed_paths: set[str] = set()
    for _k, _v in overrides.items():
        if _k.endswith("_mmproj") or not _v:
            continue
        try:
            _claimed_paths.add(str(Path(_v.strip()).resolve()).lower())
        except (OSError, ValueError):
            continue

    for name, files in cands.items():
        if not files:
            continue
        winner = files[0]
        _losers: list[Path] = []
        _log_new = False
        if len(files) > 1:
            def _score(f: Path) -> tuple[int, int]:
                claimed = str(f.resolve()).lower() in _claimed_paths
                try:
                    size = f.stat().st_size
                except OSError:
                    size = 0
                return (1 if claimed else 0, size)
            winner = max(files, key=_score)
            _losers = [f for f in files if f != winner]
            _coll_key = (name, tuple(sorted(f.name for f in files)))
            _log_new = _coll_key not in _logged_quant_collisions
            if _log_new:
                _logged_quant_collisions.add(_coll_key)
                _why = ("registered/known file"
                        if str(winner.resolve()).lower() in _claimed_paths
                        else "largest file")
                _logger.warning(
                    "[GGUF-INDEX] '%s': %d quants (%s) — '%s' wins (%s)",
                    name, len(files), ", ".join(f.name for f in files),
                    winner.name, _why)
        index[name] = winner
        for _i, _loser in enumerate(_losers):
            _q = _quant_token(_loser.name)
            # NO-SILENT-LOSS (2026-11-05, P1 audit): mid-name quants have no
            # trailing quant token, and two losers can share one token. A
            # colliding/empty alias falls back to a numbered suffix instead
            # of silently dropping the file from the registry.
            _alt = f"{name}-{_q}".strip("-") if _q else ""
            if not _alt or _alt in index or _alt in cands:
                _alt = f"{name}-alt{_i + 1}" if len(_losers) > 1                     else f"{name}-alt"
            if _alt in index or _alt in cands:
                if _log_new:
                    _logger.warning(
                        "[GGUF-INDEX] alias '%s' still collides — %s is only "
                        "resolvable via models.json", _alt, _loser.name)
                continue
            index[_alt] = _loser
            if _log_new:
                _logger.info("[GGUF-INDEX] '%s' → alternate quant alias for %s",
                             _alt, _loser.name)


def _models_dir_stamp() -> tuple | None:
    """Cheap directory + models.json mtime signature to skip no-op TTL rebuilds."""
    try:
        if not MODELS_DIR.is_dir():
            return None
        stamps: list[tuple[str, int]] = []
        for root_dir, _dirs, _files in os.walk(MODELS_DIR):
            try:
                stamps.append((root_dir, os.stat(root_dir).st_mtime_ns))
            except OSError:
                continue
        stamps.sort()
        cfg_mt = 0
        try:
            if _MODELS_JSON.is_file():
                cfg_mt = _MODELS_JSON.stat().st_mtime_ns
        except OSError:
            cfg_mt = 0
        return (str(MODELS_DIR), cfg_mt, tuple(stamps))
    except OSError:
        return None


def _get_index() -> dict[str, Path]:
    global _index_cache, _index_ts, _index_stamp
    now = _time_module.time()
    if not _index_cache or (now - _index_ts) > _INDEX_TTL:
        stamp = _models_dir_stamp()
        if _index_cache and _index_ts > 0.0 and stamp is not None and stamp == _index_stamp:
            _index_ts = now
            return _index_cache
        if _index_ts <= 0.0:
            _logger.info(f"Building initial GGUF index (trigger={_index_trigger_callers()})")
        else:
            _age = now - _index_ts
            _logger.info(f"GGUF index cache expired (age={_age:.0f}s > TTL={_INDEX_TTL:.0f}s) — rebuilding (trigger={_index_trigger_callers()})")
        _index_cache = _build_index()
        _index_stamp = stamp
        _index_ts = _time_module.time()
    return _index_cache


def _index_trigger_callers(_depth: int = 4) -> str:
    """Short caller chain for diagnosing the index refresh (no behavior)."""
    import inspect
    names: list[str] = []
    try:
        for _fr in inspect.stack()[2:2 + _depth]:
            names.append(_fr.function)
    except Exception:
        pass
    return " <- ".join(names) if names else "?"



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
        # EXISTS != USABLE (2026-10-03): the old pin-to-temp mechanism left
        # models.json entries pointing at 0-byte placeholder GGUFs long after
        # the pin died - exists() passed, llama-server crashed with exit=1
        # and the planner lost its port (live: qwen3.5:4b-mtp, gguf=0.0GB).
        # A real GGUF is never smaller than a few MB.
        try:
            if p.exists() and p.is_file() and p.stat().st_size > 1024 * 1024:
                return p
        except OSError:
            continue
    return None


def resolve_model_path(model_name: str, *, quiet: bool = False) -> Path | None:


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

    if quiet:
        _logger.debug(
            f"Model '{model}' not found. "
            f"Available models in the index: {sorted(index.keys())}"
        )
    else:
        _logger.error(
            f"Model '{model}' not found. "
            f"Available models in the index: {sorted(index.keys())}"
        )
    if not Path(MODELS_DIR).exists():
        (_logger.debug if quiet else _logger.error)(
            f"MODELS_DIR missing: {MODELS_DIR} — set HIVEMIND_MODELS_DIR "
            f"to your models folder or run setup_models.bat."
        )
    return None


def resolve_mmproj_path(model_name: str) -> Path | None:
    """Finds the mmproj GGUF for multimodal models."""
    model = _strip_alias(model_name)
    if not model:
        return None

    # normalisierte Namen: dots AND dashes stripped, so "qwen3.5" matches
    # "qwen35" and "gemma-4" matches "gemma4" in filenames.
    def _norm(x: str) -> str:
        return str(x or "").lower().replace(".", "").replace("-", "")

    _base, _tag = (model.split(":", 1) + [""])[:2]
    _nfam = _norm(_base)                       # e.g. "qwen35", "gemma4"
    _nsize = ""
    _sm = re.match(r"(e?\d+(?:\.\d+)?b)", _tag.lower())
    if _sm:
        _nsize = _sm.group(1).replace(".", "")  # e.g. "4b", "35b", "e4b"

    def _relates(path: Path) -> bool:
        """filename actually belongs to this model family (NOT another)."""
        n = _norm(path.name)
        return bool(_nfam) and _nfam in n

    # 1. Model registry (user > shipped) WINS: exact per-model intent.
    try:
        from model_configs.models_registry import get_mmproj_filename as _reg_mmproj
        _reg_fn = _reg_mmproj(model)
        if _reg_fn:
            _reg_p = Path(_reg_fn)
            if not _reg_p.is_absolute():
                _cand = MODELS_DIR / _reg_fn
                if _cand.exists():
                    return _cand
                for _cand in MODELS_DIR.rglob(_reg_fn):
                    if _cand.is_file():
                        return _cand
                _reg_fn_low = _reg_fn.lower()
                for _cand in MODELS_DIR.rglob("*mmproj*.gguf"):
                    if _cand.is_file() and _cand.name.lower() == _reg_fn_low:
                        return _cand
            elif _reg_p.exists():
                return _reg_p
    except (ImportError, OSError, ValueError, TypeError, AttributeError) as _e:
        _logger.debug("resolve_mmproj_path registry lookup skipped for %s: %s", model, _e)

    # 2. models.json overrides - only when the file relates to the family.
    overrides = _load_overrides()
    _okeys = [f"{model}_mmproj"]
    if _tag:
        _sm = re.match(r"(e?\d+(?:\.\d+)?)b", _tag.lower())
        if _sm:
            _okeys.insert(1, f"{_base}:{_sm.group(1).replace('.', '')}b_mmproj")
    if _tag:
        _okeys.extend([f"{_base}:latest_mmproj", f"{_base}_mmproj"])
    for k in _okeys:
        raw = overrides.get(k, "")
        if not raw or raw.strip().upper().startswith("TODO:"):
            continue
        _p = Path(raw.strip())
        if _p.exists() and _relates(_p):
            return _p

    # 3. GGUF-first sibling auto-detection in the resolved GGUF's directory.
    try:
        _gguf_path = resolve_model_path(model)
        if _gguf_path and _gguf_path.is_file():
            from backend.gguf_meta import detect_sibling_files as _detect_sibs
            _sib_mm = _detect_sibs(_gguf_path).get("mmproj")
            if _sib_mm:
                _sib_p = Path(_sib_mm)
                if _sib_p.is_file():
                    return _sib_p
    except (ImportError, OSError, ValueError, TypeError, AttributeError) as _e:
        _logger.debug("resolve_mmproj_path sibling scan skipped for %s: %s", model, _e)

    # 4. Filename search in MODELS_DIR - family+size first, then family, then legacy generic.
    if MODELS_DIR.exists():
        _cands = sorted(MODELS_DIR.rglob("*mmproj*.gguf"))
        if _nsize:
            for _cp in _cands:
                _n = _norm(_cp.name)
                if _nfam in _n and _nsize in _n:
                    return _cp
        for _cp in _cands:
            if _relates(_cp):
                return _cp
        if _nfam in ("qwen36", "hermes36"):
            for _cp in _cands:
                _cn = _norm(_cp.name)
                if _cn in ("mmprojbf16gguf", "mmprojf16gguf"):
                    return _cp
    return None


def resolve_sibling_drafter(model_name: str, kind: str) -> Path | None:
    """Resolve an auto-detected sibling drafter GGUF ('mtp', 'dflash', 'dspark') for `model_name`."""
    clean_kind = str(kind or "").strip().lower()
    if clean_kind not in ("mtp", "dflash", "dspark"):
        return None
    try:
        gguf_path = resolve_model_path(model_name)
        if not gguf_path or not gguf_path.is_file():
            return None
        from backend.gguf_meta import detect_sibling_files as _detect_sibs
        sib = _detect_sibs(gguf_path).get(clean_kind)
        if sib:
            sib_p = Path(sib)
            if sib_p.is_file():
                return sib_p
    except (ImportError, OSError, ValueError, TypeError, AttributeError) as _e:
        _logger.debug("resolve_sibling_drafter(%s, %s) failed: %s", model_name, clean_kind, _e)
    return None


def list_available_models(force_refresh: bool = False) -> list[str]:
    global _names_cache, _names_cache_ts
    now = _time_module.time()
    if force_refresh:
        # HALF-REFRESH (2026-11-05 audit): force_refresh used to rebuild
        # only the names cache while the index behind it stayed TTL-gated —
        # a renamed file kept listing under its old name.
        global _index_cache, _index_ts, _index_stamp
        _index_cache = None
        _index_ts = 0.0
        _index_stamp = None
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
        # GHOST-GUARD (2026-11-05, P1 audit): override keys whose file is
        # GONE were still listed and only failed at load time. Entries
        # claiming files outside MODELS_DIR (add_model by-path flow) are
        # checked directly; keys inside MODELS_DIR are kept (the index
        # scan may not have seen them yet).
        try:
            _vpath = Path(v.strip())
            if _vpath.is_absolute() and not _vpath.is_file():
                continue
        except OSError:
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
                # AVAILABLE-ONLY (2026-10-05): shipped default agent models
                # (qwen3.5:4b-mtp, lfm2.5:2.6b, judge …) land here on every
                # fresh install — listing them without a GGUF on disk made
                # /models (and the Telegram /models) offer models that
                # cannot load. Only settings models whose GGUF is in the
                # index get listed.
                if m and (m in index or _strip_alias(m) in index):
                    names.add(_strip_alias(m))
    except Exception:
        pass

    result = sorted(n for n in names if n and "#" not in n)

    _override_names = {_strip_alias(k) for k in overrides if not k.endswith("_mmproj") and not k.startswith("_")}

    def _norm_path_key(raw_p: object) -> str:
        try:
            return os.path.normcase(str(Path(str(raw_p)).resolve()))
        except OSError:
            return os.path.normcase(str(raw_p))

    file_to_names: dict[str, list[str]] = {}
    for name, path in index.items():
        key = _norm_path_key(path)
        lst = file_to_names.setdefault(key, [])
        if name not in lst:
            lst.append(name)
    for ov_k, ov_v in overrides.items():
        if "mmproj" in ov_k.lower() or ov_k.startswith("_"):
            continue
        if str(ov_v).strip().upper().startswith("TODO:"):
            continue
        ov_name = _strip_alias(ov_k)
        if ov_name not in result:
            continue
        ov_key = _norm_path_key(str(ov_v).strip())
        lst = file_to_names.setdefault(ov_key, [])
        if ov_name not in lst:
            lst.append(ov_name)
    # DEDUP PRIORITY (2026-11-05, P1 audit): the old length sort kept the
    # LONGEST alias — for gemma-4-e4b-it-qat-ud-q4_k_xl.gguf that is the
    # parsed '…-ud' alias, and the catalog/profile name
    # 'gemma-4:e4b-it-qat' became unselectable (its per-model config was
    # silently skipped). Priority now: known manifest tag >
    # models.json override > shortest (closest to the profile key).
    _manifest_tags = set(_known_filename_tags().values())
    _override_keys = set(overrides.keys())

    def _name_rank(n: str) -> tuple[int, int]:
        # manifest/override names are canonical — the shorter spelling wins
        # inside a rank; plain parsed aliases keep the LONGEST (the old
        # behavior — a shorter parse like 'qwen3.6:35b' says less than
        # 'qwen3.6:35b-a3b-ud').
        if n in _manifest_tags:
            return (0, len(n))
        if n in _override_keys:
            return (1, len(n))
        return (2, -len(n))

    for path_key, name_list in file_to_names.items():
        if len(name_list) > 1:
            name_list.sort(key=_name_rank)
            for dup in name_list[1:]:
                # DEDUP FIX (2026-10-07, user feedback: minicpm listed twice under different names): an override name stays protected
                # only when it points at a DIFFERENT file. Same-file aliases
                # (manifest tag vs models.json override vs parsed name) now
                # collapse to ONE listing instead of confusing the model
                # picker with three entries for the same weights.
                if dup in _override_names and _norm_path_key(
                        overrides.get(dup, "")) != path_key:
                    continue
                try:
                    result.remove(dup)
                except ValueError:
                    pass

    _names_cache = result
    _names_cache_ts = _time_module.time()
    return result



def save_model_mapping(model_name: str, gguf_path: str):
    model = _strip_alias(model_name)
    if not model:
        return
    if model.lower().startswith("llamacpp:"):
        # SLOT-ID (2026-11-05, live): llama.cpp ohne -a meldet geladene
        # Modelle als 'llamacpp:<sha>'. So ein interner Slot-Name ist kein
        # Registrierungsname — sonst steht er dauerhaft im Model-Picker.
        _logger.warning(
            "[MODELS-JSON] refusing to register slot id '%s' (load the "
            "model under its registry name instead)", model)
        return
    data = _load_overrides()
    data[model] = str(gguf_path)
    payload = {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    # ATOMIC (2026-11-05 audit): a crash mid-write truncated the registry
    # to {} — models.json is resolution priority 1.
    _tmp = _MODELS_JSON.with_name(f"{_MODELS_JSON.name}.tmp-{os.getpid()}")
    _tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _tmp.replace(_MODELS_JSON)
    invalidate_caches()


def invalidate_caches() -> None:
    """Drop every model-registry cache (index, names, manifest).

    The single invalidation entry point (2026-11-05 audit): rename/move
    recovery used to rely on TTLs alone, and `force_refresh` on the names
    list did not rebuild the index behind it. Callers: save_model_mapping,
    add_model, and anything that touches files under MODELS_DIR.
    """
    global _index_cache, _index_ts, _index_stamp, _names_cache, _names_cache_ts
    global _filename_tag_map
    _index_cache = None
    _index_ts = 0.0
    _index_stamp = None
    _names_cache = None
    _names_cache_ts = 0.0
    _filename_tag_map = None
