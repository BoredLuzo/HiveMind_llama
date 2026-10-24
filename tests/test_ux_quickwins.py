"""UX quick wins (2026-10-24): English-only UI, honest auto-mode text,
actionable run-end hints, opt-in browser autostart. String-level pins —
the full frontend suites cover the surrounding machinery."""
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


app = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")
html = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
srv = (ROOT / "server.py").read_text(encoding="utf-8", errors="replace")
bat = (ROOT / "start_hivemind.bat").read_text(encoding="utf-8", errors="replace")

# ── English-only UI ──────────────────────────────────────────────────────
check('html lang is "en"', 'lang="en"' in html and 'lang="de"' not in html)
check("no German failure strings left in app.js",
      "fehlgeschlagen" not in app)

# ── honest auto-mode description ─────────────────────────────────────────
check("auto-mode text reflects the decider state",
      "S.routingDecider === 'on'" in app
      and "keyword rules route direct vs agentic" in app)
check("routing changes refresh the mode description",
      "updateModeDesc();  // UX: the auto text names the router" in app)

# ── actionable run-end hints ─────────────────────────────────────────────
check("stop reasons carry one-line hints",
      "hint: 'The model repeated itself" in app
      and "hint: 'Check logs/hivemind.log" in app
      and "hint: 'The run died mid-flight" in app)
check("the hint renders as its own line in the divider (CSS class)",
      "_hintEl.className = 'hint-line'" in app
      and ".divider .hint-line{flex-basis:100%" in
      (ROOT / "static" / "style.css").read_text(encoding="utf-8", errors="replace"))

# ── opt-in browser autostart ─────────────────────────────────────────────
check("server opens the browser only when asked (env-gated, case-insensitive)",
      "HIVEMIND_OPEN_BROWSER" in srv and "webbrowser.open" in srv
      and '.strip().lower() in ("1", "true", "yes")' in srv)
check("the interactive launcher asks, headless does not",
      'set "HIVEMIND_OPEN_BROWSER=1"' in bat
      and "HIVEMIND_OPEN_BROWSER" not in
      (ROOT / "watchdog_engine.bat").read_text(encoding="utf-8", errors="replace"))
check("the launcher contains its env (setlocal) - no leak into a calling shell",
      "setlocal" in bat)

print()
print(f"SUMMARY ux_quickwins: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
