"""Tool-gen stream card tests (2026-09-18).

Live complaint: during write generation the UI only showed
"✍️ code generated 5913 chars" — no target file, no content. The rework
streams the generated code into the RIGHT code panel (tab per file,
plain render while streaming), while the chat keeps only a compact chip
(tool + target path + char counter) that disappears once the real
tool_call chip arrives. The DIFFSTAT result block starts collapsed —
the summary carries the +N/−M metrics, the diff body opens on click.

Checked here:
  - static analysis of static/app.js: panel-stream wiring, throttle,
    cleanup hooks called from BOTH the tool_call and tool_result handlers
    (otherwise the streaming chip duplicates the real chip), DIFFSTAT
    collapsed by default;
  - behavioral: the argument-stream parser (path extraction, JSON string
    unescape incl. windows paths and partial \\u tails, edit_file new_text
    extraction) via the node sim in tg_stream_sim.js.

Run: python tests/test_toolgen_stream_card.py
Exit 0 = all pass, Exit 1 = failures.
"""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
APP = ROOT / "static" / "app.js"
SIM = ROOT / "tests" / "tg_stream_sim.js"
DIFF_SIM = ROOT / "tests" / "tg_diff_cleanup_sim.js"

passed = 0
failed = 0


def ok(name):
    global passed
    passed += 1
    print(f"  PASS  {name}")


def fail(name, msg=""):
    global failed
    failed += 1
    print(f"  FAIL  {name}  {msg}")


def check(name, cond, msg=""):
    if cond:
        ok(name)
    else:
        fail(name, msg)


def test_static_wiring():
    src = APP.read_text(encoding="utf-8")
    check("card state keyed by index (_tgCards)", "_tgCards" in src)
    check("path extracted from streaming args", '"path"\\s*:\\s*"' in src.replace("\\\\", "\\"))
    check("edit_file streams new_text into panel", "'new_text'" in src)
    # The code streams into the RIGHT code panel — not an inline pre in chat.
    check("panel stream helper wired (_tgPanelStream -> _cpAddOrUpdateFile)",
          "_tgPanelStream" in src and "_cpAddOrUpdateFile(st.path" in src)
    # The stream must NOT gate on the path: models that emit content before
    # path otherwise stream into nothing (live: chip "…" at 4.7k chars).
    check("pending tab when path not yet parsed", "_TG_PENDING_KEY" in src)
    check("pending rekeyed to real path on arrival", "_tgPendingRekey" in src)
    check("pending dropped at stream end", "_tgPanelDropPending" in src)
    check("tab name is a renameable span (.cp-name)", 'class="cp-name"' in src)
    check("path-fail leaves console diagnose", "path not parsed" in src)
    check("plain render mode for streaming (no highlight)", "'plain'" in src)
    check("throttled panel renders", "_TG_PANEL_RENDER_MS" in src)
    # Sticky follow: while the user sits at the panel bottom, the view
    # scrolls along; a scroll listener tracks the intent.
    check("sticky follow tracked by scroll listener",
          "_cpFollowStream" in src and "_cpEnsureFollowListener" in src
          and "body.scrollTop = body.scrollHeight" in src)
    # The chat itself follows with the same sticky semantics (scroll
    # listener tracks intent; appends pin while the user is at the bottom).
    check("chat sticky follow (_chatFollow listener)",
          "_chatFollow" in src and "_chatEnsureFollowListener" in src
          and "if (!_chatFollow) return;" in src)
    check("no inline stream pre left in chat", "tg-pre" not in src)
    # Cleanup: without this the streaming chip duplicates the real chip.
    tc_idx = src.find("d.type === 'tool_call'")
    tr_idx = src.find("d.type === 'tool_result'")
    check("tool_call handler found", tc_idx >= 0)
    check("tool_result handler found", tr_idx >= 0)
    check("_toolGenDone called in tool_call handler",
          0 < src.find("_toolGenDone();", tc_idx, tr_idx if tr_idx > tc_idx else len(src)))
    check("_toolGenDone called in tool_result handler",
          src.find("_toolGenDone();", tr_idx) > 0)
    # DIFFSTAT result block: collapsed by default (summary carries the
    # +N/−M metrics; the diff body opens on click). Only short results
    # auto-open.
    check("DIFFSTAT block not auto-opened", src.count("_trBlock.open = true") == 1)
    # Diff-body cleanup: file headers dropped, hunk headers humanized,
    # summary says "changed blocks" not diff-jargon "hunks".
    check("diff body cleaned via _cleanDiffBody", "_cleanDiffBody(_dsBody)" in src)
    check("no 'hunks' jargon in summary", "' hunks'" not in src and "changed block" in src)


def test_behavior_sim():
    node = shutil.which("node")
    if not node:
        print("  SKIP  behavioral sim (node not available)")
        return
    for sim in (SIM, DIFF_SIM):
        r = subprocess.run([node, str(sim)], capture_output=True, text=True)
        print(r.stdout.rstrip())
        if r.returncode != 0:
            fail(f"node sim {sim.name} exit code", r.stderr[-300:])
            return
    ok("node sims: all parser + diff-cleanup cases pass")


if __name__ == "__main__":
    test_static_wiring()
    test_behavior_sim()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
