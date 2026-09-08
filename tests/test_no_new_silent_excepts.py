# -*- coding: utf-8 -*-
"""Guardrail: no NEW silent exception swallowing.

Counts, per file (AST-based, no third-party deps):
  - except_pass : handlers whose body is only `pass` (silent swallow)
  - broad_except: handlers catching the bare `Exception`/`BaseException` type

Counts are compared against tests/lint_baseline.json. The test FAILS when a
file has MORE of either count than its baseline entry (new debt) — the
existing debt stays frozen and can be paid down incrementally (baseline is
then regenerated lower).

Regenerate baseline after legit cleanups:
    python tests/test_no_new_silent_excepts.py --update
"""
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
BASELINE = Path(__file__).parent / "lint_baseline.json"

EXCLUDE_DIRS = {"__pycache__", ".git", ".venv", ".pytest_cache", "llama", "models"}


def _iter_py_files():
    for p in sorted(ROOT.rglob("*.py")):
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        yield p


def _handler_is_broad(handler) -> bool:
    t = handler.type
    if t is None:                      # bare `except:`
        return True
    if isinstance(t, ast.Name):
        return t.id in ("Exception", "BaseException")
    if isinstance(t, ast.Attribute):   # e.g. builtins.Exception
        return t.attr in ("Exception", "BaseException")
    return False


def _count_file(path: Path) -> tuple[int, int]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, OSError, UnicodeDecodeError):
        return (0, 0)
    n_pass = 0
    n_broad = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for h in node.handlers:
            if _handler_is_broad(h):
                n_broad += 1
                if len(h.body) == 1 and isinstance(h.body[0], ast.Pass):
                    n_pass += 1
    return (n_pass, n_broad)


def collect() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for p in _iter_py_files():
        rel = p.relative_to(ROOT).as_posix()
        n_pass, n_broad = _count_file(p)
        if n_pass or n_broad:
            out[rel] = {"except_pass": n_pass, "broad_except": n_broad}
    return out


def main() -> int:
    update = "--update" in sys.argv
    current = collect()
    if update:
        BASELINE.write_text(
            json.dumps(current, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        total_p = sum(v["except_pass"] for v in current.values())
        total_b = sum(v["broad_except"] for v in current.values())
        print(f"baseline updated: {len(current)} files, "
              f"{total_p} except-pass, {total_b} broad-except")
        return 0

    baseline = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    failures = []
    for rel, cur in current.items():
        base = baseline.get(rel, {"except_pass": 0, "broad_except": 0})
        for key in ("except_pass", "broad_except"):
            if cur[key] > base[key]:
                failures.append(f"{rel}: {key} {cur[key]} > baseline {base[key]}")
    if failures:
        print("NEW silent-except debt detected (fix it or justify + lower baseline):")
        for f in failures:
            print(f"  FAIL {f}")
        return 1
    total_p = sum(v["except_pass"] for v in current.values())
    total_b = sum(v["broad_except"] for v in current.values())
    print(f"OK — no new silent excepts ({len(current)} files tracked, "
          f"{total_p} except-pass / {total_b} broad-except frozen in baseline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
