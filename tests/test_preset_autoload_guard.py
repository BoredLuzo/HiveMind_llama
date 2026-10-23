"""The startup preset auto-load must never revert newer user settings.

Live demonstration (2026-10-23): a settings.json edit was silently
reverted by the next engine boot, because the auto-load re-applied the
active preset snapshot unconditionally and PERSISTED it. The guard:
a preset application stamps preset_applied_at == settings_saved_at in
one write; a boot skips the auto-load when settings_saved_at is newer.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from routers.config import preset_autoload_stale  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── the decision itself ──────────────────────────────────────────────────
check("no stamps (old settings file) -> applies (historical behavior)",
      preset_autoload_stale(None, None) is False)
check("applied only (no saved_at) -> applies",
      preset_autoload_stale("2026-10-23T14:18:00+02:00", None) is False)
check("saved only (no applied_at) -> applies",
      preset_autoload_stale(None, "2026-10-23T14:20:00+02:00") is False)
check("equal stamps (a load persists both in one write) -> applies",
      preset_autoload_stale("2026-10-23T14:18:00+02:00",
                            "2026-10-23T14:18:00+02:00") is False)
check("settings saved AFTER the preset -> stale, skip the auto-load",
      preset_autoload_stale("2026-10-23T14:18:00+02:00",
                            "2026-10-23T14:30:00+02:00") is True)
check("preset applied AFTER the last save (clock skew) -> applies",
      preset_autoload_stale("2026-10-23T14:30:00+02:00",
                            "2026-10-23T14:18:00+02:00") is False)
check("garbage stamps -> applies (never block on a parse issue)",
      preset_autoload_stale("not-a-date", "also-not") is False)

# ── the wiring exists ────────────────────────────────────────────────────
cfg_src = (ROOT / "routers" / "config.py").read_text(encoding="utf-8", errors="replace")
set_src = (ROOT / "settings.py").read_text(encoding="utf-8", errors="replace")
check("the autoload consults the guard",
      "preset_autoload_stale(settings.get(\"preset_applied_at\")" in cfg_src
      and "settings.get(\"settings_saved_at\")" in cfg_src)
check("a preset application stamps applied_at == saved_at in one write",
      'settings["preset_applied_at"] = _stamp' in cfg_src
      and "save_settings, settings, saved_at=_stamp" in cfg_src)
check("every plain save stamps settings_saved_at",
      'settings["settings_saved_at"] = saved_at or _now_iso()' in set_src)

print()
print(f"SUMMARY preset_autoload_guard: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
