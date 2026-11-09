# -*- coding: utf-8 -*-
"""ToolLoop phase tools x tool_mode intersection (task F12, 2026-11-08).

Before: ToolLoop.run replaced the advertised payload with
get_tools_for_phase(tool_phase) WITHOUT consulting cfg.tool_mode - the mode
only gated EXECUTION (_run_inline_tool), so a mode-restricted caller with a
phase advertised tools the executor then rejected.

Now: when a phase is set AND cfg.tool_mode is set, the phase subset is
intersected with the mode allowlist (_tool_names_for_mode). On an empty
intersection the phase subset ships unchanged (never an empty tool list)
and a debug line records the misconfiguration. No phase or empty
tool_mode -> behavior exactly as before (no caller sets tool_phase today).

Hermetic: local ThreadingHTTPServer fake, no llama-server, no model.

Run: python tests/test_tool_loop_phase_mode.py
Exit 0 = all pass, Exit 1 = failures.
"""
import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.tool_loop import ToolLoop, ToolLoopConfig  # noqa: E402
from tools.definitions import get_tools_for_phase, _tool_names_for_mode  # noqa: E402

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}{extra}")
    else:
        failed += 1
        print(f"  FAIL {name}{extra}")


def _names(tools):
    return sorted(str(t.get("function", {}).get("name", "")) for t in (tools or []))


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            body = {}
        self.server.received_tools = _names(body.get("tools"))
        out = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"completion_tokens": 1, "prompt_tokens": 3},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _names_of(phase):
    return _names(get_tools_for_phase(phase))


def _run_once(*, tool_phase, tool_mode, tools):
    """Drive one ToolLoop.run against a fresh fake server; return shipped names."""
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.received_tools = None
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    async def _drive():
        async with httpx.AsyncClient() as client:
            loop = ToolLoop(
                config=ToolLoopConfig(
                    stream=False, max_rounds=1, max_post_attempts=1,
                    model="fake:1b", tools=list(tools), tool_mode=tool_mode,
                    tool_phase=tool_phase, require_tool_call=False,
                ),
                http_client=client, port=srv.server_address[1], workspace="",
            )
            async for _ev in loop.run([{"role": "user", "content": "hi"}]):
                pass
            return loop.state

    try:
        state = asyncio.run(_drive())
        assert state.stop_reason == "completed", f"stop={state.stop_reason}"
        return srv.received_tools
    finally:
        srv.shutdown()


# Expected sets (computed from the real definitions, not hardcoded):
_write_phase = _names_of("write")                      # + task_complete/ask_user
_test_phase = _names_of("test")
_fix_mode = sorted(_tool_names_for_mode("fix_agent"))
_write_x_fix = sorted(set(_write_phase) & set(_fix_mode))
_test_x_fix = sorted(set(_test_phase) & set(_fix_mode))

# The scenarios only make sense if the fixtures behave as assumed:
check("fixture: phase 'write' x fix_agent is a PROPER subset",
      _write_x_fix and _write_x_fix != _write_phase, f" {_write_x_fix}")
check("fixture: phase 'test' x fix_agent is EMPTY",
      _test_x_fix == [], f" {_test_x_fix}")


# ── 1. phase only, tool_mode empty: unchanged behavior ─────────────────────
shipped = _run_once(tool_phase="write", tool_mode="",
                    tools=get_tools_for_phase("write"))
check("phase without mode ships the full phase subset",
      shipped == _write_phase, f" {shipped}")

# ── 2. phase + tool_mode: intersection ships, mode wins over phase ─────────
shipped = _run_once(tool_phase="write", tool_mode="fix_agent",
                    tools=get_tools_for_phase("write"))
check("phase x mode ships the intersection",
      shipped == _write_x_fix, f" {shipped}")
check("mode drops phase tools it does not allow (task_complete/ask_user)",
      "task_complete" not in shipped and "ask_user" not in shipped,
      f" {shipped}")

# ── 3. phase + tool_mode with EMPTY intersection: phase subset survives ────
shipped = _run_once(tool_phase="test", tool_mode="fix_agent",
                    tools=get_tools_for_phase("test"))
check("empty intersection keeps the phase subset (never an empty list)",
      shipped == _test_phase and shipped, f" {shipped}")
check("empty-intersection fallback is not the mode allowlist",
      set(shipped) <= set(_test_phase), f" {shipped}")

# ── 4. no phase (default 'all') + tool_mode: cfg.tools untouched ────────────
_custom = [{"type": "function", "function": {"name": "read_file",
                                             "parameters": {"type": "object"}}},
           {"type": "function", "function": {"name": "ask_user",
                                             "parameters": {"type": "object"}}}]
shipped = _run_once(tool_phase="all", tool_mode="fix_agent", tools=_custom)
check("phase 'all' + tool_mode ships cfg.tools unchanged",
      shipped == _names(_custom), f" {shipped}")

shipped = _run_once(tool_phase="", tool_mode="fix_agent", tools=_custom)
check("empty phase + tool_mode ships cfg.tools unchanged",
      shipped == _names(_custom), f" {shipped}")


# ── 5. source guards ────────────────────────────────────────────────────────
_src = (ROOT / "core" / "tool_loop.py").read_text(encoding="utf-8")
check("phase branch intersects with the mode allowlist",
      "_tool_names_for_mode(" in _src)
check("empty intersection keeps the phase subset + logs",
      "shipping the unfiltered phase subset" in _src)
check("phase-only path (no tool_mode) unchanged",
      "_phase_tools" in _src)


print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
