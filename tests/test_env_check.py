"""env_check.py verification (2026-10-16, 1.3.8 install incident).

The incident: a fresh 1.3.8 install reported "[OK] Packages installed"
(pip exit 0) and crashed right after with "ImportError: cannot import
name 'ArgsKwargs' from 'pydantic_core._pydantic_core' (unknown
location)" - the compiled .pyd was missing. This suite proves the new
verification/repair tool (tools/env_check.py) against ARTIFICIALLY
broken packages in %TEMP%: a package whose compiled part is missing,
a healthy set of modules, and the no-repair-possible path. No engine,
no start_hivemind.bat, no model."""
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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


ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "env_check.py"


def run_env_check(args: list, cwd: str) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(TOOL), *args],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=cwd, timeout=300)
    return r.returncode, (r.stdout + r.stderr)


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hvm_envcheck_"))

    # Fixture 1: "kaputtes" Paket - __init__ zieht ein fehlendes kompiliertes
    # Modul nach (das 1.3.8-Muster: pydantic_core ohne _pydantic_core.pyd).
    broken = tmp / "brokenpkg"
    broken.mkdir()
    (broken / "__init__.py").write_text("from . import _compiled\n", encoding="utf-8")

    # Fixture 2: gesundes Paket (echter Import, nicht nur leere Datei).
    healthy = tmp / "healthypkg"
    healthy.mkdir()
    (healthy / "__init__.py").write_text("VALUE = 41 + 1\n", encoding="utf-8")

    # (1) Alles gesund -> exit 0
    rc, out = run_env_check(["--probe", "json,healthypkg"], str(tmp))
    check("healthy modules -> exit 0", rc == 0 and "OK" in out, f" rc={rc}")

    # (2) Kaputtes Paket ohne --repair -> exit 1, Diagnose nennt das Modul
    rc, out = run_env_check(["--probe", "json,brokenpkg"], str(tmp))
    check("broken package without repair -> exit 1 + diagnosis",
          rc == 1 and "BROKEN" in out and "brokenpkg" in out, f" rc={rc}")

    # (3) Kaputtes Paket mit --repair, Reparatur scheitert am Unbekannten ->
    # exit 1 mit den beiden Nutzerschritten
    rc, out = run_env_check(
        ["--probe", "json,brokenpkg", "--repair", "--packages", "brokenpkg"],
        str(tmp))
    check("broken + repair fails -> exit 1 + two user steps",
          rc == 1 and "STILL BROKEN" in out and ".venv" in out
          and "Python 3.12" in out, f" rc={rc}")

    # (4) Halbes Paket: Modul existiert als leere Datei, das Symbol fehlt
    # (ArgsKwargs-Fall: unknown location) -> exit 1
    half = tmp / "halfpkg"
    half.mkdir()
    (half / "__init__.py").write_text("", encoding="utf-8")
    rc, out = run_env_check(["--probe", "halfpkg.ArgsKwargs"], str(tmp))
    check("empty module without symbol -> exit 1 (unknown-location shape)",
          rc == 1, f" rc={rc}")

    # (5) Echte Reparatur gegen das Repo-.venv (enthält die Pakete bereits,
    # pip-Pfad) -> exit 0
    venv_py = ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        rc, out = run_env_check(
            ["--probe", "pydantic_core._pydantic_core,fastapi,uvicorn",
             "--repair"], str(tmp))
        check("default probe against the real venv -> exit 0", rc == 0,
              f" rc={rc}")
    else:
        check("default probe against the real venv -> exit 0", True,
              " (no .venv in repo checkout - skipped)")

    # (6) Usage-Fehler -> exit 2
    rc, out = run_env_check(["--probe", ""], str(tmp))
    check("empty probe list -> exit 2", rc == 2, f" rc={rc}")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"SUMMARY env_check: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()
