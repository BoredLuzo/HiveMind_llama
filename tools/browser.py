# -*- coding: utf-8 -*-


from __future__ import annotations


import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

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
            "Playwright ist nicht installiert. Installiere: "
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


def _dispatch(args: dict) -> str:
    action = str(args.get("action", "")).strip().lower()
    page = _ensure_page()

    if action == "navigate":
        url = str(args.get("url", "")).strip()
        if not url:
            return "[browser error] action='navigate' requires 'url'"
        # S-SEC (2026-08-23/25): Scheme-Blockliste + Host-Guard (Metadata/
        # lokales Dev-Testing).
        _gerr = _guard_browser_url(url)
        if _gerr:
            return f"[browser error] {_gerr}"
        _console_msgs.clear()
        _pageerrors.clear()
        resp = page.goto(url, timeout=40000, wait_until="domcontentloaded")
        status = resp.status if resp else "?"
        return f"[browser] navigated to {url} (status {status})\n\n" + _snapshot(page)

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
            _reset_state()
        return "[browser] closed"

    return (
        f"[browser error] unknown action '{action}'. "
        "Valid: navigate, snapshot, screenshot, click, type, evaluate, console, close"
    )


def _dispatch_on_executor(args: dict) -> str:


    try:
        return _dispatch(args)
    except Exception as e:
        if not _is_thread_affinity_error(e):
            raise
        logger.warning(
            "[browser] thread-affinity error (%s: %.120s) - self-heal: "
            "state reset + retry on browser thread",
            type(e).__name__, e,
        )
        _reset_state()
        return _dispatch(args)


async def browser_tool(args: dict, workspace, workspace_lock) -> str:
    """Inline-Tool-Handler (blocking Playwright sync API, pinned to ONE thread)."""
    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(_BROWSER_EXECUTOR, _dispatch_on_executor, args or {})
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
            _reset_state()

    try:
        _BROWSER_EXECUTOR.submit(_shutdown).result(timeout=timeout)
    except Exception as e:
        logger.debug("[browser] shutdown error: %s", e)
