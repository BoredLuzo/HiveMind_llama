"""Coverage wrapper around tests/run_regressions.py.

Usage:
    python tests/coverage_run.py                  # full suite, measured
    python tests/coverage_run.py --changed        # changed-mode, measured
    python tests/coverage_run.py --only budgets   # any run_regressions flag
    python tests/coverage_run.py --report         # + combine + report after
    python tests/coverage_run.py --help           # this text

How it works: instead of instrumenting one process, the wrapper points the
environment at the repo's .coveragerc (COVERAGE_PROCESS_START) and prepends
tests/coverage_site to PYTHONPATH. Interpreter startup then starts coverage
in the runner itself, in every tests/test_*.py suite it spawns and in any
gateway/engine grandchild (see tests/coverage_site/sitecustomize.py). With
parallel = True each process writes its own .coverage.* data file, which
`coverage combine` merges into a single report afterwards.

Without coverage installed the wrapper still runs the suite - unmeasured -
and prints the install hint: a dev tool must never change the regression
result. The runner's exit code is always propagated.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".coveragerc"
SITE_DIR = ROOT / "tests" / "coverage_site"
RUNNER = ROOT / "tests" / "run_regressions.py"


def split_args(argv: list[str]) -> tuple[bool, list[str]]:
    """Pull the wrapper-only --report flag out; the rest goes to the runner."""
    return "--report" in argv, [a for a in argv if a != "--report"]


def coverage_installed() -> bool:
    return importlib.util.find_spec("coverage") is not None


def _install_hint() -> str:
    return ("  uv sync --group dev                   # project dev group\n"
            '  pip install "coverage[toml]>=7.6"     # any other environment')


def _clear_stale_data() -> int:
    """Remove leftover .coverage* files from previous runs so combine only
    sees this run's data. Narrow OSError catch: deletion is best effort."""
    removed = 0
    for p in ROOT.glob(".coverage*"):
        try:
            p.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def main() -> int:
    report_mode, rest = split_args(sys.argv[1:])
    if "--help" in rest or "-h" in rest:
        print(__doc__)
        return 0
    if not CONFIG.is_file() or not SITE_DIR.is_dir() or not RUNNER.is_file():
        print(f"ERROR: coverage setup incomplete in {ROOT} "
              f"(need .coveragerc, tests/coverage_site/, tests/run_regressions.py)",
              file=sys.stderr)
        return 2

    measured = coverage_installed()
    if not measured:
        print(f"ERROR: coverage is not installed for this Python "
              f"({sys.executable}).\nRunning the suite UNMEASURED. Install with:",
              file=sys.stderr)
        print(_install_hint(), file=sys.stderr)
    elif _clear_stale_data():
        print("coverage: cleared stale .coverage* data files from a previous run")

    env = dict(os.environ)
    env["COVERAGE_PROCESS_START"] = str(CONFIG)
    env["PYTHONPATH"] = (str(SITE_DIR) + os.pathsep + env["PYTHONPATH"]
                         if env.get("PYTHONPATH") else str(SITE_DIR))

    proc = subprocess.run([sys.executable, str(RUNNER), *rest], env=env, cwd=str(ROOT))
    rc = proc.returncode

    if not measured:
        return rc
    if report_mode:
        combine = subprocess.run([sys.executable, "-m", "coverage", "combine"],
                                 cwd=str(ROOT))
        report = subprocess.run([sys.executable, "-m", "coverage", "report",
                                 "--skip-covered"], cwd=str(ROOT))
        return rc or combine.returncode or report.returncode
    print("=" * 64)
    print("Coverage data collected from this run's processes (.coverage.*).")
    print("Merge and view it:")
    print("  python -m coverage combine")
    print("  python -m coverage report --skip-covered")
    print("(or re-run with --report to do both automatically)")
    return rc


if __name__ == "__main__":
    sys.exit(main())
