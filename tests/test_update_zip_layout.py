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

    # ---- core+utils, aber OHNE server.py ---------------------------------
    # Der Layout-Guard (core/utils) laesst das durch; erst der Versions-Probe
    # (update.bat:132) bricht ab. Ohne diesen Fall waere "core+utils reicht"
    # eine unbewiesene Annahme.
    with zipfile.ZipFile(tmp / "nosrv.zip", "w") as z:
        z.writestr("HiveMind_v1.3.9-test/core/x.py", "a = 1\n")
        z.writestr("HiveMind_v1.3.9-test/utils/y.py", "b = 2\n")
    dest_nosrv = tmp / "extracted_nosrv"
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Expand-Archive -Path '{tmp / 'nosrv.zip'}' "
         f"-DestinationPath '{dest_nosrv}' -Force"],
        capture_output=True, text=True, timeout=60)
    check("extract nosrv.zip", r.returncode == 0)
    exroot_nosrv = next(iter(dest_nosrv.iterdir()), None)
    check("nosrv: Layout-Guard (core+utils) greift NICHT - kein core/utils-Fehler",
          exroot_nosrv is not None
          and (exroot_nosrv / "core").is_dir()
          and (exroot_nosrv / "utils").is_dir()
          and not (exroot_nosrv / "server.py").exists())
    v_nosrv = ps(VERSION_PS.format(srv=exroot_nosrv / "server.py")) \
        if exroot_nosrv else "?"
    check("nosrv: Versions-Probe leer -> 'Zip carries no server.py' greift",
          v_nosrv == "", f" got {v_nosrv!r}")

    # ---- package_release: Leak-Check funktional gegen Fake-Zips -----------
    # Der Regex wird NICHT nachgebaut, sondern aus deploy/package_release.bat
    # gelesen und per PowerShell auf echte Zips angewandt.
    prb = (Path(__file__).parent.parent / "deploy" / "package_release.bat"
           ).read_text(encoding="utf-8", errors="replace")

    def _leak_cmd(zip_path):
        for line in prb.splitlines():
            s = line.strip()
            if s.lower().startswith("powershell") and "sessions/" in s:
                body = s[s.index('"') + 1: s.rindex('"')]
                return body.replace("..\\%OUT%",
                                    str(zip_path).replace("\\", "/"))
        return None

    check("package_release: Leak-Check-Zeile gefunden", _leak_cmd(tmp) is not None)

    def _run_leak(zp):
        cmd = _leak_cmd(zp)
        rr = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                            capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120)
        return rr.returncode, (rr.stdout or "").strip()

    clean = tmp / "leak_clean.zip"
    with zipfile.ZipFile(clean, "w") as z:
        z.writestr("HiveMind_v1.3.9/README.md", "x\n")
        z.writestr("HiveMind_v1.3.9/requirements.txt", "fastapi\n")
        z.writestr("HiveMind_v1.3.9/hivemind_gateway/requirements-gateway.txt", "x\n")
    rc, out = _run_leak(clean)
    check("package_release: sauberes Zip akzeptiert (requirements*.txt zaehlen NICHT)",
          rc == 0, f" rc={rc} out={out[:60]!r}")

    for name in ("out.txt", "logs/dump_1.json", "a.patch", "b.csv",
                 "c.log", "d.pyc", "e.gguf", ".env", "out_run2.txt"):
        zp = tmp / ("leak_" + name.replace("/", "_").replace(".", "_") + ".zip")
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("HiveMind_v1.3.9/README.md", "x\n")
            z.writestr(f"HiveMind_v1.3.9/{name}", "x\n")
        rc, out = _run_leak(zp)
        check(f"package_release: {name} wird als LEAK abgelehnt",
              rc == 1 and "LEAK" in out, f" rc={rc} out={out[:50]!r}")

    # Gegenprobe mit dem echten getrackten Baum: kein Pfad darf anschlagen.
    tracked = subprocess.run(["git", "ls-files"],
                             cwd=str(Path(__file__).parent.parent),
                             capture_output=True, text=True, encoding="utf-8",
                             errors="replace").stdout.splitlines()
    import re as _re
    _pats = []
    for line in prb.splitlines():
        s = line.strip()
        if s.lower().startswith("powershell") and "sessions/" in s:
            body = s[s.index('"') + 1: s.rindex('"')]
            _pats = _re.findall(r"-match '([^']+)'", body)
            break
    check("package_release: mindestens 3 Match-Muster gefunden", len(_pats) >= 3,
          f" got {len(_pats)}")
    if len(_pats) >= 3:
        rx = _re.compile("|".join(f"(?:{p})" for p in _pats))
        hitting = [f for f in tracked if rx.search("/" + f)]
        check("package_release: kein getrackter Pfad matcht die Guard-Muster",
              not hitting, f" {hitting[:5]}")

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    print()
    print(f"SUMMARY update_zip_layout: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()
