"""Pre-Explore darf keine undefinierten Modul-Globals mehr benutzen.

Regression fuer den Live-Fehler (2026-10-20, Freundes-Instanz):
    [hivemind.pre_explore] ERROR LLM error port=8101 attempt=22:
        name '_llm_client_lock' is not defined
    [hivemind.pre_explore] ERROR LLM FAILED after 30 attempts

`global X` deklariert nur die Zuweisung; das Lesen wirft NameError, solange X
nirgends auf Modulebene gebunden ist. Der Test prueft statisch (AST) fuer alle
pre_explore-Module, dass jeder per `global` benutzte Name auch existiert, und
ruft die betroffenen Helfer einmal echt auf.
"""
from __future__ import annotations

import ast
import asyncio
import glob
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add((a.asname or a.name).split(".")[0])
    return names


def _declared_globals(tree: ast.Module) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, ast.Global):
                    out.update(sub.names)
    return out


for path in sorted(glob.glob(str(ROOT / "hive_functions" / "pre_explore" / "*.py"))):
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    missing = sorted(_declared_globals(tree) - _module_level_names(tree))
    check(f"{Path(path).name}: every `global` name exists", not missing, f"(missing {missing})")


async def _smoke():
    from hive_functions.pre_explore import llm

    c1 = await llm._get_llm_client()
    c2 = await llm._get_llm_client()
    same = c1 is c2
    llm._reset_pre_explore_usage()
    usage = llm._snapshot_pre_explore_usage()
    await c1.aclose()
    return same, usage


try:
    same, usage = asyncio.run(_smoke())
    check("_get_llm_client returns a shared client", same is True)
    check("usage counter is a dict with the three keys",
          isinstance(usage, dict) and set(usage) == {"completion_tokens", "prompt_tokens", "cached_tokens"},
          f"({sorted(usage)})")
except (RuntimeError, ImportError, OSError, AttributeError, NameError) as exc:
    check("pre_explore llm helpers work", False, f"({type(exc).__name__}: {exc})")

print()
print(f"SUMMARY pre_explore_globals: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
