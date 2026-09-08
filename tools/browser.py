# -*- coding: utf-8 -*-


from __future__ import annotations


import asyncio
import functools
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("hivemind.browser")

_playwright = None
_browser = None
_page = None
_console_msgs: list[str] = []
_pageerrors: list[str] = []

_SNAPSHOT_MAX_CHARS = 8000

_BROWSER_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hivemind-browser")

_AFFINITY_MARKERS = ("Cannot switch to a different thread",)


def _guard_browser_url(url: str) -> str | None:


    _low = (url or "").strip().lower()
    for _bad in ("file://", "javascript:", "data:", "blob:", "vbscript:"):
        if _low.startswith(_bad):
            return f"scheme '{_bad}' is not allowed — http/https only"
    if "://" in _low and not _low.startswith(("http://", "https://")):
        return "unsupported scheme — http/https only"
    from urllib.parse import urlparse as _up
    import ipaddress as _ipa
    try:
        _host = (_up(url).hostname or "").lower()
    except Exception:
        _host = ""
    if not _host:
        return "invalid URL — no hostname"
    try:
        _ipo = _ipa.ip_address(_host)
        if isinstance(_ipo, _ipa.IPv6Address) and _ipo.ipv4_mapped:
            _ipo = _ipo.ipv4_mapped
        _cgn = _ipa.ip_network("100.64.0.0/10")
        if _ipo.version == 4 and _ipo in _cgn:
            return "CGNAT range is not navigable"
        if (_ipo.is_link_local or _ipo.is_reserved or _ipo.is_multicast):
            return ("metadata/link-local/reserved/multicast IPs are not "
                    "navigable — use web_fetch targets instead")
    except ValueError:
        pass  # normaler DNS-Hostname
    return None


class _QuietFileHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug("[browser-fileserver] " + fmt, *args)


_file_server = None
_file_server_root: str | None = None


def _plan_file_navigation(url: str, workspace) -> tuple[str, str]:
    """Map a workspace file:// URL onto the loopback file server.
    Returns (http_url, "") or ("", rejection reason)."""
    from urllib.parse import unquote as _unq, urlparse as _up
    import pathlib as _pl
    try:
        _parts = _up(url)
    except ValueError:
        return "", "unparseable file:// URL"
    if _parts.netloc not in ("", "localhost"):
        return "", "file:// network hosts are not supported — use a path inside the workspace"
    _raw = _unq(_parts.path or "")
    if not _raw:
        return "", "file:// URL has no path"
    _path = _pl.Path(_raw)
    if len(_raw) >= 3 and _raw[0] == "/" and _raw[2] == ":":
        _path = _pl.Path(_raw[1:])          # "/C:/ws/x" -> "C:/ws/x"
    try:
        _path = _path.resolve()
        _ws = _pl.Path(str(workspace or "")).resolve() if workspace else None
    except OSError:
        return "", "cannot resolve the file:// path"
    if not _ws:
        return "", ("no workspace set — file:// needs a workspace (files are "
                    "auto-served) or serve the folder yourself via run_bash "
                    "(python -m http.server <port> --directory <folder>) and "
                    "open http://localhost:<port>/")
    if not _path.is_relative_to(_ws):
        return "", ("file:// path is outside the workspace — move the file into "
                    "the workspace (it is then auto-served over loopback HTTP, "
                    "ES modules and fetch work) or serve the folder yourself via "
                    "run_bash (python -m http.server <port> --directory <folder>) "
                    "and open http://localhost:<port>/")
    if not _path.exists():
        return "", f"file not found: {_path}"
    _port = _ensure_file_server(str(_ws))
    return f"http://127.0.0.1:{_port}/{_path.relative_to(_ws).as_posix()}", ""


def _ensure_file_server(root: str) -> int:
    """(Re)start the loopback file server for the workspace root; return port."""
    global _file_server, _file_server_root
    import threading as _th
    if _file_server is not None and _file_server_root == root:
        return _file_server.server_address[1]
    _stop_file_server()
    _file_server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        functools.partial(_QuietFileHandler, directory=root),
    )
    _file_server.daemon_threads = True
    _file_server_root = root
    _th.Thread(target=_file_server.serve_forever, daemon=True,
               name="hivemind-browser-fileserver").start()
    logger.info("[browser] file server: %s -> http://127.0.0.1:%s/",
                root, _file_server.server_address[1])
    return _file_server.server_address[1]


def _stop_file_server():
    global _file_server, _file_server_root
    if _file_server is not None:
        try:
            _file_server.shutdown()
            _file_server.server_close()
        except OSError as e:
            logger.debug("[browser] file server shutdown error: %s", e)
    _file_server = None
    _file_server_root = None


def _is_thread_affinity_error(e: BaseException) -> bool:
    if "greenlet" in type(e).__name__.lower():
        return True
    return any(m in str(e) for m in _AFFINITY_MARKERS)


def _reset_state() -> None:
    global _playwright, _browser, _page
    _playwright = None
    _browser = None
    _page = None


def _ensure_page():
    global _playwright, _browser, _page, _console_msgs, _pageerrors
    if _page is not None:
        return _page
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "Playwright is not installed. Install: "
            "pip install playwright && playwright install chromium"
        )
    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(headless=True)
    _page = _browser.new_page()
    _console_msgs = []
    _pageerrors = []
    _page.on("console", lambda m: _console_msgs.append(f"[{m.type}] {m.text}"))
    _page.on("pageerror", lambda e: _pageerrors.append(str(e)))

    def _route_guard(route):
        if _guard_browser_url(route.request.url):
            route.abort()
        else:
            route.continue_()

    try:
        _page.route("**/*", _route_guard)
    except Exception:
        pass
    return _page


def _snapshot(page) -> str:
    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    out = (text or "").strip()
    if len(out) > _SNAPSHOT_MAX_CHARS:
        out = out[:_SNAPSHOT_MAX_CHARS] + "\n… [truncated]"
    if _console_msgs:
        out += "\n\n[JS-Console]\n" + "\n".join(_console_msgs[-20:])
    if _pageerrors:
        out += "\n\n[JS-Errors]\n" + "\n".join(_pageerrors[-10:])
    return out or "(empty page)"


def _dispatch(args: dict, workspace) -> str:
    action = str(args.get("action", "")).strip().lower()
    page = _ensure_page()

    if action == "navigate":
        url = str(args.get("url", "")).strip()
        if not url:
            return "[browser error] action='navigate' requires 'url'"
        _orig_url = url
        _served_via = ""
        if url.lower().startswith("file://"):
            # LOCAL-FILE-SERVE (2026-09-08): file:// inside the workspace is
            # transparently served over a loopback HTTP server, so ES modules
            # and fetch() work like on a real site. Outside -> guided reject.
            url, _ferr = _plan_file_navigation(url, workspace)
            if _ferr:
                return f"[browser error] {_ferr}"
            _served_via = url
        else:
            # S-SEC (2026-08-23/25): Scheme-Blockliste + Host-Guard (Metadata/
            # lokales Dev-Testing).
            _gerr = _guard_browser_url(url)
            if _gerr:
                return f"[browser error] {_gerr}"
        _console_msgs.clear()
        _pageerrors.clear()
        resp = page.goto(url, timeout=40000, wait_until="domcontentloaded")
        status = resp.status if resp else "?"
        _head = f"[browser] navigated to {_orig_url} (status {status})"
        if _served_via:
            _head += f"\n[local file served via {_served_via}]"
        return _head + "\n\n" + _snapshot(page)

    if action == "snapshot":
        return _snapshot(page)

    if action == "screenshot":
        path = str(args.get("path", "screenshot.png")).strip()
        path = os.path.basename(path.replace("\\", "/")) or "screenshot.png"
        full = bool(args.get("full_page", False))
        page.screenshot(path=path, full_page=full)
        return f"[browser] screenshot saved to {path}"

    if action == "click":
        selector = str(args.get("selector", "")).strip()
        if not selector:
            return "[browser error] action='click' requires 'selector'"
        page.click(selector, timeout=15000)
        return "[browser] clicked " + selector + "\n\n" + _snapshot(page)

    if action == "type":
        selector = str(args.get("selector", "")).strip()
        text = str(args.get("text", "")).strip()
        if not selector:
            return "[browser error] action='type' requires 'selector'"
        page.fill(selector, text, timeout=15000)
        return f"[browser] typed into {selector}"

    if action == "evaluate":
        js = str(args.get("js", "")).strip()
        if not js:
            return "[browser error] action='evaluate' requires 'js'"
        result = page.evaluate(js)
        return "[browser] evaluate result:\n" + str(result)

    if action == "console":
        if not _console_msgs and not _pageerrors:
            return "[browser] no console messages captured"
        out = "\n".join(_console_msgs[-30:])
        if _pageerrors:
            out += "\n[JS-Errors]\n" + "\n".join(_pageerrors[-10:])
        return out

    if action == "close":
        global _browser, _playwright, _page
        try:
            if _browser is not None:
                _browser.close()
            if _playwright is not None:
                _playwright.stop()
        finally:
            _stop_file_server()
            _reset_state()
        return "[browser] closed"

    return (
        f"[browser error] unknown action '{action}'. "
        "Valid: navigate, snapshot, screenshot, click, type, evaluate, console, close"
    )


def _dispatch_on_executor(args: dict, workspace) -> str:


    try:
        return _dispatch(args, workspace)
    except Exception as e:
        if not _is_thread_affinity_error(e):
            raise
        logger.warning(
            "[browser] thread-affinity error (%s: %.120s) - self-heal: "
            "state reset + retry on browser thread",
            type(e).__name__, e,
        )
        _reset_state()
        return _dispatch(args, workspace)


async def browser_tool(args: dict, workspace, workspace_lock) -> str:
    """Inline-Tool-Handler (blocking Playwright sync API, pinned to ONE thread)."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_BROWSER_EXECUTOR, _dispatch_on_executor, args or {}, workspace)
    except RuntimeError as e:
        return f"[browser error] {e}"
    except Exception as e:
        return f"[browser error] {type(e).__name__}: {e}"


def close_browser(timeout: float = 15.0) -> None:


    def _shutdown():
        try:
            if _browser is not None:
                _browser.close()
            if _playwright is not None:
                _playwright.stop()
        finally:
            _stop_file_server()
            _reset_state()

    try:
        _BROWSER_EXECUTOR.submit(_shutdown).result(timeout=timeout)
    except Exception as e:
        logger.debug("[browser] shutdown error: %s", e)
