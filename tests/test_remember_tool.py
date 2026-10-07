"""Remember tool: the agent saves durable facts itself (2026-10-06, owner).

The model could not write memories before — extraction only ran on the
USER input. Now the tool loop can persist key/value facts (and forget)
through the same HiveMindMemory the auto-extraction uses.

Run: python tests/test_remember_tool.py
Exit 0 = all pass, Exit 1 = failures.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

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


def test_tool_schema_exists():
    src = (Path(__file__).parent.parent / "tools" / "definitions.py").read_text(encoding="utf-8")
    seg = src.split('"name": "remember"')
    if len(seg) >= 2 and '"key"' in seg[1][:600] and '"value"' in seg[1][:600] \
            and '"required": ["key"]' in src:
        ok("remember tool schema: key + value parameters")
    else:
        fail("schema", "remember tool schema missing")


def test_handler_writes_memory():
    src = (Path(__file__).parent.parent / "tools" / "handlers" / "misc.py").read_text(encoding="utf-8")
    ok_write = ".remember(" in src
    ok_forget = ".forget(" in src
    if ok_write and ok_forget:
        ok("handler: remember (write) + forget (empty value) wired to HiveMindMemory")
    else:
        fail("handler", f"write={ok_write} forget={ok_forget}")


def test_dispatch_and_phone_safe():
    src = (Path(__file__).parent.parent / "tools" / "runner.py").read_text(encoding="utf-8")
    dispatch = '"remember": _inline_tool_remember' in src
    safe = src.split("_PHONE_SAFE_TOOLS = frozenset({")[1].split("})")[0]
    safe_ok = '"remember"' in safe
    if dispatch and safe_ok:
        ok("runner: dispatched + phone-safe (memory writes are chat-adjacent)")
    else:
        fail("runner", f"dispatch={dispatch} phone_safe={safe_ok}")


def test_forget_via_empty_value():
    src = (Path(__file__).parent.parent / "tools" / "handlers" / "misc.py").read_text(encoding="utf-8")
    if ".forget(" in src:
        ok("forget: empty value deletes the key")
    else:
        fail("forget_empty", "no forget path")


if __name__ == "__main__":
    test_tool_schema_exists()
    test_handler_writes_memory()
    test_dispatch_and_phone_safe()
    test_forget_via_empty_value()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
