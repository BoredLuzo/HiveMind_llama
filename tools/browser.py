# -*- coding: utf-8 -*-


from __future__ import annotations


import asyncio
import contextvars
import functools
import ipaddress as _ipa
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger("hivemind.browser")

_playwright = None
_browser = None
_page = None
_console_msgs: list[str] = []
_pageerrors: list[str] = []

# RUN-SCOPED blocked-navigation memory (2026-09-18): without this the model
# retries blocked targets (loopback/private hosts, file:// outside the
# workspace) with reshuffled paths until the round budget dies (live: 3
# navigates to the same blocked preview server). The second attempt on the
# same block escalates hard instead.
_blocked_navigations: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "blocked_navigations", default=None)


def _check_blocked_repeat(url: str, err: str) -> str | None:
    """First blocked attempt -> None (normal error). Second attempt on the
    same block -> escalation telling the model to stop navigating."""
    from urllib.parse import urlparse as _urlparse
    try:
        _host = (_urlparse(str(url)).hostname or str(url)).lower()
    except ValueError:
        _host = str(url)[:60]
    ident = f"{str(err)[:40]}|{_host}"
    _m = _blocked_navigations.get(None)
    if _m is None:
        _m = {}
        _blocked_navigations.set(_m)
    _m[ident] = _m.get(ident, 0) + 1
    if _m[ident] < 2:
        return None
    return (
        f"[browser error] {err} \u2014 attempt #{_m[ident]} on the SAME blocked "
        "target. STOP using the browser here: this target can never be reached "
        "(SSRF guard \u2014 only the tool's own file server origin is navigable). "
        "Do NOT retry navigation, not with other paths or hosts of the same "
        "server \u2014 continue the task without browser verification."
    )

_SNAPSHOT_MAX_CHARS = 8000

_BROWSER_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hivemind-browser")

_AFFINITY_MARKERS = ("Cannot switch to a different thread",)


_CGNAT_NETWORK = _ipa.ip_network("100.64.0.0/10")


def _check_blocked_ip(ipo, parts, *, resolved: bool = False) -> str | None:
    """Shared IP block check for both IP literals and DNS-resolved addresses."""
    if isinstance(ipo, _ipa.IPv6Address) and ipo.ipv4_mapped:
        ipo = ipo.ipv4_mapped
    if ipo.version == 4 and ipo in _CGNAT_NETWORK:
        return "CGNAT range is not navigable"
    if ipo.is_unspecified or ipo.is_link_local or ipo.is_reserved or ipo.is_multicast:
        return ("metadata/link-local/reserved/multicast IPs are not "
                "navigable — use web_fetch targets instead")
    # LOOPBACK-GUARD (2026-09-13): loopback/private ranges were reachable
    # (llama-server :8101, SearXNG :8888, the HiveMind UI). The ONLY
    # permitted loopback origin is the tool's own workspace file server.
    if ipo.is_loopback or ipo.is_private:
        _fs_port = _file_server.server_address[1] if _file_server is not None else None
        _uport = parts.port or (443 if parts.scheme == "https" else 80)
        if not (_fs_port is not None and parts.scheme == "http" and ipo.version == 4
                and ipo == _ipa.ip_address("127.0.0.1") and _uport == _fs_port):
            if resolved:
                return ("hostname resolves to loopback/private - not "
                        "navigable (only the tool's own file server)")
            return ("loopback/private IPs are not navigable (only the tool's "
                    "own file server origin)")
    return None


def _guard_browser_url(url: str) -> str | None:
    _low = (url or "").strip().lower()
    for _bad in ("file://", "javascript:", "data:", "blob:", "vbscript:"):
        if _low.startswith(_bad):
            return f"scheme '{_bad}' is not allowed — http/https only"
    if "://" in _low and not _low.startswith(("http://", "https://")):
        return "unsupported scheme — http/https only"
    from urllib.parse import urlparse as _up
    try:
        _parts = _up(url)
        _host = (_parts.hostname or "").lower()
    except ValueError:
        return "invalid URL — no hostname"
    if not _host:
        return "invalid URL — no hostname"
    _host_clean = _host.split("%", 1)[0]
    try:
        _ipo = _ipa.ip_address(_host_clean)
    except ValueError:
        # T3 (audit r2): resolve hostnames and apply the exact same IP rules
        # per resolved address to block DNS rebinding / nip.io-style tricks.
        import socket as _sock
        try:
            _infos = _sock.getaddrinfo(_host, None)
        except (OSError, UnicodeError):
            return "hostname does not resolve — refusing"
        if not _infos:
            return "hostname does not resolve — refusing"
        for _ai in _infos:
            _addr_raw = str(_ai[4][0]).split("%", 1)[0]
            try:
                _rip = _ipa.ip_address(_addr_raw)
            except ValueError:
                return "hostname resolved to unparseable address — refusing"
            if _err := _check_blocked_ip(_rip, _parts, resolved=True):
                return _err
    else:
        if _err := _check_blocked_ip(_ipo, _parts, resolved=False):
            return _err
    # DNS-LOOPBACK (2026-09-13): explicit loopback names.
    if _host.lower().strip(".") in ("localhost", "127.0.0.1", "::1", "ip6-localhost"):
        _fs_port = _file_server.server_address[1] if _file_server is not None else None
        _uport = _parts.port or (443 if _parts.scheme == "https" else 80)
        if not (_fs_port is not None and _parts.scheme == "http" and _uport == _fs_port):
            return ("localhost is not navigable (only the tool's own file server origin)")
    return None


def _browser_path_violation(raw_path: Path, root_raw: Path, root_resolved: Path) -> str | None:
    """Return a rejection reason when *raw_path* escapes *root_resolved*,
    traverses a symlink or Windows junction inside the workspace, targets an
    NTFS ADS, or matches the secret-path denylist."""
    from utils.file import _is_junction, is_secret_path

    def _has_bad_segment(rel_parts: tuple[str, ...]) -> bool:
        for part in rel_parts:
            if ":" in part:
                return True
            if sys.platform == "win32" and part != part.rstrip(". "):
                return True
        return False

    try:
        if raw_path.is_symlink() or _is_junction(raw_path):
            return "symlinks and junctions are not allowed"
    except OSError:
        return "cannot inspect path"

    rel_walk = None
    walk_base = None
    for cand_root in (root_raw, root_resolved):
        try:
            rel_walk = raw_path.relative_to(cand_root)
            walk_base = cand_root
            break
        except ValueError:
            continue
    if rel_walk is not None and walk_base is not None:
        if _has_bad_segment(rel_walk.parts):
            return "invalid path segment"
        cur = walk_base
        for part in rel_walk.parts:
            cur = cur / part
            try:
                if cur.is_symlink() or _is_junction(cur):
                    return "symlinks and junctions are not allowed"
            except OSError:
                return "cannot inspect path"

    try:
        resolved = raw_path.resolve()
    except (OSError, RuntimeError):
        return "cannot resolve path"
    if not resolved.is_relative_to(root_resolved):
        return "outside the workspace"
    rel_res = resolved.relative_to(root_resolved)
    if _has_bad_segment(rel_res.parts):
        return "invalid path segment"
    if is_secret_path(raw_path, root_raw) or is_secret_path(resolved, root_resolved):
        return "target is a protected secret file (SECRET_PATH_DENIED)"
    return None


class _QuietFileHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.debug("[browser-fileserver] " + fmt, *args)

    def list_directory(self, path):
        self.send_error(403, "Directory listing is disabled")
        return None

    def send_head(self):
        root_raw = Path(self.directory)
        try:
            root_resolved = root_raw.resolve()
        except OSError:
            self.send_error(403, "Forbidden")
            return None
        fs_path = Path(self.translate_path(self.path))
        if _browser_path_violation(fs_path, root_raw, root_resolved) is not None:
            self.send_error(403, "Forbidden")
            return None
        if fs_path.is_dir():
            for index_name in self.index_pages:
                idx_path = fs_path / index_name
                if os.path.isfile(idx_path):
                    if _browser_path_violation(idx_path, root_raw, root_resolved) is not None:
                        self.send_error(403, "Forbidden")
                        return None
                    break
        return super().send_head()


_file_server = None
_file_server_root: str | None = None


def _plan_file_navigation(url: str, workspace) -> tuple[str, str]:
    """Map a workspace file:// URL onto the loopback file server.
    Returns (http_url, "") or ("", rejection reason)."""
    from urllib.parse import unquote as _unq, urlparse as _up
    try:
        _parts = _up(url)
    except ValueError:
        return "", "unparseable file:// URL"
    if _parts.netloc not in ("", "localhost"):
        return "", "file:// network hosts are not supported — use a path inside the workspace"
    _raw = _unq(_parts.path or "")
    if not _raw or "\x00" in _raw:
        return "", "file:// URL has no path"
    _orig_path = Path(_raw)
    if len(_raw) >= 3 and _raw[0] == "/" and _raw[2] == ":":
        _orig_path = Path(_raw[1:])          # "/C:/ws/x" -> "C:/ws/x"
    if not workspace:
        return "", ("no workspace set — file:// needs a workspace (files are "
                    "auto-served) or serve the folder yourself via run_bash "
                    "(python -m http.server <port> --directory <folder>) and "
                    "open http://localhost:<port>/")
    _ws_raw = Path(str(workspace))
    try:
        _ws = _ws_raw.resolve()
        _path = _orig_path.resolve()
    except OSError:
        return "", "cannot resolve the file:// path"
    _violation = _browser_path_violation(_orig_path, _ws_raw, _ws)
    if _violation is not None:
        if _violation == "outside the workspace":
            return "", ("file:// path is outside the workspace — move the file into "
                        "the workspace (it is then auto-served over loopback HTTP, "
                        "ES modules and fetch work) or serve the folder yourself via "
                        "run_bash (python -m http.server <port> --directory <folder>) "
                        "and open http://localhost:<port>/")
        return "", _violation
    if not _path.exists():
        return "", f"file not found: {_path}"
    _port = _ensure_file_server(str(_ws))
    from urllib.parse import quote as _url_quote
    _rel_q = _url_quote(_path.relative_to(_ws).as_posix(), safe="/")
    return f"http://127.0.0.1:{_port}/{_rel_q}", ""


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


def _cleanup_failed_launch() -> None:
    global _playwright, _browser
    try:
        if _browser is not None:
            try:
                _browser.close()
            except (OSError, RuntimeError, AttributeError, ValueError, TimeoutError) as _br_err:
                logger.debug("[browser] browser.close() after failed launch: %s", _br_err)
        if _playwright is not None:
            try:
                _playwright.stop()
            except (OSError, RuntimeError, AttributeError, ValueError, TimeoutError) as _pw_err:
                logger.debug("[browser] playwright.stop() after failed launch: %s", _pw_err)
    finally:
        _reset_state()


def _retire_stalled_executor() -> None:
    """Replace the single-worker executor after a hard timeout so subsequent calls never queue behind a stuck thread."""
    global _BROWSER_EXECUTOR
    old = _BROWSER_EXECUTOR
    _BROWSER_EXECUTOR = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="hivemind-browser"
    )
    try:
        old.shutdown(wait=False, cancel_futures=True)
    except (RuntimeError, OSError) as _ex_err:
        logger.debug("[browser] stalled executor shutdown: %s", _ex_err)
    _stop_file_server()
    _reset_state()


def _apply_route_guard(route) -> None:
    """Enforce URL/SSRF guard on every intercepted browser request, failing closed."""
    try:
        blocked = _guard_browser_url(route.request.url)
    except (ValueError, OSError, TypeError, AttributeError, RuntimeError) as _guard_err:
        logger.warning("[browser] route guard failed closed: %s", _guard_err)
        blocked = "route guard error"
    if blocked:
        route.abort()
    else:
        route.continue_()


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
    try:
        _browser = _playwright.chromium.launch(headless=True)
    except Exception as _launch_err:  # browser build/launch failures
        # BROWSER-BINARIES (2026-11-02): a requirement refresh can bump
        # playwright past the installed browser build ("Executable doesn't
        # exist ... chromium_headless_shell-1248"). Try the full browser
        # first (it ships with the same build), then say exactly what to run.
        _msg = str(_launch_err)
        if "Executable doesn't exist" not in _msg:
            _cleanup_failed_launch()
            raise
        try:
            _browser = _playwright.chromium.launch(
                headless=True, channel="chromium")
        except Exception:            # the fallback failed too - explain it
            _cleanup_failed_launch()
            raise RuntimeError(
                "The Playwright browser build does not match the installed "
                "playwright package. Fix once with: "
                + ".venv\\Scripts\\python.exe -m playwright install chromium "
                + f"(original error: {_msg.splitlines()[0][:160]})"
            ) from None
    _page_ok = False
    try:
        _page = _browser.new_page()
        _console_msgs = []
        _pageerrors = []
        _page.on("console", lambda m: _console_msgs.append(f"[{m.type}] {m.text}"))
        _page.on("pageerror", lambda e: _pageerrors.append(str(e)))

        _page.route("**/*", _apply_route_guard)
        _page_ok = True
    finally:
        if not _page_ok:
            _cleanup_failed_launch()
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


def _dispatch(args: dict, workspace, workspace_lock=None) -> str:
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
                _esc = _check_blocked_repeat(_orig_url, _ferr)
                return _esc or f"[browser error] {_ferr}"
            _served_via = url
        else:
            # S-SEC (2026-08-23/25): Scheme-Blockliste + Host-Guard (Metadata/
            # lokales Dev-Testing).
            _gerr = _guard_browser_url(url)
            if _gerr:
                _esc = _check_blocked_repeat(url, _gerr)
                return _esc or f"[browser error] {_gerr}"
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
        _raw_p = str(args.get("path") or "").strip() or "screenshot.png"
        from utils.file import (
            _inline_check_workspace as _bws_check,
            _inline_resolve_path as _bws_resolve,
        )
        _ws_str = str(workspace or "").strip()
        _ws_path = Path(_ws_str) if _ws_str else Path(".")
        _out = _bws_resolve(_ws_path, _raw_p)
        if _err := _bws_check(_out, workspace_lock, "browser"):
            return _err
        try:
            _hint_path = _out.resolve().relative_to(_ws_path.resolve()).as_posix()
        except (OSError, ValueError, RuntimeError):
            _hint_path = _raw_p
        _out.parent.mkdir(parents=True, exist_ok=True)
        full = bool(args.get("full_page", False))
        page.screenshot(path=str(_out), full_page=full)
        try:
            from tools.handlers._shared import coder_vision_truth as _cvt
            _vt = _cvt()
            if _vt is False:
                return (f"[browser] screenshot saved to {_out} "
                        "(this coder runs WITHOUT vision/mmproj - you cannot view "
                        "this image. Inspect the page via browser snapshot/evaluate "
                        "text output instead.)")
            if _vt is True:
                return f"[browser] screenshot saved to {_out} (call view_image(path='{_hint_path}') to inspect it visually)"
        except Exception:
            pass
        return f"[browser] screenshot saved to {_out}"

    if action == "click":
        selector = str(args.get("selector", "")).strip()
        if not selector:
            return "[browser error] action='click' requires 'selector'"
        # CLICK-DELTA (2026-10-14, audit phase 7): a click used to re-send the
        # FULL page snapshot (up to 8k chars) per interaction. Send only the
        # CHANGED lines against the last snapshot (capped) — the model just
        # saw the rest; explicit action='snapshot' still returns everything.
        _before = ""
        try:
            _before = (page.inner_text("body") or "").strip()
        except Exception:
            _before = ""
        page.click(selector, timeout=15000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        _after = _snapshot(page)
        _after_body = _after.split("\n\n[JS-Console]")[0].split("\n\n[JS-Errors]")[0]
        _before_lines = set(_before.splitlines())
        _changed = [ln for ln in _after_body.splitlines() if ln not in _before_lines]
        _delta = "\n".join(_changed).strip()
        if not _delta:
            return (f"[browser] clicked {selector} — page content unchanged "
                    "(call action='snapshot' for the full page if needed)")
        if len(_delta) > 2000:
            _delta = _delta[:2000] + "\n… [delta truncated — action='snapshot' for full page]"
        return f"[browser] clicked {selector} — changed content:\n{_delta}" + _after[len(_after_body):]

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
        _out = "[browser] evaluate result:\n" + str(result)
        # EVALUATE-CAP (2026-10-14, audit phase 7): evaluate was the only
        # browser action with NO cap at all (only the executor's universal
        # 8k bounded it in duo mode).
        if len(_out) > _SNAPSHOT_MAX_CHARS:
            _out = _out[:_SNAPSHOT_MAX_CHARS] + "\n… [truncated]"
        return _out

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


def _dispatch_on_executor(args: dict, workspace, workspace_lock=None) -> str:


    try:
        return _dispatch(args, workspace, workspace_lock)
    except Exception as e:
        if not _is_thread_affinity_error(e):
            raise
        logger.warning(
            "[browser] thread-affinity error (%s: %.120s) - self-heal: "
            "state reset + retry on browser thread",
            type(e).__name__, e,
        )
        _reset_state()
        return _dispatch(args, workspace, workspace_lock)


async def browser_tool(args: dict, workspace, workspace_lock) -> str:
    """Inline-Tool-Handler (blocking Playwright sync API, pinned to ONE thread)."""
    loop = asyncio.get_running_loop()
    if _blocked_navigations.get(None) is None:
        _blocked_navigations.set({})
    _ctx = contextvars.copy_context()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                _BROWSER_EXECUTOR, _ctx.run, _dispatch_on_executor,
                args or {}, workspace, workspace_lock,
            ),
            timeout=65.0,
        )
    except asyncio.TimeoutError:
        _retire_stalled_executor()
        return "[browser error] browser action timed out after 65s"
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
        _retire_stalled_executor()
