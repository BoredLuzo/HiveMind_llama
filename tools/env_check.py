"""Post-install dependency verification for start_hivemind.bat.

Incident (1.3.8, fresh 3.14 install): pip reported success, then the
engine crashed with "ImportError: cannot import name 'ArgsKwargs' from
'pydantic_core._pydantic_core' (unknown location)" - the compiled
_pydantic_core*.pyd was missing/quarantined while pip exit 0 said OK.
The batch never re-verified after installing.

This tool IS the verification step the batch calls after installing:

    python tools/env_check.py [--probe m1,m2,...] [--repair] [--packages p1,p2]

Exit codes: 0 = all probes import; 1 = broken after optional repair
(guidance printed); 2 = usage error.

Defaults probe the exact modules of the incident and repair the exact
packages. --repair first tries pip (works in plain venvs), then uv
(uv-managed venvs ship without pip). Everything prints plainly; no
engine, no model, no slot.
"""
import argparse
import importlib
import subprocess
import sys

DEFAULT_PROBE = "pydantic_core._pydantic_core,fastapi,uvicorn"
DEFAULT_PACKAGES = "pydantic,pydantic-core"


def _run(cmd: list) -> int:
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=600).returncode
    except (OSError, subprocess.TimeoutExpired):
        return 1


def probe_all(modules: list) -> tuple[str, str] | None:
    """Import every module; return (module, error) of the first failure."""
    for mod in modules:
        try:
            importlib.import_module(mod)
        except ImportError as e:  # incl. "DLL load failed" for broken .pyd
            return mod, f"{type(e).__name__}: {e}"
    return None


def repair(packages: list) -> tuple[bool, str]:
    """Try pip (plain venvs), fall back to uv (uv venvs ship without pip).
    Returns (ok, how). Never raises."""
    if not packages:
        return False, "no packages to repair"
    tried: list[str] = []
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)
    if in_venv and _run([sys.executable, "-m", "pip", "--version"]) == 0:
        tried.append("pip")
        if _run([sys.executable, "-m", "pip", "install", "--force-reinstall",
                 "--no-cache-dir", *packages]) == 0:
            return True, "pip"
    if _run(["uv", "--version"]) == 0:
        tried.append("uv")
        if _run(["uv", "pip", "install", "--python", sys.executable,
                 "--reinstall", *packages]) == 0:
            return True, "uv"
    if not tried:
        return False, "neither pip nor uv available"
    return False, " and ".join(tried)


def main() -> int:
    # The batch runs us with cwd = install root; probes must resolve from
    # there (script dir is sys.path[0] otherwise).
    import os
    sys.path.insert(0, os.getcwd())
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", default=DEFAULT_PROBE,
                    help="comma-separated modules to import")
    ap.add_argument("--repair", action="store_true",
                    help="reinstall the packages once before giving up")
    ap.add_argument("--packages", default=DEFAULT_PACKAGES,
                    help="comma-separated packages for --repair")
    args = ap.parse_args()

    modules = [m.strip() for m in args.probe.split(",") if m.strip()]
    packages = [p.strip() for p in args.packages.split(",") if p.strip()]
    if not modules:
        print("[ENV-CHECK] no probe modules given")
        return 2

    fail = probe_all(modules)
    if fail is None:
        print(f"[ENV-CHECK] OK: {' '.join(modules)} import cleanly")
        return 0

    mod, err = fail
    print(f"[ENV-CHECK] BROKEN: importing '{mod}' failed: {err}")
    if not args.repair:
        return 1

    print(f"[ENV-CHECK] attempting repair: reinstall {' '.join(packages)}")
    ok, how = repair(packages)
    if not ok:
        print(f"[ENV-CHECK] repair via {how} did not succeed")
    if probe_all(modules) is None:
        print("[ENV-CHECK] OK after repair")
        return 0
    print("[ENV-CHECK] STILL BROKEN after repair. Two steps:")
    print("  1. Delete the folder .venv, then run install.bat again.")
    print("  2. If it still fails, install Python 3.12 or 3.13 from python.org")
    print("     and re-run install.bat (a broken build or antivirus quarantine")
    print("     of the compiled modules is the usual cause).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
