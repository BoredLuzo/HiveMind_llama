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

from deploy import updater as UPDMOD  # noqa: E402

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


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
    upd = (Path(__file__).parent.parent / "deploy" / "updater.py").read_text(
        encoding="utf-8", errors="replace")
    check("updater.py: --zip parsing present",
          '"--zip"' in upd and "local_zip = Path(argv[1])" in upd
          and "--zip needs the path" in upd)
    check("updater.py: layout guard on core+utils",
          upd.count("no core\\\\ folder") >= 2
          and upd.count("no utils\\\\ folder") >= 2)
    check("updater.py: --zip skips the GitHub fetch",
          "if local_zip is not None:" in upd
          and UPDMOD.APPLY_XF[0] == "update.bat")
    check("updater.py: backup before apply, settings in backup",
          "def backup_cmd(" in upd and "def apply_cmd(" in upd
          and UPDMOD.APPLY_XF[1] == "settings.json")
    check("updater.py: no /MIR in any robocopy command (user data would die)",
          not any("/mir" in str(t).lower()
                  for t in UPDMOD.backup_cmd(Path("B"), "bk")
                  + UPDMOD.apply_cmd(Path("X"), Path("B"))))
    check("updater.py: BUILD_INFO fallback after verify",
          "def write_build_info_fallback(" in upd
          and upd.find("write_build_info_fallback(base, newver, tagnum)") > upd.find("9 done: verify"))

    tmp = Path(tempfile.mkdtemp(prefix="hvm_ziplayout_"))
    with zipfile.ZipFile(tmp / "good.zip", "w") as z:
        z.writestr("HiveMind_v1.3.9-test/core/x.py", "a = 1\\n")
        z.writestr("HiveMind_v1.3.9-test/utils/y.py", "b = 2\\n")
        z.writestr("HiveMind_v1.3.9-test/core/version.py", SERVER_PY)
    with zipfile.ZipFile(tmp / "bad.zip", "w") as z:
        z.writestr("HiveMind_v1.3.9-test/core/x.py", "a = 1\\n")
        z.writestr("HiveMind_v1.3.9-test/core/version.py", SERVER_PY)
    with zipfile.ZipFile(tmp / "empty.zip", "w") as z:
        z.writestr("loose_file.txt", "x\\n")

    n, exroot = UPDMOD.extract_release(tmp / "good.zip", tmp / "extracted_good")
    check("good: version root folder found", n == 1 and exroot is not None)
    check("good: core/ present", (exroot / "core").is_dir() if exroot else False)
    check("good: utils/ present", (exroot / "utils").is_dir() if exroot else False)
    if exroot:
        v = UPDMOD.read_version(exroot / "core")
        check("good: HIVEMIND_VERSION parsed from zip", v == "1.3.9-test", f" got {v!r}")
        check("good: updater version regex matches this file",
              (exroot / "core" / "version.py").read_text(encoding="utf-8")
              .count("HIVEMIND_VERSION") == 1)

    n, exroot_bad = UPDMOD.extract_release(tmp / "bad.zip", tmp / "extracted_bad")
    check("bad: utils/ fehlt -> updater guard waere gefeuert",
          exroot_bad is not None and not (exroot_bad / "utils").is_dir())

    n, exroot_empty = UPDMOD.extract_release(tmp / "empty.zip", tmp / "extracted_empty")
    check("empty: kein Root-Ordner -> 'Zip contained no version folder'",
          exroot_empty is None and n == 0)

    with zipfile.ZipFile(tmp / "nosrv.zip", "w") as z:
        z.writestr("HiveMind_v1.3.9-test/core/x.py", "a = 1\\n")
        z.writestr("HiveMind_v1.3.9-test/utils/y.py", "b = 2\\n")
    n, exroot_nosrv = UPDMOD.extract_release(tmp / "nosrv.zip", tmp / "extracted_nosrv")
    check("nosrv: Layout-Guard (core+utils) greift NICHT - kein core/utils-Fehler",
          exroot_nosrv is not None
          and (exroot_nosrv / "core").is_dir()
          and (exroot_nosrv / "utils").is_dir()
          and not (exroot_nosrv / "core" / "version.py").exists())
    v_nosrv = UPDMOD.read_version(exroot_nosrv / "core") if exroot_nosrv else "?"
    check("nosrv: Versions-Probe leer -> 'Zip carries no core version.py' greift",
          v_nosrv is None, f" got {v_nosrv!r}")

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

    sys.exit(h.finish("update_zip_layout"))


main()
