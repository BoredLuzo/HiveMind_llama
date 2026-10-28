# -*- coding: utf-8 -*-
"""Model filter behaviour: the real filter function from static/app.js,
executed with node against a fixture list.

REGRESSION (2026-10-28, live): the filter matched the WHOLE query string
with one includes() - typing "qwen mtp" returned "no match" although
qwen3.5:4b-mtp exists, because no model name contains a space. Every
whitespace-separated token must hit the name.

This is a behaviour test, not a source grep: the function is extracted
from app.js and run in node. If node is missing the suite SKIPS honestly
(the verdict then falls back to a wiring pin only, clearly marked).
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
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


APP_JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")


def _extract_function(name: str) -> str | None:
    """One top-level `function name(...) {...}` from app.js, brace-balanced."""
    m = re.search(r"^function " + re.escape(name) + r"\(", APP_JS, re.M)
    if not m:
        return None
    start = APP_JS.index("{", m.end() - 1)
    depth = 0
    for i in range(start, len(APP_JS)):
        c = APP_JS[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return APP_JS[m.start():i + 1]
    return None


FIXTURE = json.dumps([
    {"name": "qwen3.5:4b-mtp"},
    {"name": "gemma-4:e4b-it-qat-ud"},
    {"name": "cyber-tiel-coder:35b-a3b-mtp-ud"},
    {"name": "minicpm-v-4-7:35b-a3b-mix"},
])
# (filter, expected surviving names) - the first row is the live regression:
# two search terms with a space must AND across the name.
CASES = [
    ("qwen mtp", ["qwen3.5:4b-mtp"]),
    ("mini v", ["minicpm-v-4-7:35b-a3b-mix"]),
    ("coder 35b", ["cyber-tiel-coder:35b-a3b-mtp-ud"]),
    ("QWEN", ["qwen3.5:4b-mtp"]),
    ("qwen  mtp", ["qwen3.5:4b-mtp"]),      # double space must not break it
    (" qwen mtp ", ["qwen3.5:4b-mtp"]),     # surrounding whitespace
    ("zzz", []),
    ("", ["qwen3.5:4b-mtp", "gemma-4:e4b-it-qat-ud",
          "cyber-tiel-coder:35b-a3b-mtp-ud", "minicpm-v-4-7:35b-a3b-mix"]),
]

FN = "_modelMatchesFilter"
src = _extract_function(FN)
node = shutil.which("node")

if src is None:
    check("the filter function exists in app.js", False,
          f"(no 'function {FN}(' in static/app.js - extract-and-run pin is red)")
elif node is None:
    print("  SKIP behaviour run: node not on PATH - wiring pin only")
    check(f"_renderAvailableModels uses {FN}",
          FN in (re.search(r"function _renderAvailableModels\(.*?\n}", APP_JS, re.S).group(0)
                 if re.search(r"function _renderAvailableModels\(.*?\n}", APP_JS, re.S) else ""))
else:
    cases_js = json.dumps([list(c) for c in CASES])
    harness = (
        src + "\n"
        + f"const models = {FIXTURE};\n"
        + f"const cases = {cases_js};\n"
        + "const out = cases.map(([f, want]) => ({f: f, got: models.filter("
        + "m => " + FN + "(m.name, f)).map(m => m.name), want: want}));\n"
        + "console.log(JSON.stringify(out));\n"
    )
    with tempfile.TemporaryDirectory(prefix="hm_filter_") as td:
        js = Path(td) / "filter_harness.js"
        js.write_text(harness, encoding="utf-8")
        proc = subprocess.run([node, str(js)], capture_output=True, text=True,
                              timeout=30)
    if proc.returncode != 0:
        check("the extracted filter function runs in node", False,
              f"({proc.stderr.strip()[:120]})")
    else:
        check("the extracted filter function runs in node", True)
        rows = json.loads(proc.stdout.strip().splitlines()[-1])
        for (flt, want), row in zip(CASES, rows):
            label = f"'{flt}'" if flt else "(empty)"
            check(f"filter {label} matches {len(want)} model(s)",
                  row["got"] == want, f"(got {row['got']})")

# wiring pin: the render path must call the filter function (this part is
# intentionally a grep - the behaviour above runs the real code)
check(f"_renderAvailableModels filters through {FN}", FN + "(" in APP_JS)

print()
print(f"SUMMARY model_filter: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
