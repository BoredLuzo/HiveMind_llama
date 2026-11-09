# -*- coding: utf-8 -*-
"""Multi-edit batch on edit_file (D3 edit/diff engine, 2026-11-09).

edit_file gains optional `edits: [{old_text, new_text}]`: several exact
replacements applied SEQUENTIALLY to one file with ONE atomic write. The
single old_text/new_text API stays byte-compatible. Guards (empty, malformed,
non-unique, not-found, shrink, symbol loss) must reject the WHOLE batch
before any byte reaches disk.

Run: python tests/test_multi_edit.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.handlers.file_ops import (      # noqa: E402
    _inline_tool_edit_file,
    _inline_tool_write_file,
)
from tests._harness import Harness         # noqa: E402

h = Harness(style="plain", pad=True)
check = h.check


def _read(ws: Path, name: str = "app.py") -> str:
    return (ws / name).read_text(encoding="utf-8")


_BODY = (
    "TITLE = 'demo'\n"
    "VERSION = 1\n"
    "\n"
    "def greet():\n"
    "    return 'hello'\n"
    "\n"
    "def farewell():\n"
    "    return 'bye'\n"
)


async def _setup(ws: Path, body: str = _BODY, name: str = "app.py") -> None:
    await _inline_tool_write_file({"path": name, "content": body}, ws, str(ws))


# 1+2: the happy path — two edits, one call, one write; old API unchanged.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_setup(ws))
    out = asyncio.run(_inline_tool_edit_file({
        "path": "app.py",
        "edits": [
            {"old_text": "TITLE = 'demo'", "new_text": "TITLE = 'demo v2'"},
            {"old_text": "def farewell():\n    return 'bye'",
             "new_text": "def farewell():\n    return 'see you'"},
        ],
    }, ws, str(ws)))
    body = _read(ws)
    check("two edits applied in one atomic write",
          "TITLE = 'demo v2'" in body and "return 'see you'" in body
          and "applied 2 edits" in out,
          f"({out.strip()[:120]})")
    check("untouched passages survive the batch",
          "def greet():" in body and "VERSION = 1" in body)

    out_single = asyncio.run(_inline_tool_edit_file({
        "path": "app.py",
        "old_text": "VERSION = 1", "new_text": "VERSION = 2",
    }, ws, str(ws)))
    check("old single-edit API still works unchanged",
          "VERSION = 2" in _read(ws) and "replaced" in out_single,
          f"({out_single.strip()[:120]})")

# 3: non-unique old_text in the batch -> rejected, file untouched.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_setup(ws))
    out = asyncio.run(_inline_tool_edit_file({
        "path": "app.py",
        "edits": [{"old_text": "return", "new_text": "return None"}],
    }, ws, str(ws)))
    check("non-unique old_text rejected with file unchanged",
          "EDIT_FILE_NOT_UNIQUE" in out and _read(ws) == _BODY,
          f"({out.strip()[:120]})")

    # 4: second edit's old_text not found -> whole batch rejected.
    out = asyncio.run(_inline_tool_edit_file({
        "path": "app.py",
        "edits": [
            {"old_text": "VERSION = 1", "new_text": "VERSION = 9"},
            {"old_text": "def missing():", "new_text": "def x():"},
        ],
    }, ws, str(ws)))
    check("not-found later edit aborts the whole batch",
          "EDIT_FILE_OLD_TEXT_NOT_FOUND" in out and _read(ws) == _BODY,
          f"({out.strip()[:120]})")

# 5: symbol-loss guard fires across the batch.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_setup(ws))
    out = asyncio.run(_inline_tool_edit_file({
        "path": "app.py",
        "edits": [
            {"old_text": "def farewell():", "new_text": "# step 1 done"},
            {"old_text": "    return 'bye'", "new_text": "# step 2 done"},
        ],
    }, ws, str(ws)))
    check("batch that drops a top-level symbol is rejected",
          "WOULD_DROP_SYMBOLS" in out and "farewell" in out
          and _read(ws) == _BODY, f"({out.strip()[:120]})")

    # allow_overwrite is the documented escape hatch.
    out_ok = asyncio.run(_inline_tool_edit_file({
        "path": "app.py",
        "edits": [
            {"old_text": "def farewell():", "new_text": "# step 1 done"},
            {"old_text": "    return 'bye'", "new_text": "# step 2 done"},
        ],
        "allow_overwrite": True,
    }, ws, str(ws)))
    check("allow_overwrite lets an intentional symbol removal through",
          "applied 2 edits" in out_ok and "def farewell" not in _read(ws),
          f"({out_ok.strip()[:120]})")

# 6: invalid list shapes -> clear error, nothing written.
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_setup(ws))
    out_str = asyncio.run(_inline_tool_edit_file({
        "path": "app.py", "edits": "TITLE = 'x'",
    }, ws, str(ws)))
    out_empty = asyncio.run(_inline_tool_edit_file({
        "path": "app.py", "edits": [],
    }, ws, str(ws)))
    out_entry = asyncio.run(_inline_tool_edit_file({
        "path": "app.py", "edits": [{"old_text": "VERSION = 1"}],
    }, ws, str(ws)))
    out_cap = asyncio.run(_inline_tool_edit_file({
        "path": "app.py",
        "edits": [{"old_text": f"MARK{i}", "new_text": "M"} for i in range(21)],
    }, ws, str(ws)))
    check("non-list / empty / invalid entry / over-cap all rejected",
          all("EDIT_FILE_BAD_EDITS" in o or "EDIT_FILE_EMPTY" in o
              for o in (out_str, out_empty, out_entry, out_cap))
          and _read(ws) == _BODY,
          f"(str={out_str.strip()[:60]} cap={out_cap.strip()[:60]})")

sys.exit(h.finish("multi_edit"))
