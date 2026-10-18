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


# ── item 4: no dead switches ─────────────────────────────────────────────
from settings import DEFAULT_SETTINGS as _D  # noqa: E402
_DEAD = ("automap_code_duo_enabled", "automap_duo_pre_explore",
         "automap_duo_parallel_preexplore", "automap_pipeline_websearch_enabled",
         "duo_distilled_executor", "startup_preload_analyst",
         "startup_preload_coder", "startup_preload_judge_in_agentic")
_still = [k for k in _DEAD if k in _D]
check("dead settings are gone from DEFAULT_SETTINGS", not _still, f"({_still})")
appjs = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
_uses = [k for k in _DEAD if f"postSettings({{{k}:" in appjs]
check("the UI no longer writes them", not _uses, f"({_uses})")
check("Automap pre-explore drives the real key",
      "postSettings({duo_pre_explore:" in appjs)
check("Automap websearch drives the real key",
      "postSettings({pipeline_websearch_enabled:" in appjs)
check("startup preload keeps its working switch",
      "postSettings({startup_preload_enabled:" in appjs)


# ── the settings doc is generated and current ────────────────────────────
_r = subprocess.run([sys.executable, str(ROOT / "deploy" / "gen_settings_docs.py"),
                     "--check"], cwd=str(ROOT), capture_output=True, text=True)
check("docs/settings.md is generated and current", _r.returncode == 0,
      (_r.stdout or _r.stderr).strip()[:60])

print()
print(f"SUMMARY audit_fixes_2026_10_18: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
