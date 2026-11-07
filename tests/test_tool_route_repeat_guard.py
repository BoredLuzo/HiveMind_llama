# -*- coding: utf-8 -*-
"""A4 integration: repeat guard ends a repeated tool call (2026-11-07).

A fake llama-server repeats the SAME list_dir call until it sees the repeat
nudge in the request messages. Without the guard the loop exhausts its
rounds (stop_reason max_tool_rounds); with _repeat_guard() the nudge lands
after round 2 and the model answers -> stop_reason completed.
"""
import asyncio
import json
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from core.chat_tools_route import _repeat_guard  # noqa: E402
from core.tool_loop import ToolLoop, ToolLoopConfig  # noqa: E402
from tests._harness import Harness  # noqa: E402

h = Harness()
check = h.check

_TOOLS = [{
    "type": "function",
    "function": {
        "name": "list_dir",
        "description": "list a directory",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        nudged = any(m.get("role") == "user"
                     and "already called this tool" in str(m.get("content") or "")
                     for m in messages)
        if nudged:
            msg = {"role": "assistant", "content": "Folder listed."}
        else:
            msg = {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "list_dir",
                             "arguments": '{"path": "."}'},
            }]}
        out = json.dumps({
            "choices": [{"message": msg}],
            "usage": {"completion_tokens": 1, "prompt_tokens": 3},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()


async def _drive(workspace: str, guard: bool):
    async with httpx.AsyncClient() as client:
        loop = ToolLoop(
            config=ToolLoopConfig(
                stream=False, max_rounds=4, max_post_attempts=1,
                model="fake:1b", tools=_TOOLS, tool_mode="tool_agent",
                require_tool_call=False,
            ),
            http_client=client, port=port, workspace=workspace,
            on_after_round=_repeat_guard() if guard else None,
        )
        events = []
        async for ev in loop.run([{"role": "user", "content": "List files"}]):
            events.append(ev)
        return loop.state, events


try:
    with tempfile.TemporaryDirectory(prefix="hm_a4_") as td:
        (Path(td) / "sample.txt").write_text("x", encoding="utf-8")

        plain_state, _ = asyncio.run(_drive(td, guard=False))
        check("without guard the repeated call exhausts the rounds",
              plain_state.stop_reason == "max_tool_rounds",
              f" (stop={plain_state.stop_reason}, calls={plain_state.tool_calls_made})")

        guarded_state, guarded_events = asyncio.run(_drive(td, guard=True))
        check("with guard the repeat is nudged and the loop completes",
              guarded_state.stop_reason == "completed",
              f" (stop={guarded_state.stop_reason})")
        check("guard still executed the tool twice",
              guarded_state.tool_calls_made == 2,
              f" ({guarded_state.tool_calls_made})")
        _text = "".join(str(e.get("content") or "") for e in guarded_events
                        if e.get("type") == "token")
        check("final answer is emitted", "Folder listed." in _text, f" ({_text!r})")
finally:
    srv.shutdown()

sys.exit(h.finish("tool_route_repeat_guard"))
