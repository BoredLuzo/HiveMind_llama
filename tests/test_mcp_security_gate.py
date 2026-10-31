# -*- coding: utf-8 -*-
"""MCP security fixes from the 2026-10-31 audit (behavioural, in-process).

Covers P1 S2 (exec-class tools missing from the internal-token set), P1 S1
(the IDE/MCP bridge had no run id, so the approval gate silently allowed),
P2 S5 (SSE response for another id was returned as the answer) and P2 C3
(no DELETE on close).
"""
import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name} {extra}")
    else:
        failed += 1
        print(f"  FAIL {name} {extra}")


# ── S2: exec-class tools require the internal token ─────────────────────────
from routers.core import (_internal_exec_allowed,  # noqa: E402
                          _INTERNAL_MUTATING_TOOLS)

_old_tok = os.environ.pop("HIVEMIND_INTERNAL_TOKEN", None)
try:
    for _tool in ("run_bash", "shell", "run_python", "start_background",
                  "install_package", "run_tests", "browser"):
        _ok, _why = _internal_exec_allowed(_tool)
        check(f"S2 {_tool} is token-gated", not _ok, _why[:60])
    _ok, _ = _internal_exec_allowed("read_file")
    check("S2 read_file stays available", _ok)
finally:
    if _old_tok is not None:
        os.environ["HIVEMIND_INTERNAL_TOKEN"] = _old_tok

# ── S1: the mutating class needs the same token over /internal/tool/exec ────
from tools.runner import _APPROVAL_TOOLS  # noqa: E402

check("S1 mutating set is the runner's gated set (import identity)",
      _INTERNAL_MUTATING_TOOLS == _APPROVAL_TOOLS,
      f"(missing={sorted(_APPROVAL_TOOLS - _INTERNAL_MUTATING_TOOLS)})")
_old_tok = os.environ.pop("HIVEMIND_INTERNAL_TOKEN", None)
try:
    for _tool in ("write_file", "edit_file", "write_file_append", "edit_ast",
                  "replace_lines", "patch_file", "git_commit", "mcp_foo_bar"):
        _ok, _why = _internal_exec_allowed(_tool)
        check(f"S1 {_tool} is token-gated", not _ok, _why[:60])
    _ok, _ = _internal_exec_allowed("read_file")
    check("S1 read-only tools stay available", _ok)
finally:
    if _old_tok is not None:
        os.environ["HIVEMIND_INTERNAL_TOKEN"] = _old_tok

# ── S5: an SSE answer for another id is an error, not the answer ────────────
# 2026-10-31 (audit C2/C10): _sse_response is now the per-event matcher of
# the streaming reader; it returns None for a foreign id and records it, and
# the raise lives in HttpConn._read_sse (pinned end-to-end in
# tests/test_mcp_client.py block 26b).
from infra.mcp_client import _SSEAssembler, _sse_response  # noqa: E402

_asm_5 = _SSEAssembler()
_foreign_5: list = []
_events_5 = _asm_5.feed(
    'data: {"jsonrpc":"2.0","id":2,"result":{"value":"other"}}\n\n')
check("S5 the assembler completes one event", len(_events_5) == 1)
check("S5 id mismatch is not returned as the answer",
      _sse_response(_events_5[0], 1, _foreign_5) is None and _foreign_5 == [2],
      f"(foreign={_foreign_5})")

_ok_events = _SSEAssembler().feed(
    'data: {"jsonrpc":"2.0","id":7,"result":{"value":"mine"}}\n\n')
check("S5 matching id still works",
      _sse_response(_ok_events[0], 7, [])["result"]["value"] == "mine")

# ── C3: close DELETEs the HTTP session ──────────────────────────────────────
from infra.mcp_client import HttpConn  # noqa: E402


class _FakeClient:
    def __init__(self):
        self.deleted = None
        self.closed = False

    async def delete(self, url, headers=None):
        self.deleted = (url, dict(headers or {}))

    async def aclose(self):
        self.closed = True


_conn = object.__new__(HttpConn)
_conn._session_id = "sess-1"
_conn._bearer = ""
_conn._client = _FakeClient()
_conn.spec = SimpleNamespace(url="http://127.0.0.1:9/mcp", headers={})
# C11 (2026-10-31): close() now waits on the per-conn guard; the fake conn
# bypasses __init__, so it carries its own (free) lock.
_conn._guard = asyncio.Lock()
_fake = _conn._client
asyncio.run(_conn.close())
check("C3 close sends DELETE with the session id",
      _fake.deleted is not None
      and _fake.deleted[1].get("Mcp-Session-Id") == "sess-1",
      str(_fake.deleted)[:80])
check("C3 client is closed afterwards", _fake.closed and _conn._client is None)

print(f"\nmcp_security_gate: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
