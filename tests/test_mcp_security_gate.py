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
    for _tool in ("run_bash", "run_python", "start_background", "install_package",
                  "run_tests", "browser"):
        _ok, _why = _internal_exec_allowed(_tool)
        check(f"S2 {_tool} is token-gated", not _ok, _why[:60])
    _ok, _ = _internal_exec_allowed("read_file")
    check("S2 read_file stays available", _ok)
finally:
    if _old_tok is not None:
        os.environ["HIVEMIND_INTERNAL_TOKEN"] = _old_tok

# ── S1: the mutating class needs the same token over /internal/tool/exec ────
from tools.runner import _APPROVAL_TOOLS  # noqa: E402

check("S1 mutating mirror equals the runner's gated set",
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
from infra.mcp_client import McpError, _sse_response  # noqa: E402

_body = 'data: {"jsonrpc":"2.0","id":2,"result":{"value":"other"}}\n\n'
try:
    _sse_response(_body, 1)
    check("S5 id mismatch raises", False, "returned a message")
except McpError as _exc:
    check("S5 id mismatch raises", "id mismatch" in str(_exc), str(_exc)[:70])

_ok_body = 'data: {"jsonrpc":"2.0","id":7,"result":{"value":"mine"}}\n\n'
check("S5 matching id still works", _sse_response(_ok_body, 7)["result"]["value"] == "mine")

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
_fake = _conn._client
asyncio.run(_conn.close())
check("C3 close sends DELETE with the session id",
      _fake.deleted is not None
      and _fake.deleted[1].get("Mcp-Session-Id") == "sess-1",
      str(_fake.deleted)[:80])
check("C3 client is closed afterwards", _fake.closed and _conn._client is None)

print(f"\nmcp_security_gate: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
