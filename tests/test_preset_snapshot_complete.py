"""Deep preset audit, offline: a snapshot must carry every persisted setting.

GET /settings decorates its response with derived keys (settings_rev,
git_token_saved, moe_pins, moe_autodetect, moe_expert_defaults, ctx_defaults)
that are NOT stored settings - they are built per request. Everything else must
be in the snapshot, or a preset load silently drops it.
"""
import copy
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import routers.config as rc          # noqa: E402
from settings import DEFAULT_SETTINGS  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


_GET_ONLY = {"settings_rev", "git_token_saved", "moe_pins", "moe_autodetect",
             "moe_expert_defaults", "ctx_defaults"}
_EXPECTED_EXCLUDED = {"git_token", "_registry", "models_dir",
                      "image_processing_mode", "vision_agent_mode",
                      # BOOKKEEPING (2026-10-28): written by the apply path
                      # itself, never stored in a snapshot
                      "active_preset", "settings_saved_at", "preset_applied_at"}

saved = copy.deepcopy(rc.settings)
try:
    rc.settings.clear()
    rc.settings.update(copy.deepcopy(DEFAULT_SETTINGS))
    snap = rc._preset_snapshot()

    missing = sorted(k for k in DEFAULT_SETTINGS
                     if k not in snap and k not in _EXPECTED_EXCLUDED
                     and k not in _GET_ONLY)
    check("every default setting is in the snapshot", not missing, f"({missing})")
    check("secrets and machine paths stay out",
          not ({"git_token", "models_dir"} & set(snap)))
    check("no GET-only decoration leaks in",
          not (_GET_ONLY & set(snap)), f"({sorted(_GET_ONLY & set(snap))})")

    # nested structures survive as-is
    for k in ("agents", "mcp_servers", "automap_excluded", "ctx_overrides"):
        if k in DEFAULT_SETTINGS:
            a = DEFAULT_SETTINGS[k]
            b = snap.get(k)
            same = (b == a) if not isinstance(a, dict) else (
                set(a) <= set(b or {}))
            check(f"nested '{k}' survives", same)

    # a value check on a few representative types
    for k in ("tts_volume", "duo_planner_max_tokens", "telegram_result_mode",
              "duo_chunking", "run_bash_shell"):
        if k in DEFAULT_SETTINGS:
            check(f"value of {k} is copied", snap.get(k) == DEFAULT_SETTINGS[k],
                  f"({snap.get(k)!r})")
finally:
    rc.settings.clear()
    rc.settings.update(saved)

print()
print(f"SUMMARY preset_snapshot_complete: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
