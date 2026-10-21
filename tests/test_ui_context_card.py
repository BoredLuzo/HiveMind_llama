"""UI: Context-Karte + Routing-Block (Wer entscheidet im Auto-Modus?).

Der Context-Block war eine nackte Stapelung aus Label, orangenem Zahlenfeld und
Hinweistext; ausserdem war nirgends sichtbar, wer bei mode=auto entscheidet.
Beides ist jetzt Markup + CSS + JS - dieser Test pinnt es.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HTML = (ROOT / "index.html").read_text(encoding="utf-8", errors="replace")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8", errors="replace")
JS = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. Eine Karte statt drei loser Bloecke ──────────────────────────────
check("context card exists", 'id="ctx-agentic-card"' in HTML)
check("card wraps the coder row", HTML.index('id="ctx-agentic-card"') < HTML.index('id="wrapper-ctx-agentic"'))
check("card wraps the planner toggle", HTML.index('id="wrapper-planner-coder-toggle"') < HTML.index('<!-- /ctx-agentic-card -->'))
check("card wraps the planner row", HTML.index('id="wrapper-ctx-planner"') < HTML.index('<!-- /ctx-agentic-card -->'))
check("card has a header", "ctx-card-title" in HTML and "Context &mdash; agentic" in HTML)
check("rows use the label grid", HTML.count('class="ctx-row') >= 3, f"({HTML.count('class=\"ctx-row')})")

# ── 2. Zahlenfelder sind kompakt + Einheit, kein Inline-Orange mehr ─────
check("ctx inputs use the class", HTML.count('class="ctx-num') >= 2, f"({HTML.count('class=\"ctx-num')})")
check("no leftover inline orange input style",
      "width:76px;background:var(--bg2);border:1px solid var(--b2);border-radius:4px;color:var(--amber)"
      not in HTML)
check("fields carry a unit", HTML.count('class="ctx-unit"') == 2,
      f"({HTML.count('class=\"ctx-unit\"')})")
for cls in (".ctx-card", ".ctx-card-head", ".ctx-row", ".ctx-label", ".ctx-num",
            ".ctx-hint", ".ctx-field", ".ctx-unit", ".rt-select", ".rt-status"):
    check(f"css has {cls}", f"{cls} {{" in CSS)
check("spinner buttons are hidden", "-webkit-appearance: none" in CSS)

# ── 3. JS verschiebt die Karte, nicht die Einzelbloecke ─────────────────
check("js moves the whole card", "_ctxMoveTo(_card, _slot)" in JS)
check("js hides the card outside agentic", "_card.style.display = 'none'" in JS)
check("planner row follows the toggle",
      "plannerUseCoder ? 'none' : 'block'" in JS)

# ── 4. Routing-Block: drei Schalter + Live-Zeile ────────────────────────
check("routing card exists", 'id="routing-card"' in HTML)
check("routing card sits in the Agents panel under MODE",
      HTML.index('id="mode-btns-row"') < HTML.index('id="mode-routing"')
      < HTML.index('id="chat-tools-section"'), "(position)")
check("routing card is hidden outside auto",
      'id="mode-routing" style="display:none"' in HTML)
check("js ties the card to the auto mode",
      "function updateRoutingVisibility" in JS and "S.mode === 'auto'" in JS)
check("mode switch refreshes the routing card", "updateRoutingVisibility();" in JS)
check("routing card no longer sits in the configs column",
      HTML.count('id="routing-card"') == 1)
for sel in ("routing-decider-sel", "routing-judge-sel", "routing-planner-sel"):
    check(f"select {sel}", f'id="{sel}"' in HTML)
for opt in ('value="off"', 'value="shadow"', 'value="on"'):
    check(f"decider/judge option {opt}", HTML.count(opt) >= 2, f"({HTML.count(opt)})")
for pl in ("always", "decider", "never"):
    check(f"plan step option {pl}", f'<option value="{pl}"' in HTML)
_pln_block = HTML[HTML.index('id="routing-planner-sel"'):
                 HTML.index("</select>", HTML.index('id="routing-planner-sel"'))]
for gone in ("auto", "heuristic", "judge"):
    check(f"plan step no longer offers {gone}", f'<option value="{gone}"' not in _pln_block)
check("the select offers exactly three choices",
      HTML[HTML.index('id="routing-planner-sel"'):HTML.index("</select>", HTML.index('id="routing-planner-sel"'))].count("<option") == 3)
check("routing status line exists", 'id="routing-status"' in HTML)

check("js has setRouting", "function setRouting(" in JS)
check("js has renderRoutingStatus", "function renderRoutingStatus(" in JS)
check("status names the decider", "encoder on CPU" in JS)
check("plan step is labelled as such, not as a model", "Plan step" in HTML)
check("plan step options read as decisions",
      all(x in HTML for x in ("Always plan", "Decider decides", "Never plan")))
check("legacy planner values stay visible instead of being rewritten",
      "routingPlannerLegacy" in JS)
check("plan step hint points at the model location",
      "The planning model itself is set in Agents" in HTML)
check("routing card has no em dashes left",
      "&mdash;" not in HTML[HTML.index('id="routing-card"'):HTML.index('id="routing-status"')])
check("status line separates route and plan step",
      "Route: <b>" in JS and "Plan step: <b>" in JS)
check("status names the plan step source", "Plan step: <b>" in JS)
check("status shows the threshold", "threshold ' + thr.toFixed(2)" in JS)
check("status warns when a fixed mode skips the card",
      "skips this card entirely" in JS)
check("loadSettings fills the three selects",
      "routing-decider-sel" in JS and "routing-judge-sel" in JS and "routing-planner-sel" in JS)
check("settings keys are persisted", "patch[key] = value" in JS and "postSettings(patch)" in JS)

# ── 5. Die alten, unabhaengig positionierten Inline-Styles sind weg ─────
check("no absolute inline margins on the moved wrappers",
      'id="wrapper-ctx-agentic" style="margin-bottom:10px' not in HTML)
check("no inline purple label style left",
      'font-size:11px;color:#9a74dc;cursor:pointer' not in HTML)

print()
print(f"SUMMARY ui_context_card: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
