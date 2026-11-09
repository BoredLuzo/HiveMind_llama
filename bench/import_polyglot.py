# -*- coding: utf-8 -*-
"""Import a fixed Python subset of the Aider polyglot benchmark.

Source: https://github.com/Aider-AI/polyglot-benchmark (Exercism exercises).
The generated task dirs are local benchmark fixtures; they are not part of the
shipped product. `.meta/example.py` is never copied — only the stub and the
hidden test file.
"""
from __future__ import annotations

import argparse
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


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in {".docs", ".meta", "__pycache__"}
            or n.endswith("_test.py")}


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
        if p.name in {".docs", ".meta", "__pycache__"} or p.name.endswith("_test.py"):
            continue
        if p.is_dir():
            shutil.copytree(p, dst / "repo" / p.name, ignore=_ignore)
        elif p.is_file():
            shutil.copy2(p, dst / "repo" / p.name)
    for p in tests:
        shutil.copy2(p, dst / "tests" / p.name)
    prompt = _read_instructions(ex)
    prompt += (
        "\n\nImplement the missing function(s) in the existing Python file(s). "
        "Run the tests you can infer from the instructions. Do not modify or "
        "create test files."
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
