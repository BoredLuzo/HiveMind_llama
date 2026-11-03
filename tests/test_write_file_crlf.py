# -*- coding: utf-8 -*-
"""A2: write_file must not double CRs on an existing CRLF file.

The handler reads the old file with newline="" to learn its EOL style, then
re-expanded every "\n" in the incoming content. A payload that already used
CRLF became "a\r\r\nb" on disk.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tools.handlers.file_ops import _inline_tool_write_file   # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


def _write(ws, content, name="x.txt"):
    return asyncio.run(_inline_tool_write_file(
        {"path": name, "content": content}, ws, None))


ws = Path(tempfile.mkdtemp())
p = ws / "x.txt"
p.write_bytes(b"old\r\nfile\r\n")
out = _write(ws, "a\r\nb\r\n")
raw = p.read_bytes()
check("1a the write succeeded", "x.txt" in out, out[:80])
check("1b no doubled carriage return", b"\r\r\n" not in raw, repr(raw))
check("1c the CRLF style of the file is preserved", raw == b"a\r\nb\r\n", repr(raw))

# A new file keeps the style of the payload (no rewrite to the platform EOL).
ws2 = Path(tempfile.mkdtemp())
p2 = ws2 / "y.txt"
_write(ws2, "a\r\nb\r\n", "y.txt")
check("2a new CRLF file stays CRLF", p2.read_bytes() == b"a\r\nb\r\n",
      repr(p2.read_bytes()))

# An LF file must not gain CRs from an LF payload.
ws3 = Path(tempfile.mkdtemp())
p3 = ws3 / "z.txt"
p3.write_bytes(b"old\nfile\n")
_write(ws3, "a\nb\n", "z.txt")
check("2b LF file stays LF", p3.read_bytes() == b"a\nb\n", repr(p3.read_bytes()))

# P2 (deep audit 2026-11-03): a mixed file keeps its majority EOL.
ws4 = Path(tempfile.mkdtemp())
p4 = ws4 / "mixed.txt"
p4.write_bytes(b"a\r\nb\nc\nd\n")          # 1 CRLF, 3 LF -> LF majority
_write(ws4, "x\ny\n", "mixed.txt")
check("2c a mixed file follows the majority EOL",
      p4.read_bytes() == b"x\ny\n", repr(p4.read_bytes()))

print(f"\n=== Results: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
