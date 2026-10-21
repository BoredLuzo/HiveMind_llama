"""Tests fuer den Editor-Kontext (IDE -> HiveMind).

Deckt ab: defensives Parsen, Laengenbegrenzung, TTL, Workspace-Normalisierung,
Prompt-Block und die HTTP-Semantik des Endpoints (ohne laufenden Server).
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from infra import editor_context as ec  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


WS = str(ROOT / "tests")

# ── Parsen + Grenzen ─────────────────────────────────────────────────────
ctx = ec.parse_context({
    "workspace": WS,
    "file": "src/main.py",
    "cursor_line": "42",
    "selection": "def f():\n    return 1\n",
    "open_files": ["src/main.py", "src/main.py", "src/util.py"],
    "diagnostics": [{"file": "src/main.py", "line": 3, "severity": "error", "message": "boom"}],
    "source": "jetbrains",
})
check("workspace/file parsed", ctx.file == "src/main.py" and ctx.source == "jetbrains")
check("cursor_line cast to int", ctx.cursor_line == 42, f"({ctx.cursor_line})")
check("open_files deduped", ctx.open_files == ["src/main.py", "src/util.py"], f"({ctx.open_files})")

long_ctx = ec.parse_context({"workspace": WS, "selection": "x" * (ec.MAX_SELECTION_CHARS + 500)})
check("selection truncated", len(long_ctx.selection) <= ec.MAX_SELECTION_CHARS + 40,
      f"({len(long_ctx.selection)})")
check("truncation marked", "[truncated]" in long_ctx.selection)

many = ec.parse_context({"workspace": WS, "diagnostics": [
    {"file": "a", "line": i, "message": f"m{i}"} for i in range(50)]})
check("diagnostics capped", len(many.diagnostics) == ec.MAX_DIAGNOSTICS, f"({len(many.diagnostics)})")

junk = ec.parse_context({"workspace": WS, "cursor_line": "abc", "diagnostics": "nope",
                         "open_files": None, "selection": None})
check("junk input does not raise", junk.cursor_line == 0 and junk.diagnostics == [])
check("missing workspace rejected", ec.parse_context({}).normalized_workspace() == "")

# ── Store + TTL ──────────────────────────────────────────────────────────
store = ec.EditorContextStore(ttl_s=0.2)
store.put(ctx)
check("store returns context", store.get(WS) is not None)
check("store is keyed by resolved path (case-insensitive)",
      store.get(WS.upper()) is not None)
time.sleep(0.3)
check("context expires after TTL", store.get(WS) is None)
check("clearing a missing entry is False", store.clear(WS) is False)

store2 = ec.EditorContextStore()
store2.put(ctx)
check("clear removes it", store2.clear(WS) is True and store2.get(WS) is None)

# ── Prompt-Block ─────────────────────────────────────────────────────────
block = ec.format_block(ctx)
check("block names the file", "Open file: src/main.py" in block)
check("block carries the selection", "def f():" in block)
check("block carries diagnostics", "[error] src/main.py:3 boom" in block)
check("block is delimited", block.startswith("[EDITOR CONTEXT") and block.rstrip().endswith("[/EDITOR CONTEXT]"))
check("empty context gives empty block", ec.format_block(ec.EditorContext()) == "")

# ── HTTP-Endpoint (ASGI, ohne Server) ────────────────────────────────────
from routers.context import context_router  # noqa: E402

_paths = {(r.path, tuple(sorted(r.methods))) for r in context_router.routes}
check("POST /context/editor registered", ("/context/editor", ("POST",)) in _paths, f"({_paths})")
check("GET handles missing context", ("/context/editor", ("GET",)) in _paths)
check("DELETE registered", ("/context/editor", ("DELETE",)) in _paths)


class _Req:
    """Minimaler Request-Ersatz fuer den Handler (body + json)."""
    def __init__(self, payload, raw_len=None):
        self._payload = payload
        self._raw_len = raw_len if raw_len is not None else 64

    async def body(self):
        return b"x" * self._raw_len

    async def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


_post = next(r.endpoint for r in context_router.routes
             if r.path == "/context/editor" and "POST" in getattr(r, "methods", set()))
_delete = next(r.endpoint for r in context_router.routes
               if r.path == "/context/editor" and "DELETE" in getattr(r, "methods", set()))

ok_res = asyncio.run(_post(_Req({"workspace": WS, "file": "b.py"})))
check("POST returns ok", isinstance(ok_res, dict) and ok_res.get("ok") is True, f"({ok_res})")
big = asyncio.run(_post(_Req({}, raw_len=2 * 1024 * 1024)))
check("oversized body rejected with 413", getattr(big, "status_code", None) == 413,
      f"({getattr(big, 'status_code', None)})")
bad = asyncio.run(_post(_Req(ValueError("no json"))))
check("invalid JSON rejected with 400", getattr(bad, "status_code", None) == 400)
missing = asyncio.run(_post(_Req({"file": "b.py"})))
check("missing workspace rejected with 400", getattr(missing, "status_code", None) == 400)
removed = asyncio.run(_delete(workspace=WS))
check("DELETE reports removal", removed.get("removed") is True, f"({removed})")

print()
print(f"SUMMARY editor_context: {passed} passed, {failed} failed")
sys.exit(0 if failed == 0 else 1)
