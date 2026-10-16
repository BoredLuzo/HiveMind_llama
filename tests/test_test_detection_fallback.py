"""A plain Python folder must not be reported as "untested".

Live owner report: after the coder wrote and ran a Python test file, the verify
gate still said
  [TEST-RESULT] ⚠ No tests found or no test command available (unknown).
  ⚠ No test runner detected — verify manually.
and the run stayed "untested" (task_complete badge).

Cause: detect_project_languages() only looked for manifests
(pyproject.toml / setup.py / requirements.txt). The workspace in question is a
plain folder - index.html plus test_n64_website.py - so no language was found,
run_tests() returned "unknown", and the gate never ran anything.

Fix: a second detection pass over `detect_files_fallback` (test_*.py, *_test.py,
tests/*.py). Only used when NO manifest matched, so every existing project keeps
its current language list and command.

Run: python tests/test_test_detection_fallback.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


from hive_functions.language_config import (                     # noqa: E402
    LANGUAGE_RUNNERS, detect_project_languages, get_test_cmd)

_cfg = LANGUAGE_RUNNERS["python"]
check("python config carries a fallback list",
      _cfg.get("detect_files_fallback") == ["test_*.py", "*_test.py", "tests/*.py"])
check("manifest list unchanged",
      _cfg.get("detect_files") == ["pyproject.toml", "setup.py", "requirements.txt"])


def _ws(files):
    d = tempfile.mkdtemp(prefix="hvm_detect_")
    for f in files:
        p = Path(d) / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n", encoding="utf-8")
    return d


# ── the live case: html + a python test file, no manifest ───────────────────
_d1 = _ws(["index.html", "test_n64_website.py"])
check("plain folder with test_*.py -> python detected",
      "python" in detect_project_languages(_d1), f" ({detect_project_languages(_d1)})")
check("its test command is the python one",
      get_test_cmd("python") == "pytest -x -q 2>&1")

_d2 = _ws(["app.html", "helper_test.py"])
check("folder with *_test.py -> python detected",
      "python" in detect_project_languages(_d2))

_d3 = _ws(["site/index.html", "tests/test_site.py"])
check("folder with tests/*.py -> python detected",
      "python" in detect_project_languages(_d3))

# ── nothing must change for the existing cases ──────────────────────────────
_d4 = _ws(["index.html", "style.css"])
check("html-only folder still detects nothing",
      detect_project_languages(_d4) == [], f" ({detect_project_languages(_d4)})")

_d5 = _ws(["requirements.txt", "main.py"])
check("manifest folder still python", detect_project_languages(_d5) == ["python"])

_d6 = _ws(["package.json", "index.js", "test_thing.py"])
check("manifest wins over the fallback (no duplicate python)",
      detect_project_languages(_d6) == ["javascript"], f" ({detect_project_languages(_d6)})")

_d7 = _ws(["package.json", "index.js"])
check("js-only folder unchanged", detect_project_languages(_d7) == ["javascript"])

# ── source guard: the fallback is a real second pass, not a rewrite ─────────
_src = (ROOT / "hive_functions" / "language_config.py").read_text(encoding="utf-8", errors="replace")
check("fallback runs only when nothing matched", "if not found:" in _src
      and _src.index("if not found:") > _src.index("for lang, cfg in LANGUAGE_RUNNERS.items():"))
check("fallback reads detect_files_fallback", 'cfg.get("detect_files_fallback", [])' in _src)

print()
print(f"SUMMARY test_detection_fallback: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
