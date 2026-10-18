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
import importlib.util
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


def diagnose() -> None:
    """Print the facts that separate the usual causes - without importing the
    broken package (an import is exactly what fails, so the old version printed
    nothing useful for pydantic_core):

    (a) a stale/mismatched pair (old pydantic-core next to a new pydantic),
    (b) a broken/partial/quarantined extension (exists but tiny/unloadable),
    (c) something shadowing the package (PYTHONPATH, a user site, a copy in the
        install folder).
    """
    import os

    print(f"[ENV-CHECK] python {sys.version.split()[0]} ({sys.executable})")
    for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        val = os.environ.get(var)
        if val:
            print(f"[ENV-CHECK]   {var}={val}  <-- can shadow the venv")

    for name in ("pydantic", "pydantic_core"):
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError) as exc:
            print(f"[ENV-CHECK]   {name}: not found ({exc})")
            continue
        if spec is None:
            print(f"[ENV-CHECK]   {name}: not installed")
            continue
        where = spec.origin or (spec.submodule_search_locations or ["?"])[0]
        print(f"[ENV-CHECK]   {name}: {where}")

    # every directory that carries a pydantic_core, in sys.path ORDER - the first
    # one wins, and a second copy is the classic shadowing cause
    import site
    found = [e for e in sys.path
             if e and os.path.isdir(os.path.join(e, "pydantic_core"))]
    for i, entry in enumerate(found):
        mark = "used" if i == 0 else "SHADOWED"
        print(f"[ENV-CHECK]   pydantic_core #{i + 1} [{mark}]: {entry}")

    # the compiled module itself: a 0-byte or missing .pyd is a quarantine or a
    # half-written install, and size is the only evidence that survives
    for base in found:
        pkg = os.path.join(base, "pydantic_core")
        try:
            entries = sorted(os.listdir(pkg))
        except OSError:
            continue
        init = os.path.join(pkg, "__init__.py")
        try:
            init_size = os.stat(init).st_size
        except OSError:
            init_size = -1
        if init_size <= 0:
            # no (or empty) __init__.py -> Python treats the folder as a NAMESPACE
            # package, the import resolves to "unknown location" and a symbol like
            # ArgsKwargs can never be found. This is the quarantine/partial-install
            # fingerprint, not a version mismatch.
            print(f"[ENV-CHECK]   {pkg}: __init__.py missing or empty "
                  f"({init_size} bytes) <-- namespace package, 'unknown location'")
        pyds = [e for e in entries if e.startswith("_pydantic_core")]
        if not pyds:
            print(f"[ENV-CHECK]   {pkg}: NO compiled _pydantic_core*.pyd inside")
        for fn in pyds:
            try:
                size = os.stat(os.path.join(pkg, fn)).st_size
            except OSError:
                size = -1
            print(f"[ENV-CHECK]   {fn}: {size} bytes")

    # a second pydantic_core outside the venv (user site) is worth naming even if
    # it is not on sys.path right now
    user_site = getattr(site, "getusersitepackages", lambda: "")()
    if user_site and os.path.isdir(os.path.join(user_site, "pydantic_core")):
        print(f"[ENV-CHECK]   also present in the USER site: {user_site}")


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
        # --only-binary :all: : a source build needs Rust and can leave a
        # half-written .pyd behind - exactly the incident this tool exists for.
        # Without a wheel we want a LOUD failure, not a broken extension.
        if _run([sys.executable, "-m", "pip", "install", "--force-reinstall",
                 "--no-cache-dir", "--only-binary", ":all:", *packages]) == 0:
            return True, "pip"
        # ESCALATION (2026-10-18): --force-reinstall overwrites files but never
        # removes an orphan left by another version. Uninstall first, then install
        # again - the pair is reinstalled either way.
        if _run([sys.executable, "-m", "pip", "uninstall", "-y", *packages]) == 0:
            if _run([sys.executable, "-m", "pip", "install", "--no-cache-dir",
                     "--only-binary", ":all:", *packages]) == 0:
                return True, "pip (uninstall + install)"
    if _run(["uv", "--version"]) == 0:
        tried.append("uv")
        if _run(["uv", "pip", "install", "--python", sys.executable,
                 "--reinstall", "--no-binary", ":none:", *packages]) == 0:
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
    diagnose()
    print("[ENV-CHECK] STILL BROKEN after repair. In this order:")
    print("  1. Check the lines above: a 0-byte _pydantic_core*.pyd (antivirus")
    print("     quarantine, half-written install) or a second 'pydantic_core'")
    print("     marked SHADOWED (PYTHONPATH / user site) are the usual causes.")
    print("  1b. Antivirus/quarantine: the compiled module may have been")
    print("     removed right after install. Check the AV log, exclude the")
    print("     install folder, then re-run start_hivemind.bat.")
    print("  2. Reinstall the PAIR by hand (a mismatch between pydantic and")
    print("     pydantic-core is the other usual cause; the versions must fit):")
    print(f"     {sys.executable} -m pip install --force-reinstall --no-cache-dir "
          "--only-binary :all: pydantic pydantic-core")
    print("  3. Only if that fails: delete .venv and run install.bat again.")
    print("  4. As a last resort install into a FRESH folder with install.bat -")
    print("     the installer provisions its own interpreter and a clean venv,")
    print("     which rules out a half-written package from an earlier attempt.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
