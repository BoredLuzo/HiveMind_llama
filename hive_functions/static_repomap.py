"""
static_repomap.py — Deterministic Static Repo-Map
===================================================
Pure code-analysis (no LLM) — runs parallel to LLM contracts.
Produces a token-budgeted, per-partition symbol + import map.

Reuses existing HiveMind tools:
  - ast_tools.get_signatures_report()  — symbol extraction (Python AST, JS/TS regex)
  - tree_scout._IMPORT_RE              — multi-language import regex
  - tree_scout._SKIP_DIRS              — directory filtering
  - chunking.build_file_signature()    — cache invalidation
"""
from __future__ import annotations


import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hivemind.static_repomap")

from hive_functions.tree_scout import (
    _pagerank_scores, _resolve_import_to_file,
)

_CACHE: dict[str, dict] = {}
_CACHE_MAX = 12
_CACHE_TTL = 120.0

_RELEVANT_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs",
    ".java", ".go", ".rs", ".cpp", ".c", ".h", ".cs", ".kt",
    ".rb", ".php", ".swift", ".vue", ".svelte",
    ".json", ".yaml", ".yml", ".toml",
    ".html", ".css", ".scss", ".sql", ".sh",
}
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".tar",
    ".exe", ".dll", ".so", ".dylib", ".o", ".obj", ".class", ".jar",
    ".pyc", ".pyo", ".pyd", ".whl", ".lock", ".map", ".min.css", ".min.js",
    ".mp3", ".mp4", ".avi", ".mov", ".wav", ".ogg", ".webm",
    ".iso", ".img", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
}

_SKIP_DIRS = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".idea", ".vscode", ".mypy_cache",
    ".pytest_cache", ".next", ".nuxt", "coverage", ".tox",
}


@dataclass
class FileInfo:
    rel_path: str
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    size: int = 0


@dataclass
class PartitionMap:
    label: str
    files: list[FileInfo] = field(default_factory=list)


_WS_RE = re.compile(r"\s+")
_SIG_TAIL_RE = re.compile(r"(\([^)]*\))(?:\s*(->\s*[^:{=]+|:\s*[^={]+))?")


def _compact_params(raw: str, max_len: int = 72) -> str:
    s = _WS_RE.sub(" ", raw or "").strip()
    if not s:
        return ""
    if not s.startswith("("):
        s = f"({s})"
    if len(s) > max_len:
        s = s[: max_len - 2].rstrip(", ") + "…)"
    return s


def _compact_return(raw: str, max_len: int = 32) -> str:
    s = _WS_RE.sub(" ", raw or "").strip()
    if not s:
        return ""
    if s.startswith("->"):
        s = s[2:].strip()
    elif s.startswith(":"):
        s = s[1:].strip()
    s = s.rstrip("{;").strip()
    if not s:
        return ""
    if len(s) > max_len:
        s = s[: max_len - 1].rstrip() + "…"
    return f" -> {s}"


def _extract_symbols_from_report(report: str) -> list[str]:
    lines = report.splitlines()
    classes: list[str] = []
    functions: list[str] = []
    methods: list[str] = []
    variables: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[signatures]") or stripped.startswith("language="):
            continue
        if stripped.startswith("(no ") or stripped.startswith("[get_signatures") or stripped.startswith("[summary"):
            continue
        tag = stripped.split()[0].lower() if stripped.split() else ""
        colon_pos = stripped.find(": ")
        head = stripped[:colon_pos] if colon_pos > 0 else stripped
        tail = stripped[colon_pos + 2:].strip() if colon_pos > 0 else ""

        formatted = head
        if tail and tag in ("function", "def", "async", "method", "class"):
            m_sig = _SIG_TAIL_RE.search(tail)
            if m_sig:
                params = _compact_params(m_sig.group(1))
                ret = _compact_return(m_sig.group(2) or "") if tag != "class" else ""
                if " @L" in head:
                    prefix, loc = head.rsplit(" @L", 1)
                    formatted = f"{prefix}{params}{ret} @L{loc}"
                else:
                    formatted = f"{head}{params}{ret}"

        if tag == "class":
            classes.append(formatted)
        elif tag in ("function", "def", "async"):
            functions.append(formatted)
        elif tag == "method":
            methods.append(formatted)
        elif tag == "variable":
            variables.append(head)
        elif head.startswith("L") and ":" in stripped:
            functions.append(stripped[:110])
        else:
            functions.append(formatted[:110])
    return classes + functions + methods + variables[:4]


def _extract_imports(file_content: str) -> list[str]:
    from hive_functions.tree_scout import _IMPORT_RE
    seen: set[str] = set()
    imports: list[str] = []
    for m in _IMPORT_RE.finditer(file_content):
        name = next((g for g in m.groups() if g), None)
        if not name:
            continue
        if name not in seen:
            seen.add(name)
            imports.append(name)
    return imports


_TS_LANG_BY_EXT: dict[str, str] = {
    ".py": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".cs": "csharp",
    ".php": "php",
    ".rb": "ruby",
    ".swift": "swift",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".kt": "kotlin", ".kts": "kotlin",
}

_TS_DEF_TYPES: dict[str, dict[str, str]] = {
    "python":     {"class_definition": "class", "function_definition": "func"},
    "javascript": {"class_declaration": "class", "function_declaration": "func",
                   "generator_function_declaration": "func", "method_definition": "func"},
    "typescript": {"class_declaration": "class", "abstract_class_declaration": "class",
                   "interface_declaration": "type", "type_alias_declaration": "type",
                   "enum_declaration": "type", "function_declaration": "func",
                   "generator_function_declaration": "func", "method_definition": "func",
                   "method_signature": "func"},
    "tsx":        {"class_declaration": "class", "interface_declaration": "type",
                   "type_alias_declaration": "type", "enum_declaration": "type",
                   "function_declaration": "func", "method_definition": "func",
                   "method_signature": "func"},
    "go":         {"function_declaration": "func", "method_declaration": "func",
                   "type_declaration": "type"},
    "rust":       {"function_item": "func", "struct_item": "type", "enum_item": "type",
                   "trait_item": "type", "impl_item": "type", "mod_item": "mod",
                   "const_item": "const", "static_item": "const", "type_item": "type"},
    "java":       {"class_declaration": "class", "interface_declaration": "type",
                   "enum_declaration": "type", "record_declaration": "type",
                   "annotation_type_declaration": "type", "method_declaration": "func"},
    "csharp":     {"class_declaration": "class", "interface_declaration": "type",
                   "struct_declaration": "type", "enum_declaration": "type",
                   "record_declaration": "type", "method_declaration": "func"},
    "php":        {"function_definition": "func", "class_declaration": "class",
                   "interface_declaration": "type", "trait_declaration": "type",
                   "method_declaration": "func"},
    "ruby":       {"class": "class", "module": "mod", "method": "func",
                   "singleton_method": "func"},
    "swift":      {"class_declaration": "class", "struct_declaration": "type",
                   "enum_declaration": "type", "protocol_declaration": "type",
                   "function_declaration": "func"},
    "c":          {"function_definition": "func", "struct_specifier": "type",
                   "union_specifier": "type", "enum_specifier": "type"},
    "cpp":        {"function_definition": "func", "class_specifier": "class",
                   "struct_specifier": "type", "enum_specifier": "type",
                   "namespace_definition": "mod"},
    "kotlin":     {"class_declaration": "class", "function_declaration": "func",
                   "object_declaration": "class"},
}

_TS_SYMBOL_CAP = 200


_TS_JS_LANGS = frozenset({"javascript", "typescript", "tsx"})
_TS_FUNC_VALUE_TYPES = frozenset({"function_expression", "arrow_function", "generator_function"})
_TS_EXPORT_PREFIXES = ("window.", "globalThis.", "self.", "module.exports", "exports.")


def _ts_text(node) -> str:
    if node is None:
        return ""
    return node.text.decode("utf-8", "replace").strip()


def _ts_func_sig(node) -> tuple[str, str]:
    """Return compact (params, return_type) strings for a function/method node."""
    params_node = (
        node.child_by_field_name("parameters")
        or node.child_by_field_name("formal_parameters")
        or node.child_by_field_name("parameter")
    )
    params = _compact_params(_ts_text(params_node)) if params_node is not None else "()"
    ret_node = node.child_by_field_name("return_type") or node.child_by_field_name("result")
    ret = _compact_return(_ts_text(ret_node)) if ret_node is not None else ""
    return params, ret


def _ts_cpp_func_name_and_params(node) -> tuple[str, str]:
    """Unwrap C/C++ function_definition declarator to (name, compact_params)."""
    decl = node.child_by_field_name("declarator")
    for _ in range(6):
        if decl is None:
            return "", ""
        if decl.type == "function_declarator":
            inner = decl.child_by_field_name("declarator")
            params_node = decl.child_by_field_name("parameters")
            return _ts_text(inner), _compact_params(_ts_text(params_node))
        decl = decl.child_by_field_name("declarator")
    return "", ""


def _ts_is_iife(node) -> bool:
    """True if node is a JS/TS IIFE like `(function(){...})()` or `(() => {...})()`."""
    if node.type not in _TS_FUNC_VALUE_TYPES:
        return False
    cur = node.parent
    while cur is not None and cur.type == "parenthesized_expression":
        cur = cur.parent
    return cur is not None and cur.type == "call_expression" and cur.child_by_field_name("function") == (
        node.parent if node.parent is not None and node.parent.type == "parenthesized_expression" else node
    )


def _ts_object_keys(obj_node, max_keys: int = 6) -> list[str]:
    keys: list[str] = []
    for ch in obj_node.children:
        if ch.type in ("pair", "shorthand_property_identifier", "method_definition"):
            knode = ch.child_by_field_name("key") or ch.child_by_field_name("name") or ch
            k = _ts_text(knode).strip("'\"")
            if k and k not in keys:
                keys.append(k)
                if len(keys) >= max_keys:
                    break
    return keys


def _extract_symbols_tree_sitter(
    abs_path: str,
    raw_bytes: Optional[bytes] = None,
) -> Optional[list[str]]:

    ext = os.path.splitext(abs_path)[1].lower()
    lang = _TS_LANG_BY_EXT.get(ext)
    if lang is None:
        return None

    try:
        from tree_sitter_language_pack import get_parser
        parser = get_parser(lang)
        if raw_bytes is None:
            with open(abs_path, "rb") as f:
                content = f.read(512_000)
        else:
            content = raw_bytes[:512_000]
        tree = parser.parse(content)
    except Exception:
        return None

    def_types = _TS_DEF_TYPES.get(lang, {})
    if not def_types:
        return None

    symbols: list[str] = []
    seen: set[str] = set()
    is_js = lang in _TS_JS_LANGS

    def _add(entry: str) -> None:
        if entry and entry not in seen and len(symbols) < _TS_SYMBOL_CAP:
            seen.add(entry)
            symbols.append(entry)

    def _walk(node, enclosing_class: Optional[str] = None, fn_depth: int = 0) -> None:
        if len(symbols) >= _TS_SYMBOL_CAP:
            return
        ntype = node.type
        lineno = node.start_point[0] + 1
        next_class = enclosing_class
        next_depth = fn_depth

        if ntype in def_types:
            kind = def_types[ntype]
            if kind == "func" and lang in ("c", "cpp") and ntype == "function_definition":
                name, params = _ts_cpp_func_name_and_params(node)
                if name and fn_depth == 0:
                    qual = f"{enclosing_class}.{name}" if enclosing_class and "::" not in name else name
                    label = "method" if enclosing_class else "func"
                    _add(f"{label} {qual}{params} @L{lineno}")
                next_depth = fn_depth + 1
            else:
                name_node = node.child_by_field_name("name")
                name = _ts_text(name_node)
                if name:
                    if kind == "class":
                        _add(f"class {name} @L{lineno}")
                        next_class = name
                        next_depth = 0
                    elif kind == "func":
                        if fn_depth == 0:
                            params, ret = _ts_func_sig(node)
                            if enclosing_class:
                                _add(f"method {enclosing_class}.{name}{params}{ret} @L{lineno}")
                            else:
                                _add(f"func {name}{params}{ret} @L{lineno}")
                        next_depth = fn_depth + 1
                    else:
                        if fn_depth == 0:
                            _add(f"{kind} {name} @L{lineno}")
        elif is_js and fn_depth == 0 and ntype == "variable_declarator":
            name = _ts_text(node.child_by_field_name("name"))
            val = node.child_by_field_name("value")
            if name and val is not None:
                if val.type in _TS_FUNC_VALUE_TYPES:
                    params, ret = _ts_func_sig(val)
                    _add(f"func {name}{params}{ret} @L{lineno}")
                    return  # don't walk into function body for nested noise
                if val.type == "class":
                    _add(f"class {name} @L{lineno}")
                    next_class = name
        elif is_js and fn_depth == 0 and ntype == "assignment_expression":
            left = _ts_text(node.child_by_field_name("left"))
            right = node.child_by_field_name("right")
            if left and right is not None:
                if right.type in _TS_FUNC_VALUE_TYPES:
                    params, ret = _ts_func_sig(right)
                    if ".prototype." in left:
                        qual = left.replace(".prototype.", ".")
                        _add(f"method {qual}{params}{ret} @L{lineno}")
                        return
                    if any(left.startswith(p) for p in _TS_EXPORT_PREFIXES):
                        _add(f"func {left}{params}{ret} @L{lineno}")
                        return
                elif any(left.startswith(p) for p in _TS_EXPORT_PREFIXES):
                    if right.type == "object":
                        keys = _ts_object_keys(right)
                        kstr = f" {{{', '.join(keys)}}}" if keys else ""
                        _add(f"export {left}{kstr} @L{lineno}")
                    elif right.type in ("identifier", "member_expression"):
                        _add(f"export {left} = {_ts_text(right)} @L{lineno}")
                    return
        elif is_js and ntype in _TS_FUNC_VALUE_TYPES:
            if not _ts_is_iife(node):
                next_depth = fn_depth + 1

        for child in node.children:
            _walk(child, next_class, next_depth)
            if len(symbols) >= _TS_SYMBOL_CAP:
                return

    try:
        _walk(tree.root_node)
    except Exception:
        return None

    return symbols or None


def _analyze_file(abs_path: str, rel_path: str) -> Optional[FileInfo]:
    try:
        size = os.path.getsize(abs_path)
    except OSError:
        return None
    if size == 0 or size > 512_000:
        return None

    try:
        with open(abs_path, "rb") as _bf:
            _raw = _bf.read(512_000)
        if b"\x00" in _raw[:4096]:
            return None
    except Exception:
        return None

    symbols = _extract_symbols_tree_sitter(abs_path, _raw)
    if symbols is None:
        from hive_functions.hivemind_feature.ast_tools import get_signatures_report
        report = get_signatures_report(abs_path)
        symbols = _extract_symbols_from_report(report)

    content = _raw[:32_000].decode("utf-8", errors="replace")

    imports = _extract_imports(content)

    return FileInfo(
        rel_path=rel_path,
        symbols=symbols,
        imports=imports,
        size=size,
    )


def _analyze_partition(partition: dict, workspace_root: str) -> PartitionMap:
    label = partition.get("label", "?")
    paths = partition.get("paths", [])
    pm = PartitionMap(label=label)

    for rel_path in paths:
        ext = os.path.splitext(rel_path)[1].lower()
        if ext in _BINARY_EXTS:
            continue
        abs_path = os.path.join(workspace_root, rel_path)
        if not os.path.isfile(abs_path):
            continue
        fi = _analyze_file(abs_path, rel_path)
        if fi is not None:
            pm.files.append(fi)

    return pm


def _build_cross_partition_deps(
    partition_maps: list[PartitionMap],
) -> dict[str, set[str]]:
    file_to_partition: dict[str, str] = {}
    all_files: set[str] = set()
    for pm in partition_maps:
        for fi in pm.files:
            norm = fi.rel_path.replace("\\", "/")
            file_to_partition[norm] = pm.label
            all_files.add(norm)

    edges: dict[str, set[str]] = defaultdict(set)
    for pm in partition_maps:
        for fi in pm.files:
            source = fi.rel_path.replace("\\", "/")
            for imp in fi.imports:
                resolved = _resolve_import_to_file(imp, source, all_files)
                if resolved and resolved in file_to_partition:
                    target_label = file_to_partition[resolved]
                    if target_label != pm.label:
                        edges[pm.label].add(target_label)
    return dict(edges)


def _build_file_graph(
    partition_maps: list[PartitionMap],
) -> tuple[dict[str, int], dict[int, list[int]]]:
    """File import graph across ALL partitions.

    Nodes = normalized file paths, edges = resolved imports
    (source -> target). Rueckgabe: (path->index, index->[target-indices]).
    """
    node_index: dict[str, int] = {}
    all_files: list[FileInfo] = []
    for pm in partition_maps:
        all_files.extend(pm.files)

    for fi in all_files:
        norm = fi.rel_path.replace("\\", "/")
        if norm not in node_index:
            node_index[norm] = len(node_index)

    file_set = set(node_index.keys())
    out_edges: dict[int, list[int]] = {i: [] for i in range(len(node_index))}
    for fi in all_files:
        source = fi.rel_path.replace("\\", "/")
        src_idx = node_index[source]
        for imp in fi.imports:
            resolved = _resolve_import_to_file(imp, source, file_set)
            if resolved and resolved in node_index:
                tgt_idx = node_index[resolved]
                if tgt_idx != src_idx and tgt_idx not in out_edges[src_idx]:
                    out_edges[src_idx].append(tgt_idx)
    return node_index, out_edges


def _rank_files(files: list[FileInfo], scores: dict[str, float] | None = None) -> list[FileInfo]:


    if scores is not None:
        def _key(fi: FileInfo) -> tuple[float, int]:
            norm = fi.rel_path.replace("\\", "/")
            return (-scores.get(norm, 0.0), -len(fi.symbols))
        return sorted(files, key=_key)

    def score(fi: FileInfo) -> float:
        sym_count = len(fi.symbols)
        imp_count = len(fi.imports)
        size_score = min(fi.size / 5000.0, 2.0)
        return sym_count * 1.5 + imp_count * 2.0 + size_score
    return sorted(files, key=score, reverse=True)


def _format_repomap(
    partition_maps: list[PartitionMap],
    cross_deps: dict[str, set[str]],
    char_budget: int = 6000,
    scores: dict[str, float] | None = None,
    stats: dict | None = None,
) -> str:
    lines: list[str] = ["## Static Repo-Map (deterministic, ground truth)\n"]
    used = len(lines[0]) + 1

    _st = stats if stats is not None else {}
    _st["files_included"] = 0
    _st["files_truncated"] = 0
    _st["partitions"] = 0

    budget_per_partition = max(
        500,
        (char_budget - 200) // max(1, len(partition_maps)),
    )

    for pm in partition_maps:
        if not pm.files:
            continue

        total_symbols = sum(len(f.symbols) for f in pm.files)
        header = f"### {pm.label} ({len(pm.files)} files, {total_symbols} symbols)"
        if used + len(header) + 2 > char_budget:
            _st["files_truncated"] += len(pm.files)
            continue
        lines.append(header)
        used += len(header) + 1
        _st["partitions"] += 1

        partition_used = 0
        ranked = _rank_files(pm.files, scores)
        for fi in ranked:
            file_line = fi.rel_path
            entry_lines = [file_line]
            for sym in fi.symbols[:16]:
                entry_lines.append(f"  {sym}")
            if len(fi.symbols) > 16:
                entry_lines.append(f"  ... (+{len(fi.symbols) - 16} more symbols)")
            if fi.imports:
                imp_str = ", ".join(fi.imports[:8])
                entry_lines.append(f"  imports: {imp_str}")

            entry_text = "\n".join(entry_lines)
            entry_cost = len(entry_text) + 1

            if used + entry_cost > char_budget or partition_used + entry_cost > budget_per_partition:
                remaining = len(ranked) - ranked.index(fi)
                if remaining > 0:
                    skip_line = f"  ... ({remaining} more files)"
                    lines.append(skip_line)
                    used += len(skip_line) + 1
                    _st["files_truncated"] += remaining
                break

            lines.append(entry_text)
            used += entry_cost
            partition_used += entry_cost
            _st["files_included"] += 1

        lines.append("")
        used += 1

    if cross_deps:
        dep_header = "### Cross-Partition Dependencies"
        if used + len(dep_header) + 10 < char_budget:
            lines.append(dep_header)
            used += len(dep_header) + 1
            for src, targets in sorted(cross_deps.items()):
                dep_line = f"  {src} → {', '.join(sorted(targets))}"
                if used + len(dep_line) + 1 > char_budget:
                    break
                lines.append(dep_line)
                used += len(dep_line) + 1
            lines.append("")

    return "\n".join(lines)


def _cache_key(workspace_root: str, partitions: list[dict]) -> str:
    h = hashlib.md5()
    h.update(workspace_root.encode())
    for p in partitions:
        h.update(p.get("label", "").encode())
        for fp in p.get("paths", [])[:50]:
            h.update(fp.encode())
    return h.hexdigest()


def _cache_valid(entry: dict, workspace_root: str) -> bool:
    if time.time() - entry.get("ts", 0) > _CACHE_TTL:
        return False
    from hive_functions.chunking import file_signature_matches
    for rel, sig in (entry.get("sigs") or {}).items():
        abs_path = os.path.join(workspace_root, rel)
        if not file_signature_matches(abs_path, sig, allow_missing=True):
            return False
    return True


_CONFIG_FILES: tuple[str, ...] = (
    "README.md", "readme.md", "README", "Readme.md",
    "docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
    "package.json", "pyproject.toml", "requirements.txt",
    "Dockerfile", "Makefile", "tsconfig.json", "go.mod", "Cargo.toml",
)


def _json_digest(raw: str) -> str:
    """Compact digest for JSON configs (package.json/tsconfig.json)."""
    try:
        data = json.loads(raw)
    except Exception:
        return raw.strip()
    if not isinstance(data, dict):
        return raw.strip()
    parts: list[str] = []
    for _k in ("name", "version", "description"):
        if data.get(_k):
            parts.append(f"{_k}={data[_k]}")
    _scripts = data.get("scripts")
    if isinstance(_scripts, dict) and _scripts:
        parts.append("scripts=" + ",".join(sorted(_scripts.keys())[:10]))
    _deps = data.get("dependencies")
    if isinstance(_deps, dict) and _deps:
        parts.append("deps=" + ",".join(sorted(_deps.keys())[:12]))
    _dev = data.get("devDependencies")
    if isinstance(_dev, dict) and _dev:
        parts.append("devDeps=" + ",".join(sorted(_dev.keys())[:8]))
    if parts:
        return "; ".join(parts)
    return raw.strip()


def _config_summary(workspace_root: str, char_budget: int = 1600) -> str:
    root = Path(workspace_root)
    lines: list[str] = ["## Config / Docs (ground truth)"]
    used = len(lines[0]) + 1
    _seen: set[str] = set()
    for _fname in _CONFIG_FILES:
        _fp = root / _fname
        if not _fp.is_file():
            continue
        # Case-insensitive Dateisysteme (Windows): README.md == readme.md.
        _resolved = str(_fp.resolve()).lower()
        if _resolved in _seen:
            continue
        _seen.add(_resolved)
        try:
            _raw = _fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _raw.strip():
            continue
        if _fname.lower().endswith(".json"):
            _summary = _json_digest(_raw)
        else:
            _summary = _raw.strip()
        _max_len = max(80, char_budget - used - len(_fname) - 10)
        if len(_summary) > _max_len:
            _summary = _summary[:_max_len] + "\n…"
        _entry = f"### {_fname}:\n{_summary}"
        lines.append(_entry)
        used += len(_entry) + 1
        if used >= char_budget:
            break
    if len(lines) == 1:
        return ""
    return "\n".join(lines)


def _build_sync(
    workspace_root: str,
    partitions: list[dict],
    char_budget: int = 6000,
) -> str:
    t0 = time.perf_counter()

    partition_maps: list[PartitionMap] = []
    for p in partitions:
        pm = _analyze_partition(p, workspace_root)
        partition_maps.append(pm)

    cross_deps = _build_cross_partition_deps(partition_maps)

    node_index, out_edges = _build_file_graph(partition_maps)
    scores = _pagerank_scores(node_index, out_edges)

    stats: dict = {}
    result = _format_repomap(partition_maps, cross_deps, char_budget=char_budget, scores=scores, stats=stats)

    _cfg = _config_summary(workspace_root, char_budget=1600)
    if _cfg:
        result = result.rstrip() + "\n\n" + _cfg

    elapsed = time.perf_counter() - t0
    total_files = sum(len(pm.files) for pm in partition_maps)
    total_symbols = sum(len(f.symbols) for pm in partition_maps for f in pm.files)
    _inc = int(stats.get("files_included", 0))
    _trunc = int(stats.get("files_truncated", 0))
    _density = (len(result) / _inc) if _inc else 0.0
    logger.info(
        "[STATIC-REPO-MAP] Built in %.2fs — %d files, %d symbols, %d chars (budget: %d) | "
        "included=%d, truncated=%d, ~%.1f chars/file",
        elapsed, total_files, total_symbols, len(result), char_budget,
        _inc, _trunc, _density,
    )
    return result


async def build_static_repomap(
    workspace_root: str,
    partitions: list[dict],
    char_budget: int = 6000,
) -> str:
    if not partitions or not workspace_root:
        return ""

    cache_k = _cache_key(workspace_root, partitions)
    cached = _CACHE.get(cache_k)
    if cached and _cache_valid(cached, workspace_root):
        logger.info("[STATIC-REPO-MAP] cache hit")
        return cached["output"]

    try:
        result = await asyncio.to_thread(
            _build_sync, workspace_root, partitions, char_budget,
        )
    except Exception as e:
        logger.warning("[STATIC-REPO-MAP] build failed: %s", e)
        return ""

    if not result:
        return ""

    from hive_functions.chunking import build_file_signature
    sigs: dict[str, dict] = {}
    for p in partitions:
        for rel in p.get("paths", [])[:30]:
            abs_path = os.path.join(workspace_root, rel)
            sig = build_file_signature(abs_path)
            if sig:
                sigs[rel] = sig

    if len(_CACHE) >= _CACHE_MAX:
        oldest = min(_CACHE, key=lambda k: _CACHE[k].get("ts", 0))
        del _CACHE[oldest]

    _CACHE[cache_k] = {"output": result, "sigs": sigs, "ts": time.time()}

    return result
