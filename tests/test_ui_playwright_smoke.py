"""Headless Chromium Playwright UI smoke tests for modularized HiveMind web UI.

Covers boot cleanliness across all static/js/*.js modules, tab switching,
unclipped sidebar planner effort dropdown, model profiles modal, advanced
settings drawer + Ctrl+K feature search, live SSE stream + sticky scroll,
run-journal reattach tailing, and chat switch / newChat isolation.

Run: python tests/test_ui_playwright_smoke.py
Exit 0 = all pass (or explicit skip when Playwright/Chromium is unavailable), Exit 1 = failure.
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import socketserver
import sys
import threading
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.ui_sources import ui_js_files  # noqa: E402


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _build_api_payloads() -> dict:
    models_payload = {
        "models": ["demo:7b", "coder:14b"],
        "default": "demo:7b",
        "vision_preprocessing_allowlist": [],
        "profiles": [
            {
                "name": "demo:7b",
                "source": "shipped",
                "thinking": True,
                "reasoning_mode": "effort",
                "reasoning_levels": ["none", "low", "medium", "high", "max"],
                "reasoning_effort": "medium",
                "vision": False,
                "tool_call": True,
            },
            {
                "name": "coder:14b",
                "source": "shipped",
                "thinking": True,
                "reasoning_mode": "effort",
                "reasoning_levels": ["none", "low", "medium", "high"],
                "reasoning_effort": "medium",
                "vision": False,
                "tool_call": True,
            },
        ],
    }

    settings_payload = {
        "mode": "code_duo",
        "max_iterations": 2,
        "workspace": "C:/smoke/default-ws",
        "active_preset": "",
        "constraint_mode": True,
        "smart_preload_enabled": False,
        "chat_autosave_enabled": False,
        "image_uploads_persistent": False,
        "duo_pair": "coder:14b+demo:7b",
        "duo_tool_rounds": 50,
        "duo_coder_tool_tier": "core",
        "duo_planner_model": "demo:7b",
        "duo_coder_model": "coder:14b",
        "duo_critic_model": "demo:7b",
        "duo_planner_enabled": True,
        "reasoning_effort": "auto",
        "planner_reasoning_effort": "auto",
        "agents": {
            "analyst": {"model": "demo:7b", "num_ctx": 8192, "temperature": 0.4, "num_predict": 2048},
            "refiner": {"model": "demo:7b", "num_ctx": 8192, "temperature": 0.4, "num_predict": 2048},
            "critic": {"model": "demo:7b", "num_ctx": 8192, "temperature": 0.2, "num_predict": 2048},
            "synthesizer": {"model": "demo:7b", "num_ctx": 8192, "temperature": 0.3, "num_predict": 2048},
            "direct": {"model": "demo:7b", "num_ctx": 8192, "temperature": 0.5, "num_predict": 2048},
            "judge": {"model": "demo:7b", "num_ctx": 4096, "temperature": 0.1, "num_predict": 512},
        },
        "tts": {"enabled": False},
        "stt": {"enabled": False},
    }

    schema_payload = {
        "ok": True,
        "settings_rev": 1,
        "groups": ["duo", "runtime"],
        "entries": [
            {
                "key": "duo_run_timeout_seconds",
                "group": "duo",
                "type": "int",
                "value": 900,
                "default": 900,
                "min": 60,
                "max": 3600,
                "apply_mode": "live",
                "description": "Wall-clock timeout for duo agentic runs.",
            },
            {
                "key": "duo_ubatch",
                "group": "runtime",
                "type": "int",
                "value": 512,
                "default": 512,
                "choices": [256, 512, 1024],
                "apply_mode": "next_load",
                "description": "Micro-batch size used during prompt prefill.",
            },
            {
                "key": "smart_preload_enabled",
                "group": "runtime",
                "type": "bool",
                "value": False,
                "default": False,
                "apply_mode": "live",
                "description": "Preload pipeline models ahead of stage handoffs.",
            },
        ],
    }

    chats_list = [
        {
            "id": "chat-a",
            "title": "Chat A Initial",
            "preview": "Previous chat A",
            "msg_count": 2,
            "tokens": 120,
            "bytes": 512,
            "updated_at": "2026-11-23T12:00:00",
            "interrupted": False,
        },
        {
            "id": "chat-b",
            "title": "Chat B Workspace Check",
            "preview": "Alpha from Chat B",
            "msg_count": 2,
            "tokens": 240,
            "bytes": 640,
            "updated_at": "2026-11-23T13:00:00",
            "interrupted": False,
        },
    ]

    chat_details = {
        "chat-a": {
            "id": "chat-a",
            "title": "Chat A Initial",
            "workspace": "C:/ws/chat-a",
            "rev": 1,
            "messages": [
                {"role": "user", "content": "Hello Chat A"},
                {"role": "assistant", "agent": "direct", "content": "Response from Chat A"},
            ],
        },
        "chat-b": {
            "id": "chat-b",
            "title": "Chat B Workspace Check",
            "workspace": "C:/ws/chat-b",
            "rev": 2,
            "messages": [
                {"role": "user", "content": "Open Chat B workspace"},
                {"role": "assistant", "agent": "direct", "content": "Alpha from Chat B verified."},
            ],
        },
    }

    profile_map = {
        "demo:7b": {
            "ok": True,
            "model": "demo:7b",
            "source": "shipped",
            "profile": {
                "model": "demo:7b",
                "reasoning": "on",
                "moe": False,
                "num_ctx": 16384,
                "kv_cache_type": "q4_0",
                "capabilities": {"thinking": True, "vision": False, "tool_call": True},
            },
            "gguf": {},
            "file": "demo_7b.json",
        },
        "coder:14b": {
            "ok": True,
            "model": "coder:14b",
            "source": "shipped",
            "profile": {
                "model": "coder:14b",
                "reasoning": "on",
                "moe": False,
                "num_ctx": 32768,
                "kv_cache_type": "q8_0",
                "capabilities": {"thinking": True, "vision": False, "tool_call": True},
            },
            "gguf": {},
            "file": "coder_14b.json",
        },
    }

    return {
        "models": models_payload,
        "settings": settings_payload,
        "schema": schema_payload,
        "chats_list": chats_list,
        "chat_details": chat_details,
        "profiles": profile_map,
    }


def _make_handler(
    payloads: dict,
    requested_paths: list[str],
    unexpected_routes: list[str],
    lock: threading.Lock,
):
    class SmokeHandler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args) -> None:
            return

        def _send_bytes(self, status: int, content_type: str, data: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, obj: object, status: int = 200) -> None:
            self._send_bytes(status, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"))

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length", "0") or "0")
            return self.rfile.read(length) if length > 0 else b""

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            with lock:
                requested_paths.append(path)

            if path == "/" or path == "/index.html":
                data = (ROOT / "index.html").read_bytes()
                self._send_bytes(200, "text/html; charset=utf-8", data)
                return

            if path.startswith("/static/"):
                rel = urllib.parse.unquote(path.lstrip("/"))
                target = (ROOT / rel).resolve()
                static_root = (ROOT / "static").resolve()
                if static_root not in target.parents or not target.is_file():
                    self._send_bytes(404, "text/plain; charset=utf-8", b"Not found")
                    return
                ctype, _ = mimetypes.guess_type(str(target))
                if target.suffix == ".js":
                    ctype = "application/javascript; charset=utf-8"
                elif target.suffix == ".css":
                    ctype = "text/css; charset=utf-8"
                elif not ctype:
                    ctype = "application/octet-stream"
                self._send_bytes(200, ctype, target.read_bytes())
                return

            if path == "/favicon.ico":
                self._send_bytes(204, "image/x-icon", b"")
                return

            if path == "/models":
                self._send_json(payloads["models"])
                return

            if path.startswith("/models/profile/"):
                model_name = urllib.parse.unquote(path[len("/models/profile/"):])
                prof = payloads["profiles"].get(model_name) or payloads["profiles"]["demo:7b"]
                self._send_json(prof)
                return

            if path == "/settings":
                self._send_json(payloads["settings"])
                return

            if path == "/settings/schema":
                self._send_json(payloads["schema"])
                return

            if path in ("/automap/current", "/agents/map"):
                self._send_json({
                    "assignments": {
                        "analyst": {"model": "demo:7b", "display": "demo:7b", "tags": ["thinking"], "vision": False, "thinking": True},
                        "refiner": {"model": "demo:7b", "display": "demo:7b", "tags": ["thinking"], "vision": False, "thinking": True},
                        "critic": {"model": "demo:7b", "display": "demo:7b", "tags": ["thinking"], "vision": False, "thinking": True},
                        "synthesizer": {"model": "demo:7b", "display": "demo:7b", "tags": ["thinking"], "vision": False, "thinking": True},
                        "direct": {"model": "demo:7b", "display": "demo:7b", "tags": ["thinking"], "vision": False, "thinking": True},
                        "judge": {"model": "demo:7b", "display": "demo:7b", "tags": [], "vision": False, "thinking": False},
                    }
                })
                return

            if path == "/presets":
                self._send_json({"presets": [], "active": ""})
                return

            if path == "/presets/active":
                self._send_json({"name": "", "dirty": False})
                return

            if path == "/presets/prompt-roles":
                self._send_json({"roles": ["direct", "duo_coder", "duo_planner"]})
                return

            if path == "/memory":
                self._send_json({"memories": []})
                return

            if path == "/vision/config":
                self._send_json({"vision_model": "", "mode": "auto"})
                return

            if path == "/special_agents":
                self._send_json({"intent_agent": {}, "soul_evolve_agent": {}})
                return

            if path == "/soul/status":
                self._send_json({"use_learned": False, "learned": {}})
                return

            if path == "/soul/current":
                self._send_json({"text": "Default soul prompt for smoke test."})
                return

            if path in ("/soul/history", "/soul/insights", "/soul/learned", "/soul/skills"):
                self._send_json({"items": [], "skills": [], "insights": []})
                return

            if path == "/soul/token-stats":
                self._send_json({"stats": {}, "runs": []})
                return

            if path == "/skills":
                self._send_json({"skills": []})
                return

            if path == "/chats":
                self._send_json({"chats": payloads["chats_list"]})
                return

            if path.startswith("/chats/") and path != "/chats/search":
                cid = urllib.parse.unquote(path[len("/chats/"):])
                chat = payloads["chat_details"].get(cid)
                if chat:
                    self._send_json(chat)
                else:
                    self._send_json({"error": "not found"}, status=404)
                return

            if path.startswith("/pause-state/"):
                self._send_json({"paused": False, "state": "idle"})
                return

            if path == "/automations":
                self._send_json({"automations": [], "capabilities": {"scheduler": True}})
                return

            if path == "/mcp/servers":
                self._send_json({"enabled": False, "servers": []})
                return

            if path in ("/vram/status", "/models/vram"):
                self._send_json({
                    "budget_gb": 16.0,
                    "free_gb": 12.0,
                    "models": [{"name": "demo:7b", "vram_gb": 4.0, "total_gb": 4.0, "from_lookup": True}],
                })
                return

            if path == "/vram/budget":
                self._send_json({"budget_gb": 16.0})
                return

            if path == "/vram/table":
                self._send_json({"models": {"demo:7b": {"vram_gb": 4.0}, "coder:14b": {"vram_gb": 9.0}}})
                return

            if path == "/models/available":
                self._send_json({"models": ["demo:7b", "coder:14b"]})
                return

            if path == "/model_configs/base":
                self._send_json({"models": {"demo:7b": {"num_ctx": 16384}, "coder:14b": {"num_ctx": 32768}}})
                return

            if path == "/model_configs/learned":
                self._send_json({"models": {}})
                return

            if path in ("/websearch/status", "/lsp/status", "/gateway/status", "/tts/status", "/stt/status"):
                self._send_json({"ok": True, "available": False, "running": False})
                return

            if path == "/tts/voices":
                self._send_json({"voices": ["juno", "cosette"]})
                return

            if path == "/prefetch/stats":
                self._send_json({"prefetch_lead_seconds": 8.0, "runs_logged": 0, "agents": {}})
                return

            if path == "/run/journal":
                qs = urllib.parse.parse_qs(parsed.query)
                rid = (qs.get("run_id") or [""])[0]
                if rid == "smoke-reattach-1":
                    self._send_json({
                        "active": True,
                        "run_id": "smoke-reattach-1",
                        "done": True,
                        "aborted": False,
                        "n": 3,
                        "frames": [
                            'data: {"type": "agent", "content": "direct", "model": "demo:7b", "role": "Direct"}\n\n',
                            'data: {"type": "token", "content": "Reattached frame ok"}\n\n',
                            'data: {"type": "done"}\n\n',
                        ],
                    })
                else:
                    self._send_json({"active": False})
                return

            if path.startswith("/approval/pending/"):
                self._send_json({"active": False})
                return

            if path == "/workspace/placeholder":
                self._send_json({"count": 0, "files": []})
                return

            if path == "/git/checkpoints":
                self._send_json({"ok": True, "checkpoints": []})
                return

            if path == "/vram/log":
                self._send_json({"log": ""})
                return

            if path.startswith("/model_configs/"):
                self._send_json({"ok": True, "defaults": {}, "entries": [], "models": {}})
                return

            with lock:
                unexpected_routes.append(f"GET {path}")
            self._send_json({"error": f"unmocked route: GET {path}"}, status=404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path
            _ = self._read_body()
            with lock:
                requested_paths.append(f"POST {path}")

            if path == "/stream":
                events = [
                    {"type": "run_id", "run_id": "smoke-run-1"},
                    {"type": "complexity", "complexity": "direct", "mode": "direct"},
                    {"type": "agent", "content": "direct", "model": "demo:7b", "role": "Direct"},
                    {"type": "thinking_token", "content": "Analyzing smoke prompt step by step."},
                    {"type": "tool_call", "name": "read_file", "label": "app.js"},
                    {"type": "tool_result", "ok": True, "text": "148 lines read cleanly"},
                ]
                for idx in range(28):
                    events.append({
                        "type": "token",
                        "content": f"Paragraph {idx + 1}: Modularized UI stream line to ensure vertical overflow in chat container.\n\n",
                    })
                events.append({"type": "agent_done", "content": "direct", "ok": True})
                events.append({"type": "done"})

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for ev in events:
                    line = ("data: " + json.dumps(ev) + "\n\n").encode("utf-8")
                    self.wfile.write(line)
                    self.wfile.flush()
                return

            if path == "/settings":
                self._send_json({"ok": True, "settings_rev": 2})
                return

            if path in ("/memory/clear_session", "/chats/persist", "/abort", "/workspace/placeholder/clear"):
                self._send_json({"ok": True})
                return

            with lock:
                unexpected_routes.append(f"POST {path}")
            self._send_json({"error": f"unmocked route: POST {path}"}, status=404)

        def do_PUT(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            _ = self._read_body()
            with lock:
                unexpected_routes.append(f"PUT {parsed.path}")
            self._send_json({"error": f"unmocked route: PUT {parsed.path}"}, status=404)

        def do_DELETE(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            _ = self._read_body()
            with lock:
                unexpected_routes.append(f"DELETE {parsed.path}")
            self._send_json({"error": f"unmocked route: DELETE {parsed.path}"}, status=404)

    return SmokeHandler


def main() -> int:
    require_pw = os.environ.get("HIVEMIND_REQUIRE_PLAYWRIGHT") == "1"
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        if require_pw:
            print(f"FAIL: playwright not available while HIVEMIND_REQUIRE_PLAYWRIGHT=1 ({exc})")
            return 1
        print(f"SKIP: playwright not available ({exc})")
        return 0

    payloads = _build_api_payloads()
    lock = threading.Lock()
    requested_paths: list[str] = []
    unexpected_routes: list[str] = []
    handler_cls = _make_handler(payloads, requested_paths, unexpected_routes, lock)

    server = _ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
    srv_thread.start()
    base_url = f"http://127.0.0.1:{port}"

    passed = 0
    failed = 0

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal passed, failed
        if cond:
            passed += 1
            print(f"  PASS {name}" + (f" ({detail})" if detail else ""))
        else:
            failed += 1
            print(f"  FAIL {name}" + (f" :: {detail}" if detail else ""))

    page_errors: list[str] = []
    console_errors: list[str] = []
    bad_responses: list[str] = []
    blocked_external: list[str] = []

    try:
        with sync_playwright() as p:
            browser = None
            context = None
            try:
                try:
                    browser = p.chromium.launch(headless=True, args=["--mute-audio"])
                except PlaywrightError as launch_exc:
                    err_msg = str(launch_exc)
                    if "Executable doesn't exist" in err_msg or "Please run" in err_msg:
                        if require_pw:
                            print(f"FAIL: Chromium binary missing while HIVEMIND_REQUIRE_PLAYWRIGHT=1 ({err_msg.splitlines()[0]})")
                            return 1
                        print(f"SKIP: Chromium binary missing ({err_msg.splitlines()[0]})")
                        return 0
                    raise

                context = browser.new_context(viewport={"width": 1366, "height": 900})
                page = context.new_page()

                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )
                page.on(
                    "response",
                    lambda resp: bad_responses.append(f"{resp.status} {resp.url}") if resp.status >= 400 else None,
                )

                # Keep smoke runs deterministic offline: allow localhost/data/blob,
                # return empty CSS only for Google Fonts, abort anything else.
                def _route_filter(route):
                    url = route.request.url
                    if url.startswith((base_url, "data:", "blob:")):
                        route.continue_()
                        return
                    host = urllib.parse.urlparse(url).netloc.lower()
                    if host in ("fonts.googleapis.com", "fonts.gstatic.com"):
                        route.fulfill(status=200, content_type="text/css", body="")
                        return
                    blocked_external.append(url)
                    route.abort()

                page.route("**/*", _route_filter)

                # 1. boot_clean + prototype-chain dispatch safety
                page.goto(base_url + "/", wait_until="domcontentloaded")
                page.wait_for_selector("#h-dot.on", timeout=10000)

                expected_js = ["/" + p.relative_to(ROOT).as_posix() for p in ui_js_files()]
                with lock:
                    req_snapshot = list(requested_paths)
                missing_js = [p for p in expected_js if p not in req_snapshot]
                handler_count = page.evaluate("Object.keys(window.EVENT_HANDLERS || {}).length")
                proto_ok = page.evaluate(
                    """() => {
                      handleEvent({ type: '__proto__' });
                      handleEvent({ type: 'toString' });
                      handleEvent({ type: 'constructor' });
                      handleEvent({ type: 'hasOwnProperty' });
                      return true;
                    }"""
                )
                check(
                    "1. boot_clean: #h-dot.on, all modular scripts loaded, 59 EVENT_HANDLERS & proto-safe dispatch",
                    not missing_js
                    and handler_count == 59
                    and proto_ok is True
                    and not page_errors
                    and not console_errors
                    and not bad_responses,
                    f"missing={missing_js}, handlers={handler_count}, proto={proto_ok}, errs={len(page_errors) + len(console_errors)}, bad_http={bad_responses}",
                )

                # 2. tabs_switch
                tabs = ["agents", "presets", "configs", "memory", "soul", "models", "chats", "automations"]
                bad_tabs = []
                for tab in tabs:
                    page.click(f'.tab[data-p="{tab}"]')
                    page.wait_for_function(
                        "(t) => document.getElementById('p-' + t)?.classList.contains('on')",
                        arg=tab,
                        timeout=3000,
                    )
                    is_on = page.evaluate(f"document.getElementById('p-{tab}').classList.contains('on')")
                    if not is_on:
                        bad_tabs.append(tab)
                check("2. tabs_switch: all 8 sidebar tabs activate #p-<tab>.on", not bad_tabs, f"bad={bad_tabs}")

                # 3. sidebar_effort_dropdown
                page.click('.tab[data-p="agents"]')
                has_set_mode = page.evaluate("typeof setMode === 'function' && (setMode('code_duo'), true)")
                page.wait_for_selector("#dmo-agent-btn", state="visible", timeout=3000)
                page.click("#dmo-agent-btn")
                page.wait_for_selector("#planner-effort-trigger", state="visible", timeout=3000)
                page.locator("#planner-effort-trigger").scroll_into_view_if_needed()
                page.click("#planner-effort-trigger")
                menu_geo = page.evaluate(
                    """() => {
                      const menu = document.getElementById('planner-effort-menu');
                      if (!menu || menu.hidden) return { ok: false, reason: 'hidden' };
                      const r = menu.getBoundingClientRect();
                      const clippedBy = [];
                      let p = menu.parentElement;
                      while (p && p !== document.documentElement) {
                        const cs = getComputedStyle(p);
                        const ovX = (cs.overflowX || cs.overflow || '').toLowerCase();
                        const ovY = (cs.overflowY || cs.overflow || '').toLowerCase();
                        if (['hidden', 'clip', 'auto', 'scroll'].includes(ovX) || ['hidden', 'clip', 'auto', 'scroll'].includes(ovY)) {
                          const pr = p.getBoundingClientRect();
                          if (r.top < pr.top - 1 || r.bottom > pr.bottom + 1 || r.left < pr.left - 1 || r.right > pr.right + 1) {
                            clippedBy.push((p.id || p.className || p.tagName) + ':' + ovX + '/' + ovY);
                          }
                        }
                        p = p.parentElement;
                      }
                      return {
                        ok: r.width > 80 && r.height > 40 && r.left >= 0 && r.top >= 0 && r.right <= 1366 && r.bottom <= 900 && clippedBy.length === 0,
                        rect: { l: Math.round(r.left), t: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height), b: Math.round(r.bottom) },
                        clippedBy
                      };
                    }"""
                )
                page.click("#planner-effort-trigger")
                menu_closed = page.evaluate("document.getElementById('planner-effort-menu')?.hidden === true")
                check(
                    "3. sidebar_effort_dropdown: opens unclipped inside 1366x900 viewport and closes",
                    bool(has_set_mode) and bool(menu_geo.get("ok")) and menu_closed,
                    f"setMode={has_set_mode}, geo={menu_geo}, closed={menu_closed}",
                )

                # 4. model_profiles_modal (assert API-populated form fields, not synchronous list)
                page.click('.tab[data-p="models"]')
                page.wait_for_selector("#open-model-profiles-btn", state="visible", timeout=3000)
                page.click("#open-model-profiles-btn")
                page.wait_for_selector("#model-profiles-modal", state="visible", timeout=3000)
                page.wait_for_function(
                    "() => document.getElementById('mp-num-ctx')?.value === '16384'"
                    " && document.getElementById('mp-kv-cache-type')?.value === 'q4_0'"
                    " && (document.getElementById('mp-file-badge')?.textContent || '').includes('demo_7b.json')",
                    timeout=3000,
                )
                modal_populated = page.evaluate(
                    """() => ({
                      ctx: document.getElementById('mp-num-ctx')?.value,
                      kv: document.getElementById('mp-kv-cache-type')?.value,
                      file: document.getElementById('mp-file-badge')?.textContent,
                    })"""
                )
                page.evaluate("closeModelProfilesModal()")
                modal_hidden = page.evaluate("document.getElementById('model-profiles-modal').style.display === 'none'")
                check(
                    "4. model_profiles_modal: opens, populates profile fields from API, and closes cleanly",
                    modal_populated["ctx"] == "16384"
                    and modal_populated["kv"] == "q4_0"
                    and "demo_7b.json" in (modal_populated["file"] or "")
                    and modal_hidden,
                    f"populated={modal_populated}, hidden={modal_hidden}",
                )

                # 5. adv_drawer_and_feature_search
                page.click('.tab[data-p="configs"]')
                page.wait_for_selector("#adv-engine-settings-card summary", state="visible", timeout=3000)
                page.click("#adv-engine-settings-card summary")
                page.wait_for_selector("#adv-settings-list .adv-setting-row", timeout=4000)
                rows_all = page.locator("#adv-settings-list .adv-setting-row").count()
                page.fill("#adv-settings-search", "timeout")
                rows_filtered = page.locator("#adv-settings-list .adv-setting-row").count()
                page.fill("#adv-settings-search", "")

                page.keyboard.press("Control+k")
                page.wait_for_selector("#fs-overlay", state="visible", timeout=3000)
                fs_input = page.locator("#fs-overlay input")
                fs_input.fill("effort")
                fs_rows = page.locator("#fs-overlay .fs-row").count()
                page.keyboard.press("Escape")
                fs_closed = page.evaluate("document.getElementById('fs-overlay')?.style.display === 'none'")
                check(
                    "5. adv_drawer_and_feature_search: loads/filters schema rows & Ctrl+K search opens/closes",
                    rows_all >= 3 and rows_filtered == 1 and fs_rows >= 1 and fs_closed,
                    f"rows={rows_all}->{rows_filtered}, fs_rows={fs_rows}, fs_closed={fs_closed}",
                )

                # 6. stream_and_sticky_scroll (await double rAF after scrollBtmIfNearBottom)
                page.fill("#input", "Smoke test streaming message")
                page.click("#send")
                page.wait_for_function(
                    "() => !S.streaming && document.querySelectorAll('#chat .ablock').length >= 1",
                    timeout=8000,
                )
                stream_dom = page.evaluate(
                    """() => {
                      const chat = document.getElementById('chat');
                      return {
                        hasUser: !!chat.querySelector('.msg-user'),
                        hasThink: !!chat.querySelector('.think-block'),
                        hasTc: !!chat.querySelector('.tool-call-row'),
                        hasTr: !!chat.querySelector('.tool-result-block'),
                        hasText: (chat.innerText || '').includes('Paragraph 28'),
                        overflow: chat.scrollHeight > chat.clientHeight,
                        followAtEnd: _chatFollow === true,
                      };
                    }"""
                )
                page.evaluate(
                    """() => {
                      const c = document.getElementById('chat');
                      c._userUpTs = Date.now();
                      c.scrollTop = 0;
                      c.dispatchEvent(new Event('scroll'));
                    }"""
                )
                paused_follow = page.evaluate(
                    """async () => {
                      scrollBtmIfNearBottom(120);
                      await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
                      const c = document.getElementById('chat');
                      return { follow: _chatFollow, top: c.scrollTop };
                    }"""
                )
                # Wait past 180ms userUpTs grace window in scroll.js before scrolling back to bottom
                page.wait_for_function(
                    "() => (Date.now() - (document.getElementById('chat')._userUpTs || 0)) > 200",
                    timeout=2000,
                )
                resumed_follow = page.evaluate(
                    """() => {
                      const c = document.getElementById('chat');
                      c.scrollTop = c.scrollHeight;
                      c.dispatchEvent(new Event('scroll'));
                      return _chatFollow;
                    }"""
                )
                check(
                    "6. stream_and_sticky_scroll: renders SSE frames & toggles _chatFollow cleanly",
                    all(stream_dom.values())
                    and paused_follow["follow"] is False
                    and paused_follow["top"] == 0
                    and resumed_follow is True,
                    f"dom={stream_dom}, paused={paused_follow}, resumed={resumed_follow}",
                )

                # 7. journal_reattach_banner
                page.evaluate(
                    """() => {
                      S._sseFrameSeq = 0;
                      S._sawDoneEvent = false;
                      _journalNote('Connection lost - reattaching via the run journal ...', { reattachBanner: true });
                      _journalStartTail('smoke-reattach-1');
                    }"""
                )
                page.wait_for_function(
                    "() => !S._journalTailing"
                    " && (document.getElementById('chat')?.innerText || '').includes('Reattached via run journal')"
                    " && (document.getElementById('chat')?.innerText || '').includes('Reattached frame ok')",
                    timeout=5000,
                )
                journal_ok = page.evaluate(
                    "!S._journalTailing && S._reattachBannerEl === null"
                )
                check(
                    "7. journal_reattach_banner: tails /run/journal and completes cleanly",
                    bool(journal_ok),
                    f"journal_ok={journal_ok}",
                )

                # 8. chat_switch_clean
                page.evaluate("loadChat('chat-b')")
                page.wait_for_function(
                    "() => S.currentChatId === 'chat-b' && (document.getElementById('chat')?.innerText || '').includes('Alpha from Chat B verified.')",
                    timeout=5000,
                )
                ws_val = page.evaluate("document.getElementById('workspace-input')?.value || ''")
                old_gone = page.evaluate("!(document.getElementById('chat')?.innerText || '').includes('Paragraph 28')")
                page.evaluate("newChat()")
                page.wait_for_function(
                    "() => !S.currentChatId && (document.getElementById('chat')?.textContent || '').includes('New chat started.') && document.querySelectorAll('#chat .ablock, #chat .msg-user').length === 0",
                    timeout=3000,
                )
                check(
                    "8. chat_switch_clean: switches workspace/DOM to chat-b & newChat clears state",
                    ws_val == "C:/ws/chat-b" and old_gone,
                    f"ws={ws_val!r}, old_gone={old_gone}",
                )

                with lock:
                    unmocked = list(unexpected_routes)
                check(
                    "9. zero_runtime_errors: no pageerror, console.error, HTTP >= 400, unmocked routes, or unexpected externals",
                    not page_errors
                    and not console_errors
                    and not bad_responses
                    and not unmocked
                    and not blocked_external,
                    f"page_errors={page_errors}, console_errors={console_errors}, bad_responses={bad_responses}, unmocked={unmocked}, blocked_ext={blocked_external}",
                )
            except PlaywrightError as pw_exc:
                failed += 1
                with lock:
                    unmocked = list(unexpected_routes)
                print(
                    f"  FAIL playwright_execution :: {pw_exc}\n"
                    f"    diagnostics: page_errors={page_errors}, console_errors={console_errors}, "
                    f"bad_responses={bad_responses}, unmocked={unmocked}, blocked_ext={blocked_external}"
                )
            finally:
                if context is not None:
                    context.close()
                if browser is not None:
                    browser.close()
    finally:
        server.shutdown()
        server.server_close()

    print(f"\nSUMMARY ui_playwright_smoke: {passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
