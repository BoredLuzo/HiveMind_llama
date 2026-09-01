


from __future__ import annotations

import os
import re
import asyncio
import logging
import time
import hashlib
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hivemind.tree_scout")

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None


_SKIP_DIRS: set[str] = {
    "node_modules", ".git", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "out", "target", ".idea",
    ".vscode", ".pytest_cache", ".mypy_cache", "coverage", ".turbo",
    "vendor", "Pods", ".gradle", ".angular", "eggs", ".eggs",
}

_SKIP_FILE_PREFIXES: tuple[str, ...] = (".hivemind_ckpt_", ".hivemind_")

_BINARY_EXTS: set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar",
    ".exe", ".dll", ".so", ".dylib", ".o", ".obj", ".class", ".jar",
    ".pyc", ".pyo", ".pyd", ".whl", ".lock", ".map", ".min.css", ".min.js",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".ogg", ".webm",
    ".iso", ".img", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
}

_MAX_DEPTH     = 6
_MAX_FILES     = 200
_LLM_TIMEOUT   = 30.0
_TREE_INCLUDE_FILE_SIZES = False

# ── Walk vs. Analyse-Cap (2026-08-17) ──────────────────────────────────────────
_WALK_MAX_FILES = 50_000

_TREE_CACHE_TTL = 15.0
_TREE_CACHE_MAX = 8
_TREE_CACHE: dict[tuple[str, int, int], dict] = {}

_PARTITION_TREE_CACHE_TTL = 15.0
_PARTITION_TREE_CACHE_MAX = 24
_PARTITION_TREE_CACHE: dict[tuple[str, int, str, str], dict] = {}

_PARTITION_IMPORT_GRAPH_CACHE_TTL = 15.0
_PARTITION_IMPORT_GRAPH_CACHE_MAX = 16
_PARTITION_IMPORT_GRAPH_CACHE: dict[tuple[str, tuple[str, ...]], dict] = {}
_IMPORT_GRAPH_MAX_FILES = 180
_IMPORT_GRAPH_MAX_BYTES = 3_000_000
_MERGE_TINY_THRESHOLD = 2


def _prune_ttl_cache(cache: dict, ttl: float, max_items: int):
    _now = time.time()
    _expired = [
        k for k, v in cache.items()
        if (_now - float(v.get("ts", 0.0))) > float(ttl)
    ]
    for k in _expired:
        cache.pop(k, None)
    while len(cache) > int(max_items):
        _oldest = min(cache, key=lambda k: float(cache[k].get("ts", 0.0)))
        cache.pop(_oldest, None)


_WIN_PATH_RE = re.compile(
    r'(?:^|[\s"\'])([A-Za-z]:[\\\/][^\s\n\'"<>|?*]{3,})',
    re.MULTILINE,
)

_UNIX_PATH_RE = re.compile(
    r'(?:^|[\s"\'])(/(?:home|Users|opt|var|srv|workspace|projects?|code|app|src'
    r'|desktop|tmp|mnt|media|data|run|repos?|dev|work|sandbox|volumes?)'
    r'[^\s\n\'"<>|?*]{0,200})',
    re.MULTILINE,
)

_UNIX_ABS_PATH_FALLBACK_RE = re.compile(
    r'(?:^|[\s"\'])(/[a-zA-Z][^\s\n\'"<>|?*]{4,200})',
    re.MULTILINE,
)

def _extract_path_from_text(text: str) -> Optional[str]:
    for m in _WIN_PATH_RE.finditer(text):
        candidate = m.group(1).rstrip(".,;:\"'")
        if len(candidate) > 5:
            if os.path.exists(candidate):
                if os.path.isdir(candidate):
                    return candidate
                parent = str(Path(candidate).parent)
                if os.path.isdir(parent):
                    return parent
            elif "\\" in candidate or "/" in candidate:
                return candidate

    for m in _UNIX_PATH_RE.finditer(text):
        candidate = m.group(1).rstrip(".,;:\"'")
        if os.path.exists(candidate):
            if os.path.isdir(candidate):
                return candidate
            parent = str(Path(candidate).parent)
            if os.path.isdir(parent):
                return parent

    for m in _UNIX_ABS_PATH_FALLBACK_RE.finditer(text):
        candidate = m.group(1).rstrip(".,;:\"'")
        if os.path.isdir(candidate):
            return candidate
        parent = str(Path(candidate).parent)
        if os.path.isdir(parent) and parent not in ("/", ""):
            return parent

    return None


def build_tree(root: str, max_depth: int = _MAX_DEPTH, max_files: int = _MAX_FILES) -> str:
    root_path = Path(root)
    if not root_path.exists():
        return f"[tree_scout: path does not exist: {root}]"
    if not root_path.is_dir():
        root_path = root_path.parent
        if not root_path.is_dir():
            return f"[tree_scout: no directory: {root}]"

    lines   = [f"📁 {root_path}"]
    counter = [0]

    def _walk(path: Path, depth: int, prefix: str):
        if depth > max_depth or counter[0] >= max_files:
            return

        try:
            entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except PermissionError:
            lines.append(f"{prefix}└── [access denied]")
            return

        dirs  = [e for e in entries if e.is_dir()  and e.name not in _SKIP_DIRS and not e.name.endswith(".egg-info")]
        files = [e for e in entries if e.is_file() and not e.name.startswith(_SKIP_FILE_PREFIXES) and (
            e.suffix.lower() not in _BINARY_EXTS
        )]

        all_visible = dirs + files
        for i, entry in enumerate(all_visible):
            if counter[0] >= max_files:
                lines.append(f"{prefix}└── … [{max_files}+ entries, rest truncated]")
                return
            is_last    = (i == len(all_visible) - 1)
            connector  = "└── " if is_last else "├── "
            new_prefix = prefix + ("    " if is_last else "│   ")

            if entry.is_dir():
                lines.append(f"{prefix}{connector}📁 {entry.name}/")
                _walk(entry, depth + 1, new_prefix)
            else:
                size_str = ""
                if _TREE_INCLUDE_FILE_SIZES:
                    try:
                        size = entry.stat().st_size
                        if size > 1024 * 1024:
                            size_str = f"  ({size // (1024*1024)}MB)"
                        elif size > 1024:
                            size_str = f"  ({size // 1024}KB)"
                    except OSError:
                        pass
                lines.append(f"{prefix}{connector}{entry.name}{size_str}")
                counter[0] += 1

    _walk(root_path, depth=0, prefix="")

    summary = f"\n[{counter[0]} Dateien angezeigt"
    if counter[0] >= max_files:
        summary += f" — limit reached, tree may be incomplete"
    summary += "]"
    lines.append(summary)

    return "\n".join(lines)


def walk_repo_paths(
    root: str,
    max_depth: int = _MAX_DEPTH,
    max_files: int = _WALK_MAX_FILES,
) -> list[str]:


    root_path = Path(root)
    if not root_path.exists() or not root_path.is_dir():
        return []

    _norm_root = str(root_path.resolve())
    result: list[str] = []

    def _walk_dir(path: Path, depth: int):
        if depth > max_depth or len(result) >= max_files:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except PermissionError:
            return
        for entry in entries:
            if len(result) >= max_files:
                return
            try:
                if entry.is_dir():
                    if entry.name in _SKIP_DIRS or entry.name.endswith(".egg-info"):
                        continue
                    _walk_dir(entry, depth + 1)
                elif entry.is_file():
                    if entry.name.startswith(_SKIP_FILE_PREFIXES):
                        continue
                    if entry.suffix.lower() in _BINARY_EXTS:
                        continue
                    rel = os.path.relpath(entry, root_path).replace("\\", "/")
                    result.append(rel)
            except OSError:
                continue

    try:
        _walk_dir(root_path, 0)
    except Exception:
        pass
    return result


def build_tree_from_paths(
    root: str,
    rel_paths: list[str],
    max_depth: int = _MAX_DEPTH,
) -> str:


    root_path = Path(root)
    lines = [f"📁 {root_path}"]
    counter = [0]

    tree: dict = {}
    for rel in rel_paths:
        parts = [p for p in rel.replace("\\", "/").split("/") if p]
        if not parts:
            continue
        node = tree
        for i, part in enumerate(parts):
            if i == len(parts) - 1:
                node.setdefault(part, None)
                if not isinstance(node[part], dict):
                    node[part] = None
            else:
                cur = node.get(part)
                if not isinstance(cur, dict):
                    cur = {}
                    node[part] = cur
                node = cur

    def _render(node: dict, prefix: str, depth: int):
        if depth > max_depth:
            return
        keys = sorted(node.keys(), key=str.lower)
        for i, name in enumerate(keys):
            if counter[0] >= len(rel_paths):
                return
            is_last = (i == len(keys) - 1)
            connector = "└── " if is_last else "├── "
            new_prefix = prefix + ("    " if is_last else "│   ")
            child = node[name]
            if child is None:
                lines.append(f"{prefix}{connector}{name}")
                counter[0] += 1
            else:
                lines.append(f"{prefix}{connector}📁 {name}/")
                _render(child, new_prefix, depth + 1)

    _render(tree, "", 0)

    summary = f"\n[{counter[0]} Dateien angezeigt]"
    lines.append(summary)
    return "\n".join(lines)


async def build_tree_async(root: str, max_depth: int = _MAX_DEPTH, max_files: int = _MAX_FILES) -> str:
    return await asyncio.to_thread(build_tree, root, max_depth, max_files)


async def partition_tree_async(
    tree_str: str,
    max_files_per_partition: int = 30,
    workspace_root: str = "",
    preselect_paths: Optional[list[str]] = None,
) -> list[dict]:
    return await asyncio.to_thread(
        partition_tree, tree_str, max_files_per_partition, workspace_root, preselect_paths,
    )


async def _llm_extract_path(
    task: str,
    client,
    port: int,
    model: str,
) -> Optional[str]:
    import httpx

    prompt = (
        "The user task references a project directory. "
        "Reply with ONLY the absolute path to that project directory — "
        "nothing else, no explanation, no code block, just the raw path.\n\n"
        f"Task:\n{task[:800]}"
    )

    try:
        resp = await client.post(
            f"http://127.0.0.1:{port}/v1/chat/completions",
            json={
                "model":          model,
                "messages":       [{"role": "user", "content": prompt}],
                "stream":         False,
                "temperature":    0,
                "max_tokens":     80,
                "thinking":       False,
                "thinking_budget": 0,
            },
            timeout=httpx.Timeout(connect=5.0, read=_LLM_TIMEOUT, write=5.0, pool=5.0),
        )
        if resp.status_code >= 400:
            logger.warning("tree_scout LLM-Call HTTP %d", resp.status_code)
            return None

        data    = resp.json()
        content = (data.get("choices") or [{}])[0].get("message", {}).get("content", "").strip()
        logger.debug("tree_scout LLM raw: %r", content)

        path = _extract_path_from_text(content)
        if path:
            return path

        candidate = content.strip().strip("\"'`").rstrip("/\\")
        if len(candidate) > 3 and (os.path.exists(candidate) or "\\" in candidate or "/" in candidate):
            return candidate

        return None

    except Exception as e:
        logger.warning("tree_scout LLM call error: %s", e)
        return None


def partition_tree(
    tree_str: str,
    max_files_per_partition: int = 30,
    workspace_root: str = "",
    preselect_paths: Optional[list[str]] = None,
) -> list[dict]:
    if not tree_str and not preselect_paths:
        return []

    if "## Workspace-Verzeichnisbaum" in tree_str:
        _parts = tree_str.split("\n\n", 1)
        if len(_parts) > 1:
            tree_str = _parts[1]

    # ── Cache-Lookup ─────────────────────────────────────────────────────────────
    _ws_cache_key = ""
    if workspace_root:
        try:
            _ws_cache_key = str(Path(workspace_root).resolve()).lower()
        except Exception:
            _ws_cache_key = str(workspace_root).strip().lower()
            
    _tree_hash    = hashlib.sha1((tree_str or "").encode("utf-8", errors="ignore")).hexdigest()
    
    # BUG-5 FIX: Content Fingerprint in Cache-Key aufnehmen
    _content_fingerprint = ""
    if workspace_root:
        try:
            _ws_path = Path(workspace_root)
            _mtimes = []
            for _f in _ws_path.rglob("*"):
                if _f.is_file() and not any(skip in _f.parts for skip in _SKIP_DIRS):
                    try:
                        _mtimes.append(int(_f.stat().st_mtime))
                    except OSError:
                        pass
                    if len(_mtimes) >= 50:
                        break
            if _mtimes:
                _content_fingerprint = hashlib.sha1(
                    ",".join(str(m) for m in sorted(_mtimes)).encode()
                ).hexdigest()[:12]
        except Exception:
            pass

    _preselect_fp = hashlib.sha1(
        "\n".join(preselect_paths or []).encode("utf-8", errors="ignore")
    ).hexdigest()[:16] if preselect_paths else ""
    _pt_cache_key = (_tree_hash, int(max_files_per_partition or 30), _ws_cache_key, _content_fingerprint, _preselect_fp)
    _pt_entry     = _PARTITION_TREE_CACHE.get(_pt_cache_key)
    if _pt_entry and (time.time() - float(_pt_entry.get("ts", 0.0))) <= _PARTITION_TREE_CACHE_TTL:
        return _pt_entry.get("partitions") or []

    # PRESELECT-FIX (2026-08-17): preselect_paths = PageRank-selektiertes
    if preselect_paths:
        extracted_files = preselect_paths
    else:
        lines = tree_str.splitlines()
        if not lines:
            return []

        dir_stack: list[str] = []
        extracted_files: list[str] = []
        _indent_re = re.compile(r"^((?:│ {3}| {4})*)([├└]── )(.*)")

        for line in lines[1:]:
            if line.startswith("[") or not line.strip():
                continue
            _m = _indent_re.match(line)
            if not _m:
                continue
            depth     = len(_m.group(1)) // 4
            name_part = _m.group(3).split("  ")[0].strip()
            if not name_part:
                continue
            is_dir = name_part.startswith("📁") or name_part.endswith("/")
            name   = name_part.replace("📁", "").strip().rstrip("/")
            dir_stack = dir_stack[:depth]
            if is_dir:
                dir_stack.append(name)
            else:
                rel_path = "/".join(dir_stack + [name]) if dir_stack else name
                extracted_files.append(rel_path)

    _seen: set[str] = set()
    _deduped: list[str] = []
    for _fp in extracted_files:
        _nk = str(_fp or "").replace("\\", "/").strip()
        if not _nk or _nk.lower() in _seen:
            continue
        _seen.add(_nk.lower())
        _deduped.append(_nk)
    extracted_files = _deduped

    if not extracted_files:
        return []

    groups: dict[str, list[str]] = {}
    for fp in extracted_files:
        parts = fp.split("/")
        top   = parts[0] if len(parts) > 1 else "__root__"
        groups.setdefault(top, []).append(fp)

    split_threshold = max(1, max_files_per_partition // 2)
    refined: dict[str, list[str]] = {}
    for top, files in groups.items():
        if len(files) <= split_threshold:
            refined[top] = files
        else:
            sub: dict[str, list[str]] = {}
            for fp in files:
                parts = fp.split("/")
                key   = f"{top}/{parts[1]}" if len(parts) > 2 else top
                sub.setdefault(key, []).append(fp)
            refined.update(sub)

    _TINY = max(1, _MERGE_TINY_THRESHOLD)
    big_groups:  dict[str, list[str]] = {k: v for k, v in refined.items() if len(v) >= _TINY}
    tiny_groups: dict[str, list[str]] = {k: v for k, v in refined.items() if len(v) <  _TINY}

    if tiny_groups:
        _importer_counts: dict[str, dict[str, int]] = {}
        if workspace_root and big_groups:
            try:
                _ws_path     = Path(workspace_root)
                _ws_resolved = _ws_path.resolve()
                _readable    = {".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".cs", ".go", ".rs"}
                _tiny_norm:  dict[str, str] = {}
                for _tlbl, _tfiles in tiny_groups.items():
                    for _tf in _tfiles:
                        _tiny_norm[str(_tf).replace("\\", "/").lower()] = _tlbl

                _bytes_total = 0
                _budget_ok   = True
                for _blbl, _bfiles in big_groups.items():
                    if not _budget_ok:
                        break
                    for _bf in _bfiles:
                        _bsrc = _ws_path / _bf
                        if _bsrc.suffix not in _readable:
                            continue
                        try:
                            _content = _bsrc.read_text(encoding="utf-8", errors="ignore")
                        except OSError:
                            continue
                        _bytes_total += len(_content)
                        if _bytes_total > _IMPORT_GRAPH_MAX_BYTES:
                            _budget_ok = False
                            break
                        for _im in _IMPORT_RE.finditer(_content):
                            _raw = next((g for g in _im.groups() if g), None)
                            if not _raw or not _raw.startswith((".", "./")):
                                continue
                            _base_dir = _bsrc.parent
                            _cand     = (_base_dir / _raw).resolve()
                            for _ext in ("", ".ts", ".tsx", ".js", ".jsx", ".py"):
                                _cp = Path(str(_cand) + _ext)
                                try:
                                    _rel = _cp.relative_to(_ws_resolved).as_posix().lower()
                                except ValueError:
                                    continue
                                if _rel in _tiny_norm:
                                    _t = _tiny_norm[_rel]
                                    _importer_counts.setdefault(_t, {})
                                    _importer_counts[_t][_blbl] = _importer_counts[_t].get(_blbl, 0) + 1
            except Exception:
                pass

        _big_labels_sorted = sorted(big_groups.keys(), key=lambda k: -len(big_groups[k]))
        _fallback = _big_labels_sorted[0] if _big_labels_sorted else None

        _file_size_cache: dict[str, int] = {}
        if workspace_root:
            try:
                _ws_path = Path(workspace_root)
                for _blbl, _bfiles in big_groups.items():
                    for _bf in _bfiles:
                        try:
                            _fp = _ws_path / _bf
                            if _fp.is_file():
                                _file_size_cache[_bf] = _fp.stat().st_size
                        except OSError:
                            pass
            except Exception:
                pass

        def _best_merge_target(tiny_label: str) -> str | None:
            _icounts = _importer_counts.get(tiny_label, {})
            if _icounts:
                _top_count = max(_icounts.values())
                _tied = [k for k, v in _icounts.items() if v == _top_count]
                if len(_tied) > 1:
                    return min(_tied, key=lambda x: hashlib.md5(x.encode()).hexdigest())
                return max(_icounts, key=_icounts.get)
            _tl_lower = tiny_label.lower()
            _tl_parts = _tl_lower.split("/")
            _best_prefix: str | None = None
            _best_depth = -1
            for _bl in _big_labels_sorted:
                _bl_parts = _bl.lower().split("/")
                _common = sum(1 for a, b in zip(_tl_parts, _bl_parts) if a == b)
                if _common > _best_depth:
                    _best_depth, _best_prefix = _common, _bl
            if _best_depth > 0 and _best_prefix:
                return _best_prefix
            if _file_size_cache and big_groups:
                _tiny_sizes = [_file_size_cache.get(f, 0) for f in tiny_groups.get(tiny_label, [])]
                if _tiny_sizes:
                    _tiny_avg = sum(_tiny_sizes) / len(_tiny_sizes)
                    _best_affinity: str | None = None
                    _best_diff = float('inf')
                    for _blbl in _big_labels_sorted:
                        _big_sizes = [_file_size_cache.get(f, 0) for f in big_groups[_blbl]]
                        if _big_sizes:
                            _big_avg = sum(_big_sizes) / len(_big_sizes)
                            _diff = abs(_big_avg - _tiny_avg)
                            if _diff < _best_diff:
                                _best_diff, _best_affinity = _diff, _blbl
                    if _best_affinity:
                        return _best_affinity
            return _fallback

        for _tlbl, _tfiles in sorted(tiny_groups.items(), key=lambda kv: kv[0]):
            _target = _best_merge_target(_tlbl)
            if _target and _target in big_groups:
                logger.debug(
                    "partition_tree: merge tiny '%s' (%d files) → '%s'",
                    _tlbl, len(_tfiles), _target,
                )
                big_groups[_target] = big_groups[_target] + _tfiles
            else:
                if not _tfiles:
                    logger.warning(
                        "partition_tree: EMPTY PARTITION detected '%s' (seed: %s)",
                        _tlbl, hashlib.md5(_tlbl.encode()).hexdigest()[:8]
                    )
                else:
                    logger.debug(
                        "partition_tree: retained tiny '%s' (%d files) as own partition",
                        _tlbl, len(_tfiles)
                    )
                big_groups[_tlbl] = _tfiles

        refined = big_groups

    # Slot-Mathe: 1 Slot ≈ 4KB Dateiinhalt (konservativ — Worker liest ~14KB bei 70K ctx,
    _READ_SLOT_BYTES = 4000    # 4KB pro Slot
    _MAX_SLOTS_PER_PARTITION = max(4, int(max_files_per_partition * 1.5))

    def _file_slots(filepath: str) -> int:
        try:
            _ws_path_local = Path(workspace_root)
            size = (_ws_path_local / filepath).stat().st_size
        except (OSError, TypeError):
            return 1
        slots = max(1, (size + _READ_SLOT_BYTES - 1) // _READ_SLOT_BYTES)
        return min(slots, 4)  # Cap bei 4 Slots

    _TOTAL_FILES = sum(len(v) for v in refined.values())
    _SIZE_SPLIT_ENABLED = bool(workspace_root)

    if _SIZE_SPLIT_ENABLED:
        _sz: dict[str, int] = {}
        try:
            _ws_path_local = Path(workspace_root)
            for _files in refined.values():
                for _f in _files:
                    try:
                        _sz[_f] = (_ws_path_local / _f).stat().st_size
                    except OSError:
                        _sz[_f] = 0
        except Exception:
            _SIZE_SPLIT_ENABLED = False

    if _SIZE_SPLIT_ENABLED:
        _resplit: dict[str, list[str]] = {}
        for _lbl, _files in refined.items():
            _total_slots = sum(_file_slots(f) for f in _files)
            if _total_slots <= _MAX_SLOTS_PER_PARTITION:
                _resplit[_lbl] = _files
                continue

            _sorted_files = sorted(_files, key=lambda f: -_file_slots(f))
            _chunks: list[list[str]] = []
            _cur: list[str] = []
            _cur_slots = 0

            for _sf in _sorted_files:
                _u = _file_slots(_sf)
                if _cur and _cur_slots + _u > _MAX_SLOTS_PER_PARTITION:
                    _chunks.append(_cur)
                    _cur = [_sf]
                    _cur_slots = _u
                else:
                    _cur.append(_sf)
                    _cur_slots += _u
            if _cur:
                _chunks.append(_cur)

            for _ci, _chunk in enumerate(_chunks):
                _new_lbl = _lbl if _ci == 0 else f"{_lbl}:sz{_ci}"
                _resplit[_new_lbl] = _chunk
                logger.debug(
                    "partition_tree slot-split: '%s' chunk %d → %d files, %d slots",
                    _lbl, _ci, len(_chunk), sum(_file_slots(f) for f in _chunk),
                )
        refined = _resplit

    _global_seen_paths: set[str] = set()
    _dedup_order = sorted(refined.items(), key=lambda kv: -len(kv[1]))
    refined = {}
    for _dlbl, _dfiles in _dedup_order:
        _clean: list[str] = []
        for _df in _dfiles:
            _dk = str(_df).replace("\\", "/").lower().strip()
            if _dk not in _global_seen_paths:
                _global_seen_paths.add(_dk)
                _clean.append(_df)
        if _clean:
            refined[_dlbl] = _clean
        else:
            logger.debug("partition_tree dedup: partition '%s' empty after dedup — dropped", _dlbl)

    partitions:   list[dict] = []
    _used_labels: set[str]   = set()

    for label, files in sorted(refined.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if not files:
            continue
        unique_label = label
        _sfx = 0
        while unique_label in _used_labels:
            _sfx += 1
            unique_label = f"{label}:{_sfx}"
        _used_labels.add(unique_label)

        _capped = files[:max_files_per_partition]
        if len(files) > max_files_per_partition:
            logger.debug("partition_tree: '%s' truncated %d->%d", unique_label, len(files), max_files_per_partition)

        _file_sizes = {f: _sz[f] for f in _capped if f in _sz} if "_sz" in locals() else {}
        partitions.append({"label": unique_label, "paths": _capped, "is_shared": False,
                           "file_sizes": _file_sizes})

    if not partitions and extracted_files:
        partitions.append({
            "label":     "__all__",
            "paths":     extracted_files[:max_files_per_partition],
            "is_shared": False,
        })

    _PARTITION_TREE_CACHE[_pt_cache_key] = {"ts": time.time(), "partitions": partitions}
    _prune_ttl_cache(_PARTITION_TREE_CACHE, _PARTITION_TREE_CACHE_TTL, _PARTITION_TREE_CACHE_MAX)

    return partitions


TREE_HEADER_PREFIX = "## Workspace directory tree (auto-generated)"


async def get_workspace_tree(
    task: str,
    ws_str: str = "",
    client=None,
    port: int = 0,
    model: str = "",
    max_depth: int = _MAX_DEPTH,
    max_files: int = _MAX_FILES,
    enabled: bool = True,
) -> str:
    if not enabled:
        logger.debug("tree_scout: disabled per enabled=False")
        return ""

    path = _extract_path_from_text(task)
    if path:
        logger.info("tree_scout Stufe 1 (Regex): %s", path)

    if not path and ws_str:
        candidate = ws_str.strip().strip("\"'")
        if candidate and os.path.isdir(candidate):
            path = candidate
            logger.info("tree_scout Stufe 1.5 (ws_str): %s", path)

    if not path and client and port and model:
        logger.info("tree_scout stage 2 (LLM): starting path extraction")
        path = await _llm_extract_path(task=task, client=client, port=port, model=model)
        if path:
            logger.info("tree_scout Stufe 2 (LLM): %s", path)
        else:
            logger.info("tree_scout stage 2: no path - pre-explore without tree")
    elif not path:
        logger.info("tree_scout: no path found (no LLM client passed)")

    if not path:
        return ""

    _resolved_root = str(Path(path).resolve())
    _tree_cache_key = (_resolved_root.lower(), int(max_depth), int(max_files))
    _root_mtime = 0.0
    _first_level_mtime_sum = 0.0
    try:
        _root_mtime = Path(_resolved_root).stat().st_mtime
    except OSError:
        pass
    try:
        _first_level_mtime_sum = sum(
            e.stat().st_mtime for e in Path(_resolved_root).iterdir()
            if not e.name.startswith(".")
        )
    except OSError:
        pass

    _tree_cache_entry = _TREE_CACHE.get(_tree_cache_key)
    _cache_tree = ""
    if _tree_cache_entry and (time.time() - float(_tree_cache_entry.get("ts", 0.0))) <= _TREE_CACHE_TTL:
        _cached_mtime = float(_tree_cache_entry.get("root_mtime", 0.0))
        _cached_fl_sum = float(_tree_cache_entry.get("first_level_mtime_sum", 0.0))
        if abs(_cached_mtime - _root_mtime) < 1e-6 and abs(_cached_fl_sum - _first_level_mtime_sum) < 1e-3:
            _cache_tree = str(_tree_cache_entry.get("tree", "") or "")

    if _cache_tree:
        tree = _cache_tree
    else:
        tree = await build_tree_async(root=_resolved_root, max_depth=max_depth, max_files=max_files)
        _TREE_CACHE[_tree_cache_key] = {
            "ts": time.time(),
            "root_mtime": _root_mtime,
            "first_level_mtime_sum": _first_level_mtime_sum,
            "tree": tree,
        }
        _prune_ttl_cache(_TREE_CACHE, _TREE_CACHE_TTL, _TREE_CACHE_MAX)

    header = (
        TREE_HEADER_PREFIX + "\n"
        "You already know the complete project structure. "
        "Do NOT use list_dir or find_files anymore — "
        "read the relevant files directly with read_file.\n\n"
    )
    return header + tree


# ── Websearch-Trigger ──────────────────────────────────────────────────────────

_STDLIB_MODULES: frozenset[str] = frozenset({
    "os", "sys", "re", "io", "abc", "ast", "cgi", "cmd", "csv", "dis",
    "enum", "ftp", "gc", "gzip", "hmac", "html", "http", "imaplib", "inspect",
    "json", "logging", "math", "mmap", "operator", "os.path", "pathlib",
    "pickle", "platform", "pprint", "queue", "random", "shutil", "signal",
    "socket", "sqlite3", "ssl", "stat", "string", "struct", "subprocess",
    "tarfile", "tempfile", "threading", "time", "timeit", "traceback",
    "typing", "unittest", "urllib", "uuid", "warnings", "weakref", "xml",
    "xmlrpc", "zipfile", "zlib", "collections", "contextlib", "copy",
    "dataclasses", "datetime", "decimal", "difflib", "email", "functools",
    "hashlib", "heapq", "importlib", "itertools", "multiprocessing",
    "numbers", "textwrap", "asyncio", "concurrent", "contextvars",
    "base64", "binascii", "codecs", "colorsys", "configparser", "fractions",
    "getpass", "glob", "grp", "ipaddress", "keyword", "linecache", "locale",
    "mailbox", "mimetypes", "netrc", "optparse", "pdb", "posixpath",
    "pstats", "pwd", "py_compile", "pyclbr", "readline", "resource",
    "rlcompleter", "sched", "secrets", "select", "shelve", "shlex",
    "smtplib", "sndhdr", "spwd", "statistics", "sysconfig", "syslog",
    "telnetlib", "termios", "token", "tokenize", "trace", "tty", "turtle",
    "turtledemo", "unicodedata", "uu", "venv", "wave", "wsgiref",
    "xdrlib", "xmlrpc",
})

_IMPORT_RE = re.compile(
    r"""
    (?:^|\n)\s*
    (?:
        import\s+([\w.]+)
      | from\s+([\w.]+)\s+import
      | import\s+\{[^}]+\}\s+from\s+['"]([^'"]+)['"]
      | import\s+['"]([^'"]+)['"]
      | require\s*\(\s*['"]([^'"]+)['"]\s*\)
      | import\s+[\w.]+\s*;
    )
    """,
    re.VERBOSE | re.MULTILINE,
)

_KNOWN_LOCAL_PREFIXES = re.compile(
    r"^(?:\.|\.\.\/|src\/|lib\/|hive_functions\/|app\/|pkg\/|internal\/|cmd\/)"
)

_SHARED_PATTERNS = re.compile(
    r"(?i)(interface|dto|types?|contract|schema|config|constants?|"
    r"shared|common|base|abstract|proto|api[-_]?types?|models?)\."
    r"(ts|tsx|java|kt|cs|py|go|rs|graphql|prisma|d\.ts)$"
)


# ── Rang-Selektion: voller Import-Graph + PageRank VOR teurer Analyse ─────────

_IMPORT_SCAN_BYTES = 64_000


def _resolve_import_to_file(imp: str, source_file: str, file_set: set[str]) -> Optional[str]:


    if not imp:
        return None
    cand_dotted: str = ""
    if "." in imp and "/" not in imp and not imp.startswith("."):
        cand = imp.replace(".", "/")
    elif imp.startswith("."):
        source_dir = os.path.dirname(source_file).replace("\\", "/")
        parts = imp.split("/") if "/" in imp else [imp]
        if parts[0] == ".":
            candidate_base = source_dir
            rest = "/".join(parts[1:])
        elif parts[0] == "..":
            candidate_base = os.path.dirname(source_dir)
            rest = "/".join(parts[1:])
        else:
            dots = len(imp) - len(imp.lstrip("."))
            candidate_base = source_dir
            for _ in range(dots - 1):
                candidate_base = os.path.dirname(candidate_base)
            rest = imp.lstrip(".")
        if rest:
            cand = (candidate_base + "/" + rest).lstrip("/")
        else:
            cand = candidate_base
        if "." in rest and "/" not in rest:
            cand_dotted = (candidate_base + "/" + rest.replace(".", "/")).lstrip("/")
        else:
            cand_dotted = ""
    else:
        cand = imp.replace("\\", "/").lstrip("/")
        cand_dotted = ""

    for base in {cand, cand_dotted} if cand_dotted else {cand}:
        if not base:
            continue
        for ext in ("", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
                    ".java", ".cs", ".cpp", ".c", ".h", ".rb", ".php", ".kt", ".swift",
                    "/__init__.py", "/index.ts", "/index.js", "/index.py"):
            full = base + ext
            if full in file_set:
                return full
    return None


def _resolve_module_import(
    mod: str,
    names: list[str],
    source_file: str,
    file_set: set[str],
) -> Optional[str]:


    if not mod:
        return None
    # Relative Module: Basis aus Source-Dir + Dot-Count ableiten
    if mod.startswith("."):
        source_dir = os.path.dirname(source_file).replace("\\", "/")
        dots = len(mod) - len(mod.lstrip("."))
        base = source_dir
        for _ in range(dots - 1):
            base = os.path.dirname(base)
        rest = mod.lstrip(".")
        mod_path = (base + "/" + rest.replace(".", "/")).lstrip("/") if rest else base.lstrip("/")
    else:
        mod_path = mod.replace(".", "/").lstrip("/")

    mod_path = mod_path.rstrip("/")
    for ext in ("", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs",
                ".java", ".cs", ".cpp", ".c", ".h", ".rb", ".php", ".kt", ".swift"):
        if mod_path + ext in file_set:
            return mod_path + ext
    for entry in ("__init__.py", "index.ts", "index.js", "index.py"):
        if mod_path + "/" + entry in file_set:
            return mod_path + "/" + entry
    for n in names[:4]:
        if not n or n == "*":
            continue
        n = n.strip()
        for ext in ("", ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java",
                    ".cs", ".cpp", ".c", ".h", ".rb", ".php", ".kt", ".swift",
                    "/__init__.py", "/index.ts", "/index.js", "/index.py"):
            cand = f"{mod_path}/{n}" + ext
            if cand in file_set:
                return cand
    return None


def scan_import_graph(
    workspace_root: str,
    paths: list[str],
) -> tuple[dict[str, int], dict[int, list[int]]]:


    node_index: dict[str, int] = {}
    for p in paths:
        norm = p.replace("\\", "/")
        if norm not in node_index:
            node_index[norm] = len(node_index)
    file_set = set(node_index.keys())

    _from_import_re = re.compile(r"^\s*from\s+([\w.]+)\s+import\s+([^\n#]+)", re.MULTILINE)
    _import_mod_re = re.compile(r"^\s*import\s+([\w.]+)", re.MULTILINE)
    _es_import_re = re.compile(r"\bimport\s+\{[^}]*\}\s+from\s+['\"]([^'\"]+)['\"]")
    _import_str_re = re.compile(r"\bimport\s+['\"]([^'\"]+)['\"]")
    _require_re = re.compile(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)")

    out_edges: dict[int, list[int]] = {i: [] for i in range(len(node_index))}
    ws = Path(workspace_root)
    for src_norm in file_set:
        src_idx = node_index[src_norm]
        abs_path = ws / src_norm
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(_IMPORT_SCAN_BYTES)
        except OSError:
            continue

        def _add_edge(module: str, names: Optional[list[str]] = None) -> None:
            if not module:
                return
            if module.startswith(".") or "/" in module or "\\" in module:
                resolved = _resolve_module_import(module, names or [], src_norm, file_set)
            else:
                resolved = _resolve_import_to_file(module, src_norm, file_set)
            if resolved and resolved in node_index:
                tgt_idx = node_index[resolved]
                if tgt_idx != src_idx and tgt_idx not in out_edges[src_idx]:
                    out_edges[src_idx].append(tgt_idx)

        for m in _from_import_re.finditer(content):
            mod = m.group(1).strip()
            raw_names = m.group(2)
            names = [n.strip() for n in re.split(r"[,\s]+", raw_names) if n.strip()]
            _add_edge(mod, names)
        for m in _import_mod_re.finditer(content):
            _add_edge(m.group(1).strip())
        for m in _es_import_re.finditer(content):
            mod = m.group(1).strip()
            if mod.startswith(".") or "/" in mod:
                _add_edge(mod)
        for m in _import_str_re.finditer(content):
            mod = m.group(1).strip()
            if mod.startswith(".") or "/" in mod:
                _add_edge(mod)
        for m in _require_re.finditer(content):
            mod = m.group(1).strip()
            if mod.startswith(".") or "/" in mod:
                _add_edge(mod)

    return node_index, out_edges


def _pagerank_scores(
    node_index: dict[str, int],
    out_edges: dict[int, list[int]],
    damping: float = 0.85,
    iterations: int = 30,
) -> dict[str, float]:


    n = len(node_index)
    if n == 0:
        return {}
    if n == 1:
        return {name: 1.0 for name in node_index}

    dangling = [src for src, targets in out_edges.items() if not targets]
    r = [1.0 / n] * n
    for _ in range(iterations):
        dangling_sum = 0.0
        for src in dangling:
            dangling_sum += r[src]
        dangling_share = damping * dangling_sum / n
        teleport = (1.0 - damping) / n

        new_r = [teleport + dangling_share] * n
        for src, targets in out_edges.items():
            if targets:
                share = damping * r[src] / len(targets)
                for tgt in targets:
                    new_r[tgt] += share
        total = sum(new_r)
        if total > 0:
            new_r = [v / total for v in new_r]
        r = new_r

    return {name: r[idx] for name, idx in node_index.items()}


def rank_repo_paths(
    workspace_root: str,
    paths: list[str],
    stats: Optional[dict] = None,
) -> list[str]:


    if not paths:
        return []
    try:
        t_scan = time.perf_counter()
        node_index, out_edges = scan_import_graph(workspace_root, paths)
        t_scan = time.perf_counter() - t_scan
        if stats is not None:
            stats["scan_s"] = t_scan
        if not node_index:
            return list(paths)
        t_pr = time.perf_counter()
        scores = _pagerank_scores(node_index, out_edges)
        t_pr = time.perf_counter() - t_pr
        if stats is not None:
            stats["pagerank_s"] = t_pr
        return sorted(
            paths,
            key=lambda p: -scores.get(p.replace("\\", "/"), 0.0),
        )
    except Exception as _rank_exc:
        logger.warning("[RANK] import-graph ranking failed: %s", _rank_exc)
        return list(paths)


def select_analysis_window(
    workspace_root: str,
    max_files: int = _MAX_FILES,
    max_depth: int = _MAX_DEPTH,
) -> list[str]:


    t0 = time.perf_counter()
    all_paths = walk_repo_paths(workspace_root, max_depth=max_depth)
    t_walk = time.perf_counter() - t0

    if len(all_paths) <= max_files:
        logger.info(
            "[ANALYSIS-WINDOW] %d files <= window %d - no ranking needed (walk %.2fs)",
            len(all_paths), max_files, t_walk,
        )
        return all_paths

    _stats: dict = {}
    t1 = time.perf_counter()
    ranked = rank_repo_paths(workspace_root, all_paths, stats=_stats)
    t_rank = time.perf_counter() - t1
    window = ranked[:max_files]
    logger.info(
        "[ANALYSIS-WINDOW] full walk=%d files (walk %.2fs, scan %.2fs, pagerank %.2fs), "
        "window=%d — Selektion nach PageRank statt Walk-Reihenfolge",
        len(all_paths), t_walk,
        float(_stats.get("scan_s", 0.0)), float(_stats.get("pagerank_s", 0.0)),
        len(window),
    )
    return window


async def select_analysis_window_async(
    workspace_root: str,
    max_files: int = _MAX_FILES,
    max_depth: int = _MAX_DEPTH,
) -> list[str]:
    return await asyncio.to_thread(select_analysis_window, workspace_root, max_files, max_depth)


def _extract_ws_query(file_content: str, known_paths: set[str]) -> list[str]:
    if not file_content or len(file_content) < 20:
        return []

    seen: set[str] = set()
    candidates: list[str] = []
    for m in _IMPORT_RE.finditer(file_content):
        name = next((g for g in m.groups() if g), None)
        if not name:
            continue
        if _KNOWN_LOCAL_PREFIXES.match(name):
            continue
        if name.startswith((".", "/")):
            continue
        top = name.split(".")[0].split("/")[0].lstrip("@")
        if not top or top in seen:
            continue
        seen.add(top)
        candidates.append(top)

    if not candidates:
        return []

    unknown = [
        c for c in candidates
        if c.lower() not in _STDLIB_MODULES
        and not any(c.lower() in p.lower() for p in known_paths)
    ]

    return [f"{lib} documentation api" for lib in unknown]


# ── Contract-Summary-Format (TOML) ────────────────────────────────────────────

_TOML_BLOCK_RE = re.compile(r"```toml\s*([\s\S]*?)```", re.IGNORECASE)


def _regex_fallback_contract(raw: str) -> dict | None:
    import re
    def _str(pattern):
        m = re.search(pattern, raw)
        return m.group(1).strip() if m else ""
    def _arr(pattern):
        m = re.search(pattern, raw)
        if not m: return []
        return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]

    partition = _str(r'partition\s*=\s*["\']([^"\']*)["\']')
    if not partition:
        return None
    return {
        "partition":        partition,
        "role":             _str(r'role\s*=\s*["\']([^"\']*)["\']'),
        "files_read":       _arr(r'files_read\s*=\s*\[([^\]]*)\]'),
        "exports":          _arr(r'exports\s*=\s*\[([^\]]*)\]'),
        "imports_internal": _arr(r'imports_internal\s*=\s*\[([^\]]*)\]'),
        "imports_external": _arr(r'imports_external\s*=\s*\[([^\]]*)\]'),
        "entry_points":     _arr(r'entry_points\s*=\s*\[([^\]]*)\]'),
        "touched_by_task":  _str(r'touched_by_task\s*=\s*["\']([^"\']*)["\']') or "unknown",
        "complexity_score": float(re.search(r'complexity_score\s*=\s*(\d+\.?\d*)', raw).group(1) if re.search(r'complexity_score\s*=\s*(\d+\.?\d*)', raw) else 0.5),
        "hint":             _str(r'hint\s*=\s*["\']([^"\']*)["\']'),
        "data_flow":        _str(r'data_flow\s*=\s*["\']([^"\']*)["\']'),
        "config":           _str(r'config\s*=\s*["\']([^"\']*)["\']'),
        "sibling_partitions": _arr(r'sibling_partitions\s*=\s*\[([^\]]*)\]'),
        "shared_dirs":      _arr(r'shared_dirs\s*=\s*\[([^\]]*)\]'),
        "plan_steps":       [],
        "plan":             "",
        "imports_needed":   [],
        "_fallback":        True,
    }


def parse_contract_summary(text: str) -> list[dict]:


    if not text:
        return []

    if tomllib is None:
        logger.warning("tomllib not available - TOML parsing disabled (pip install tomli for Python <3.11)")
        return []

    _block_count = 0
    _fail_count = 0
    results = []

    for m in _TOML_BLOCK_RE.finditer(text):
        _block_count += 1
        raw = m.group(1).strip()
        raw = raw.replace("\\", "/")
        try:
            parsed = tomllib.loads(raw)
        except Exception as e:
            _fail_count += 1
            logger.warning("parse_contract_summary TOML error (block %d): %s", _block_count, str(e)[:200])
            fallback = _regex_fallback_contract(raw)
            if fallback:
                logger.warning("parse_contract_summary: block %d recovered via regex fallback (partition=%s)", _block_count, fallback["partition"])
                results.append(fallback)
            continue

        c = parsed.get("contract", {})
        if not isinstance(c, dict) or not c:
            if "partition" in parsed:
                c = parsed
            else:
                logger.warning("parse_contract_summary: block %d - empty contract dict, skipped", _block_count)
                continue

        plan_steps = parsed.get("plan", [])
        if not isinstance(plan_steps, list):
            plan_steps = []
        for ps in plan_steps:
            if isinstance(ps, dict):
                if "files" in ps and "file" not in ps:
                    _flist = ps["files"]
                    if isinstance(_flist, list):
                        ps["file"] = ", ".join(str(f) for f in _flist)
                    elif isinstance(_flist, str):
                        ps["file"] = _flist
                elif "file" in ps and "files" not in ps:
                    ps["files"] = [ps["file"]]

        plan_str = "\n".join(
            f"{s.get('step','?')!s}. [{s.get('file','')}] {s.get('action','')}"
            for s in plan_steps
            if isinstance(s, dict)
        )
        
        _tbt = c.get("touched_by_task", "unknown")
        if isinstance(_tbt, bool):
            _tbt = "yes" if _tbt else "unlikely"
        elif isinstance(_tbt, str):
            _tbt = _tbt.lower()
            if _tbt not in ("yes", "unlikely", "unknown"):
                _tbt = "unknown"
        else:
            _tbt = "unknown"

        _required = ["partition", "files_read"]
        _missing_fields = [k for k in _required if not c.get(k)]
        if _missing_fields:
            logger.warning(
                "parse_contract_summary: Contract '%s' missing fields: %s",
                c.get("partition", "?"), ", ".join(_missing_fields)
            )

        results.append({
            "partition":          c.get("partition", ""),
            "role":               c.get("role", ""),                # NEU: Architektonische Rolle
            "entry_points":       c.get("entry_points", []),
            "files_read":         c.get("files_read", []),
            "exports":            c.get("exports", []),
            "imports_internal":   c.get("imports_internal", []),
            "imports_external":   c.get("imports_external", []),    # NEU: Externe Libraries
            "imports_needed":     c.get("imports_needed", c.get("imports_internal", [])),  # Backward-Compat
            "data_flow":          c.get("data_flow", ""),           # NEU: Datenfluss
            "config":             c.get("config", ""),
            "touched_by_task":    _tbt,
            "complexity_score":   float(c.get("complexity_score", 0.5)),
            "hint":               c.get("hint", ""),
            "plan_steps":         plan_steps,
            "plan":               plan_str,
            "sibling_partitions": c.get("sibling_partitions", []),
            "shared_dirs":        c.get("shared_dirs", []),
            "_fallback":          c.get("_fallback", False),
        })

    if _block_count > 0 and not results:
        logger.warning(
            "parse_contract_summary: ALL %d TOML blocks malformed - "
            "Planner has no contracts for this partition.",
            _block_count
        )
    elif _fail_count > 0:
        logger.warning(
            "parse_contract_summary: %d/%d TOML blocks malformed - "
            "Planner has reduced contracts.",
            _fail_count, _block_count
        )

    return results


def build_contract_prompt(partition: dict, all_contracts: list[dict]) -> str:
    own_label    = partition.get("partition", "")
    entry_points = partition.get("entry_points", [])
    hint         = partition.get("hint", "")
    plan_steps   = partition.get("plan_steps", [])
    own_plan     = partition.get("plan", "")

    other_parts: list[str] = []
    for c in all_contracts:
        if c.get("partition") == own_label:
            continue
        label   = c.get("partition", "?")
        exports = c.get("exports", [])
        ep      = c.get("entry_points", [])
        ch      = c.get("hint", "")
        line = f"  [{label}]"
        if exports:
            line += f" exports: {', '.join(exports)}"
        if ep:
            line += (f" | entry: {ep[0]}" if len(ep) == 1
                     else f" | entries: {', '.join(ep)}")
        if ch:
            line += f"\n    hint: {ch}"
        other_parts.append(line)

    parts: list[str] = []

    if entry_points:
        parts.append(
            f"## Einstiegspunkte ({own_label})\n"
            + "\n".join(f"  - {e}" for e in entry_points)
        )

    if hint:
        parts.append(f"## Note\n{hint}")

    if plan_steps:
        steps_txt = "\n".join(
            f"  {s.get('step','?')!s}. [{s.get('file','')}] {s.get('action','')}"
            for s in plan_steps
            if isinstance(s, dict)
        )
        parts.append(f"## Implementierungsplan ({own_label})\n{steps_txt}")
    elif own_plan:
        parts.append(f"## Implementierungsplan ({own_label})\n{own_plan}")

    if other_parts:
        parts.append(
            "## Contracts anderer Partitionen\n"
            "For OTHER partitions: use their exports and contracts. Do NOT read their source files.\n"
            "For YOUR OWN files: read them before editing unless their content is already provided in context.\n"
            + "\n".join(other_parts)
        )

    return "\n\n".join(parts)