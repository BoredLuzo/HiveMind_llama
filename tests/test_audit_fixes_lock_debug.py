"""Pins for the 2026-10-18 audit fixes: lockfile, debug gate, log metadata."""
import io
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


install = (ROOT / "install.bat").read_text(encoding="utf-8", errors="replace")
update = (ROOT / "update.bat").read_text(encoding="utf-8", errors="replace")
linux = (ROOT / "deploy" / "install_linux.sh").read_text(encoding="utf-8", errors="replace")
server = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
tool = (ROOT / "utils" / "tool.py").read_text(encoding="utf-8", errors="replace")
attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8", errors="replace")

# ── reproducibility ──────────────────────────────────────────────────────
# existence is not enough: .gitignore had `*.lock`, so `git add -A` skipped
# the lockfile and `git archive` (the release zip) never saw it.
_tracked = subprocess.run(["git", "ls-files", "uv.lock"], cwd=str(ROOT),
                         capture_output=True, text=True).stdout.strip()
check("uv.lock is tracked by git", _tracked.endswith("uv.lock"),
      " (untracked -> it cannot ship)")
check("install.bat syncs frozen", "--all-extras --frozen" in install)
check("update.bat prefers the lock", "--frozen --all-extras" in update)
check("linux installer syncs frozen", "--frozen" in linux)
check("uv.lock is not export-ignored", "/uv.lock" not in attrs)

# ── debug endpoints ──────────────────────────────────────────────────────
from settings import DEFAULT_SETTINGS  # noqa: E402
check("debug_api_enabled defaults to False",
      DEFAULT_SETTINGS.get("debug_api_enabled") is False)
check("the debug router is gated",
      'if settings.get("debug_api_enabled", False):\n    app.include_router(debug_router)'
      in server)

# ── truncation log keeps no user code ────────────────────────────────────
check("truncation entry has no raw payload", '"raw": str(' not in tool)
check("truncation entry keeps the path", '"path": _path_hint' in tool)
check("truncation entry records json_complete", '"json_complete"' in tool)

print()
print(f"SUMMARY audit_fixes_2026_10_18: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
