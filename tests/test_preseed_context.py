# -*- coding: utf-8 -*-
"""Pre-seed context for tiny workspaces (2026-11-09).

The coder no longer needs a read_file round when the workspace is only a few
small top-level files. The helper must never preseed a real repo, a binary
file, or content above the context-scaled cap.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.duo_helpers import build_preseed_context
from tests._harness import Harness

h = Harness(style="plain", pad=True)
check = h.check

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "a.py").write_text("A = 1\n", encoding="utf-8")
    (ws / "b.py").write_text("def f():\n    return 2\n", encoding="utf-8")
    block, names = build_preseed_context(str(ws), 16384)
    check("two small files are inlined",
          "### FILE: a.py" in block and "### FILE: b.py" in block,
          f"({names})")
    check("the block tells the coder the files are already read",
          "already read" in block.lower() and "read_file" in block)
    check("both relative names are returned",
          names == ["a.py", "b.py"], f"({names})")

    for i in range(3):
        (ws / f"c{i}.py").write_text("X = 1\n", encoding="utf-8")
    block5, names5 = build_preseed_context(str(ws), 16384)
    check("more than four top-level files -> no preseed",
          block5 == "" and names5 == [])

    # D2 (2026-11-09): with a task query, the index selects up to four
    # RELEVANT files from a larger top-level set instead of refusing.
    (ws / "renderer.py").write_text(
        "def draw_frame():\n    return 'frame'\n", encoding="utf-8")
    (ws / "network.py").write_text(
        "def send_packet():\n    return 'pkt'\n", encoding="utf-8")
    block_q, names_q = build_preseed_context(
        str(ws), 16384, query="fix the renderer draw_frame output")
    check("query selects relevant files from a 6-file workspace",
          bool(block_q) and "renderer.py" in names_q
          and len(names_q) <= 4, f"({names_q})")
    check("the selected file's content is inlined",
          bool(block_q) and "draw_frame" in block_q)
    check("irrelevant files stay out of the selection",
          not names_q or "network.py" not in names_q, f"({names_q})")

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "big.py").write_text("x = 1\n" * 900, encoding="utf-8")
    block_small, _ = build_preseed_context(str(ws), 4096)
    check("content above the 4k-context cap is rejected", block_small == "")
    block_large, names_large = build_preseed_context(str(ws), 32768)
    check("a larger context allows the same file", bool(block_large) and names_large == ["big.py"])

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "bin.dat").write_bytes(b"\x00\x01\x02")
    block_bin, _ = build_preseed_context(str(ws), 16384)
    check("binary/unknown extensions are ignored", block_bin == "")

sys.exit(h.finish("preseed_context"))
