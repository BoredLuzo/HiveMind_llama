# -*- coding: utf-8 -*-
"""FORENSIC CASE 2026-10-24/25 (chunk/mlock revert): render must not write.

The owner set duo_test_feedback_chunk=true / llama_mlock=false and both kept
reverting. Proven writer for the recurring part: applySettings() calls
onChunkingChange() on every render, and its chunking-OFF branch posted
duo_test_feedback_chunk:false unconditionally — with duo_chunking=false
(owner config, settings.json AND Default preset) every page load re-cleared
the saved value ~180 ms later (log: preset snapshot 18:01:52 carried
chunk=true, the very next save_settings at 18:02:27 carried false, no user
toggle in between). A preset saved from such a page snapshotted the poisoned
value into presets.json — that is the presets.json half of the case.

Fix: onChunkingChange(persist) — render calls pass persist=false and never
post; the real user toggle (index.html chunking checkbox) keeps the old
persisting behavior. This suite pins the wiring via source anchors (no JS
test infra in the repo — anchor style per house rules).
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


APP = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")


def main() -> int:
    print("\n=== chunk render must not persist (forensic case 2026-10-24/25) ===\n")

    check("A1: onChunkingChange takes the persist flag",
          "function onChunkingChange(persist) {" in APP)
    check("A2: persist default = post (persist !== false)",
          "var _persist = persist !== false;" in APP)

    # the four render-driven posts are the bug surface — all must be gated
    check("B1: chunk=false post gated",
          "if (_persist) postSettings({duo_test_feedback_chunk: false});" in APP)
    check("B2: thinking-restore post gated",
          "if (_persist) postSettings({duo_agentic_thinking: S.duoAgenticThinking});" in APP)
    check("B3: tbc-clear post gated",
          "if (_persist) postSettings({_thinking_before_chunking: null});" in APP)
    check("B4: tbc-record post gated (chunking-ON branch)",
          "if (_persist) postSettings({_thinking_before_chunking: S.duoAgenticThinking});" in APP)

    # applySettings render calls: exactly the persist=false form, no bare call
    render_calls = APP.count("onChunkingChange(false);")
    check("C1: applySettings render calls pass persist=false (found %d)" % render_calls,
          render_calls == 2)
    check("C2: no bare onChunkingChange() left in app.js "
          "(a default-persist render call would reintroduce the revert)",
          "onChunkingChange();" not in APP)

    # the real user toggle keeps persisting
    check("D1: index.html chunking checkbox still calls the persisting form",
          "postSettings({duo_chunking:S.duoChunking});onChunkingChange();" in HTML)

    print(f"\n  {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
