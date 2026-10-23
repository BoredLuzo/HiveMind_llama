"""The edit→test mapping must find the tests that cover an edited file —
and never invent matches that would scope a verify run to nothing."""
import sys
import tempfile
import os
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from hive_functions.testmap import map_edits_to_tests, is_test_file  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _ws(structure: dict) -> str:
    tmp = tempfile.mkdtemp(prefix="hivemind_testmap_")
    for rel, content in structure.items():
        p = os.path.join(tmp, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content)
    return tmp


# ── is_test_file classification ──────────────────────────────────────────
check("pytest layout is a test file", is_test_file("tests/test_calc.py"))
check("node spec layout is a test file", is_test_file("src/app/calc.test.ts"))
check("tests/ placement is a test file", is_test_file("tests/calc_suite.js"))
check("source file is not a test file", not is_test_file("src/calc.py"))
check("__init__ under tests counts (placement wins)", is_test_file("tests/__init__.py"))

# ── direct test edit maps to itself ──────────────────────────────────────
ws = _ws({"tests/test_calc.py": "def test_a():\n    assert True\n"})
check("edited test file maps to itself",
      map_edits_to_tests(ws, ["tests/test_calc.py"]) == ["tests/test_calc.py"])

# ── python stem convention ───────────────────────────────────────────────
ws = _ws({
    "calc.py": "def add(a, b):\n    return a + b\n",
    "tests/test_calc.py": "from calc import add\n",
})
check("source maps to test_<stem>.py in tests/",
      map_edits_to_tests(ws, ["calc.py"]) == ["tests/test_calc.py"])
check("workspace-prefixed edit path is normalized",
      map_edits_to_tests(ws, [ws.replace("\\", "/") + "/calc.py"]) == ["tests/test_calc.py"])

# ── node stem convention ─────────────────────────────────────────────────
ws = _ws({
    "src/parser.ts": "export const parse = (s: string) => s;\n",
    "src/__tests__/parser.spec.ts": "import { parse } from '../parser';\n",
})
check("source maps to <stem>.spec.ts in __tests__/",
      map_edits_to_tests(ws, ["src/parser.ts"]) == ["src/__tests__/parser.spec.ts"])

# ── python import probe fallback (unconventional test name) ──────────────
ws = _ws({
    "geometry.py": "def area(r):\n    return 3.14 * r * r\n",
    "checks/test_shapes_suite.py": "import geometry\n",
})
check("import probe finds unconventionally named test",
      map_edits_to_tests(ws, ["geometry.py"]) == ["checks/test_shapes_suite.py"])

# ── no match → full suite (empty list, never a wrong guess) ─────────────
ws = _ws({
    "app.py": "print('hi')\n",
    "tests/test_other.py": "import os\n",
})
check("unmapped source returns [] (full suite fallback)",
      map_edits_to_tests(ws, ["app.py"]) == [])
check("no edited files returns []", map_edits_to_tests(ws, []) == [])

# ── skip dirs: node_modules is never a match source ──────────────────────
ws = _ws({
    "calc.py": "def add(a, b):\n    return a + b\n",
    "node_modules/pkg/test_calc.py": "from calc import add\n",
})
check("test files in node_modules are ignored",
      map_edits_to_tests(ws, ["calc.py"]) == [])

# ── multi-edit union + result cap ────────────────────────────────────────
struct = {"calc.py": "x = 1\n", "util.py": "y = 2\n",
          "tests/test_calc.py": "import calc\n", "tests/test_util.py": "import util\n"}
ws = _ws(struct)
check("multi-edit maps to the union of covering tests",
      map_edits_to_tests(ws, ["calc.py", "util.py"]) ==
      ["tests/test_calc.py", "tests/test_util.py"])
many = {f"mod{i}.py": f"v{i} = {i}\n" for i in range(12)}
many.update({f"tests/test_mod{i}.py": f"import mod{i}\n" for i in range(12)})
ws = _ws(many)
check("result is capped at 8 test files",
      len(map_edits_to_tests(ws, [f"mod{i}.py" for i in range(12)])) == 8)

print()
print(f"SUMMARY testmap: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
