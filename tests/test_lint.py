# -*- coding: utf-8 -*-
"""Lint gate: run ruff (see ruff.toml for the selected rule set).

Standalone suite in the run_regressions style: exit 0 = clean, 1 = violations.
ruff must be launchable via the current interpreter (`python -m ruff`;
listed in requirements-dev.txt).
"""
import subprocess
import sys


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    r = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "."],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        print(out.strip()[-4000:])
        print("ruff check FAILED")
        return 1
    print("OK — ruff check clean (see ruff.toml for the rule set)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
