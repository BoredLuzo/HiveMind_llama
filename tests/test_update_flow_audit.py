# -*- coding: utf-8 -*-
r"""Update/release path stress audit (2026-11-04).

Pins the updater fixes from the update-path stress audit. update.bat itself
is NEVER executed against a real install; the suite runs the EXACT
robocopy/PowerShell fragments extracted from update.bat against fake trees
in %TEMP%, plus source guards so the fixes cannot silently regress.

Covered findings:
  1. bare /XD models excluded the tracked model_configs\models\*.json from
     the apply AND the backup - the anchored "%BASE%\models" must ship them
  2. a zip with several root folders applied the LAST one silently - the
     NROOTS guard must reject that in BOTH extract sections
  3. the GitHub flow applied a wrong/empty zip layout and only noticed in
     the post-apply verify - core/utils/version probes must run pre-apply
  4. an install path with an apostrophe/brackets broke every inline
     PowerShell call - %TMQ%/%EXQ% doubling + -LiteralPath must be used
  5. an unparseable version left VCMP empty and fell through both version
     gates - the guard must abort
  6. an uppercase API digest failed the compare as a false tamper alarm
  7. the watchdog could revive the engine mid-update (dying gasp) and the
     re-arm raced the dependency refresh - grace wait + port re-check +
     late re-arm
  8. update_prune.ps1 must never move the RUNNING update.bat
  9. package_release.bat must reject a zip whose name version differs from
     the packaged server.py (uncommitted version bump)
 10. .gitattributes: dev-gate scripts stay out of the zip, the updater's
     own deploy helpers keep shipping
"""
import os
import time
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


UPDATE = (ROOT / "update.bat").read_text(encoding="utf-8", errors="replace")
ULINES = UPDATE.splitlines()
PRUNE_PS1 = (ROOT / "deploy" / "update_prune.ps1").read_text(encoding="utf-8", errors="replace")
PKG_BAT = (ROOT / "deploy" / "package_release.bat").read_text(encoding="utf-8", errors="replace")
ATTRS = (ROOT / ".gitattributes").read_text(encoding="utf-8", errors="replace")

SERVER_PY = "HIVEMIND_VERSION = '1.3.9-audit'\n"


def robocopy_lines():
    return [l.strip() for l in ULINES if l.strip().lower().startswith("robocopy")]


def ps_command(cmd, timeout=90):
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def xd_bare_names(line):
    """Tokens of the /XD list (names only, stops at the next /flag)."""
    seg = line.split("/XD", 1)[1] if "/XD" in line else ""
    names = []
    for tok in seg.split():
        if tok.startswith("/"):
            break
        names.append(tok)
    return names


def extract_inline_ps(line, backquoted):
    """Pull the -Command body out of a cmd line that calls powershell."""
    if backquoted:
        m = re.search(r'powershell -NoProfile -Command "(.+?)"`', line)
    else:
        m = re.search(r'powershell -NoProfile -Command "(.+)"\s*$', line)
    return m.group(1) if m else None


def run_cmd_fragment(tmp: Path, name: str, line: str):
    """Run a substituted cmd line from a temp .cmd file.

    `cmd /c <line>` directly would hit cmd's first-and-last-quote stripping
    (many quoted args) - inside a batch file the line parses exactly as it
    does in update.bat."""
    script = tmp / f"run_{name}.cmd"
    script.write_bytes(("\r\n" + line + "\r\n").encode("ascii"))
    return subprocess.run(["cmd", "/c", str(script)], capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=120)


def make_zip(path, rootname, files):
    with zipfile.ZipFile(path, "w") as z:
        for rel, content in files.items():
            z.writestr(f"{rootname}/{rel}", content)


def write(p: Path, content: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def age_tree(root: Path, seconds: int = 120) -> None:
    """Make a fake destination older than the fake source.

    Robocopy skips a destination file when it is newer AND has the same size
    (the "same" check). Two files written within the same clock tick can
    otherwise look identical, making a copy assertion flaky under load.
    """
    _t = time.time() - seconds
    for p in root.rglob("*"):
        if p.is_file():
            os.utime(p, (_t, _t))


# ── A. source guards ─────────────────────────────────────────────────────
def section_a():
    print("  -- A. source guards (update.bat / prune / package_release) --")
    rc_lines = robocopy_lines()
    apply_line = next((l for l in rc_lines if "%EXROOT%" in l), "")
    backup_line = next((l for l in rc_lines if "%BK%" in l), "")
    check("A1 two robocopy commands (apply + backup)",
          len(rc_lines) == 2 and bool(apply_line) and bool(backup_line),
          f" got {len(rc_lines)}")

    check("A2 apply /XD anchors to the SOURCE root (%EXROOT%\\models)",
          '"%EXROOT%\\models"' in apply_line)
    check("A3 backup /XD anchors to the install root (%BASE%\\models)",
          '"%BASE%\\models"' in backup_line)
    check("A4 no bare 'models' left in any /XD list",
          all("models" not in xd_bare_names(l) for l in rc_lines),
          f" {[(xd_bare_names(l)) for l in rc_lines]}")
    check("A5 no /MIR anywhere (user data would die)",
          not any("/mir" in l.lower() for l in rc_lines))
    check("A6 apply keeps /XF update.bat settings.json",
          "/XF update.bat settings.json" in apply_line)
    check("A7 backup keeps update_backup_* nesting exclude",
          "update_backup_*" in backup_line)

    check("A8 NROOTS counted in BOTH extract sections",
          UPDATE.count("set /a NROOTS=0") == 2
          and UPDATE.count('if not "%NROOTS%"=="1"') == 2)
    check("A9 pre-apply core/utils probes in the common path",
          UPDATE.count("no core\\ folder") >= 2
          and UPDATE.count("no utils\\ folder") >= 2)
    check("A10 pre-apply zip-version vs tag gate",
          'if /I not "%ZIPVER%"=="%TAGNUM%"' in UPDATE)

    check("A11 VCMP guard present", "if not defined VCMP" in UPDATE)
    check("A12 ASSET_DIGEST initialized", 'set "ASSET_DIGEST="' in UPDATE)
    check("A12b PRUNED count filtered to digits (error text cannot explode the if-line)",
          'findstr /r /c:"^[0-9][0-9]*$"`' in UPDATE
          and 'for /f "usebackq delims=" %%P in (`type "%TMPDIR%\\prune_count.txt"'
          in UPDATE)
    check("A13 digest lowercased before compare", ".Substring(7).ToLower()" in UPDATE)

    check("A14 TMQ defined from TMPDIR", 'set "TMQ=%TMPDIR:\'=\'\'%"' in UPDATE)
    check("A15 EXQ defined in BOTH extract sections",
          UPDATE.count('set "EXQ=%EXROOT:\'=\'\'%"') == 2)
    check("A16 no raw %TMPDIR% inside PS single quotes",
          "'%TMPDIR%" not in UPDATE)
    check("A17 no raw %EXROOT% inside PS single quotes",
          "'%EXROOT%" not in UPDATE)
    check("A18 inline Expand-Archive uses -LiteralPath",
          all("Expand-Archive -LiteralPath" in l
              for l in ULINES if "Expand-Archive" in l))

    stop_i = UPDATE.find('watchdog_engine.bat" stop')
    grace_i = UPDATE.find("ping -n 26 127.0.0.1")
    recheck_i = UPDATE.find("Get-NetTCPConnection", max(grace_i, 0))
    rearm_lines = [l for l in ULINES
                   if 'start "" /b /d' in l and "watchdog_engine.bat" in l]
    check("A19 watchdog stop -> grace wait -> port recheck, in order",
          0 < stop_i < grace_i < recheck_i)
    check("A19b grace wait uses ping (timeout /nobreak dies on redirected stdin)",
          "timeout /t" not in UPDATE)
    check("A20 watchdog re-arm appears exactly once", len(rearm_lines) == 1,
          f" got {len(rearm_lines)}")
    co_i = UPDATE.rfind('call "%~dp0deploy\\check_optionals.bat"')
    swap_i = UPDATE.find("FINAL SELF-SWAP")
    check("A21 re-arm after optional components, before self-swap",
          bool(rearm_lines) and co_i < UPDATE.find(rearm_lines[0]) < swap_i)
    check("A22 apply-failure message names the stopped watchdog",
          "watchdog is STOPPED" in UPDATE)

    check("A23 prune never moves the running update.bat",
          "($_ -ne 'update.bat')" in PRUNE_PS1)
    check("A24 package_release cross-checks zip version vs name",
          'if not "%ZIPVER%"=="%VER%"' in PKG_BAT
          and "HiveMind_v%VER%_release" in PKG_BAT)
    check("A25 package_release deletes a rejected zip",
          PKG_BAT.count('del "..\\%OUT%"') >= 3)

    for keep in ("render_release_notes.ps1", "update_prune.ps1",
                 "check_optionals.bat", "setup_voice.bat", "fetch_models.py"):
        check(f"A26 .gitattributes does NOT export-ignore {keep}",
              not re.search(r"^/deploy/" + re.escape(keep) + r"\s+export-ignore",
                            ATTRS, re.M))
    for dev in ("prepush_check.ps1", "promote_main.bat", "release_checks.bat"):
        check(f"A27 .gitattributes export-ignores {dev}",
              re.search(r"^/deploy/" + re.escape(dev) + r"\s+export-ignore",
                        ATTRS, re.M))


# ── B1. apply robocopy fragment against a fake install ───────────────────
def section_b1(tmp: Path):
    print("  -- B1. apply robocopy fragment (real robocopy, fake trees) --")
    ex = tmp / "apply_ex" / "HiveMind_v1.3.9-audit_release"
    base = tmp / "apply_base"
    write(ex / "core" / "a.py", "new core\n")
    write(ex / "utils" / "b.py", "new utils\n")
    write(ex / "core" / "version.py", SERVER_PY)
    write(ex / "model_configs" / "models" / "m1.json", '{"zip": true}\n')
    write(ex / "models" / "big.gguf", "zip gguf\n")
    write(ex / "update.bat", "@echo zip updater\n")
    write(ex / "settings.json", '{"token":"LEAK"}\n')
    write(ex / "sessions" / "s.bin", "zip session\n")

    # Distinct size/timestamp: robocopy skips same-size+same-tick files.
    write(base / "core" / "a.py", "old core file\n")
    write(base / "model_configs" / "models" / "m1.json", '{"old": true}\n')
    write(base / "models" / "big.gguf", "user gguf\n")
    write(base / "settings.json", '{"token":"USER"}\n')
    write(base / "sessions" / "s.bin", "user session\n")
    write(base / ".hive_uploads" / "u.bin", "user upload\n")
    write(base / "update.bat", "@echo running updater\n")
    age_tree(base)

    line = next(l for l in robocopy_lines() if "%EXROOT%" in l)
    line = line.replace("%EXROOT%", str(ex)).replace("%BASE%", str(base))
    r = run_cmd_fragment(tmp, "apply", line)
    check("B1 robocopy rc < 8", r.returncode < 8, f" rc={r.returncode}")
    check("B1 tracked model_configs/models json APPLIED (the fix)",
          (base / "model_configs" / "models" / "m1.json").read_text(encoding="utf-8")
          == '{"zip": true}\n')
    check("B1 top-level models/ untouched (anchored exclude)",
          (base / "models" / "big.gguf").read_text(encoding="utf-8") == "user gguf\n")
    check("B1 release code applied",
          (base / "core" / "a.py").read_text(encoding="utf-8") == "new core\n")
    check("B1 settings.json NOT clobbered (/XF)",
          (base / "settings.json").read_text(encoding="utf-8") == '{"token":"USER"}\n')
    check("B1 running update.bat NOT clobbered (/XF)",
          (base / "update.bat").read_text(encoding="utf-8") == "@echo running updater\n")
    check("B1 sessions/ untouched (/XD)",
          (base / "sessions" / "s.bin").read_text(encoding="utf-8") == "user session\n")
    check("B1 .hive_uploads/ untouched (/XD)",
          (base / ".hive_uploads" / "u.bin").read_text(encoding="utf-8") == "user upload\n")


# ── B2. backup robocopy fragment ─────────────────────────────────────────
def section_b2(tmp: Path):
    print("  -- B2. backup robocopy fragment --")
    base = tmp / "backup_base"
    write(base / "core" / "a.py", "code\n")
    write(base / "model_configs" / "models" / "m1.json", '{"current": true}\n')
    write(base / "models" / "big.gguf", "user gguf\n")
    write(base / "settings.json", '{"token":"USER"}\n')
    write(base / "sessions" / "s.bin", "user session\n")
    write(base / "update_backup_1.3.8" / "core" / "old.py", "old backup\n")
    age_tree(base)

    bk = "update_backup_1.3.9-audit"
    line = next(l for l in robocopy_lines() if "%BK%" in l)
    line = line.replace("%BASE%", str(base)).replace("%BK%", bk)
    r = run_cmd_fragment(tmp, "backup", line)
    check("B2 robocopy rc < 8", r.returncode < 8, f" rc={r.returncode}")
    dst = base / bk
    check("B2 robocopy rc < 8", r.returncode < 8, f" rc={r.returncode}")
    check("B2 model_configs/models json BACKED UP (the fix)",
          (dst / "model_configs" / "models" / "m1.json").read_text(encoding="utf-8")
          == '{"current": true}\n')
    check("B2 settings.json in the backup (rollback works)",
          (dst / "settings.json").read_text(encoding="utf-8") == '{"token":"USER"}\n')
    check("B2 top-level models/ excluded (gigabytes)",
          not (dst / "models").exists())
    check("B2 sessions/ excluded", not (dst / "sessions").exists())
    check("B2 old backup NOT nested into the new one",
          not (dst / "update_backup_1.3.8").exists())
    check("B2 source tree untouched", (base / "core" / "a.py").exists())


# ── B3. odd zip layouts vs the extract+count logic ───────────────────────
def section_b3(tmp: Path):
    print("  -- B3. odd zip layouts --")
    good = tmp / "layout_good.zip"
    make_zip(good, "HiveMind_v1.3.9-audit_release",
             {"core/x.py": "1", "core/version.py": SERVER_PY})
    multi = tmp / "layout_multi.zip"
    with zipfile.ZipFile(multi, "w") as z:
        z.writestr("HiveMind_v1.3.9-audit_release/core/x.py", "1")
        z.writestr("HiveMind_v1.3.8_old/core/x.py", "2")
        z.writestr("__MACOSX/junk", "3")
        z.writestr("loose.txt", "4")
    loose = tmp / "layout_loose.zip"
    with zipfile.ZipFile(loose, "w") as z:
        z.writestr("loose.txt", "4")

    counts = {}
    for name, zp in (("good", good), ("multi", multi), ("loose", loose)):
        dest = tmp / f"layout_ex_{name}"
        r = ps_command(f"Expand-Archive -LiteralPath '{zp}' "
                       f"-DestinationPath '{dest}' -Force")
        check(f"B3 extract {name}.zip", r.returncode == 0, f" rc={r.returncode}")
        counts[name] = sum(1 for k in dest.iterdir() if k.is_dir())
    check("B3 good layout: exactly 1 root folder", counts["good"] == 1)
    check("B3 multi-root hazard present (two build folders + __MACOSX junk)",
          counts["multi"] == 3, f" dirs={counts['multi']}")
    check("B3 loose-only zip has 0 root folders (EXROOT empty path)",
          counts["loose"] == 0)
    check("B3 NROOTS guards reject counts != 1 in both sections",
          UPDATE.count('if not "%NROOTS%"=="1"') == 2)


# ── B4. apostrophe/bracket install path vs the PS fragments ──────────────
def section_b4(tmp: Path):
    print("  -- B4. apostrophe/bracket install path --")
    nasty = tmp / "O'Brien's HD [1]"
    nasty.mkdir()
    zp_src = tmp / "nasty.zip"
    make_zip(zp_src, "HiveMind_v1.3.9-audit_release",
             {"core/x.py": "1", "core/version.py": SERVER_PY})
    tmpdir = nasty / "_update_tmp"
    tmpdir.mkdir(parents=True)
    zp = tmpdir / "release.zip"
    shutil.copy(zp_src, zp)

    ex_line = next(l for l in ULINES
                   if "Expand-Archive" in l and "-LiteralPath" in l).strip()
    body = extract_inline_ps(ex_line, backquoted=False)
    check("B4 extraction fragment found in update.bat", body is not None)
    if body:
        fixed = body.replace("%TMQ%", str(tmpdir).replace("'", "''"))
        r = ps_command(fixed)
        extracted = tmpdir / "extracted"
        check("B4 extract works on an apostrophe+bracket path (the fix)",
              r.returncode == 0
              and (extracted / "HiveMind_v1.3.9-audit_release" / "core").is_dir(),
              f" rc={r.returncode}")
        exroot = extracted / "HiveMind_v1.3.9-audit_release"
        v_line = next(l for l in ULINES
                      if "HIVEMIND_VERSION" in l and "-LiteralPath" in l)
        vbody = extract_inline_ps(v_line, backquoted=True)
        check("B4 version-probe fragment found", vbody is not None)
        if vbody:
            fixed_v = vbody.replace("%EXQ%", str(exroot).replace("'", "''"))
            r2 = ps_command(fixed_v)
            check("B4 version parsed from apostrophe path (the fix)",
                  r2.stdout.strip() == "1.3.9-audit", f" got {r2.stdout.strip()!r}")

    # the OLD unsafe form must demonstrably fail on this path
    old_body = (f"Expand-Archive -Path '{zp}' "
                f"-DestinationPath '{tmpdir}\\extracted_old' -Force")
    r3 = ps_command(old_body)
    check("B4 OLD -Path form breaks on an apostrophe path (documents the bug)",
          r3.returncode != 0
          or not (tmpdir / "extracted_old" / "HiveMind_v1.3.9-audit_release").exists(),
          f" rc={r3.returncode}")


# ── B5/B6. update_prune.ps1: running updater + apostrophe base ───────────
def _run_prune(base: Path, ex: Path, trash: Path, manifest: Path):
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(ROOT / "deploy" / "update_prune.ps1"),
         "-Base", str(base), "-Ex", str(ex),
         "-Trash", str(trash), "-Manifest", str(manifest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)


def section_b5(tmp: Path):
    print("  -- B5. prune protects the running updater --")
    base, ex = tmp / "prune2_base", tmp / "prune2_ex"
    write(base / "core" / "stale.py", "stale\n")
    write(base / "update.bat", "@echo RUNNING\n")
    write(ex / "core" / "keep.py", "new\n")
    manifest = base / "update_manifest_installed.txt"
    # the manifest lists update.bat (it shipped in the PREVIOUS zip) but the
    # new release dropped it - the running script must survive
    manifest.write_text("core/stale.py\nupdate.bat\n", encoding="utf-8")
    r = _run_prune(base, ex, base / "update_removed_1.3.9-audit", manifest)
    check("B5 prune ran", r.returncode == 0,
          f" rc={r.returncode} {(r.stdout + r.stderr)[-80:]!r}")
    check("B5 RUNNING update.bat NOT moved (the fix)",
          (base / "update.bat").read_text(encoding="utf-8") == "@echo RUNNING\n")
    check("B5 stale release file still moved",
          not (base / "core" / "stale.py").exists()
          and (base / "update_removed_1.3.9-audit" / "core" / "stale.py").exists())
    check("B5 count output is 1", (r.stdout or "").strip().endswith("1"))


def section_b6(tmp: Path):
    print("  -- B6. prune with an apostrophe install path --")
    base = tmp / "O'Brien's HD [1]" / "prune_base"
    ex = tmp / "prune6_ex"
    write(base / "core" / "stale.py", "stale\n")
    write(base / "core" / "keep.py", "kept\n")
    write(ex / "core" / "keep.py", "new\n")
    manifest = base / "update_manifest_installed.txt"
    manifest.write_text("core/keep.py\ncore/stale.py\n", encoding="utf-8")
    r = _run_prune(base, ex, base / "update_removed_1.3.9-audit", manifest)
    check("B6 prune ran on apostrophe path", r.returncode == 0,
          f" rc={r.returncode} {(r.stdout + r.stderr)[-80:]!r}")
    check("B6 stale moved, kept file intact",
          not (base / "core" / "stale.py").exists()
          and (base / "core" / "keep.py").read_text(encoding="utf-8") == "kept\n")


# ── B7. package_release zip-version cross-check fragment ─────────────────
def section_b7(tmp: Path):
    print("  -- B7. package_release zip-version probe fragment --")
    line = next((l for l in PKG_BAT.splitlines()
                 if "OpenRead" in l and "ZIPVER" in l), "")
    body = extract_inline_ps(line, backquoted=True)
    check("B7 cross-check fragment found", body is not None)
    if not body:
        return
    repo = tmp / "pkg_repo"
    repo.mkdir()
    rootname = "HiveMind_v1.3.9-audit_release"
    for ver in ("1.3.9-audit", "1.3.8-old"):
        zp = tmp / "HiveMind_v1.3.9-audit_release.zip"
        make_zip(zp, rootname, {"core/version.py": f"HIVEMIND_VERSION = '{ver}'\n"})
        fixed = body.replace("..\\%OUT%", "..\\HiveMind_v1.3.9-audit_release.zip")
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command", fixed],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=90, cwd=str(repo),
            env=dict(os.environ, ROOTNAME=rootname))
        got = (r.stdout or "").strip()
        if ver == "1.3.9-audit":
            check("B7 matching zip reports its version", got == "1.3.9-audit",
                  f" got {got!r} rc={r.returncode}")
        else:
            check("B7 mismatched zip reports the OLD version (bat guard aborts)",
                  got == "1.3.8-old" and 'if not "%ZIPVER%"=="%VER%"' in PKG_BAT,
                  f" got {got!r}")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hvm_update_audit_"))
    try:
        section_a()
        section_b1(tmp)
        section_b2(tmp)
        section_b3(tmp)
        section_b4(tmp)
        section_b5(tmp)
        section_b6(tmp)
        section_b7(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print(f"SUMMARY update_flow_audit: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()
