"""Behavioral Test: Server-side AUTO-SPLIT for oversized writes (2026-09-03).

Live-Befund: a single write_file that exceeded the per-call char limit was
rejected AFTER the model had generated the whole content (e.g. ~29k chars, ~7
minutes) -> total loss, no progress.

Fix: write the first chunk immediately, cache the remainder server-side, and
let a tiny write_file_append(path, content="<AUTO_SPLIT_CONTINUE>") finish the
file - no regeneration of the content.

Run: python tests/test_auto_split_pending.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tools.handlers import file_ops as _F  # noqa: E402

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


def main():
    _F._auto_lint_result = lambda *a, **k: ""  # noqa: E731 (lint needs runtime deps)
    wd = Path(tempfile.mkdtemp(prefix="hivemind_autosplit_"))
    try:
        print("\n=== AUTO-SPLIT server-side remainder (2026-09-03) ===\n")
        content = "".join("line %05d - %s\n" % (i, "x" * 40) for i in range(900))
        base = content.strip()  # normal write_file stores the stripped content

        r1 = asyncio.run(_F._inline_tool_write_file(
            {"path": "f.txt", "content": content, "_tool_name": "write_file"}, wd, None))
        p = wd / "f.txt"
        if "[AUTO-SPLIT]" in r1:
            ok("A1: oversized new write returns AUTO-SPLIT instruction")
        else:
            fail("A1: no AUTO-SPLIT in result", r1[:160])
        if p.exists() and p.stat().st_size < len(content):
            ok("A2: first chunk written immediately (file smaller than full content)")
        else:
            fail("A2: first chunk not written", str(p.stat().st_size if p.exists() else "missing"))

        r2 = asyncio.run(_F._inline_tool_write_file_append(
            {"path": "f.txt", "content": _F.AUTO_SPLIT_CONTINUE_MARKER,
             "_tool_name": "write_file_append"}, wd, None))
        if "AUTO-SPLIT-DONE" in r2:
            ok("A3: continuation marker completes the file")
        else:
            fail("A3: continuation marker not handled", r2[:160])
        disk = p.read_text(encoding="utf-8")
        if disk == base:
            ok("A4: final file content matches a normal (non-split) write_file")
        else:
            fail("A4: content mismatch", f"len disk={len(disk)} len base={len(base)}")

        r3 = asyncio.run(_F._inline_tool_write_file_append(
            {"path": "f.txt", "content": _F.AUTO_SPLIT_CONTINUE_MARKER,
             "_tool_name": "write_file_append"}, wd, None))
        if "AUTO_SPLIT_NO_PENDING" in r3:
            ok("A5: repeated marker without pending remainder yields clear fallback")
        else:
            fail("A5: missing fallback", r3[:160])

        # Existing-file full rewrite path (the original 7-minute-loss case).
        f2 = wd / "old.txt"
        f2.write_text("old\n", encoding="utf-8")
        r4 = asyncio.run(_F._inline_tool_write_file(
            {"path": "old.txt", "content": content, "_tool_name": "write_file",
             "__model__": ""}, wd, None))
        if "[AUTO-SPLIT]" in r4 and "EDIT_FILE_CONTENT_TOO_LARGE" not in r4:
            ok("A6: oversized rewrite of an EXISTING file auto-splits (no hard error)")
        else:
            fail("A6: existing-file rewrite not split", r4[:160])
        r5 = asyncio.run(_F._inline_tool_write_file_append(
            {"path": "old.txt", "content": _F.AUTO_SPLIT_CONTINUE_MARKER,
             "_tool_name": "write_file_append"}, wd, None))
        if "AUTO-SPLIT-DONE" in r5 and f2.read_text(encoding="utf-8") == base:
            ok("A7: existing-file AUTO-SPLIT + drain reconstructs the full content")
        else:
            fail("A7: existing-file drain mismatch", r5[:160])

        print("\n=== AUTO-SPLIT robust marker (2026-09-04) ===\n")

        # B: quoted marker ("<AUTO_SPLIT_CONTINUE>") must DRAIN, not be written
        # literally (live bug: model copies the quotes into content).
        fq = wd / "q.txt"
        _quote_marker = '"' + _F.AUTO_SPLIT_CONTINUE_MARKER + '"'
        rq1 = asyncio.run(_F._inline_tool_write_file(
            {"path": "q.txt", "content": content, "_tool_name": "write_file"}, wd, None))
        if "[AUTO-SPLIT]" in rq1:
            ok("B1: quoted-marker file split into part1")
        else:
            fail("B1: quoted-marker file not split", rq1[:120])
        rq2 = asyncio.run(_F._inline_tool_write_file_append(
            {"path": "q.txt", "content": _quote_marker,
             "_tool_name": "write_file_append"}, wd, None))
        _q_disk = fq.read_text(encoding="utf-8")
        if "AUTO-SPLIT-DONE" in rq2 and _q_disk == base:
            ok("B2: QUOTED marker drains (no literal write), file complete")
        else:
            fail("B2: quoted marker not drained correctly",
                 f"r={rq2[:120]} len_disk={len(_q_disk)} marker_in_file="
                 f"{_F.AUTO_SPLIT_CONTINUE_MARKER in _q_disk}")
        if _F.AUTO_SPLIT_CONTINUE_MARKER not in _q_disk:
            ok("B3: no literal marker text left in file")
        else:
            fail("B3: marker text leaked into file")

        # C: heal already-corrupted tail (literal quoted marker appended earlier)
        fh = wd / "h.txt"
        rh1 = asyncio.run(_F._inline_tool_write_file(
            {"path": "h.txt", "content": content, "_tool_name": "write_file"}, wd, None))
        if "[AUTO-SPLIT]" in rh1:
            ok("C1: heal-case file split into part1")
        else:
            fail("C1: heal-case file not split", rh1[:120])
        # simulate the old bug: literal quoted marker was appended to the file
        fh.write_text(fh.read_text(encoding="utf-8") + _quote_marker, encoding="utf-8")
        rh2 = asyncio.run(_F._inline_tool_write_file_append(
            {"path": "h.txt", "content": _quote_marker,
             "_tool_name": "write_file_append"}, wd, None))
        _h_disk = fh.read_text(encoding="utf-8")
        if "AUTO-SPLIT-DONE" in rh2 and _h_disk == base:
            ok("C2: drain heals prior literal marker + completes file")
        else:
            fail("C2: heal/drain failed",
                 f"r={rh2[:120]} len_disk={len(_h_disk)} marker_in_file="
                 f"{_F.AUTO_SPLIT_CONTINUE_MARKER in _h_disk}")

        # D: quoted marker WITHOUT pending -> clear error, never a literal append
        fg = wd / "g.txt"
        fg.write_text("hello\n", encoding="utf-8")
        rg = asyncio.run(_F._inline_tool_write_file_append(
            {"path": "g.txt", "content": _quote_marker,
             "_tool_name": "write_file_append"}, wd, None))
        _g_disk = fg.read_text(encoding="utf-8")
        if "AUTO_SPLIT_NO_PENDING" in rg and _F.AUTO_SPLIT_CONTINUE_MARKER not in _g_disk:
            ok("D1: quoted marker without pending -> error, no literal write")
        else:
            fail("D1: marker-without-pending handled wrong",
                 f"r={rg[:120]} disk={_g_disk!r}")

        # E: real content while a pending split exists -> normal append, pending
        # dropped (model moved on), marker is NOT drained into it.
        fe = wd / "e.txt"
        re1 = asyncio.run(_F._inline_tool_write_file(
            {"path": "e.txt", "content": content, "_tool_name": "write_file"}, wd, None))
        if "[AUTO-SPLIT]" in re1:
            ok("E1: pending-dropped file split into part1")
        else:
            fail("E1: pending-dropped file not split", re1[:120])
        re2 = asyncio.run(_F._inline_tool_write_file_append(
            {"path": "e.txt", "content": "tail-extra\n",
             "_tool_name": "write_file_append"}, wd, None))
        _e_disk = fe.read_text(encoding="utf-8")
        if "tail-extra" in _e_disk:
            ok("E2: real append content written verbatim while stale pending existed")
        else:
            fail("E2: real append not written", re2[:120])
        # pending was dropped -> a later marker must NOT resurrect stale remainder
        re3 = asyncio.run(_F._inline_tool_write_file_append(
            {"path": "e.txt", "content": _F.AUTO_SPLIT_CONTINUE_MARKER,
             "_tool_name": "write_file_append"}, wd, None))
        if "AUTO_SPLIT_NO_PENDING" in re3:
            ok("E3: stale pending dropped -> later marker errors (no ghost drain)")
        else:
            fail("E3: stale pending resurrected", re3[:120])

    finally:
        shutil.rmtree(str(wd), ignore_errors=True)

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    return failed


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
