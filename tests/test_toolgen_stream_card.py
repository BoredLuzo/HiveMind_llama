"""Tool-gen stream card tests (2026-09-18).

Live complaint: during write generation the UI only showed
"✍️ code generated 5913 chars" — no target file, no content. The rework
turns the tool_gen event stream into a real tool card: chip appears at
generation start, shows the target path as soon as it parses out of the
streaming JSON, and an expandable pre the code streams into (old_text/
new_text split for edit_file).

Checked here:
  - static analysis of static/app.js: card wiring exists, cleanup hooks
    are called from BOTH the tool_call and tool_result handlers (otherwise
    the streaming card duplicates the real chip);
  - behavioral: the argument-stream parser (path extraction, JSON string
    unescape incl. windows paths and partial \\u tails, edit_file old/new
    split) via the node sim in tg_stream_sim.js.

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
    check("edit_file old/new split in stream body", "'old_text'" in src and "'new_text'" in src)
    check("stream panel is a real pre (.tg-pre)", "tg-pre" in src)
    # Cleanup: without this the streaming card duplicates the real chip.
    tc_idx = src.find("d.type === 'tool_call'")
    tr_idx = src.find("d.type === 'tool_result'")
    check("tool_call handler found", tc_idx >= 0)
    check("tool_result handler found", tr_idx >= 0)
    check("_toolGenDone called in tool_call handler",
          0 < src.find("_toolGenDone();", tc_idx, tr_idx if tr_idx > tc_idx else len(src)))
    check("_toolGenDone called in tool_result handler",
          src.find("_toolGenDone();", tr_idx) > 0)


def test_behavior_sim():
    node = shutil.which("node")
    if not node:
        print("  SKIP  behavioral sim (node not available)")
        return
    r = subprocess.run([node, str(SIM)], capture_output=True, text=True)
    print(r.stdout.rstrip())
    if r.returncode != 0:
        fail("node sim exit code", r.stderr[-300:])
    else:
        ok("node sim: all parser cases pass")


if __name__ == "__main__":
    test_static_wiring()
    test_behavior_sim()
    print("\n" + "=" * 60)
    print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
    print("=" * 60)
    sys.exit(1 if failed else 0)
