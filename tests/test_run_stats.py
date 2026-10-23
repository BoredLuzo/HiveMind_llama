"""The per-answer stats line must be engine-fed, persisted and restored.

Structural pinning (same style as test_verify_gate): the done event carries
the model, the direct path reports real usage, and the UI renders + saves +
reloads the stats line. A broken link in that chain shows as a silent
missing footer — this suite makes it loud instead.
"""
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


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


chat_run = _read("core/chat_run.py")
direct = _read("core/direct_runner.py")
app = _read("static/app.js")
css = _read("static/style.css")

# ── engine: the done event names the model ────────────────────────────────
check("done event carries the model", '"model": _ev_model' in chat_run)
check("model resolution prefers coder then direct then synthesizer",
      '_run_models.get("coder")' in chat_run
      and '_run_models.get("direct")' in chat_run
      and '_run_models.get("synthesizer")' in chat_run)

# ── engine: the direct path reports real usage, not just estimates ────────
check("direct stream passes usage_out to llama chat_stream",
      direct.count("usage_out=_usage") >= 2)
check("direct emits usage_meta on the direct phase",
      '"phase": "direct"' in direct and '"type": "usage_meta"' in direct)
check("direct attributes itself via run_meta",
      '{"direct": str(_direct_model)}' in direct)

# ── UI: render, persist, restore ──────────────────────────────────────────
check("app.js renders a stats footer (renderRunStatsLine)",
      "function renderRunStatsLine(stats, footEl)" in app)
check("the done handler stashes + renders the stats",
      "S._lastRunStats = {elapsed:" in app
      and "renderRunStatsLine(S._lastRunStats)" in app)
check("a new run never inherits the previous stats line",
      app.count("S._lastRunStats = null") >= 1)
check("autosave attaches stats to the final assistant message, once per run",
      "msgs[_i].stats = S._lastRunStats" in app
      and "!S._lastRunStats._saved" in app)
check("loadChat restores the stats footer from msg.stats",
      "if (msg.stats && typeof renderRunStatsLine === 'function')" in app)
check("the footer is muted small text with hover detail (.msg-stats)",
      ".msg-stats{" in css and ".msg-stats:hover{" in css)

print()
print(f"SUMMARY run_stats: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
