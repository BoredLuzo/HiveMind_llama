# -*- coding: utf-8 -*-
"""wait_ready fallback ladder (2026-10-26, previously untested):

  primary: /health answers JSON status=ok (and the server hint is not an
           MCP server) + /v1/models 200 -> ready
  fallback: /health does not answer JSON (plain 200) -> require THREE
            consecutive stable 200s (1 s apart) + /v1/models 200 -> ready
  never:   dead process, MCP hint, unstable health, missing models probe

Drives the REAL wait_ready against a local stdlib HTTP server."""
import asyncio
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parent.parent
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


from backend.llama_slots import ModelSlot  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    # behavior knobs per scenario
    mode = "json_ok"          # json_ok | mcp | plain | flaky | models_down
    hits = {"health": 0}

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            _Handler.hits["health"] += 1
            if _Handler.mode == "json_ok":
                body = json.dumps({"status": "ok", "server": "llama.cpp"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif _Handler.mode == "mcp":
                body = json.dumps({"status": "ok", "server": "some mcp proxy"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif _Handler.mode == "plain":
                body = b"OK"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif _Handler.mode == "flaky":
                ok = _Handler.hits["health"] % 2 == 1
                self.send_response(200 if ok else 500)
                body = b"OK" if ok else b"err"
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        elif self.path == "/v1/models":
            if _Handler.mode == "models_down":
                self.send_response(503)
                self.end_headers()
                return
            body = b'{"data":[]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{port}"


def slot():
    return SimpleNamespace(url=url, process=None)


async def _run(mode, timeout):
    _Handler.mode = mode
    _Handler.hits["health"] = 0
    t0 = time.time()
    ok = await ModelSlot.wait_ready(slot(), timeout=timeout)
    return ok, time.time() - t0


ok, dt = asyncio.run(_run("json_ok", 10))
check("1a json health + models probe -> ready fast", ok is True and dt < 5, f" ({dt:.1f}s)")

ok, _ = asyncio.run(_run("mcp", 6))
check("1b MCP server hint -> never ready", ok is False)

ok, dt = asyncio.run(_run("plain", 12))
check("1c plain-text health -> fallback ladder, ready after 3 stable polls",
      ok is True and dt >= 2 and _Handler.hits["health"] >= 3,
      f" ({dt:.1f}s, {(_Handler.hits['health'])} health hits)")

ok, _ = asyncio.run(_run("flaky", 8))
check("1d alternating health -> fallback stays not ready", ok is False)

ok, _ = asyncio.run(_run("models_down", 6))
check("1e models probe down -> not ready", ok is False)

_dead = SimpleNamespace(url=url, process=SimpleNamespace(poll=lambda: 1))
_Handler.mode = "json_ok"
t0 = time.time()
ok = asyncio.run(ModelSlot.wait_ready(_dead, timeout=5))
check("1f dead process -> immediate False", ok is False and time.time() - t0 < 1.5)

srv.shutdown()
print(f"\nwait_ready: {passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
