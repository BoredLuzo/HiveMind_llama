"""Map edited source files to the test files that cover them.

Pure stdlib, no engine imports — the auto-test loop calls this before every
verify run so the harness can execute the affected test files instead of
always paying for the full suite. Callers that get an empty list back fall
back to the full suite, so a wrong "no match" is only a performance loss,
never a correctness one.

Conventions understood:
- python:   ``test_<stem>.py``, ``<stem>_test.py`` (pytest layout)
- node:     ``<stem>.test.js/.jsx/.ts/.tsx/.mjs``, ``<stem>.spec.*``
- placement: files under ``tests/``, ``test/``, ``__tests__/`` or ``src/``
  sibling test dirs count as test files even when the name pattern differs
- fallback: a test file that IMPORTS the edited python module counts as
  covering it (bounded content probe)
"""
from __future__ import annotations

import os
import re

__all__ = ["is_test_file", "map_edits_to_tests"]

# Directories that never contain first-party test files.
_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules",
    "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache",
    "dist", "build", "out", "target", ".next", ".nuxt", "coverage",
    "site-packages", ".tox", ".idea", ".vscode",
}

_TEST_DIR_PARTS = {"tests", "test", "__tests__", "spec"}

# <stem>.test.<ext> / <stem>.spec.<ext> (node families)
_NODE_TEST_RE = re.compile(
    r"^.+\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$", re.IGNORECASE)
# test_<stem>.py / <stem>_test.py / <stem>_test.<anything the runner knows>
_PY_TEST_RE = re.compile(r"^(?:test_.+|.+_test)\.py$", re.IGNORECASE)

_MAX_SCAN_FILES = 4000     # walk budget — workspace maps stay O(milliseconds)
_MAX_CONTENT_PROBES = 60   # test files opened for the python import probe
_PROBE_BYTES = 4096
_MAX_RESULTS = 8


def is_test_file(rel_path: str) -> bool:
    """True when *rel_path* (workspace-relative, any separators) is a test file."""
    norm = rel_path.replace("\\", "/").lstrip("./")
    parts = norm.split("/")
    if not parts or not parts[-1]:
        return False
    if any(p in _TEST_DIR_PARTS for p in parts[:-1]):
        return True
    name = parts[-1]
    return bool(_PY_TEST_RE.match(name) or _NODE_TEST_RE.match(name))


def _rel(workspace: str, path: str) -> str:
    rel = os.path.relpath(path, workspace).replace("\\", "/")
    return rel.lstrip("./")


def _stem(rel_path: str) -> str:
    name = rel_path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0].lower()


def _walk_test_files(workspace: str) -> list[str]:
    found: list[str] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for f in files:
            rel = _rel(workspace, os.path.join(root, f))
            if is_test_file(rel):
                found.append(rel)
            if len(found) >= _MAX_SCAN_FILES:
                return found
    return found


def _import_probe_matches(test_rel: str, module_stems: set[str], workspace: str) -> bool:
    try:
        with open(os.path.join(workspace, test_rel), "rb") as fh:
            head = fh.read(_PROBE_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return False
    for m in re.finditer(r"^\s*(?:from|import)\s+[\w.]*?(\w+)[\s.]", head, re.MULTILINE):
        if m.group(1).lower() in module_stems:
            return True
    return False


def map_edits_to_tests(workspace: str, edited: list[str]) -> list[str]:
    """Return the workspace-relative test files covering *edited* paths.

    - edited test files map to themselves (the model edited a test — run it)
    - source files map by name convention first, then by python import probe
    - ``[]`` = nothing mappable — caller runs the full suite
    """
    if not edited or not workspace:
        return []

    norm_edits: list[str] = []
    for e in edited:
        rel = str(e).replace("\\", "/").strip()
        ws_prefix = workspace.replace("\\", "/").rstrip("/") + "/"
        if rel.lower().startswith(ws_prefix.lower()):
            rel = rel[len(ws_prefix):]
        norm_edits.append(rel.lstrip("./"))

    direct = [e for e in norm_edits if is_test_file(e)
              and os.path.isfile(os.path.join(workspace, e))]
    sources = [e for e in norm_edits
               if e not in direct and os.path.isfile(os.path.join(workspace, e))]
    if not sources and not direct:
        return []

    test_files = _walk_test_files(workspace)
    source_stems = {_stem(s) for s in sources}

    by_stem: list[str] = []
    probe_candidates: list[str] = []
    for t in test_files:
        ts = _stem(t)
        ts_clean = ts
        if ts_clean.startswith("test_"):
            ts_clean = ts_clean[len("test_"):]
        if ts_clean.endswith("_test"):
            ts_clean = ts_clean[:-len("_test")]
        if ts_clean.endswith((".test", ".spec")):
            ts_clean = ts_clean[:ts_clean.rfind(".")]
        if ts in source_stems or ts_clean in source_stems:
            by_stem.append(t)
        else:
            probe_candidates.append(t)

    matches = list(direct)
    for t in by_stem:
        if t not in matches:
            matches.append(t)

    if sources and len(matches) < _MAX_RESULTS:
        probes = 0
        for t in probe_candidates:
            if probes >= _MAX_CONTENT_PROBES or len(matches) >= _MAX_RESULTS:
                break
            probes += 1
            if _import_probe_matches(t, source_stems, workspace) and t not in matches:
                matches.append(t)

    return sorted(matches)[:_MAX_RESULTS]
