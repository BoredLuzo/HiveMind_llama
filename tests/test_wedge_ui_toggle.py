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
# BETA WARNING (owner request 2026-10-17): the feature is young and off by
# default - the row says both, in the same badge style the other cards use.
check("the row carries a BETA badge",
      'class="cfl-badge"' in _html and ">BETA</span>" in _html)
check("the badge sits in the wedge row",
      _html.index(">BETA</span>") < _html.index('id="duo-wedge-handoff-toggle"')
      and _html.index(">BETA</span>") > _html.index("Wedge handoff"))
check("the badge uses the amber warning colour", "var(--amber)" in _html[_html.index(">BETA</span>") - 320:_html.index(">BETA</span>")])
check("the hint states beta explicitly", "Beta &mdash; off by default." in _html)
check("the hint names the setting key", "duo_wedge_handoff_enabled" in _html)
# ── default off, in code and in every shipped preset template ───────────────
from settings import DEFAULT_SETTINGS                            # noqa: E402
check("settings.py default is OFF",
      DEFAULT_SETTINGS.get("duo_wedge_handoff_enabled") is False)
_live_presets = Path(r"C:\Users\user\Desktop\HiveMind\live\presets.json")
if _live_presets.exists():
    import json as _json                                          # noqa: E402
    _pd = _json.loads(_live_presets.read_text(encoding="utf-8"))
    _on = [n for n, b in _pd.items()
           if isinstance(b, dict) and b.get("duo_wedge_handoff_enabled") is True]
    check("no shipped/ live preset turns it on", not _on, f" (on: {_on})")

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
