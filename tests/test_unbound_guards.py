# -*- coding: utf-8 -*-
"""A3/A9a: variables used after a platform/capability branch must be bound.

A3: `_cmd_translated` was assigned only inside `if sys.platform == "win32":`
    but read unconditionally in the command construction -> NameError on every
    POSIX host (swallowed into RUN_BASH_EXEC_ERROR).
A9a: `_load_mode` was assigned only inside `if _load_mode_supported:` but read
    unconditionally in the [LOAD-DIAG] log after Popen -> UnboundLocalError on
    binaries without --load-mode.
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(ROOT))

from tests._harness import Harness  # noqa: E402

h = Harness(style="results", pad=True)
check = h.check


def _find_func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _assigns_before(fn, target_name, before_line):
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and node.lineno < before_line:
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == target_name:
                    return node.lineno
    return None


# ── A3: exec_tools._inline_tool_run_bash ────────────────────────────────────
src = (ROOT / "tools" / "handlers" / "exec_tools.py").read_text(encoding="utf-8")
fn = _find_func(ast.parse(src), "_inline_tool_run_bash")
check("1a the handler exists", fn is not None)
if fn is not None:
    _win_if = next(
        (n for n in ast.walk(fn)
         if isinstance(n, ast.If)
         and "attr='platform'" in ast.dump(n.test)
         and "win32" in ast.dump(n.test)), None)
    check("1b the platform branch exists", _win_if is not None)
    if _win_if is not None:
        init_line = _assigns_before(fn, "_cmd_translated", _win_if.lineno)
        check("1c _cmd_translated is bound before the platform branch",
              init_line is not None, f"(line {init_line})")

# ── A9a: manager_load._start_process ────────────────────────────────────────
src = (ROOT / "backend" / "manager_load.py").read_text(encoding="utf-8")
fn = _find_func(ast.parse(src), "_start_process")
check("2a the loader exists", fn is not None)
if fn is not None:
    _cap_if = next(
        (n for n in ast.walk(fn)
         if isinstance(n, ast.If) and "_load_mode_supported" in ast.dump(n.test)),
        None)
    check("2b the capability branch exists", _cap_if is not None)
    if _cap_if is not None:
        init_line = _assigns_before(fn, "_load_mode", _cap_if.lineno)
        check("2c _load_mode is bound before the capability branch",
              init_line is not None, f"(line {init_line})")

sys.exit(h.finish())
