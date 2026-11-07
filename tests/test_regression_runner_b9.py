# -*- coding: utf-8 -*-
"""Behavior checks for the B9 runner additions (2026-11-07).

Covers the three new mechanisms without spawning suites:
  * --changed selection patterns still map source files onto suites
  * flake history records pass/fail/duration and trims to the keep window
  * --flake-report counts pass/fail flips and ignores clean suites
  * quarantine.json is read as suite -> {reason}, note keys are ignored
"""
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import run_regressions as rr  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check


def _with_history(path):
    """Context-manager-free swap of the history file path."""
    old = rr._HISTORY_FILE
    rr._HISTORY_FILE = path
    return old


# 1) changed-mode patterns --------------------------------------------------
pats = rr._change_patterns("core/duo_runner.py")
check("change pattern keeps the full path", "core/duo_runner.py" in pats, f"({pats})")
check("change pattern keeps the bare stem", "duo_runner" in pats, f"({pats})")
check("change pattern keeps the dotted module", "core.duo_runner" in pats,
      f"({pats})")

selected = [name for name, _rel, _t in rr._select_affected(["core/tool_loop.py"])]
check("tool_loop change selects the ToolLoop behavior suite",
      "tool_loop_nonstream" in selected, f"({len(selected)} suites)")
check("tool_loop change is not a full-suite selection",
      len(selected) < len(rr.SUITES), f"({len(selected)}/{len(rr.SUITES)})")

# DOCS-EXCLUDE (2026-11-07): docs/settings.md used to contribute the bare
# "docs"/"settings" patterns; the runner drops docs before selecting and the
# pattern builder no longer emits the directory for docs/tests paths.
check("docs are recognized as doc files", rr._is_doc_file("docs/settings.md"))
docs_pats = rr._change_patterns("docs/settings.md")
check("docs do not select via the docs directory",
      "docs" not in docs_pats, f"({docs_pats})")

# tests/quarantine.json used to contribute the bare "tests" pattern and pull
# every suite into the selection.
fixture_pats = rr._change_patterns("tests/quarantine.json")
check("test data files do not select via the tests directory",
      "tests" not in fixture_pats, f"({fixture_pats})")
check("test data files still select by basename",
      "quarantine.json" in fixture_pats, f"({fixture_pats})")

# 2) history write + trim ---------------------------------------------------
tmpdir = Path(tempfile.mkdtemp(prefix="hm_b9_"))
hist = tmpdir / ".regression_history.json"
old_hist = _with_history(hist)
try:
    rr._record_history([("a", True, 1.2, None), ("b", False, 0.4, None)],
                       changed_mode=False, jobs=6)
    data = json.loads(hist.read_text(encoding="utf-8"))
    run = (data.get("runs") or [{}])[-1]
    check("history run is stored", len(data.get("runs", [])) == 1,
          f"({len(data.get('runs', []))} runs)")
    check("history stores pass/fail", run.get("results", {}).get("a", {}).get("ok") is True
          and run.get("results", {}).get("b", {}).get("ok") is False)
    check("history stores duration", run.get("results", {}).get("a", {}).get("dur") == 1.2,
          f"({run.get('results', {}).get('a')})")

    # simulate more runs than the keep window
    many = {"runs": [{"at": f"run-{i}", "results": {}} for i in range(rr._HISTORY_KEEP + 5)]}
    hist.write_text(json.dumps(many), encoding="utf-8")
    rr._record_history([("a", True, 0.1, None)], changed_mode=False, jobs=6)
    trimmed = json.loads(hist.read_text(encoding="utf-8"))
    check("history keeps only the last N runs",
          len(trimmed["runs"]) == rr._HISTORY_KEEP,
          f"({len(trimmed['runs'])} kept)")

    # 3) flake report -------------------------------------------------------
    hist.write_text(json.dumps({"runs": [
        {"at": "t1", "results": {"steady": {"ok": True, "dur": 2},
                                  "flaky": {"ok": True, "dur": 3}}},
        {"at": "t2", "results": {"steady": {"ok": True, "dur": 2},
                                  "flaky": {"ok": False, "dur": 4}}},
        {"at": "t3", "results": {"steady": {"ok": True, "dur": 2},
                                  "flaky": {"ok": True, "dur": 3}}},
    ]}), encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = rr._flake_report(20)
    out = buf.getvalue()
    check("flake report succeeds", rc == 0, f"(rc={rc})")
    check("flake report names the flaky suite", "flaky" in out, f"({out[:80]!r})")
    check("flake report hides the steady suite", "steady" not in out)
    row = next((ln for ln in out.splitlines() if ln.strip().startswith("flaky")), "")
    check("flake report counts both flips",
          row.split()[1:2] == ["2"], f"({row!r})")

    # 4) quarantine parsing -------------------------------------------------
    qfile = tmpdir / "quarantine.json"
    qfile.write_text(json.dumps({
        "_comment": "notes",
        "real_suite": {"reason": "socket flake", "since": "2026-11-07"},
        "junk": "not a dict",
    }), encoding="utf-8")
    old_q = rr._QUARANTINE_FILE
    rr._QUARANTINE_FILE = qfile
    try:
        q = rr._load_quarantine()
        check("quarantine keeps only suite entries",
              list(q) == ["real_suite"], f"({list(q)})")
        check("quarantine keeps the reason",
              q["real_suite"].get("reason") == "socket flake", f"({q})")
    finally:
        rr._QUARANTINE_FILE = old_q
finally:
    rr._HISTORY_FILE = old_hist

sys.exit(h.finish("test_regression_runner_b9"))
