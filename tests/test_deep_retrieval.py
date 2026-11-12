# -*- coding: utf-8 -*-
"""D2 deep retrieval (2026-11-11): bounded nested repo index + coder hint block.

context/repo_index.deep_context_block / deep_search_workspace and
core.duo_helpers.build_deep_retrieval_context. Focus: ranking of NESTED
files, budget + cap bounds, no-match/empty fallback, and byte-identity of
the existing tiny-workspace pre-seed path.

Run: python tests/test_deep_retrieval.py
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from context.repo_index import (            # noqa: E402
    RepoIndex, deep_context_block, deep_search_workspace,
)
from core.duo_helpers import (              # noqa: E402
    build_deep_retrieval_context, build_preseed_context,
    _duo_deep_retrieval_cfg,
)
from settings_defaults import DEFAULT_SETTINGS   # noqa: E402
from tests._harness import Harness               # noqa: E402

h = Harness(style="plain", pad=True)
check = h.check


# ── 1. Nested ranking ─────────────────────────────────────────────────────

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "src" / "render").mkdir(parents=True)
    (ws / "src" / "render" / "turtle.py").write_text(
        "SCREEN_W = 800\n"
        "class TurtleRenderer:\n    pass\n",
        encoding="utf-8")
    (ws / "src" / "main.py").write_text("def main():\n    pass\n",
                                        encoding="utf-8")
    (ws / "docs").mkdir()
    (ws / "docs" / "notes.md").write_text("# notes\n", encoding="utf-8")
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "index.js").write_text(
        "function turtle_noise(){}\n", encoding="utf-8")
    (ws / ".venv").mkdir()
    (ws / ".venv" / "lib.py").write_text("TURTLE = 1\n", encoding="utf-8")
    (ws / ".secret_cfg.py").write_text("TURTLE = 2\n", encoding="utf-8")

    idx = RepoIndex(ws, nested=True)
    idx.refresh()
    check("nested files are indexed with posix rel paths",
          {"src/render/turtle.py", "src/main.py", "docs/notes.md"}
          <= set(idx.entries), f"({sorted(idx.entries)})")
    check("heavy dirs (node_modules) and dotted paths are pruned",
          not any(r.startswith("node_modules") or "/." in r or r.startswith(".")
                  for r in idx.entries), f"({sorted(idx.entries)})")

    ranked = deep_search_workspace(ws, "turtle renderer SCREEN_W")
    check("deep query ranks the nested file first with a posix rel path",
          bool(ranked) and ranked[0][0] == "src/render/turtle.py"
          and ranked[0][1] > 0, f"({ranked})")

    block, names = deep_context_block(ws, "fix the turtle renderer SCREEN_W")
    check("hint block ranks the nested file and keeps posix separators",
          bool(block) and names[:1] == ["src/render/turtle.py"]
          and "src/render/turtle.py" in block, f"({names})")
    check("hint block carries the file's top symbols",
          bool(block) and "TurtleRenderer" in block and "SCREEN_W" in block,
          f"({block[:160]})")
    check("hint block is signatures, never full contents",
          bool(block) and "class TurtleRenderer:" not in block
          and "read_file" in block, f"({block[:200]})")
    check("pruned files can never be hinted",
          not names or not any("node_modules" in n for n in names),
          f"({names})")
    check("block is deterministic (same query -> same bytes)",
          block == deep_context_block(ws, "fix the turtle renderer SCREEN_W")[0])

# three-level nesting within the default depth
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "pkg" / "net" / "impl").mkdir(parents=True)
    (ws / "pkg" / "net" / "impl" / "client.py").write_text(
        "def send_packet():\n    pass\n", encoding="utf-8")
    ranked = deep_search_workspace(ws, "send_packet")
    check("three-level nested file is found and ranked",
          bool(ranked) and ranked[0][0] == "pkg/net/impl/client.py",
          f"({ranked})")


# ── 2. Bounded walk: max_files and max_depth ──────────────────────────────

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    for i in range(5):
        (ws / "src").mkdir(exist_ok=True)
        (ws / "src" / f"mod{i}.py").write_text(f"V{i} = {i}\n",
                                               encoding="utf-8")
    capped = RepoIndex(ws, nested=True, max_files=3)
    capped.refresh()
    check("deep walk honors max_files and sets truncated",
          len(capped.entries) == 3 and capped.truncated,
          f"({len(capped.entries)} entries, truncated={capped.truncated})")

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "l1" / "l2" / "l3").mkdir(parents=True)
    (ws / "l1" / "ok.py").write_text("OK = 1\n", encoding="utf-8")
    (ws / "l1" / "l2" / "mid.py").write_text("MID = 1\n", encoding="utf-8")
    (ws / "l1" / "l2" / "l3" / "deep.py").write_text("DEEP = 1\n",
                                                     encoding="utf-8")
    shallow = RepoIndex(ws, nested=True, max_depth=2)
    shallow.refresh()
    check("max_depth stops descent (l2 indexed, l3 never visited)",
          "l1/l2/mid.py" in shallow.entries
          and "l1/l2/l3/deep.py" not in shallow.entries
          and "l1/ok.py" in shallow.entries, f"({sorted(shallow.entries)})")
    deep = RepoIndex(ws, nested=True, max_depth=6)
    deep.refresh()
    check("a larger depth indexes the same file",
          "l1/l2/l3/deep.py" in deep.entries, f"({sorted(deep.entries)})")


# ── 3. Incremental cache on the nested walk (no regression) ───────────────

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "src").mkdir()
    (ws / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (ws / "src" / "b.py").write_text("B = 1\n", encoding="utf-8")
    idx = RepoIndex(ws, nested=True)
    idx.refresh()
    first = idx.extractions
    idx.refresh()
    check("unchanged nested files keep their cached symbols",
          idx.extractions == first and len(idx.entries) == 2,
          f"(extractions {first} -> {idx.extractions})")
    (ws / "src" / "b.py").write_text("def changed():\n    pass\n",
                                     encoding="utf-8")
    os.utime(ws / "src" / "b.py", (1_600_000_000.0, 1_600_000_000.0))
    idx.refresh()
    check("changed nested file is re-extracted",
          idx.extractions == first + 1
          and "changed" in idx.entries["src/b.py"].symbols,
          f"(extractions {first} -> {idx.extractions})")
    (ws / "src" / "a.py").unlink()
    idx.refresh()
    check("vanished nested file is dropped",
          "src/a.py" not in idx.entries and "src/b.py" in idx.entries,
          f"({sorted(idx.entries)})")


# ── 4. Budget: block stays under the cap, extras are dropped ──────────────

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    for tag in ("a", "b", "c", "d"):
        d = ws / f"p_{tag}"
        d.mkdir()
        (d / f"widget_{tag}.py").write_text(
            f"WIDGET_KIND_{tag.upper()} = 1\nclass Widget_{tag.upper()}:\n"
            f"    pass\n",
            encoding="utf-8")
    ranked = deep_search_workspace(ws, "widget", limit=10)
    check("all four widget files rank for the query",
          len(ranked) == 4, f"({ranked})")
    big_block, big_names = deep_context_block(
        ws, "widget", max_hints=5, budget_chars=3000)
    check("generous budget keeps every ranked hint",
          len(big_names) == 4 and len(big_block) <= 3000, f"({big_names})")
    small_block, small_names = deep_context_block(
        ws, "widget", max_hints=5, budget_chars=300)
    check("small budget drops hints that do not fit (never mid-line)",
          0 < len(small_names) < len(big_names)
          and len(small_block) <= 300
          and small_names == big_names[:len(small_names)],
          f"({len(small_names)} hints, {len(small_block)} chars)")
    tiny_block, tiny_names = deep_context_block(
        ws, "widget", max_hints=1, budget_chars=50)
    check("budget below the minimum clamp still yields a valid block",
          (tiny_block == "" and tiny_names == [])
          or (tiny_names and len(tiny_block) <= 300),
          f"({len(tiny_names)} hints, {len(tiny_block)} chars)")


# ── 5. No-match / empty fallback (no arbitrary picks) ─────────────────────

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "src").mkdir()
    (ws / "src" / "turtle.py").write_text("T = 1\n", encoding="utf-8")
    check("empty query -> no block",
          deep_context_block(ws, "") == ("", []))
    check("whitespace query -> no block",
          deep_context_block(ws, "   \n") == ("", []))
    check("no-match query -> no block (no arbitrary files)",
          deep_context_block(ws, "kubernetes helm deployment chart")
          == ("", []))
    check("no-match deep search -> empty list",
          deep_search_workspace(ws, "kubernetes helm deployment") == [])
    check("broken workspace path -> no block, no raise",
          deep_context_block(str(ws / "missing" / "dir"), "turtle")
          == ("", []))


# ── 6. Byte-identity of the existing pre-seed path ────────────────────────

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "src").mkdir()
    (ws / "src" / "turtle.py").write_text("T = 1\n", encoding="utf-8")
    # only NESTED files: the tiny-workspace pre-seed must stay silent
    pb, pn = build_preseed_context(str(ws), 16384, query="turtle")
    check("nested-only workspace: preseed stays empty (byte-identity)",
          pb == "" and pn == [], f"({pn})")
    db, dn = build_deep_retrieval_context(str(ws), "turtle")
    check("nested-only workspace: deep hints fire where preseed did not",
          bool(db) and dn == ["src/turtle.py"], f"({dn})")

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    for name in ("a.py", "b.py", "c.py"):
        (ws / name).write_text("X = 1\n", encoding="utf-8")
    pb, pn = build_preseed_context(str(ws), 16384, query="whatever")
    check("<=4 top-level files: preseed format unchanged",
          pb.startswith("[PRE-SEEDED WORKSPACE FILES") and pn == ["a.py", "b.py", "c.py"],
          f"({pn}, starts '{pb[:40]}')")
    check("<=4 top-level files: no retrieval header leaks into the preseed",
          "RETRIEVAL HINTS" not in pb)

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "renderer.py").write_text("def draw_frame():\n    pass\n",
                                    encoding="utf-8")
    (ws / "network.py").write_text("def send_packet():\n    pass\n",
                                   encoding="utf-8")
    (ws / "ui.py").write_text("U = 1\n", encoding="utf-8")
    (ws / "audio.py").write_text("A = 1\n", encoding="utf-8")
    (ws / "cache.py").write_text("C = 1\n", encoding="utf-8")
    check(">4 top-level files + empty query: old no-preseed behavior kept",
          build_preseed_context(str(ws), 16384, query="") == ("", []))
    check(">4 top-level files + no-match query: old no-preseed behavior kept",
          build_preseed_context(
              str(ws), 16384, query="kubernetes helm deployment") == ("", []))
    check(">4 top-level files + no-match query: deep hints also stay empty",
          build_deep_retrieval_context(
              str(ws), "kubernetes helm deployment") == ("", []))


# ── 7. Config knobs ───────────────────────────────────────────────────────

check("shipped defaults carry the five deep-retrieval keys",
      DEFAULT_SETTINGS.get("duo_deep_retrieval") is True
      and DEFAULT_SETTINGS.get("duo_deep_retrieval_max_files") == 5
      and DEFAULT_SETTINGS.get("duo_deep_retrieval_max_chars") == 3000
      and DEFAULT_SETTINGS.get("duo_index_max_files") == 2000
      and DEFAULT_SETTINGS.get("duo_index_max_depth") == 6,
      f"({[DEFAULT_SETTINGS.get(k) for k in (
          'duo_deep_retrieval', 'duo_deep_retrieval_max_files',
          'duo_deep_retrieval_max_chars', 'duo_index_max_files',
          'duo_index_max_depth')]})")

check("cfg reader: defaults from an empty dict",
      _duo_deep_retrieval_cfg({}) == (True, 5, 3000, 2000, 6),
      f"({_duo_deep_retrieval_cfg({})})")
check("cfg reader: clamps out-of-range values",
      _duo_deep_retrieval_cfg({
          "duo_deep_retrieval": False,
          "duo_deep_retrieval_max_files": 0,
          "duo_deep_retrieval_max_chars": 5,
          "duo_index_max_files": 0,
          "duo_index_max_depth": 99,
      }) == (False, 1, 300, 1, 24),
      f"({_duo_deep_retrieval_cfg({'duo_deep_retrieval': False, 'duo_deep_retrieval_max_files': 0, 'duo_deep_retrieval_max_chars': 5, 'duo_index_max_files': 0, 'duo_index_max_depth': 99})})")
check("cfg reader: garbage values fall back to defaults",
      _duo_deep_retrieval_cfg({
          "duo_deep_retrieval_max_files": "many",
          "duo_deep_retrieval_max_chars": None,
          "duo_index_max_files": "huge",
          "duo_index_max_depth": "deep",
      }) == (True, 5, 3000, 2000, 6),
      f"({_duo_deep_retrieval_cfg({'duo_deep_retrieval_max_files': 'many', 'duo_deep_retrieval_max_chars': None, 'duo_index_max_files': 'huge', 'duo_index_max_depth': 'deep'})})")

with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "src").mkdir()
    (ws / "src" / "turtle.py").write_text("T = 1\n", encoding="utf-8")
    off = build_deep_retrieval_context(
        str(ws), "turtle", settings_dict={"duo_deep_retrieval": False})
    check("knob duo_deep_retrieval=false disables the block entirely",
          off == ("", []), f"({off})")
    capped_block, capped_names = build_deep_retrieval_context(
        str(ws), "turtle",
        settings_dict={"duo_deep_retrieval_max_files": 1,
                       "duo_deep_retrieval_max_chars": 300})
    check("knobs cap hint count and budget via the helper",
          bool(capped_block) and len(capped_names) == 1
          and len(capped_block) <= 300, f"({capped_names}, {len(capped_block)})")
    check("empty query never produces a block (helper guard)",
          build_deep_retrieval_context(str(ws), "") == ("", []))

sys.exit(h.finish("deep_retrieval"))
