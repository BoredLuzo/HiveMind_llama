# -*- coding: utf-8 -*-
"""Metadata/smoke checks for the coverage measurement setup (2026-11-07).

Cheap on purpose: no regression run, no measured run - only config parsing,
import behavior and wrapper argument handling. Complements the real thing
(`python tests/coverage_run.py --report`), which is too slow for a check.

Run standalone:  python tests/test_coverage_setup.py
(registered in run_regressions.py SUITES as coverage_setup).
"""
import configparser
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

COVERAGERC = ROOT / ".coveragerc"
SITECUSTOMIZE = ROOT / "tests" / "coverage_site" / "sitecustomize.py"
WRAPPER = ROOT / "tests" / "coverage_run.py"
PACKAGES = {"core", "backend", "tools", "context", "infra", "learning",
            "hive_functions", "routers", "vision", "vram", "hivemind_gateway"}

h = Harness()
check = h.check

# --- .coveragerc: parses and pins the expected knobs ----------------------
check(".coveragerc exists", COVERAGERC.is_file())
cp = configparser.ConfigParser()
_parse_err = ""
try:
    cp.read(COVERAGERC, encoding="utf-8")
except configparser.Error as exc:
    _parse_err = f"{type(exc).__name__}: {exc}"
check(".coveragerc parses as INI", _parse_err == "", _parse_err)
check("[run]/[paths]/[report] sections present",
      cp.has_section("run") and cp.has_section("paths") and cp.has_section("report"))
check("run.parallel = True",
      cp.get("run", "parallel", fallback="").strip().lower() == "true")
_src = {ln.strip() for ln in cp.get("run", "source", fallback="").splitlines()
        if ln.strip()}
check("run.source covers exactly the 11 packages", _src == PACKAGES,
      f" got {_src ^ PACKAGES}" if _src != PACKAGES else "")
_paths = [ln.strip() for ln in cp.get("paths", "source", fallback="").splitlines()
          if ln.strip()]
check("paths maps repo root as canonical prefix", bool(_paths) and _paths[0] == ".")
_omit = cp.get("report", "omit", fallback="")
check("report omits tests/__pycache__/archive dirs",
      "tests/*" in _omit and "__pycache__" in _omit and "archive" in _omit)
check("report has exclude_lines", bool(cp.get("report", "exclude_lines",
                                              fallback="").strip()))

# --- sitecustomize: imports without starting coverage when env is unset ----
check("sitecustomize exists", SITECUSTOMIZE.is_file())
_saved_env = os.environ.pop("COVERAGE_PROCESS_START", None)
sys.path.insert(0, str(SITECUSTOMIZE.parent))
sys.modules.pop("sitecustomize", None)
_import_err = ""
try:
    import sitecustomize  # noqa: F401
except (ImportError, SyntaxError) as exc:
    _import_err = f"{type(exc).__name__}: {exc}"
finally:
    if str(SITECUSTOMIZE.parent) in sys.path:
        sys.path.remove(str(SITECUSTOMIZE.parent))
    sys.modules.pop("sitecustomize", None)
    if _saved_env is not None:
        os.environ["COVERAGE_PROCESS_START"] = _saved_env
check("sitecustomize imports cleanly (COVERAGE_PROCESS_START unset)",
      _import_err == "", _import_err)
check("coverage was NOT started on import (env unset)",
      "coverage" not in sys.modules)
_sc_text = SITECUSTOMIZE.read_text(encoding="utf-8") if SITECUSTOMIZE.is_file() else ""
check("sitecustomize guards on COVERAGE_PROCESS_START",
      "COVERAGE_PROCESS_START" in _sc_text)
check("sitecustomize calls coverage.process_startup()",
      "coverage.process_startup()" in _sc_text)

# --- wrapper: importable, --report split, wiring present -------------------
check("coverage_run.py exists", WRAPPER.is_file())
spec = importlib.util.spec_from_file_location("hivemind_coverage_run", WRAPPER)
_wrap_err = ""
mod = None
if spec is not None and spec.loader is not None:
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except (ImportError, SyntaxError) as exc:
        _wrap_err = f"{type(exc).__name__}: {exc}"
        mod = None
else:
    _wrap_err = "spec/loader unavailable"
check("wrapper imports without side effects", mod is not None, _wrap_err)
if mod is not None:
    _report, _rest = mod.split_args(["--report", "--changed", "--jobs", "2"])
    check("split_args detects --report", _report is True)
    check("split_args passes runner flags through",
          _rest == ["--changed", "--jobs", "2"], f" got {_rest}")
    _report2, _rest2 = mod.split_args(["--only", "budgets"])
    check("split_args without --report", _report2 is False and _rest2 == ["--only", "budgets"])
    check("wrapper sets COVERAGE_PROCESS_START",
          "COVERAGE_PROCESS_START" in WRAPPER.read_text(encoding="utf-8"))
    check("wrapper targets run_regressions.py",
          "run_regressions.py" in WRAPPER.read_text(encoding="utf-8"))
    _wt = WRAPPER.read_text(encoding="utf-8")
    check("wrapper prepends coverage_site to PYTHONPATH",
          "coverage_site" in _wt and "PYTHONPATH" in _wt)

# --- packaging metadata: dev-only files stay out of the release zip --------
_attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
for _rel in (".coveragerc", "tests/coverage_site/**", "tests/coverage_run.py",
             "requirements-dev.txt"):
    _line = next((ln for ln in _attrs.splitlines() if _rel in ln), "")
    check(f".gitattributes export-ignore: {_rel}",
          "export-ignore" in _line, f" line: {_line!r}")
_ign = (ROOT / ".gitignore").read_text(encoding="utf-8")
check(".gitignore ignores .coverage / .coverage.* / htmlcov/",
      ".coverage" in _ign and ".coverage.*" in _ign and "htmlcov/" in _ign)

sys.exit(h.finish("coverage_setup"))
