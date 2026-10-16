"""The wedge-handoff (fix agent) must be switchable from the UI.

duo_wedge_handoff_enabled defaults to False, so the fix agent never runs on a
fresh install - and until now the only way to switch it on was editing
settings.json by hand (the key was in no UI control). Owner request
2026-10-17: "the fix agent should be togglable in the UI".

Pins:
  (a) index.html carries the checkbox with the real settings key + id
  (b) app.js reads the setting into S.duoWedgeHandoff (default off)
  (c) app.js syncs the checkbox on settings load
  (d) the app.js cache buster was bumped (otherwise the browser keeps the old
      bundle and the toggle appears dead)
  (e) the setting is in DEFAULT_SETTINGS and documented

Run: python tests/test_wedge_ui_toggle.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


_html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")

# ── (a) markup ──────────────────────────────────────────────────────────────
check("index.html has the toggle input",
      'id="duo-wedge-handoff-toggle"' in _html)
check("the toggle posts the real settings key",
      "onFlagToggle('duo_wedge_handoff_enabled', this.checked)" in _html)
check("the toggle has a visible label",
      "Wedge handoff" in _html)
check("the toggle explains what it does",
      "fresh-context repair agent" in _html)

# ── (b) read + (c) sync ─────────────────────────────────────────────────────
check("app.js reads the setting",
      "S.duoWedgeHandoff  = s.duo_wedge_handoff_enabled === true;" in _js)
check("app.js defaults it to off when absent",
      "=== true" in _js.split("S.duoWedgeHandoff")[1][:40])
check("app.js syncs the checkbox",
      "_syncFlagCheck('duo-wedge-handoff-toggle', S.duoWedgeHandoff);" in _js)

# ── (d) cache buster ────────────────────────────────────────────────────────
_m = re.search(r'app\.js\?v=(\d{8})-(\d+)', _html)
check("app.js cache buster present", _m is not None)
if _m:
    check("cache buster bumped past the last UI change",
          (_m.group(1), int(_m.group(2))) >= ("20261017", 1),
          f" ({_m.group(0)})")

# ── (e) settings + docs ─────────────────────────────────────────────────────
from settings import DEFAULT_SETTINGS                           # noqa: E402

check("setting exists and defaults to off",
      DEFAULT_SETTINGS.get("duo_wedge_handoff_enabled") is False)
_doc = (ROOT / "docs" / "settings.md").read_text(encoding="utf-8", errors="replace")
check("docs/settings.md documents it", "`duo_wedge_handoff_enabled`" in _doc)

# every id referenced by _syncFlagCheck must exist in the HTML
_ids = re.findall(r"_syncFlagCheck\('([^']+)'", _js)
_missing = [i for i in _ids if f'id="{i}"' not in _html]
check("every synced toggle id exists in index.html", not _missing,
      f" (missing {_missing})")

print()
print(f"SUMMARY wedge_ui_toggle: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
