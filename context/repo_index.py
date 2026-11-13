# -*- coding: utf-8 -*-
"""Incremental per-workspace repo index (D2 context engine, 2026-11-09).

stdlib only. One small index per workspace: text/code files with their
mtime+size signature and top-level symbols. `refresh()` is incremental
— a file whose mtime_ns+size is unchanged keeps its extracted symbols (the
only expensive step: content read + ast parse), vanished files are dropped,
new/changed files are re-extracted. Two walk modes:

- top-level (default, `nested=False`): exactly the 2026-11-09 behavior —
  `root.iterdir()`, no recursion. The tiny-workspace pre-seed uses this so
  its selection behavior and cost stay unchanged.
- nested (`nested=True`, D2 deep retrieval 2026-11-11): a BOUNDED
  `os.walk` — hidden and heavy directories (node_modules, __pycache__,
  venvs, build output, VCS metadata …) are pruned before descent, symlinked
  directories are never followed (`followlinks=False`), descent stops at
  `max_depth` levels below the root and the index stops at `max_files`
  entries (`truncated=True`). A huge repo can therefore never make this
  scan itself expensive.

`search(query, limit)` ranks entries by token overlap between the task query
and three per-file token sets: file stem (weight 4), extracted symbols
(weight 2) and the full relative path (weight 1). Only positive scores are
returned — a query that matches nothing yields an empty list, and callers
(duo_helpers.build_preseed_context, duo_helpers.build_deep_retrieval_context)
then emit no block instead of inlining arbitrary files.
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
_SYMBOL_SUFFIXES = frozenset({".py", ".js", ".jsx", ".ts", ".tsx"})
_MAX_EXTRACT_BYTES = 512 * 1024

# Cheap JS/TS symbol extraction — no tree-sitter. Good enough for retrieval
# ranking; never used for guards or edits.
_JS_SYMBOL_RES = (
    re.compile(r"\b(?:function|class|interface|enum)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=(?!=)"),
)

_MAX_SYMBOLS_PER_FILE = 64
_DEFAULT_MAX_FILES = 2000
_DEFAULT_MAX_DEPTH = 6
_MAX_CACHED_INDEXES = 8
_MAX_HINTS = 32          # hard ceiling for deep_context_block's hint count
_MAX_HINT_SYMBOLS = 5    # symbols rendered per hint line
_MIN_HINT_BUDGET = 300   # chars — below this the block cannot fit one line

# Deep walk (nested=True): directories that are never descended into. Pure
# scan protection — none of these are primary source locations in normal
# repos; dotted entries are already covered by the "." prefix rule.
_HEAVY_DIRS = frozenset({
    "node_modules", "bower_components", "__pycache__", "venv", "env",
    "virtualenv", "dist", "build", "target", "out", "coverage", "htmlcov",
    "site-packages", "__macosx", ".git", ".hg", ".svn", ".tox", ".nox",
    ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".idea",
    ".vscode", ".next", ".nuxt", ".gradle", ".cache", ".eggs",
})

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
    """Bounded, incremental index of ONE workspace's text/code files.

    `nested=False` (default) walks top-level only — the original D2 core
    behavior. `nested=True` adds the bounded deep walk (see module doc).
    """

    def __init__(self, root, *, max_files: int = _DEFAULT_MAX_FILES,
                 nested: bool = False,
                 max_depth: int = _DEFAULT_MAX_DEPTH):
        self.root = Path(str(root or ""))
        self.max_files = max(1, int(max_files))
        self.nested = bool(nested)
        self.max_depth = max(1, int(max_depth))
        self.entries: dict[str, _Entry] = {}      # rel posix name -> entry
        self.extractions = 0                       # cache-miss counter (tests)
        self.truncated = False                     # walk hit the file cap

    def refresh(self) -> None:
        """Stat every candidate file; re-extract only changed/new ones."""
        self.truncated = False
        try:
            if self.nested:
                seen = self._walk_nested()
            else:
                seen = self._walk_top_level()
        except OSError:
            self.entries.clear()
            return
        # Drop vanished files first, then update the survivors incrementally.
        for rel in [r for r in self.entries if r not in seen]:
            del self.entries[rel]
        for rel, (mtime_ns, size) in seen.items():
            old = self.entries.get(rel)
            if old is not None and old.mtime_ns == mtime_ns and old.size == size:
                continue  # cache hit: signature unchanged, symbols stay
            self.entries[rel] = self._extract(rel, mtime_ns, size)

    def _walk_top_level(self) -> dict[str, tuple[int, int]]:
        """Top-level candidates only (pre-deep-retrieval behavior, kept
        verbatim so the tiny-workspace pre-seed path stays identical)."""
        root = self.root
        try:
            candidates = sorted(root.iterdir(), key=lambda x: x.name.lower())
        except OSError:
            self.entries.clear()
            return {}
        root_resolved = root.resolve()
        seen: dict[str, tuple[int, int]] = {}
        count = 0
        for p in candidates:
            if count >= self.max_files:
                self.truncated = True
                break
            try:
                if p.is_symlink() or not p.is_file() or p.name.startswith("."):
                    continue
                if p.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                if not p.resolve().is_relative_to(root_resolved):
                    continue
                st = p.stat()
            except OSError:
                continue
            seen[p.name] = (st.st_mtime_ns, st.st_size)
            count += 1
        return seen

    def _walk_nested(self) -> dict[str, tuple[int, int]]:
        """Bounded deep walk: posix rel names for nested candidates.

        Safety rails (module doc): heavy/hidden directories pruned before
        descent, symlinks not followed, stop at max_depth directory levels,
        hard stop at max_files entries (truncated=True). Deterministic:
        dirnames/filenames are walked in case-insensitive name order.
        """
        base = str(self.root)
        seen: dict[str, tuple[int, int]] = {}
        count = 0
        stop = False
        for dirpath, dirnames, filenames in os.walk(
                base, topdown=True, followlinks=False):
            if stop:
                break
            rel_dir = os.path.relpath(dirpath, base)
            depth = 0 if rel_dir == os.curdir else rel_dir.count(os.sep) + 1
            if depth >= self.max_depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".")
                           and d.lower() not in _HEAVY_DIRS]
            dirnames.sort(key=str.lower)
            for name in sorted(filenames, key=str.lower):
                if count >= self.max_files:
                    self.truncated = True
                    stop = True
                    break
                if name.startswith("."):
                    continue
                if os.path.splitext(name)[1].lower() not in TEXT_SUFFIXES:
                    continue
                full = os.path.join(dirpath, name)
                try:
                    if os.path.islink(full) or not os.path.isfile(full):
                        continue
                    st = os.stat(full)
                except OSError:
                    continue
                rel = name if depth == 0 else \
                    os.path.relpath(full, base).replace(os.sep, "/")
                seen[rel] = (st.st_mtime_ns, st.st_size)
                count += 1
        return seen

    def _extract(self, rel: str, mtime_ns: int, size: int) -> _Entry:
        self.extractions += 1
        suffix = "." + rel.rsplit(".", 1)[-1].lower() if "." in rel else ""
        symbols: list[str] = []
        if suffix in _SYMBOL_SUFFIXES and 0 <= size <= _MAX_EXTRACT_BYTES:
            target = self.root / rel
            src = ""
            try:
                if (not target.is_symlink()
                        and target.resolve().is_relative_to(self.root.resolve())):
                    src = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                src = ""
            if src and "\x00" not in src:
                if suffix == ".py":
                    symbols = _py_symbols(src)
                elif suffix in (".js", ".jsx", ".ts", ".tsx"):
                    symbols = _js_symbols(src)
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


def _cache_key(workspace, *, max_files: int, nested: bool, max_depth: int) -> str:
    try:
        base = os.path.normcase(str(Path(str(workspace or "")).resolve()))
    except OSError:
        base = os.path.normcase(str(workspace or ""))
    return (f"{base}|mf={max(1, int(max_files))}"
            f"|n={bool(nested)}|d={max(1, int(max_depth))}")


def get_index(workspace, *, max_files: int = _DEFAULT_MAX_FILES,
              nested: bool = False,
              max_depth: int = _DEFAULT_MAX_DEPTH) -> RepoIndex:
    """Per-workspace index (refreshed on every call — the refresh itself is
    the incremental step and costs one stat per file). Walk modes are cached
    separately: the tiny-workspace pre-seed uses top-level, deep retrieval
    uses nested=True, and neither evicts the other's invalidation state."""
    key = _cache_key(workspace, max_files=max_files, nested=nested,
                     max_depth=max_depth)
    idx = _INDEX_CACHE.get(key)
    if idx is None:
        idx = RepoIndex(workspace, max_files=max_files, nested=nested,
                        max_depth=max_depth)
        _INDEX_CACHE[key] = idx
        while len(_INDEX_CACHE) > _MAX_CACHED_INDEXES:
            _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    idx.refresh()
    return idx


def search_workspace(workspace, query: str, limit: int = 4) -> list[tuple[str, int]]:
    """Convenience: refresh + rank (top-level walk, unchanged behavior).
    Never raises — a broken workspace or an unreadable file simply scores
    nothing (callers fall back gracefully)."""
    try:
        return get_index(workspace).search(query, limit=limit)
    except OSError:
        return []


def deep_search_workspace(workspace, query: str, limit: int = 5, *,
                          max_files: int = _DEFAULT_MAX_FILES,
                          max_depth: int = _DEFAULT_MAX_DEPTH
                          ) -> list[tuple[str, int]]:
    """Deep-retrieval ranking over the bounded nested walk.

    Same contract as search_workspace: refresh + rank, never raises, only
    positive scores. Returns posix rel paths (nested files as `src/a/b.py`).
    """
    try:
        idx = get_index(workspace, max_files=max_files, nested=True,
                        max_depth=max_depth)
        return idx.search(query, limit=limit)
    except OSError:
        return []


def _hint_line(rank: int, rel: str, entry) -> str:
    """One deterministic signature line: rank, posix path, size, symbols."""
    if entry is None:
        return f"{rank}. {rel}"
    size = int(entry.size)
    size_txt = f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"
    base = f"{rank}. {rel} ({size_txt})"
    syms = [str(s) for s in (entry.symbols or [])[:_MAX_HINT_SYMBOLS]]
    if syms:
        return base + " — " + ", ".join(syms)
    return base


_HINT_HEADER = "[RETRIEVAL HINTS — repo files ranked most relevant to this task]"
_HINT_FOOTER = ("Read these files with read_file (range reads for large "
                "files) before editing them; other files matched the task "
                "less strongly.")


def deep_context_block(workspace, query: str, *, max_hints: int = 5,
                       budget_chars: int = 3000,
                       max_files: int = _DEFAULT_MAX_FILES,
                       max_depth: int = _DEFAULT_MAX_DEPTH
                       ) -> tuple[str, list[str]]:
    """Compact retrieval block for the coder prompt (D2 deep retrieval).

    Ranks indexed files (nested + top-level, bounded walk) against `query`
    and renders at most `max_hints` signature lines — path, size and top
    symbols, NEVER full file contents — under `budget_chars`. Deterministic
    and bounded: extra ranked hints that do not fit the budget are dropped,
    not truncated mid-line. Returns ("", []) for an empty query, an
    unmatched query (no arbitrary picks) or an unusable workspace.
    """
    query = str(query or "").strip()
    if not query:
        return "", []
    max_hints = max(1, min(int(max_hints or 1), _MAX_HINTS))
    budget = max(_MIN_HINT_BUDGET, int(budget_chars or 0))
    try:
        idx = get_index(workspace, max_files=max_files, nested=True,
                        max_depth=max_depth)
    except OSError:
        return "", []
    ranked = idx.search(query, limit=max_hints)
    if not ranked:
        return "", []
    lines: list[str] = []
    names: list[str] = []
    used = len(_HINT_HEADER) + len(_HINT_FOOTER) + 2
    for rank, (rel, _score) in enumerate(ranked, 1):
        line = _hint_line(rank, rel, idx.entries.get(rel))
        if used + len(line) + 1 > budget:
            break
        lines.append(line)
        names.append(rel)
        used += len(line) + 1
    if not lines:
        return "", []
    return _HINT_HEADER + "\n" + "\n".join(lines) + "\n" + _HINT_FOOTER, names
