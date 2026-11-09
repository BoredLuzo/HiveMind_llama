# -*- coding: utf-8 -*-
"""Incremental per-workspace repo index (D2 context engine, 2026-11-09).

stdlib only. One small index per workspace: top-level text/code files with
their mtime+size signature and top-level symbols. `refresh()` is incremental
— a file whose mtime_ns+size is unchanged keeps its extracted symbols (the
only expensive step: content read + ast parse), vanished files are dropped,
new/changed files are re-extracted. The walk never recurses (Tree-Scout and
the static repomap already cover deep repos) and never indexes more than
`max_files` entries, so a huge repo cannot make this scan itself expensive.

`search(query, limit)` ranks entries by token overlap between the task query
and three per-file token sets: file stem (weight 4), extracted symbols
(weight 2) and the full relative path (weight 1). Only positive scores are
returned — a query that matches nothing yields an empty list, and callers
(duo_helpers.build_preseed_context) then skip the pre-seed instead of
inlining four arbitrary files.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path

# Text/code suffixes worth indexing (same family as the pre-seed set).
TEXT_SUFFIXES = frozenset({
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".toml",
    ".yaml", ".yml", ".md", ".txt", ".html", ".css", ".sh",
})

# Cheap JS/TS symbol extraction — no tree-sitter. Good enough for retrieval
# ranking; never used for guards or edits.
_JS_SYMBOL_RES = (
    re.compile(r"\b(?:function|class|interface|enum)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=(?!=)"),
)

_MAX_SYMBOLS_PER_FILE = 64
_DEFAULT_MAX_FILES = 2000
_MAX_CACHED_INDEXES = 8

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
_STOP_TOKENS = frozenset({
    "the", "and", "for", "with", "from", "that", "this", "not", "are",
    "will", "can", "has", "was", "all", "but", "its", "new", "use", "one",
    "two", "any", "may", "set", "get", "put", "add", "run", "let", "see",
    "file", "files", "into", "code", "def", "func", "const",
})


def _tokens(text: str) -> frozenset[str]:
    return frozenset(
        t.lower() for t in _TOKEN_RE.findall(text or "")
        if t.lower() not in _STOP_TOKENS
    )


def _py_symbols(src: str) -> list[str]:
    """Top-level def/class/assign/import names of a .py file (best effort)."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, TypeError):
        return []
    names: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.append(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name != "*":
                    names.append(alias.asname or alias.name.split(".")[0])
        if len(names) >= _MAX_SYMBOLS_PER_FILE:
            break
    return names[:_MAX_SYMBOLS_PER_FILE]


def _js_symbols(src: str) -> list[str]:
    """Cheap regex symbols for .js/.ts/.jsx/.tsx (declarations + bindings)."""
    seen: list[str] = []
    for rx in _JS_SYMBOL_RES:
        for m in rx.finditer(src or ""):
            name = m.group(1)
            if name and name not in seen:
                seen.append(name)
                if len(seen) >= _MAX_SYMBOLS_PER_FILE:
                    return seen
    return seen


class _Entry:
    """One indexed file: stat signature + cached retrieval tokens."""

    __slots__ = ("rel", "mtime_ns", "size", "symbols",
                 "stem_tokens", "path_tokens", "symbol_tokens")

    def __init__(self, rel: str, mtime_ns: int, size: int, symbols: list[str]):
        self.rel = rel
        self.mtime_ns = mtime_ns
        self.size = size
        self.symbols = symbols
        stem = rel.rsplit("/", 1)[-1]
        stem_no_ext = stem.rsplit(".", 1)[0] if "." in stem else stem
        self.stem_tokens = _tokens(stem_no_ext)
        self.path_tokens = _tokens(rel.replace("/", " ").replace("_", " ")
                                   .replace("-", " ").replace(".", " "))
        self.symbol_tokens = frozenset(s.lower() for s in symbols)

    def score(self, qtokens: frozenset[str]) -> int:
        if not qtokens:
            return 0
        s = 0
        hit = qtokens & self.stem_tokens
        if hit:
            s += 4 * len(hit)
        hit = qtokens & self.symbol_tokens
        if hit:
            s += 2 * len(hit)
        hit = qtokens & self.path_tokens
        if hit:
            s += len(hit)
        return s


class RepoIndex:
    """Bounded, incremental index of ONE workspace's top-level text files."""

    def __init__(self, root, *, max_files: int = _DEFAULT_MAX_FILES):
        self.root = Path(str(root or ""))
        self.max_files = max(1, int(max_files))
        self.entries: dict[str, _Entry] = {}      # rel posix name -> entry
        self.extractions = 0                       # cache-miss counter (tests)
        self.truncated = False                     # walk hit the file cap

    def refresh(self) -> None:
        """Stat every candidate file; re-extract only changed/new ones."""
        root = self.root
        self.truncated = False
        try:
            candidates = sorted(root.iterdir(), key=lambda x: x.name.lower())
        except OSError:
            self.entries.clear()
            return
        seen: dict[str, tuple[int, int]] = {}
        count = 0
        for p in candidates:
            if count >= self.max_files:
                self.truncated = True
                break
            try:
                if not p.is_file() or p.name.startswith("."):
                    continue
                if p.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                st = p.stat()
            except OSError:
                continue
            seen[p.name] = (st.st_mtime_ns, st.st_size)
            count += 1
        # Drop vanished files first, then update the survivors incrementally.
        for rel in [r for r in self.entries if r not in seen]:
            del self.entries[rel]
        for rel, (mtime_ns, size) in seen.items():
            old = self.entries.get(rel)
            if old is not None and old.mtime_ns == mtime_ns and old.size == size:
                continue  # cache hit: signature unchanged, symbols stay
            self.entries[rel] = self._extract(rel, mtime_ns, size)

    def _extract(self, rel: str, mtime_ns: int, size: int) -> _Entry:
        self.extractions += 1
        suffix = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        try:
            src = (self.root / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            src = ""
        if "\x00" in src:
            symbols: list[str] = []
        elif suffix == ".py":
            symbols = _py_symbols(src)
        elif suffix in (".js", ".jsx", ".ts", ".tsx"):
            symbols = _js_symbols(src)
        else:
            symbols = []
        return _Entry(rel, mtime_ns, size, symbols)

    def search(self, query: str, limit: int = 4) -> list[tuple[str, int]]:
        """Rank entries by query-token overlap; only positive scores.

        Returns [(rel_path, score)] best-first, at most `limit` items.
        """
        qtokens = _tokens(str(query or ""))
        if not qtokens:
            return []
        scored = [(e.rel, e.score(qtokens))
                  for e in self.entries.values()]
        scored = [(r, s) for r, s in scored if s > 0]
        scored.sort(key=lambda x: (-x[1], x[0]))
        limit = max(1, int(limit or 1))
        return scored[:limit]


_INDEX_CACHE: dict[str, RepoIndex] = {}


def get_index(workspace, *, max_files: int = _DEFAULT_MAX_FILES) -> RepoIndex:
    """Per-workspace index (refreshed on every call — the refresh itself is
    the incremental step and costs one stat per file)."""
    try:
        key = os.path.normcase(str(Path(str(workspace or "")).resolve()))
    except OSError:
        key = os.path.normcase(str(workspace or ""))
    idx = _INDEX_CACHE.get(key)
    if idx is None or idx.max_files != max(1, int(max_files)):
        idx = RepoIndex(workspace, max_files=max_files)
        _INDEX_CACHE[key] = idx
        while len(_INDEX_CACHE) > _MAX_CACHED_INDEXES:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    idx.refresh()
    return idx


def search_workspace(workspace, query: str, limit: int = 4) -> list[tuple[str, int]]:
    """Convenience: refresh + rank. Never raises — a broken workspace or an
    unreadable file simply scores nothing (callers fall back gracefully)."""
    try:
        return get_index(workspace).search(query, limit=limit)
    except OSError:
        return []
