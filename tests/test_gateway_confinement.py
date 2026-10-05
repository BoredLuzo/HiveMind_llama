# -*- coding: utf-8 -*-
"""Gateway/Confinement matrix (audit N3): EVERY read/search/glob tool vs
the Windows escape traps. Criterion is CONTENT-BASED: the marker inside
an outside-of-workspace secret file must never appear in any tool
output — an error message naming the path is acceptable, content is not.

Traps: absolute outside path, ../ traversal, UNC, junction in the
workspace pointing outside, 8.3 short names, ADS syntax, case games,
trailing dot/space, drive-relative (C:foo).

Runs fully offline against the real handlers.
"""
import asyncio
import ctypes
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.handlers.file_ops import _inline_tool_read_file
from tools.handlers.code_intel import (
    _inline_tool_find_files,
    _inline_tool_get_signatures,
    _inline_tool_list_dir,
    _inline_tool_search_code,
)

MARKER = "XQZ-SECRET-9137"

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


def _short8x3(p: Path) -> str | None:
    """Windows 8.3 short path, or None when the volume has them off."""
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
    if n == 0 or n >= 1024:
        return None
    return buf.value


def make_junction(link: Path, target: Path) -> bool:
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return r.returncode == 0


async def _main():
    tmp = Path(tempfile.mkdtemp(prefix="gw_confine_"))
    ws = tmp / "workspace"
    ws.mkdir()
    outside = tmp / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text(MARKER, encoding="utf-8")
    (ws / "ok.txt").write_text("benign", encoding="utf-8")

    secret_rel = str(secret)
    tools = {
        "read_file": (_inline_tool_read_file, "path"),
        "list_dir": (_inline_tool_list_dir, "path"),
        "find_files": (_inline_tool_find_files, "path"),
        "search_code": (_inline_tool_search_code, "path"),
        "get_signatures": (_inline_tool_get_signatures, "path"),
    }

    traps = {
        "absolute outside": secret_rel,
        "../ traversal": str(tmp / "workspace" / ".." / "outside"
                             / "secret.txt"),
        "UNC": "\\\\\\\\localhost\\c$" + str(secret).replace(":", "$", 1)
               if False else "\\\\localhost\\c$\\" +
                             str(secret).split(":", 1)[1].replace("\\", "\\"),
        "trailing dot": secret_rel + ".",
        "trailing space": secret_rel + " ",
        "drive-relative (C:foo)": "C:" + str(outside)[2:] + "\\secret.txt",
        "mixed case escape": str(secret).lower(),
    }

    async def run_tool(fn, trap_value):
        try:
            return await fn({"path": trap_value}, ws, str(ws))
        except (OSError, ValueError) as exc:
            return f"[EXC {type(exc).__name__}]"

    for label, trap in traps.items():
        leak_free = True
        for tname, (fn, _) in tools.items():
            out = await run_tool(fn, trap)
            if MARKER in str(out):
                leak_free = False
                print(f"    -> LEAK via {tname}: {str(out)[:120]}")
        check(f"{label}: marker never in output ({len(tools)} tools)",
              leak_free)

    # junction inside the workspace pointing outside
    junc = ws / "_junc"
    if make_junction(junc, outside):
        for tname, (fn, _) in tools.items():
            out = await run_tool(fn, str(junc / "secret.txt"))
            check(f"junction escape blocked via {tname}",
                  MARKER not in str(out))
    else:
        print("  NOTE junction creation not possible on this volume — "
              "skipped (owner can re-run with mklink /J on demand)")

    # 8.3 short path of the OUTSIDE dir (volume may have 8.3 disabled)
    short = _short8x3(outside)
    if short:
        for tname, (fn, _) in tools.items():
            out = await run_tool(fn, short + "\\secret.txt")
            check(f"8.3 short path escape blocked via {tname}",
                  MARKER not in str(out))
    else:
        print("  NOTE 8.3 short names unavailable on this volume — skipped")

    # ADS syntax on an inside file: reading a stream must not bypass the
    # workspace or conjure content from outside
    out = await run_tool(_inline_tool_read_file, "ok.txt:hidden_stream")
    check("ADS syntax: no marker leak, no crash", MARKER not in str(out))

    # control: an inside file still reads (the guard must not overblock)
    out = await run_tool(_inline_tool_read_file, "ok.txt")
    check("control: inside read still works", "benign" in str(out))

    # cleanup junction
    if junc.exists():
        subprocess.run(["cmd", "/c", "rmdir", str(junc)],
                       capture_output=True)

    print()
    print(f"passed={passed} failed={failed}")
    sys.exit(0 if failed == 0 else 1)


asyncio.run(_main())
