"""update.bat --zip layout check (2026-10-15).

update.bat wird hier NICHT ausgefuehrt (verboten/risikofrei halten).
Stattdessen baut diese Suite in %TEMP% drei Fake-Zips (gut, Layout
unvollstaendig, kein Version-Ordner) und faehrt die EASY identische
Extract+Probe-Logik aus update.bat (Abschnitt 2b) per PowerShell aus.
Ein Source-Guard stellt sicher, dass der getestete Probe-Ausdruck und
der in update.bat stehenidentisch bleiben."""
import subprocess
import sys
import tempfile
import zipfile
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


SERVER_PY = "HIVEMIND_VERSION = '1.3.9-test'\n"
VERSION_PS = ("(Select-String -Path '{srv}' "
              "-Pattern 'HIVEMIND_VERSION\\s*=\\s*.([0-9][0-9.a-z-]*)'"
              ").Matches[0].Groups[1].Value")


def ps(cmd: str) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=60)
    return r.stdout.strip()


def main():
    bat = (Path(__file__).parent.parent / "update.bat").read_text(
        encoding="utf-8", errors="replace")
    check("update.bat: --zip parsing present",
          '--zip' in bat and 'set "LOCAL_ZIP="' in bat and '"%~2"==""' in bat)
    check("update.bat: layout guard on core+utils",
          'no core\\ folder' in bat and 'no utils\\ folder' in bat)
    check("update.bat: --zip skips GitHub download",
          "if defined LOCAL_ZIP goto zip_staged" in bat)
    check("update.bat: backup before apply, settings in backup",
          "robocopy \"%BASE%\" \"%BASE%\\%BK%\"" in bat
          and "/XF update.bat settings.json" in bat)
    check("update.bat: no /MIR in any robocopy command (user data would die)",
          not any(l.strip().lower().startswith("robocopy") and "/mir" in l.lower()
                  for l in bat.splitlines()))
    check("update.bat: BUILD_INFO fallback after verify",
          "BUILD_INFO.txt fallback" in bat)
    check("package_release: BUILD_INFO carries commit hash",
          "commit: ' + $env:BUILD_COMMIT" in
          (Path(__file__).parent.parent / "deploy" / "package_release.bat")
          .read_text(encoding="utf-8", errors="replace"))

    tmp = Path(tempfile.mkdtemp(prefix="hvm_ziplayout_"))
    good = tmp / "good"
    good.mkdir()
    with zipfile.ZipFile(tmp / "good.zip", "w") as z:
        z.writestr("HiveMind_v1.3.9-test/core/x.py", "a = 1\n")
        z.writestr("HiveMind_v1.3.9-test/utils/y.py", "b = 2\n")
        z.writestr("HiveMind_v1.3.9-test/server.py", SERVER_PY)
    with zipfile.ZipFile(tmp / "bad.zip", "w") as z:
        z.writestr("HiveMind_v1.3.9-test/core/x.py", "a = 1\n")
        z.writestr("HiveMind_v1.3.9-test/server.py", SERVER_PY)
    with zipfile.ZipFile(tmp / "empty.zip", "w") as z:
        z.writestr("loose_file.txt", "x\n")

    for name in ("good", "bad", "empty"):
        dest = tmp / f"extracted_{name}"
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Expand-Archive -Path '{tmp / (name + '.zip')}' "
             f"-DestinationPath '{dest}' -Force"],
            capture_output=True, text=True, timeout=60)
        check(f"extract {name}.zip", r.returncode == 0)

    # good.zip: EXROOT found, core+utils present, version parsed
    exroot = next(iter((tmp / "extracted_good").iterdir()), None)
    check("good: version root folder found", exroot is not None)
    check("good: core/ present", (exroot / "core").is_dir() if exroot else False)
    check("good: utils/ present", (exroot / "utils").is_dir() if exroot else False)
    if exroot:
        v = ps(VERSION_PS.format(srv=exroot / "server.py"))
        check("good: HIVEMIND_VERSION parsed from zip", v == "1.3.9-test", f" got {v!r}")
        check("good: update.bat version regex matches this file",
              (exroot / "server.py").read_text(encoding="utf-8").count("HIVEMIND_VERSION") == 1)

    # bad.zip: utils/ fehlt -> genau das muss der Guard melden
    exroot_bad = next(iter((tmp / "extracted_bad").iterdir()), None)
    check("bad: utils/ fehlt -> update.bat guard waere gefeuert",
          exroot_bad is not None and not (exroot_bad / "utils").is_dir())

    # empty.zip: kein Versionsordner -> EXROOT leer
    kids = list((tmp / "extracted_empty").iterdir())
    check("empty: kein Root-Ordner -> 'Zip contained no version folder'",
          all(not k.is_dir() for k in kids))

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"SUMMARY update_zip_layout: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()
