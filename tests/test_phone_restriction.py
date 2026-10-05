# -*- coding: utf-8 -*-
"""Phone restriction (2026-10-05 UI toggle): a run flagged via the
/stream body may only use web + read-only + dialog tools. One choke
point in _run_inline_tool covers every mode/phase; web_fetch/web_search
membership is asserted statically (no network)."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools import runner as tr

passed = 0
failed = 0


def check(label, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {label}{extra}")
    else:
        failed += 1
        print(f"  FAIL {label}{extra}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="hvm_phonerestrict_"))
    tr._tools_restricted_run.set(False)

    # unrestricted: a write goes through (no PHONE_RESTRICTED error)
    out = asyncio.run(tr._run_inline_tool(
        "write_file", {"path": "ok.txt", "content": "hi"},
        workspace_lock=str(tmp), tool_mode="duo_full"))
    check("unrestricted: write proceeds", "PHONE_RESTRICTED" not in str(out))

    # restricted: writes and shell are blocked at the choke point
    tr._tools_restricted_run.set(True)
    try:
        out = asyncio.run(tr._run_inline_tool(
            "write_file", {"path": "nope.txt", "content": "x"},
            workspace_lock=str(tmp), tool_mode="duo_full"))
        check("restricted: write_file blocked", "PHONE_RESTRICTED" in str(out))

        out = asyncio.run(tr._run_inline_tool(
            "run_bash", {"command": "echo hi"},
            workspace_lock=str(tmp), tool_mode="duo_full"))
        check("restricted: run_bash blocked", "PHONE_RESTRICTED" in str(out))

        out = asyncio.run(tr._run_inline_tool(
            "git_commit", {"message": "x"},
            workspace_lock=str(tmp), tool_mode="duo_full"))
        check("restricted: git_commit blocked", "PHONE_RESTRICTED" in str(out))

        # safe set still works (list_dir needs no network)
        out = asyncio.run(tr._run_inline_tool(
            "list_dir", {"path": "."},
            workspace_lock=str(tmp), tool_mode="duo_full"))
        check("restricted: list_dir allowed", "PHONE_RESTRICTED" not in str(out))

        out = asyncio.run(tr._run_inline_tool(
            "get_datetime", {},
            workspace_lock=str(tmp), tool_mode="duo_full"))
        check("restricted: get_datetime allowed",
              "PHONE_RESTRICTED" not in str(out))
    finally:
        tr._tools_restricted_run.set(False)

    # static membership: web tools are in the safe set (no live fetch here)
    check("web_search/web_fetch in safe set",
          "web_search" in tr._PHONE_SAFE_TOOLS
          and "web_fetch" in tr._PHONE_SAFE_TOOLS)
    check("writes/shell NOT in safe set",
          "write_file" not in tr._PHONE_SAFE_TOOLS
          and "run_bash" not in tr._PHONE_SAFE_TOOLS
          and "git_commit" not in tr._PHONE_SAFE_TOOLS
          and "install_package" not in tr._PHONE_SAFE_TOOLS)

    # unrestricted again: the same write goes through
    out = asyncio.run(tr._run_inline_tool(
        "write_file", {"path": "ok2.txt", "content": "hi"},
        workspace_lock=str(tmp), tool_mode="duo_full"))
    check("flag reset: write proceeds again",
          "PHONE_RESTRICTED" not in str(out))

    print()
    print(f"passed={passed} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
