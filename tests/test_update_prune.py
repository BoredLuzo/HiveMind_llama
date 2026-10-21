"""update_prune.ps1 verification (2026-10-16 updater parse-error finding).

The prune used to be a giant inline powershell -Command inside update.bat
that broke in cmd quoting on the live box (parse error - prune AND
manifest rewrite silently skipped). The logic now ships as
deploy/update_prune.ps1. This suite drives it against a fake install
tree in %TEMP%: stale release file moves to the trash, user-data stays,
the manifest is rewritten from the new tree, the count is printed.
update.bat itself is never executed."""
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
PS1 = ROOT / "deploy" / "update_prune.ps1"


def main():
    tmp = Path(tempfile.mkdtemp(prefix="hvm_prune_"))
    base, ex = tmp / "base", tmp / "ex"
    base.mkdir()
    ex.mkdir()

    # Previous install (base): two release files (one stale), one user file
    for rel in ("core/keep.py", "core/stale.py"):
        f = base / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(rel, encoding="utf-8")
    (base / "settings.json").write_text("{}", encoding="utf-8")
    # New zip tree (ex): ships core/keep.py only - core/stale.py was dropped
    keep = ex / "core" / "keep.py"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_text("new build", encoding="utf-8")
    # Old manifest: lists both release files AND the user file
    manifest = base / "update_manifest_installed.txt"
    manifest.write_text("core/keep.py\ncore/stale.py\nsettings.json\n",
                        encoding="utf-8")

    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(PS1), "-Base", str(base), "-Ex", str(ex),
         "-Trash", str(base / "update_removed_1.4.7"),
         "-Manifest", str(manifest)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120)
    out = (r.stdout + r.stderr).strip()
    check("script ran, printed pruned count", r.returncode == 0
          and out.endswith("1"), f" out={out[-60:]!r}")
    check("stale release file moved to trash",
          not (base / "core" / "stale.py").exists()
          and (base / "update_removed_1.4.7" / "core" / "stale.py").exists())
    check("kept release file untouched", (base / "core" / "keep.py").exists())
    check("protected user file untouched", (base / "settings.json").exists())
    # Windows PowerShell's Set-Content -Encoding UTF8 writes a BOM and
    # Join-Path uses backslashes - that is the established on-disk shape
    # (Get-Content strips the BOM again at the next prune's read).
    lines = manifest.read_text(encoding="utf-8-sig").split()
    check("manifest rewritten from new tree",
          lines == [r"core\keep.py"], f" got {lines}")

    print()
    print(f"SUMMARY update_prune: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()
