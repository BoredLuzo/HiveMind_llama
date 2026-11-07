# -*- coding: utf-8 -*-
r"""Update/release path stress audit (2026-11-04, updater split 2026-11-06).

Pins the updater fixes from the update-path stress audit. The updater is
NEVER executed against a real install; since the logic moved from
update.bat to deploy/updater.py (split 5), the behavior pins call the
updater module functions directly against fake trees in %TEMP%, and the
source guards read updater.py + the shim. The PowerShell prune/package
helpers are still driven as real subprocesses.

Covered findings:
  1. bare /XD models excluded the tracked model_configs\models\*.json from
     the apply AND the backup - the anchored models dir must keep shipping
  2. a zip with several root folders applied the LAST one silently - the
     root-count guard must reject that in BOTH extract call sites
  3. the GitHub flow applied a wrong/empty zip layout and only noticed in
     the post-apply verify - core/utils/version probes must run pre-apply
  4. an install path with an apostrophe/brackets broke every inline
     PowerShell call - the Python updater extracts with zipfile (immune),
     the old PS -Path form must still demonstrably fail (documents the bug)
  5. an unparseable version left VCMP empty and fell through both version
     gates - compare_versions must return None and main must abort
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
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from tests._harness import Harness  # noqa: E402

h = Harness(style="summary", pad=True)
check = h.check


UPD = (ROOT / "deploy" / "updater.py").read_text(encoding="utf-8", errors="replace")
SHIM = (ROOT / "update.bat").read_text(encoding="utf-8", errors="replace")
PRUNE_PS1 = (ROOT / "deploy" / "update_prune.ps1").read_text(encoding="utf-8", errors="replace")
PKG_BAT = (ROOT / "deploy" / "package_release.bat").read_text(encoding="utf-8", errors="replace")
ATTRS = (ROOT / ".gitattributes").read_text(encoding="utf-8", errors="replace")

from deploy import updater as UPDMOD  # noqa: E402

SERVER_PY = "HIVEMIND_VERSION = '1.3.9-audit'\n"


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


def extract_inline_ps(line, backquoted):
    """Pull the -Command body out of a cmd line that calls powershell."""
    if backquoted:
        m = re.search(r'powershell -NoProfile -Command "(.+?)"`', line)
    else:
        m = re.search(r'powershell -NoProfile -Command "(.+)"\s*$', line)
    return m.group(1) if m else None


def xd_bare_names(cmd):
    """Tokens of the /XD list (names only, stops at the next /flag)."""
    seg = None
    for i, tok in enumerate(cmd):
        if tok == "/XD":
            seg = cmd[i + 1:]
            break
    names = []
    for tok in seg or []:
        if tok.startswith("/"):
            break
        names.append(tok)
    return names


# ── A. source guards ─────────────────────────────────────────────────────
def section_a():
    print("  -- A. source guards (updater.py / shim / prune / package_release) --")
    check("A1 both robocopy commands exist (apply + backup)",
          "def backup_cmd(" in UPD and "def apply_cmd(" in UPD
          and '"robocopy"' in UPD)
    _bk = UPDMOD.backup_cmd(Path("B"), "bk")
    _ap = UPDMOD.apply_cmd(Path("X"), Path("B"))
    check("A1b both built commands are robocopy /E (no /MIR)",
          _bk[0] == "robocopy" and _ap[0] == "robocopy"
          and "/E" in _bk and "/E" in _ap)

    check("A2 apply /XD anchors to the SOURCE root (zip models dir)",
          'exroot / "models"' in UPD and str(Path("X") / "models") in _ap)
    check("A3 backup /XD anchors to the install root (base models dir)",
          'base / "models"' in UPD and str(Path("B") / "models") in _bk)
    check("A4 no bare 'models' left in any /XD list",
          "models" not in xd_bare_names(_bk) and "models" not in xd_bare_names(_ap)
          and "models" not in UPDMOD.APPLY_XD and "models" not in UPDMOD.BACKUP_XD)
    check("A5 no /MIR in any robocopy command (user data would die)",
          not any("/mir" in str(t).lower() for t in _bk + _ap))

    check("A6 apply keeps /XF update.bat settings.json first",
          _ap[_ap.index("/XF") + 1: _ap.index("/XF") + 3]
          == ["update.bat", "settings.json"])
    check("A7 backup keeps update_backup_* nesting exclude",
          "update_backup_*" in UPDMOD.BACKUP_XD and "update_backup_*" in _bk)

    check("A8 root count checked at BOTH extract call sites",
          UPD.count("extract_release(") >= 2
          and UPD.count("Zip contained no") >= 1
          and UPD.count("root folders in the zip") >= 2)
    check("A9 pre-apply core/utils probes in the common path",
          UPD.count("no core\\\\ folder") >= 2
          and UPD.count("no utils\\\\ folder") >= 2)
    check("A10 pre-apply zip-version vs tag gate",
          "zipver.lower() != tagnum.lower()" in UPD)

    check("A11 unparseable versions abort (VCMP guard)",
          "def compare_versions(" in UPD and "if vcmp is None:" in UPD
          and UPDMOD.compare_versions("1.5.0x", "1.5.1") is None)
    check("A12 ASSET_DIGEST initialized",
          'asset_digest = ""' in UPD)
    check("A12b prune count filtered to a pure integer (error text cannot flow in)",
          're.search(r"^\\s*(\\d+)\\s*$"' in UPD)
    check("A13 digest lowercased before compare",
          'asset_digest[7:].lower()' in UPD and 'hexdigest().lower()' in UPD)

    check("A14 extraction is zipfile-based (no inline PS Expand-Archive left)",
          "Expand-Archive" not in UPD and "extractall" in UPD)
    check("A15 no PS-quoting shims left (%TMQ%/%EXQ% era is over)",
          "%TMQ%" not in UPD and "%EXQ%" not in UPD)

    stop_i = UPD.find('watchdog_engine.bat"), "stop"')
    grace_i = UPD.find("time.sleep(25)")
    recheck_i = UPD.find("port_listener(hm_port)", max(grace_i, 0))
    rearm_calls = [l for l in UPD.splitlines()
                   if l.strip().startswith("arm_watchdog(")]
    check("A19 watchdog stop -> grace wait -> port recheck, in order",
          0 < stop_i < grace_i < recheck_i, f" {stop_i} {grace_i} {recheck_i}")
    check("A19b grace wait is time.sleep (the shim carries no `timeout /t`)",
          "time.sleep(25)" in UPD and "timeout /t" not in SHIM)
    check("A19c port recheck is unconditional (only the grace wait is marker-gated)",
          "    if wd_was:\n        time.sleep(25)\n    if port_listener(hm_port):" in UPD)
    check("A20 watchdog re-arm appears exactly once (as a call)",
          len(rearm_calls) == 1, f" got {len(rearm_calls)}")
    co_i = UPD.find("check_optionals.bat")
    swap_i = SHIM.find("FINAL SELF-SWAP")
    # C3 fix: pause moved BEFORE the swap; the swap line is now the LAST
    # physical line (nothing may follow it) and carries the exit in both
    # branches.
    last_line = SHIM.rstrip().splitlines()[-1]
    check("A21 re-arm after optional components, shim self-swap is last",
          bool(rearm_calls) and co_i < UPD.find(rearm_calls[0])
          and swap_i > 0 and ".update_new.bat" in last_line
          and "exit /b" in last_line
          and SHIM.rfind("pause >nul") < swap_i)
    check("A22 apply-failure message names the stopped watchdog",
          "watchdog is STOPPED" in UPD)
    check("A28 optional components run in the same console (no capture, no timeout)",
          'subprocess.run(["cmd", "/c", str(co)], timeout=None)' in UPD)
    check("A29 shim resolves py -3.14 to a real path (quote-safe)",
          "print(sys.executable)" in SHIM and 'set "PY=py -3.14"' not in SHIM)
    check("A30 shim swaps update.bat only after a successful update",
          'if not "%UPD_RC%"=="0" exit /b %UPD_RC%' in SHIM)
    check("A31 updater main has a top-level TimeoutExpired/OSError guard",
          "except subprocess.TimeoutExpired as exc:" in UPD
          and "except OSError as exc:" in UPD)
    _rc_i = UPD.find("def _run_rc")
    _rc_seg = UPD[_rc_i:UPD.find("\ndef ", _rc_i + 5)]
    check("A32 _run_rc has no mid-apply timeout (alt batch had none)",
          "timeout=" not in _rc_seg)

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

    # shim guards: python resolution + hand-over + pause
    check("A28 shim hands over to deploy/updater.py",
          "deploy\\updater.py" in SHIM and "%*" in SHIM)
    check("A29 shim resolves .venv python first (matches start_hivemind.bat)",
          ".venv\\Scripts\\python.exe" in SHIM)
    check("A30 shim preserves the exit code",
          "UPD_RC=%ERRORLEVEL%" in SHIM and "exit /b %UPD_RC%" in SHIM)

    # A32 (2026-11-07): the packaging leak list is a hand copy of APPLY_XF.
    # A newly protected file that is not added there ships the user's state.
    _leak_line = next((l for l in PKG_BAT.splitlines() if "LEAK: " in l), "")
    _lm = re.search(r"-match '([^']+)'", _leak_line)
    _leak_re = re.compile(_lm.group(1)) if _lm else None
    _missing = []
    if _leak_re is None:
        _missing.append("<leak regex not found>")
    else:
        for _name in UPDMOD.APPLY_XF:
            if _name == "update.bat":  # must ship, overlaid by the updater
                continue
            _probe = "HiveMind_v1.0/" + _name.replace(".*", ".12345")
            if _leak_re.search(_probe) is None:
                _missing.append(_name)
    check("A32 package_release leak list covers APPLY_XF",
          not _missing, f"(missing: {_missing})")


# ── B1. apply robocopy against a fake install ────────────────────────────
def section_b1(tmp: Path):
    print("  -- B1. apply robocopy (real robocopy, fake trees) --")
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

    write(base / "core" / "a.py", "old core file\n")
    write(base / "model_configs" / "models" / "m1.json", '{"old": true}\n')
    write(base / "models" / "big.gguf", "user gguf\n")
    write(base / "settings.json", '{"token":"USER"}\n')
    write(base / "sessions" / "s.bin", "user session\n")
    write(base / ".hive_uploads" / "u.bin", "user upload\n")
    write(base / "update.bat", "@echo running updater\n")
    age_tree(base)

    cmd = UPDMOD.apply_cmd(ex, base)
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300)
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


# ── B2. backup robocopy against a fake install ───────────────────────────
def section_b2(tmp: Path):
    print("  -- B2. backup robocopy --")
    base = tmp / "backup_base"
    write(base / "core" / "a.py", "code\n")
    write(base / "model_configs" / "models" / "m1.json", '{"current": true}\n')
    write(base / "models" / "big.gguf", "user gguf\n")
    write(base / "settings.json", '{"token":"USER"}\n')
    write(base / "sessions" / "s.bin", "user session\n")
    write(base / "update_backup_1.3.8" / "core" / "old.py", "old backup\n")
    age_tree(base)

    bk = "update_backup_1.3.9-audit"
    r = subprocess.run(UPDMOD.backup_cmd(base, bk), capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=300)
    check("B2 robocopy rc < 8", r.returncode < 8, f" rc={r.returncode}")
    dst = base / bk
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


# ── B3. odd zip layouts vs extract_release ────────────────────────────────
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

    for name, want_count, want_root in (("good", 1, True), ("multi", 3, False),
                                        ("loose", 0, False)):
        n, root = UPDMOD.extract_release(
            tmp / f"layout_{name}.zip", tmp / f"layout_ex_{name}")
        check(f"B3 extract {name}.zip: {want_count} root folder(s)",
              n == want_count, f" got {n}")
        if want_root:
            check(f"B3 {name}.zip returns the single root", root is not None)
        else:
            check(f"B3 {name}.zip rejects the layout (root=None)", root is None)
    check("B3 root-count guards reject counts != 1 in both sections",
          UPD.count("root folders in the zip") >= 2)


# ── B4. apostrophe/bracket install path vs the Python extractor ──────────
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

    n, exroot = UPDMOD.extract_release(zp, tmpdir / "extracted")
    check("B4 extract works on an apostrophe+bracket path (the fix)",
          n == 1 and exroot is not None
          and (exroot / "core").is_dir(), f" n={n}")
    if exroot is not None:
        check("B4 version parsed from apostrophe path (the fix)",
              UPDMOD.read_version(exroot / "core") == "1.3.9-audit",
              f" got {UPDMOD.read_version(exroot / 'core')!r}")

    # the OLD unsafe form must demonstrably fail on this path
    ps_cmd = (f"Expand-Archive -Path '{zp}' "
              f"-DestinationPath '{tmpdir}\\extracted_old' -Force")
    r3 = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd],
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=90)
    check("B4 OLD PS -Path form breaks on an apostrophe path (documents the bug)",
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
    body = extract_inline_ps(line, backquoted=True) if line else None
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
    sys.exit(h.finish("update_flow_audit"))


main()
