# -*- coding: utf-8 -*-
"""Import a fixed Python subset of the Aider polyglot benchmark.

Source: https://github.com/Aider-AI/polyglot-benchmark (Exercism exercises).
The generated task dirs are local benchmark fixtures; they are not part of the
shipped product. `.meta/example.py` is never copied — only the stub and the
hidden test file.
"""
from __future__ import annotations

import argparse
import ast
import json
import shutil
from pathlib import Path

DEFAULT_NAMES = (
    "affine-cipher", "beer-song", "bowling", "grade-school", "list-ops",
    "phone-number", "robot-name", "simple-linked-list", "tree-building", "wordy",
)


def _read_instructions(ex: Path) -> str:
    parts = []
    for name in ("instructions.md", "instructions.append.md"):
        p = ex / ".docs" / name
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8", errors="replace").strip())
    return "\n\n".join(p for p in parts if p)


_SKIP_NAMES = frozenset({".docs", ".meta", ".approaches", "__pycache__"})


def _is_skipped_name(name: str) -> bool:
    return name in _SKIP_NAMES or name.endswith("_test.py")


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if _is_skipped_name(n)}


def _extract_test_contract(tests: list[Path], slug: str) -> str:
    """Extract imported symbols and expected exception message patterns via AST.

    Keeps individual test cases/inputs hidden while surfacing exact API names
    (constants, custom exception classes, and required error message strings) so
    single-shot evaluation does not fail on blind guessing.
    """
    mod = slug.replace("-", "_")
    imported_names: list[str] = []
    seen_imports: set[str] = set()
    error_messages: list[str] = []
    seen_errors: set[str] = set()

    def _add_import(name: str) -> None:
        if name and name != "*" and name not in seen_imports:
            seen_imports.add(name)
            imported_names.append(name)

    def _add_error(msg: str) -> None:
        cleaned = msg.strip()
        if cleaned and cleaned not in seen_errors:
            seen_errors.add(cleaned)
            error_messages.append(cleaned)

    def _call_name(node: ast.expr) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            return node.attr
        return ""

    for t_file in tests:
        try:
            tree = ast.parse(t_file.read_text(encoding="utf-8", errors="replace"), filename=str(t_file))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module == mod or node.module.startswith(mod + "."):
                    for alias in node.names:
                        _add_import(alias.name)
            elif isinstance(node, ast.Call):
                cname = _call_name(node.func)
                for kw in node.keywords:
                    if (
                        kw.arg == "match"
                        and isinstance(kw.value, ast.Constant)
                        and isinstance(kw.value.value, str)
                    ):
                        _add_error(kw.value.value)
                if cname in ("assertRaisesWithMessage", "assertRaisesRegex", "assertRaisesRegexp"):
                    if (
                        len(node.args) >= 2
                        and isinstance(node.args[1], ast.Constant)
                        and isinstance(node.args[1].value, str)
                    ):
                        _add_error(node.args[1].value)
                elif cname == "assertEqual" and len(node.args) >= 2:
                    lhs_src = ast.unparse(node.args[0]) if hasattr(ast, "unparse") else ""
                    if "exception.args" in lhs_src or "str(err" in lhs_src:
                        if isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                            _add_error(node.args[1].value)

    if not imported_names and not error_messages:
        return ""
    lines = ["[Test suite API contract]"]
    if imported_names:
        lines.append(f"- Imported from `{mod}`: {', '.join(imported_names)}")
    if error_messages:
        quoted = ", ".join(repr(m) for m in error_messages[:12])
        lines.append(f"- Expected exception message patterns: {quoted}")
    return "\n".join(lines)


def import_exercise(src: Path, out: Path, slug: str) -> dict:
    ex = src / slug
    impl = sorted(p for p in ex.glob("*.py") if not p.name.endswith("_test.py"))
    tests = sorted(ex.glob("*_test.py"))
    if not impl or not tests:
        raise SystemExit(f"{slug}: expected one implementation and one test file")
    dst = out / f"polyglot_{slug.replace('-', '_')}"
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "repo").mkdir(parents=True)
    (dst / "tests").mkdir()
    for p in ex.iterdir():
        if _is_skipped_name(p.name):
            continue
        if p.is_dir():
            shutil.copytree(p, dst / "repo" / p.name, ignore=_ignore)
        elif p.is_file():
            shutil.copy2(p, dst / "repo" / p.name)
    for p in tests:
        shutil.copy2(p, dst / "tests" / p.name)
    mod = slug.replace("-", "_")
    files_list = ", ".join(p.name for p in impl)
    contract = _extract_test_contract(tests, slug)
    contract_block = f"\n\n{contract}" if contract else ""
    prompt = _read_instructions(ex)
    prompt += (
        f"{contract_block}\n\n"
        f"Implement the solution in `{files_list}` (starter stub is already in the workspace). "
        f"Verify your implementation before completing by importing `{mod}` and running "
        f"assertions on the examples and edge cases (e.g. via `python -c` or `run_python`). "
        f"Do not modify or create test files."
    )
    check = "python -m pytest -q " + " ".join(t.name for t in tests)
    data = {"id": f"polyglot_{slug.replace('-', '_')}", "prompt": prompt,
            "check": check, "timeout_s": 300}
    (dst / "task.json").write_text(json.dumps(data, indent=1) + "\n",
                                    encoding="utf-8")
    return {"id": data["id"], "source": slug, "tests": [t.name for t in tests]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="polyglot-benchmark/python/exercises/practice")
    ap.add_argument("--out", default="bench/tasks_polyglot")
    ap.add_argument("--names", default=",".join(DEFAULT_NAMES))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    src = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.names.strip().lower() in ("all", "*"):
        names = sorted([d.name for d in src.iterdir() if d.is_dir() and not d.name.startswith(".")])
    else:
        names = [n.strip() for n in args.names.split(",") if n.strip()]
    if args.limit and args.limit > 0:
        names = names[:args.limit]
    imported = []
    for slug in names:
        if not (src / slug).is_dir():
            raise SystemExit(f"exercise not found: {src / slug}")
        imported.append(import_exercise(src, out, slug))
    manifest = {
        "source": "Aider-AI/polyglot-benchmark",
        "source_path": str(src),
        "license_note": "Exercism exercise content; see the source repository.",
        "tasks": imported,
    }
    (out / "SOURCE.json").write_text(json.dumps(manifest, indent=1) + "\n",
                                     encoding="utf-8")
    print(f"imported {len(imported)} tasks into {out}")
    for row in imported:
        print(" ", row["id"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
