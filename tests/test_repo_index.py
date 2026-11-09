# -*- coding: utf-8 -*-
"""Repo index (D2 context engine, context/repo_index.py).

Incremental per-workspace index: mtime+size invalidation, symbol extraction
(.py via ast, .js/.ts via cheap regex), token-overlap ranking, hard file cap,
non-code files ignored.

Run: python tests/test_repo_index.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from context.repo_index import RepoIndex          # noqa: E402
from tests._harness import Harness                # noqa: E402

h = Harness(style="plain", pad=True)
check = h.check


with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "calc.py").write_text(
        "import os\n"
        "MAX_RETRIES = 3\n"
        "class Calculator:\n"
        "    pass\n"
        "def add(a, b):\n"
        "    return a + b\n",
        encoding="utf-8")
    idx = RepoIndex(ws)
    idx.refresh()
    entry = idx.entries.get("calc.py")
    check("py symbols: import/assign/class/def extracted",
          entry is not None and
          {"os", "MAX_RETRIES", "Calculator", "add"} <= set(entry.symbols),
          f"({getattr(entry, 'symbols', None)})")

    # CACHE HIT: an unchanged file is NOT re-extracted.
    before = idx.extractions
    idx.refresh()
    check("cache hit: unchanged mtime+size skips extraction",
          idx.extractions == before and "calc.py" in idx.entries,
          f"(extractions {before} -> {idx.extractions})")

    # CACHE MISS: mtime+size change forces re-extraction with new symbols.
    (ws / "calc.py").write_text(
        "def subtract(a, b):\n    return a - b\n", encoding="utf-8")
    os.utime(ws / "calc.py", (1_600_000_000.0, 1_600_000_000.0))
    idx.refresh()
    entry2 = idx.entries.get("calc.py")
    check("cache miss: mtime+size change re-extracts symbols",
          idx.extractions == before + 1 and entry2 is not None
          and "subtract" in entry2.symbols and "Calculator" not in entry2.symbols,
          f"(extractions {before} -> {idx.extractions}, {getattr(entry2, 'symbols', None)})")

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "app.ts").write_text(
        "export class Runner {\n}\n"
        "function startEngine(port: number) {\n}\n"
        "const MAX_SLOTS = 4;\n",
        encoding="utf-8")
    idx = RepoIndex(ws)
    idx.refresh()
    syms = set(idx.entries["app.ts"].symbols) if "app.ts" in idx.entries else set()
    check("js/ts cheap regex symbols extracted",
          {"Runner", "startEngine", "MAX_SLOTS"} <= syms, f"({sorted(syms)})")

    # VANISHED files leave the index on the next refresh.
    (ws / "gone.py").write_text("def tmp():\n    pass\n", encoding="utf-8")
    idx.refresh()
    had = "gone.py" in idx.entries
    (ws / "gone.py").unlink()
    idx.refresh()
    check("vanished file is dropped from the index",
          had and "gone.py" not in idx.entries)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "turtle_renderer.py").write_text(
        "def draw_screen():\n    pass\n", encoding="utf-8")
    (ws / "net_client.py").write_text(
        "def send_packet():\n    pass\n", encoding="utf-8")
    (ws / "data.bin").write_bytes(b"\x00\x01\x02")
    (ws / ".hidden.py").write_text("X = 1\n", encoding="utf-8")
    idx = RepoIndex(ws)
    idx.refresh()
    check("non-code and hidden files are ignored",
          set(idx.entries) == {"turtle_renderer.py", "net_client.py"},
          f"({sorted(idx.entries)})")

    top = idx.search("fix the turtle renderer draw_screen", limit=1)
    check("query ranks the matching file first",
          bool(top) and top[0][0] == "turtle_renderer.py" and top[0][1] > 0,
          f"({top})")
    check("unmatched query returns nothing (no arbitrary picks)",
          idx.search("database migration postgres", limit=4) == [])

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    for i in range(5):
        (ws / f"mod{i}.py").write_text(f"V{i} = {i}\n", encoding="utf-8")
    capped = RepoIndex(ws, max_files=3)
    capped.refresh()
    check("file cap bounds the index and sets truncated",
          len(capped.entries) == 3 and capped.truncated,
          f"({len(capped.entries)} entries, truncated={capped.truncated})")

sys.exit(h.finish("repo_index"))
