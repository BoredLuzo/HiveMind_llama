# -*- coding: utf-8 -*-
"""Polyglot import fixtures: schema, hidden tests, pre-registered subset."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from bench.import_polyglot import DEFAULT_NAMES
from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check

root = ROOT / "bench" / "tasks_polyglot"
specs = sorted(root.glob("*/task.json"))
check("the fixed subset has ten tasks", len(specs) == 10, f"({len(specs)})")
check("the default names match the generated task dirs",
      len(DEFAULT_NAMES) == 10
      and {f"polyglot_{n.replace('-', '_')}" for n in DEFAULT_NAMES}
      == {p.parent.name for p in specs})

for spec in specs:
    d = spec.parent
    data = json.loads(spec.read_text(encoding="utf-8"))
    check(f"{d.name}: prompt/check/one hidden test",
          bool(data.get("prompt")) and "pytest" in data.get("check", "")
          and len(list((d / "tests").glob("*_test.py"))) == 1)
    check(f"{d.name}: hidden test is not in repo",
          not any(p.name.endswith("_test.py") for p in (d / "repo").rglob("*.py")))
    check(f"{d.name}: example solution is not shipped",
          not (d / "repo" / ".meta" / "example.py").exists())

check("source manifest records the pinned upstream", (root / "SOURCE.json").is_file()
      and "polyglot-benchmark" in (root / "SOURCE.json").read_text(encoding="utf-8"))

sys.exit(h.finish("bench_polyglot"))
