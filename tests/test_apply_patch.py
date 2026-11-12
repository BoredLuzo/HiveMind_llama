# -*- coding: utf-8 -*-
"""apply_patch behavior tests (D3 edit/diff engine, 2026-11-11).

The patch-native path: `apply_patch` applies unified diffs to one or more
workspace files. This suite drives the HANDLER (I/O included) plus the pure
parser, and pins the four required behavior classes:

  - malformed patches are refused with NOTHING written
  - path escapes (../, absolute, drive letters) are refused
  - multi-file patches apply deterministically (patch order, deletions
    last) and all-or-nothing at the validation stage
  - a write failure midway rolls back the files already written

Run: python tests/test_apply_patch.py
"""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from tools.handlers.patch_apply import _inline_tool_apply_patch   # noqa: E402
from tools.handlers.file_ops import (                             # noqa: E402
    _inline_tool_edit_file,
    _inline_tool_write_file,
)
from tools.patch_diff import parse_unified_diff, PatchError       # noqa: E402
from tests._harness import Harness                                # noqa: E402

h = Harness(pad=True)
check = h.check

_BODY_A = "def a():\n    return 1\n"
_BODY_B = "VALUE = 10\n"


async def _seed(ws: Path, name: str, body: str) -> None:
    await _inline_tool_write_file({"path": name, "content": body}, ws, str(ws))


def _read(ws: Path, name: str) -> str:
    return (ws / name).read_text(encoding="utf-8", newline="")


# ── 1. happy path: single-file patch ────────────────────────────────────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_seed(ws, "app.py", _BODY_A))
    out = asyncio.run(_inline_tool_apply_patch({
        "patch": (
            "--- a/app.py\n"
            "+++ b/app.py\n"
            "@@ -1,2 +1,5 @@\n"
            " def a():\n"
            "-    return 1\n"
            "+    return 42\n"
            "+\n"
            "+def b():\n"
            "+    return 2\n"
        ),
    }, ws, str(ws)))
    body = _read(ws, "app.py")
    check("single-file patch applied",
          "return 42" in body and "def b():" in body
          and "apply_patch: 1 file(s) patched" in out,
          f"({out.strip()[:100]})")
    check("summary reports the line counts", "(+4/-1 lines)" in out)
    check("ordering guarantee documented in result",
          "Ordering guarantee" in out)

    # ── 2. old edit APIs unchanged ──
    out_e = asyncio.run(_inline_tool_edit_file(
        {"path": "app.py", "old_text": "    return 42", "new_text": "    return 43"},
        ws, str(ws)))
    check("edit_file old_text/new_text API untouched by apply_patch work",
          "return 43" in _read(ws, "app.py") and "replaced" in out_e,
          f"({out_e.strip()[:80]})")

# ── 2b. multi-file patch: edit + create + delete in one call ───────────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_seed(ws, "a.py", "x = 1\nprint(x)\n"))
    asyncio.run(_seed(ws, "old.py", "stale = True\n"))
    out = asyncio.run(_inline_tool_apply_patch({
        "patch": (
            "diff --git a/a.py b/a.py\n"
            "--- a/a.py\n"
            "+++ b/a.py\n"
            "@@ -1,2 +1,2 @@\n"
            "-x = 1\n"
            "+x = 2\n"
            " print(x)\n"
            "diff --git a/newmod.py b/newmod.py\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            "+++ b/newmod.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+import a\n"
            "+print(a.x)\n"
            "diff --git a/old.py b/old.py\n"
            "deleted file mode 100644\n"
            "--- a/old.py\n"
            "+++ /dev/null\n"
            "@@ -1,1 +0,0 @@\n"
            "-stale = True\n"
        ),
    }, ws, str(ws)))
    check("multi-file patch: edit applied",
          "x = 2" in _read(ws, "a.py"))
    check("multi-file patch: new file created",
          _read(ws, "newmod.py") == "import a\nprint(a.x)\n")
    check("multi-file patch: file deleted",
          not (ws / "old.py").exists() and "old.py deleted" in out,
          f"({out.strip()[:140]})")
    check("summary counts all three files",
          "apply_patch: 3 file(s) patched (+3/-2 lines)" in out,
          f"({out.strip()[:100]})")

# ── 3. malformed patches -> refused, NOTHING written ────────────────────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_seed(ws, "app.py", _BODY_A))
    originals = _read(ws, "app.py")
    bad_patches = {
        "empty": "",
        "garbage": "hello world, make it work",
        "headers-only": "--- a/app.py\n+++ b/app.py\n",
        "truncated-hunk": (
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1,5 +1,5 @@\n def a():\n-    return 1\n"),
        "junk-in-hunk": (
            "--- a/app.py\n+++ b/app.py\n"
            "@@ -1,2 +1,2 @@\n def a():\n    return 1\n+extra\n"),
        "header-without-+++": "--- a/app.py\n@@ -1,1 +1,1 @@\n",
        "truncated-call": (
            "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n"
            " def a():\n-    return 1\n+<parameter=old_text\n"),
        "no-target": (
            "--- a/missing.py\n+++ b/missing.py\n"
            "@@ -1,1 +1,1 @@\n-z\n+y\n"),
    }
    results = {}
    for label, bad in bad_patches.items():
        results[label] = asyncio.run(_inline_tool_apply_patch(
            {"patch": bad}, ws, str(ws)))
    check("malformed patches rejected with APPLY_PATCH_* errors",
          all(r.startswith("[TOOL_ERROR") for r in results.values()),
          f" ({ {k: v[:40] for k, v in results.items()} })")
    check("every malformed code carries a nameable error",
          all(("APPLY_PATCH_" in r) for r in results.values()))
    check("file untouched after every malformed attempt",
          _read(ws, "app.py") == originals)
    check("no-target error names the missing file",
          "missing.py" in results["no-target"])

# ── 4. path escapes refused ─────────────────────────────────────────────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_seed(ws, "app.py", _BODY_A))
    escapes = [
        ("..-escape", "--- a/../outside.py\n+++ b/../outside.py\n"
                      "@@ -1,1 +1,1 @@\n-z\n+y\n"),
        ("deep-escape", "--- a/../../outside.py\n+++ b/../../outside.py\n"
                        "@@ -1,1 +1,1 @@\n-z\n+y\n"),
        ("absolute", "--- /etc/passwd\n+++ b/x\n"
                      "@@ -1,1 +1,1 @@\n-z\n+y\n"),
        ("drive", "--- C:/Windows/x.py\n+++ b/x.py\n"
                   "@@ -1,1 +1,1 @@\n-z\n+y\n"),
        ("hidden-abs", "--- /abs/outside.py\n+++ b/x.py\n"
                        "@@ -1,1 +1,1 @@\n-z\n+y\n"),
    ]
    outs = []
    for label, esc in escapes:
        o = asyncio.run(_inline_tool_apply_patch({"patch": esc}, ws, str(ws)))
        outs.append((label, o))
    check("all path escapes refused",
          all("APPLY_PATCH_PATH_INVALID" in o for _, o in outs),
          f" ({[(l, o[:60]) for l, o in outs]})")
    check("escape attempts wrote nothing outside or inside the workspace",
          _read(ws, "app.py") == _BODY_A
          and not (ws / "outside.py").exists()
          and not (ws.parent / "outside.py").exists())
    # sanitizer unit checks (pure layer)
    from tools.patch_diff import sanitize_patch_path
    check("sanitize keeps /dev/null as None (new/delete marker)",
          sanitize_patch_path("/dev/null") is None)
    check("sanitize strips a/ b/ prefixes",
          sanitize_patch_path("b/pkg/mod.py") == "pkg/mod.py")

# ── 5. all-or-nothing validation: second file's context mismatch ───────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_seed(ws, "a.py", "x = 1\nprint(x)\n"))
    asyncio.run(_seed(ws, "b.py", "y = 2\n"))
    out = asyncio.run(_inline_tool_apply_patch({
        "patch": (
            "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 9\n print(x)\n"
            "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-WRONG\n+y = 3\n"
        ),
    }, ws, str(ws)))
    check("second-file mismatch rejects the WHOLE patch",
          "APPLY_PATCH_FAILED" in out and "b.py" in out
          and "NOTHING was written" in out,
          f"({out.strip()[:120]})")
    check("first file NOT written although its hunk matched",
          "x = 1" in _read(ws, "a.py") and "y = 2" in _read(ws, "b.py"))

# ── 6. rollback on write failure ────────────────────────────────────────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_seed(ws, "a.py", "x = 1\nprint(x)\n"))
    asyncio.run(_seed(ws, "b.py", "y = 2\n"))
    import tools.handlers.patch_apply as _pa
    _real_write = _pa._atomic_write
    _calls: list[str] = []

    def _flaky_write(path, data):
        _calls.append(Path(path).name)
        if Path(path).name == "b.py":
            raise OSError(28, "simulated no space left on device")
        _real_write(path, data)

    _pa._atomic_write = _flaky_write
    try:
        out = asyncio.run(_inline_tool_apply_patch({
            "patch": (
                "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 9\n print(x)\n"
                "--- a/b.py\n+++ b/b.py\n@@ -1,1 +1,1 @@\n-y = 2\n+y = 3\n"
            ),
        }, ws, str(ws)))
    finally:
        _pa._atomic_write = _real_write
    check("write failure surfaces as APPLY_PATCH_WRITE_FAILED",
          "APPLY_PATCH_WRITE_FAILED" in out and "a.py" in out,
          f"({out.strip()[:120]})")
    check("rollback restored the file written before the failure",
          "x = 1" in _read(ws, "a.py") and "restored a.py" in out,
          f"({out.strip()[:160]})")
    check("the failing file itself was never changed",
          "y = 2" in _read(ws, "b.py"))
    check("writes happened in patch order (a.py, failed b.py, rollback a.py)",
          _calls == ["a.py", "b.py", "a.py"], f" ({_calls})")

# ── 7. deterministic ordering: deletions run last ───────────────────────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    asyncio.run(_seed(ws, "a.py", "x = 1\nprint(x)\n"))
    asyncio.run(_seed(ws, "old.py", "stale = True\n"))
    import tools.handlers.patch_apply as _pa2
    _seq: list[str] = []
    _real_write2 = _pa2._atomic_write
    _real_unlink = _pa2.os.unlink

    def _rec_write(path, data):
        _seq.append("write:" + Path(path).name)
        _real_write2(path, data)

    def _rec_unlink(path, *a, **k):
        _seq.append("unlink:" + Path(path).name)
        return _real_unlink(path, *a, **k)

    _pa2._atomic_write = _rec_write
    _pa2.os.unlink = _rec_unlink
    try:
        out = asyncio.run(_inline_tool_apply_patch({
            "patch": (
                "diff --git a/old.py b/old.py\n"
                "--- a/old.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-stale = True\n"
                "diff --git a/a.py b/a.py\n"
                "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1,2 @@\n-x = 1\n+x = 5\n print(x)\n"
            ),
        }, ws, str(ws)))
    finally:
        _pa2._atomic_write = _real_write2
        _pa2.os.unlink = _real_unlink
    check("deterministic order: writes first, deletion last even when the "
          "delete section comes first in the patch",
          _seq == ["write:a.py", "unlink:old.py"], f" ({_seq})")
    check("both operations landed",
          "x = 5" in _read(ws, "a.py") and not (ws / "old.py").exists())

# ── 8. CRLF preserved + line-number drift tolerated ─────────────────────────
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
    ws = Path(td)
    (ws / "win.py").write_bytes(b"top = 1\r\n\r\ndef f():\r\n    return 'x'\r\n")
    out = asyncio.run(_inline_tool_apply_patch({
        "patch": (
            "--- a/win.py\n+++ b/win.py\n"
            "@@ -30,2 +30,2 @@\n def f():\n-    return 'x'\n+    return 'y'\n"
        ),
    }, ws, str(ws)))
    raw = (ws / "win.py").read_bytes()
    check("CRLF file stays CRLF after patch",
          b"return 'y'" in raw and b"\r\n" in raw and b"\n\n" not in raw,
          f"({raw[:80]!r})")
    check("header line numbers may drift (content match wins)",
          "apply_patch: 1 file(s) patched" in out, f"({out.strip()[:80]})")

# ── 9. pure parser checks (no I/O) ──────────────────────────────────────────
fp_patch = (
    "--- /dev/null\n+++ b/n.py\n@@ -0,0 +1,1 @@\n+brand new\n")
fps = parse_unified_diff(fp_patch)
check("new-file section classified", fps[0].is_new_file
      and fps[0].target == "n.py")
check("parser counts +/- per file",
      (fps[0].added(), fps[0].removed()) == (1, 0))
try:
    parse_unified_diff("--- /dev/null\n+++ /dev/null\n@@ -0,0 +0,0 @@\n")
    check("both-null header rejected", False)
except PatchError as e:
    check("both-null header rejected", e.code == "PATCH_MALFORMED")

sys.exit(h.finish("apply_patch"))
