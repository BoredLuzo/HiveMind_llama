# -*- coding: utf-8 -*-
"""Behavior tests for deploy/updater.py (2026-11-07 audit).

The updater writes into the LIVE installation, but only string pins covered
it. This suite exercises the pure helpers and one full `main()` pass against
a fake install + a local release zip. The process seam (`_run_rc`,
watchdog, prune, deps) is faked; the backup->apply ORDER and the
BUILD_INFO fallback are the assertions.
"""
import sys
import shutil
import tempfile
import zipfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from deploy import updater as U  # noqa: E402

passed = 0
failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}{extra}")
    else:
        failed += 1
        print(f"  FAIL {name}{extra}")


# ── 1. pure version comparer ────────────────────────────────────────────────
check("numeric triple parses", U._numeric_triple("1.5.5") == [1, 5, 5])
check("numeric triple rejects junk", U._numeric_triple("1.5.x") is None)
check("prerelease ordinal: clean release", U._prerelease_ordinal("1.5.5") == 999)
check("prerelease ordinal: preview.2", U._prerelease_ordinal("1.5.5-preview.2") == 3)
check("compare: newer", U.compare_versions("1.5.5", "1.6.0") == "newer")
check("compare: same", U.compare_versions("1.5.5", "1.5.5") == "same")
check("compare: older", U.compare_versions("1.6.0", "1.5.5") == "older")
check("compare: preview -> release is newer",
      U.compare_versions("1.5.5-preview", "1.5.5") == "newer")
check("compare: release -> preview is older",
      U.compare_versions("1.5.5", "1.5.5-preview") == "older")
check("compare: unparseable is None", U.compare_versions("x.y", "1.5.5") is None)

# ── 2. digest + warnings ────────────────────────────────────────────────────
_tmp = Path(tempfile.mkdtemp(prefix="hvm_upd_pure_"))
_z = _tmp / "a.zip"
_z.write_bytes(b"hello")
import hashlib as _hashlib  # noqa: E402
_digest = "sha256:" + _hashlib.sha256(b"hello").hexdigest().upper()
check("digest_ok accepts an uppercase API digest", U.digest_ok(_z, _digest) is True)
check("digest_ok rejects a wrong digest",
      U.digest_ok(_z, "sha256:" + "0" * 64) is False)
check("digest_ok skips when metadata has no digest",
      U.digest_ok(_z, "") is None and U.digest_ok(_z, "md5:abc") is None)

_body = ("# Release\n"
         "## Warning: migration needed\n"
         "## No breaking changes\n"
         "known issues: none\n"
         "BREAKING: config rename\n")
_w = U.extract_warnings(_body)
check("extract_warnings finds warning + known issues + breaking",
      len(_w) == 3 and any("BREAKING" in x for x in _w), f" ({_w})")
check("extract_warnings ignores 'no breaking changes'",
      all("No breaking" not in x for x in _w))
check("extract_warnings caps at five",
      len(U.extract_warnings("\n".join(["warning"] * 20))) == 5)

# ── 3. one full main() pass against a fake install ─────────────────────────
_fake = Path(tempfile.mkdtemp(prefix="hvm_upd_main_"))
_old = _fake / "core" / "version.py"
_old.parent.mkdir(parents=True)
_old.write_text('HIVEMIND_VERSION = "1.0.0"\n', encoding="utf-8")
(_fake / "utils").mkdir()
(_fake / "settings.json").write_text('{"user": "data"}\n', encoding="utf-8")
(_fake / "update.bat").write_text("@echo OLD SHIM\n", encoding="utf-8")
(_fake / "deploy").mkdir()

_zip = _tmp / "release.zip"
with zipfile.ZipFile(_zip, "w") as z:
    base = "HiveMind_v2.0.0_release/"
    z.writestr(base + "core/version.py", 'HIVEMIND_VERSION = "2.0.0"\n')
    z.writestr(base + "core/new_module.py", "# new\n")
    z.writestr(base + "utils/x.py", "# utils\n")
    z.writestr(base + "update.bat", "@echo NEW SHIM\n")

_calls = []


def _fake_run_rc(cmd):
    _calls.append(list(cmd))
    if cmd and cmd[0].lower() == "robocopy":
        src, dst = Path(cmd[1]), Path(cmd[2])
        if "update_backup_" in dst.name:
            # backup: only mark that it happened (copying into a subdir of
            # the source would recurse in this fake)
            dst.mkdir(parents=True, exist_ok=True)
            (dst / ".backup_marker").write_text("bak", encoding="utf-8")
        else:
            # apply: overlay everything except the protected top-level names
            (dst / "core").mkdir(parents=True, exist_ok=True)
            for p in src.rglob("*"):
                if not p.is_file():
                    continue
                rel = p.relative_to(src)
                if rel.parts and rel.parts[0] in U.APPLY_XF:
                    continue
                out = dst / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, out)
    return 1


_wd = {"stop": 0, "arm": 0}


def _stop_watchdog(base):
    _wd["stop"] += 1
    return True


def _arm_watchdog(base):
    _wd["arm"] += 1


with mock.patch.object(U, "__file__", str(_fake / "deploy" / "updater.py")), \
     mock.patch.object(U.shutil, "which", return_value="curl"), \
     mock.patch.object(U, "port_listener", return_value=False), \
     mock.patch.object(U, "_run_rc", side_effect=_fake_run_rc), \
     mock.patch.object(U, "stop_watchdog", side_effect=_stop_watchdog), \
     mock.patch.object(U, "arm_watchdog", side_effect=_arm_watchdog), \
     mock.patch.object(U.time, "sleep", return_value=None), \
     mock.patch.object(U, "run_prune", return_value=(0, False)), \
     mock.patch.object(U, "refresh_deps", return_value=None), \
     mock.patch.object(U, "refresh_uv_path", return_value=None):
    _rc = U.main(["--zip", str(_zip)])

check("main(local zip) exits 0", _rc == 0, f" (rc={_rc})")
check("backup before apply", len(_calls) >= 2
      and "update_backup_2.0.0" in str(_calls[0])
      and "update_backup_" not in str(_calls[1]), f" ({_calls[:2]})")
check("backup uses the install as source", _calls and Path(_calls[0][1]) == _fake)
check("apply uses the extracted zip tree",
      len(_calls) > 1 and "extracted" in str(_calls[1][1]))
check("backup directory exists",
      (_fake / "update_backup_2.0.0" / ".backup_marker").exists())
check("new core module applied", (_fake / "core" / "new_module.py").exists())
check("settings.json untouched (apply exclude)",
      (_fake / "settings.json").read_text(encoding="utf-8") == '{"user": "data"}\n')
check("running update.bat untouched",
      (_fake / "update.bat").read_text(encoding="utf-8") == "@echo OLD SHIM\n")
check("incoming update.bat staged for self-swap",
      (_fake / ".update_new.bat").read_text(encoding="utf-8") == "@echo NEW SHIM\n")
check("BUILD_INFO fallback written",
      (_fake / "BUILD_INFO.txt").exists()
      and "version: 2.0.0" in (_fake / "BUILD_INFO.txt").read_text(encoding="utf-8"))
check("watchdog stopped then re-armed", _wd["stop"] == 1 and _wd["arm"] == 1,
      f" ({_wd})")
check("temp update dir cleaned", not (_fake / U.TMPDIR_NAME).exists())

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
