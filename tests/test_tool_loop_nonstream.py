# -*- coding: utf-8 -*-
"""Behavior test: non-streaming ToolLoop POST (2026-11-07 live finding).

The legacy tool-agent route (ToolLoopConfig.stream=False) parses the
response inside `client.stream(...)`. Without `await resp.aread()` the
parse raises httpx.ResponseNotRead and the whole route ends with
stop_reason=error. This drives the REAL ToolLoop against a local fake
server, no LLM.
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

passed = failed = 0


def check(name, cond, extra=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS {name}{extra}")
    else:
        failed += 1
        print(f"  FAIL {name}{extra}")


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "OK"}}],
            "usage": {"completion_tokens": 1, "prompt_tokens": 3},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


async def _drive():
    events = []
    async with httpx.AsyncClient() as client:
        loop = ToolLoop(
            config=ToolLoopConfig(
                stream=False, max_rounds=1, max_post_attempts=1,
                model="fake:1b", tools=[], tool_mode="",
                require_tool_call=False,
            ),
            http_client=client, port=port, workspace="",
        )
        async for ev in loop.run([{"role": "user", "content": "hi"}]):
            events.append(ev)
        return loop.state, events


try:
    state, events = asyncio.run(_drive())
    types = [e.get("type") for e in events]
    text = "".join(str(e.get("content", "")) for e in events if e.get("type") == "token")
    check("non-stream POST completes without ResponseNotRead",
          state.stop_reason == "completed", f" (stop={state.stop_reason})")
    check("assistant text is emitted", text.strip() == "OK", f" ({text!r})")
    check("no error event", "error" not in types, f" ({types})")
    check("usage is summed into the loop state",
          state.prompt_tokens == 3 and state.completion_tokens == 1,
          f" (prompt={state.prompt_tokens}, completion={state.completion_tokens})")
finally:
    srv.shutdown()

print()
print("=" * 50)
print(f"  {passed} passed, {failed} failed  (total {passed + failed})")
print("=" * 50)
sys.exit(0 if failed == 0 else 1)
