# -*- coding: utf-8 -*-
"""Read-aloud follows the route the run actually took (2026-11-02).

Symptom: the "agentic" read-aloud switch was off, yet agentic and pipeline
answers were still spoken. The final answer was classified by `S.mode`, and in
`auto` the mode never says agentic — the decider picks the route per message.
It fell back to 'direct', so `tts_read_direct` spoke every answer.

Pinned here (static checks on static/app.js — no browser needed):
  1. a run resets the route flag
  2. the complexity event sets it for code_duo / complex (pipeline)
  3. pipeline_start and duo_start set it too
  4. the final answer uses the flag (plus the legacy mode check)
  5. each read-aloud kind maps to its own switch
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── 1. per-run reset ────────────────────────────────────────────────────────
_start = APP.index("S._runIsDuo = false;")
check("1a a new run clears the route flag",
      "S._routeMultiAgent = false;" in APP[_start:_start + 600])

# ── 2. the route is learned from the engine ────────────────────────────────
_cx = APP.index("else if (d.type === 'complexity') {")
_cx_block = APP[_cx:_cx + 700]
check("2a code_duo marks the run multi-agent", "code_duo" in _cx_block)
check("2b complex (pipeline) marks it too", "'complex'" in _cx_block)
check("2c the flag is assigned in the complexity handler",
      "S._routeMultiAgent =" in _cx_block)

_pipe = APP.index("else if (d.type === 'pipeline_start') {")
check("2d pipeline_start marks the run", "S._routeMultiAgent = true;" in APP[_pipe:_pipe + 300])
_duo = APP.index("else if (d.type === 'duo_start') {")
check("2e duo_start marks the run", "S._routeMultiAgent = true;" in APP[_duo:_duo + 400])

# ── 3. the final answer uses the flag ──────────────────────────────────────
_final = APP.index("_readAloud(_isDuo ? 'fazit' : 'direct'")
_ctx = APP[max(0, _final - 500):_final]
check("3a the final answer consults the route flag", "S._routeMultiAgent" in _ctx)
check("3b it still consults the duo flag", "S._runIsDuo" in _ctx)
check("3c the mode list stays as a fallback", "code_duo" in _ctx)

# ── 4. every kind has its own switch ───────────────────────────────────────
_want = APP.index("function _readAloudWanted(kind)")
_block = APP[_want:_want + 500]
for kind, key in (("direct", "ttsReadDirect"), ("plan", "ttsReadAgentic"),
                  ("fazit", "ttsReadAgentic"), ("ask", "ttsReadAsk"),
                  ("gate", "ttsReadGate")):
    check(f"4 {kind} -> {key}", re.search(rf"'{kind}'[^\n]*{key}", _block) is not None)
check("4f nothing is spoken while TTS is off",
      "if (!(S && S.ttsEnabled)) return false;" in _block)
check("4g an unknown kind is never spoken",
      _block.rstrip().split("return false;")[-1].strip().startswith("}"))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
