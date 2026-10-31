


from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import mimetypes
import os
import re
import sys
import threading
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

# ── Sys.path-Bootstrap (MCP-Audit 2.2, gefixt 2026-08-24) ──────────────────────
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import httpx
from hive_functions.hivemind_feature.ast_tools import (
    edit_ast_file,
    find_references_report,
    get_signatures_report,
)

# ── Platform ───────────────────────────────────────────────────────────────────
IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

def _default_hivemind_url() -> str:
    """Folgt settings.json server_port (install-gesetzt), sonst 8001."""
    try:
        _cfg = json.loads(
            (Path(__file__).resolve().parent.parent / "settings.json")
            .read_text(encoding="utf-8")
        )
        _p = int(_cfg.get("server_port") or 0)
        if _p:
            return f"http://localhost:{_p}"
    except Exception:
        pass
    return "http://localhost:8001"


HIVEMIND_URL = os.environ.get("HIVEMIND_URL", _default_hivemind_url())
OLLAMA_URL   = os.environ.get("OLLAMA_URL",   "http://localhost:11434")
# ACHTUNG: llama-Backend belegt Port 8101+ (BASE_PORT in llama_config.py).
HTTP_PORT    = int(os.environ.get("MCP_HTTP_PORT", "8090"))
MCP_MAX_BODY_BYTES = int(os.environ.get("MCP_MAX_BODY_BYTES", str(2 * 1024 * 1024)))
MCP_SHARED_TOOL_ENABLED = os.environ.get("MCP_SHARED_TOOL_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
MCP_SHARED_TOOL_TIMEOUT_S = max(10, int(os.environ.get("MCP_SHARED_TOOL_TIMEOUT_S", "120")))
MCP_SHARED_TOOL_MODE = os.environ.get("MCP_SHARED_TOOL_MODE", "mcp_agent")
MCP_SHARED_TOOL_MODEL = os.environ.get("MCP_SHARED_TOOL_MODEL", "qwen3:8b")
MCP_SHARED_INCLUDE_WEBSEARCH = os.environ.get("MCP_SHARED_INCLUDE_WEBSEARCH", "1").strip().lower() not in {"0", "false", "no", "off"}
MCP_SHARED_TOOL_URL = f"{HIVEMIND_URL}/internal/tool/exec"

MCP_HTTP_BIND = os.environ.get("MCP_HTTP_BIND", "127.0.0.1").strip() or "127.0.0.1"
# HARDENING (2026-10-21): Grenzen, die vorher fehlten
MCP_MAX_HEADER_BYTES = max(4096, int(os.environ.get("MCP_MAX_HEADER_BYTES", str(32 * 1024))))
MCP_MAX_CONCURRENCY = max(1, int(os.environ.get("MCP_MAX_CONCURRENCY", "8")))
MCP_MAX_BATCH = max(1, int(os.environ.get("MCP_MAX_BATCH", "16")))
# Zeitlimit fuer einen einzelnen Request (Tool-Laufzeit). 0 = aus.
# S8 (2026-10-31 audit): applies to BOTH transports now - stdio had no
# request limit at all, so a hung call blocked the loop unabortably.
MCP_REQUEST_TIMEOUT_S = float(os.environ.get("MCP_REQUEST_TIMEOUT_S", "300") or 300)
# S13 (2026-10-31 audit): the body read had no timeout (slowloris) and the
# number of concurrent connections was unbounded - only in-flight requests
# were capped by _HTTP_SEM.
MCP_BODY_TIMEOUT_S = max(1.0, float(os.environ.get("MCP_BODY_TIMEOUT_S", "30") or 30))
MCP_MAX_CONNECTIONS = max(1, int(os.environ.get("MCP_MAX_CONNECTIONS", "32")))
_CONN_SEM = asyncio.Semaphore(MCP_MAX_CONNECTIONS)
_HTTP_SEM = asyncio.Semaphore(MCP_MAX_CONCURRENCY)
MCP_HTTP_TOKEN = os.environ.get("MCP_HTTP_TOKEN", "").strip()

# ── Phase 2 (2026-10-31): resources + bounded-output limits ──────────────────
# resources/* caps: the workspace listing and every read are bounded so a huge
# tree or a huge file cannot blow up an IDE window or the transport.
MCP_RESOURCE_MAX_BYTES = max(1024, int(os.environ.get("MCP_RESOURCE_MAX_BYTES", str(256 * 1024))))
MCP_RESOURCE_MAX_ENTRIES = max(1, int(os.environ.get("MCP_RESOURCE_MAX_ENTRIES", "200")))
MCP_RESOURCE_MAX_DEPTH = max(1, int(os.environ.get("MCP_RESOURCE_MAX_DEPTH", "8")))
# run_tests output cap (the tail is kept - failures are at the end).
MCP_TEST_OUTPUT_MAX_CHARS = max(500, int(os.environ.get("MCP_TEST_OUTPUT_MAX_CHARS", "8000")))


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ── Governance-Tiers (Tier 1.5, 2026-08-24) ───────────────────────────────────
# Phase 2 (2026-10-31): git_diff/model_status are read tier; git_commit is a
# mutating tool and sits in the write tier (MCP_ALLOW_WRITE) exactly like the
# other mutating tools; run_tests executes project code and sits in the exec
# tier (MCP_ALLOW_EXEC, stdio only).
_TIER_READ = {
    "read_file", "list_dir", "find_files", "search_code",
    "get_signatures", "find_references", "memory_get", "git_status",
    "git_diff", "model_status",
}
_TIER_WRITE = {
    "write_file", "write_file_append", "patch_file", "edit_file",
    "replace_lines", "edit_ast", "memory_set", "git_commit",
}
_TIER_EXEC = {"shell", "run_bash", "run_python", "run_tests"}
# Exec/Shell is never available over HTTP; query is opt-in via MCP_ALLOW_QUERY=1.
_HTTP_FORBIDDEN = _TIER_EXEC


def _tool_allowed(tool_name: str, transport: str) -> tuple[bool, str]:
    """Zentraler Governance-Funnel. Returns (allowed, reason)."""
    if tool_name in _HTTP_FORBIDDEN and transport == "http":
        return False, f"Tool '{tool_name}' is not available via the HTTP transport (exec only locally via stdio)."
    if tool_name in _TIER_READ:
        return True, ""
    if tool_name in _TIER_WRITE:
        if _env_flag("MCP_ALLOW_WRITE"):
            return True, ""
        return False, "Write tools are disabled (opt-in: MCP_ALLOW_WRITE=1)."
    if tool_name in _TIER_EXEC:
        if _env_flag("MCP_ALLOW_EXEC"):
            return True, ""
        return False, "Exec tools are disabled (opt-in: MCP_ALLOW_EXEC=1, stdio only)."
    if tool_name == "query":
        if _env_flag("MCP_ALLOW_QUERY"):
            return True, ""
        return False, "'query' is disabled (opt-in: MCP_ALLOW_QUERY=1)."
    return True, ""


# ── HTTP-Guards (Host-Anti-Rebinding + Bearer) ────────────────────────────────

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _host_allowed(host_header: str) -> bool:
    """Anti-DNS-Rebinding: Nur Loopback-Hostnames (+ explizite Extras).
    Leerer Host-Header → reject (HTTP/1.1 verlangt Host)."""
    h = (host_header or "").strip().lower()
    if not h:
        return False
    if h.startswith("["):
        host = h.split("]", 1)[0].lstrip("[")   # [::1]:8090 → ::1
    elif h.count(":") > 1:
        host = h
    else:
        host = h.rsplit(":", 1)[0] if ":" in h else h
    allowed = set(_LOOPBACK_HOSTS)
    for extra in os.environ.get("MCP_HTTP_EXTRA_HOSTS", "").split(","):
        extra = extra.strip().lower()
        if extra:
            allowed.add(extra)
    return host in allowed


def _auth_ok(headers: dict) -> bool:
    token = (os.environ.get("MCP_HTTP_TOKEN", "") or "").strip()
    if not token:
        return True
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(auth[7:].strip(), token)


def _cors_headers(origin: str) -> str:

    configured = [o.strip() for o in os.environ.get("MCP_HTTP_CORS_ORIGINS", "").split(",") if o.strip()]
    o = (origin or "").strip()
    if o and o in configured:
        return (
            f"Access-Control-Allow-Origin: {o}\r\n"
            "Access-Control-Allow-Methods: GET, POST, OPTIONS\r\n"
            "Access-Control-Allow-Headers: Content-Type, Authorization\r\n"
            "Vary: Origin\r\n"
        )
    return ""

ROUTER_CANDIDATES = [
    "smollm2:135m",
    "smollm:135m",
    "qwen2.5:0.5b",
    "qwen2.5:1.5b",
    "llama3.2:1b",
]
ROUTER_MODEL: str | None = None


def _default_workspace() -> Path:


    env = os.environ.get("HIVEMIND_WORKSPACE", "")
    if env:
        return Path(env)
    here = Path(__file__).parent.resolve()
    if (here / "server.py").exists():
        return here
    root = here.parent
    if root.exists():
        return root
    return here


WORKSPACE = _default_workspace()

if IS_WINDOWS:
    SAFE_SHELL_RE = re.compile(
        r"^(dir|type|echo|where|findstr|find|git|python|pip|npm|node|py|powershell|ipconfig|netstat)\b",
        re.IGNORECASE,
    )
else:
    SAFE_SHELL_RE = re.compile(
        r"^(ls|cat|echo|pwd|find|grep|rg|git|python3?|pip|npm|node|cargo|go|"
        r"uname|which|type|file|wc|head|tail|diff|sort|uniq|cut|awk|sed)\b",
        re.IGNORECASE,
    )


_REGEX_ROUTES: list[tuple[re.Pattern, str, Any]] = [
    (re.compile(r"(?:read|open|cat|type)\s+['\"]?(.+?\.[a-z]{1,6})['\"]?", re.I),
     "read_file", lambda m: {"path": m.group(1).strip()}),

    (re.compile(r"(?:list|ls|dir)\s*['\"]?([./\\\w-]*)['\"]?", re.I),
     "list_dir", lambda m: {"path": m.group(1).strip() or "."}),

    # Git
    (re.compile(r"git\s+(?:status|diff|log|show|blame)", re.I),
     "git_status", lambda m: {"cmd": m.group(0).strip()}),

    # Shell direkt
    (re.compile(r"^(?:run|exec|execute)\s+(.+)$", re.I),
     "shell", lambda m: {"cmd": m.group(1).strip()}),

    # Read memory
    (re.compile(r"(?:memory\s+get|get\s+from\s+memory)\s+['\"]?(\w+)['\"]?", re.I),
     "memory_get", lambda m: {"key": m.group(1).strip()}),

    # Code suchen
    (re.compile(r"(?:search|grep|find)\s+(?:for\s+)?['\"](.+?)['\"]", re.I),
     "search_code", lambda m: {"pattern": m.group(1)}),
]


def _regex_route(text: str) -> tuple[str, dict] | None:
    for pattern, tool, extractor in _REGEX_ROUTES:
        m = pattern.search(text.strip())
        if m:
            try:
                return tool, extractor(m)
            except Exception:
                continue
    return None


_ROUTER_PROMPT = """You are an intent classifier. Answer ONLY with one of these words:
query, shell, run_bash, read_file, write_file, write_file_append, patch_file, edit_file, replace_lines, list_dir, find_files, search_code, get_signatures, find_references, edit_ast, memory_get, memory_set, git_status, run_python, none

Input: {input}
Intent:"""

_ROUTER_CACHE: dict[str, tuple[float, str]] = {}
_ROUTER_CACHE_MAX = 256
_ROUTER_CACHE_TTL = 300.0


async def _router_model_classify(text: str) -> str | None:
    if not ROUTER_MODEL:
        return None
    key = text[:80].lower().strip()
    if key in _ROUTER_CACHE:
        _ts, _intent = _ROUTER_CACHE[key]
        if time.time() - _ts <= _ROUTER_CACHE_TTL:
            return _intent
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(f"{OLLAMA_URL}/api/generate", json={
                "model":   ROUTER_MODEL,
                "prompt":  _ROUTER_PROMPT.format(input=text[:200]),
                "stream":  False,
                "options": {"temperature": 0, "num_predict": 8},
            })
            result = r.json().get("response", "").strip().lower().split()[0]
            valid  = {
                "query", "shell", "run_bash",
                "read_file", "write_file", "write_file_append", "patch_file", "edit_file", "replace_lines",
                "list_dir", "find_files", "search_code",
                "get_signatures", "find_references", "edit_ast",
                "memory_get", "memory_set", "git_status", "run_python", "none",
            }
            intent = result if result in valid else None
            if intent and intent != "none":
                if len(_ROUTER_CACHE) >= _ROUTER_CACHE_MAX:
                    _oldest = min(_ROUTER_CACHE, key=lambda _k: _ROUTER_CACHE[_k][0])
                    _ROUTER_CACHE.pop(_oldest, None)
                _ROUTER_CACHE[key] = (time.time(), intent)
            return intent
    except Exception:
        return None


# ── Hilfsfunktionen ────────────────────────────────────────────────────────────

class ToolPathError(ValueError):
    """Pfad liegt ausserhalb des Workspace oder ist unbrauchbar."""

    def __init__(self, message: str, *, path: str = "") -> None:
        super().__init__(message)
        self.path = path


def _resolve_path(path: str) -> Path:
    """Pfad innerhalb des Workspace aufloesen - sonst ToolPathError.

    SECURITY (Audit 2026-10-21): frueher wurde ein Pfad ausserhalb des Workspace
    still auf WORKSPACE/<dateiname> umgeschrieben. Das war gefaehrlich *und*
    verwirrend: das Tool schrieb irgendwohin, ohne dass der Aufrufer es merkte.
    Jetzt bricht der Aufruf mit einem klaren Fehler ab.
    """
    raw = str(path or "").strip().strip("'\"")
    if not raw:
        raise ToolPathError("empty path", path=raw)
    if "\x00" in raw:
        raise ToolPathError("path contains a NUL byte", path=raw)
    p = Path(raw)
    if not p.is_absolute():
        p = WORKSPACE / p
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as exc:     # Symlink-Loop, kaputte Pfade
        raise ToolPathError(f"cannot resolve path: {exc}", path=raw) from exc
    try:
        resolved.relative_to(WORKSPACE.resolve())
    except ValueError:
        # Absolut ODER per .. aus dem Workspace heraus -> hart ablehnen
        raise ToolPathError(
            f"path is outside the workspace ({resolved.name} not inside {WORKSPACE.name})",
            path=raw,
        ) from None
    return resolved


def _decode(data: bytes) -> str:
    """cmd.exe gibt cp1252 aus, alles andere utf-8."""
    if IS_WINDOWS:
        try:
            return data.decode("cp1252")
        except Exception:
            pass
    return data.decode("utf-8", errors="replace")


def _tool_error_response(
    code: str,
    message: str,
    *,
    tool: str = "",
    retryable: bool = False,
    details: dict | None = None,
) -> str:
    payload: dict[str, Any] = {
        "error": {
            "code": str(code or "TOOL_ERROR"),
            "message": str(message or "Tool failed"),
            "tool": str(tool or ""),
            "retryable": bool(retryable),
        }
    }
    if isinstance(details, dict) and details:
        payload["error"]["details"] = details
    return "[TOOL_ERROR] " + json.dumps(payload, ensure_ascii=False)


_MCP_SHARED_TOOL_NAMES = {
    "shell",
    "run_bash",
    "read_file",
    "write_file",
    "write_file_append",
    "patch_file",
    "edit_file",
    "replace_lines",
    "list_dir",
    "find_files",
    "search_code",
    "get_signatures",
    "find_references",
    "edit_ast",
    "git_status",
    "run_python",
}


def _looks_like_error_text(text: str) -> bool:
    """S10 (2026-10-31 audit): the old heuristic flagged ANY text starting
    with "[" that contained "error"/"timeout"/"not found" somewhere - a
    legitimate file whose content begins with "[" (a markdown link, a log
    line) was reported as isError. Only our own error envelope counts now;
    the JSON variant is handled by the structured parser in handle_request,
    this catches the inline Format B ([TOOL_ERROR:CODE] ...)."""
    t = str(text or "").strip().lower()
    return t.startswith("[tool_error")


# ── S8 (2026-10-31 audit): notifications/cancelled ────────────────────────────
# The notification used to be swallowed with all the others. The requestId is
# registered so a request that has not started yet (pipelined on stdio, a
# later batch item) is skipped instead of executing work the client no longer
# wants. An in-flight call cannot be interrupted from here (the stdio loop is
# sequential) - the request timeout is the abort path for those.
_CANCELLED_REQUESTS: list = []
_CANCELLED_REQUESTS_CAP = 256


def _remember_cancelled(params: Any) -> None:
    rid = params.get("requestId") if isinstance(params, dict) else None
    if rid is None:
        return
    if rid in _CANCELLED_REQUESTS:
        return
    _CANCELLED_REQUESTS.append(rid)
    if len(_CANCELLED_REQUESTS) > _CANCELLED_REQUESTS_CAP:
        del _CANCELLED_REQUESTS[: len(_CANCELLED_REQUESTS) - _CANCELLED_REQUESTS_CAP]


def _pop_cancelled(req_id: Any) -> bool:
    if req_id not in _CANCELLED_REQUESTS:
        return False
    try:
        _CANCELLED_REQUESTS.remove(req_id)
    except ValueError:
        pass
    return True


# ── S14 (2026-10-31 audit): initialize now NEGOTIATES the protocol version
# instead of hardcoding "2024-11-05". TODO(compat): only advertise the 2025
# revisions once the GET stream / Mcp-Session-Id headers they expect exist -
# answering them today would promise features the transport does not have.
# A client that requires 2025-only disconnects cleanly on the baseline
# answer, per spec.
_SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05",)
_PROTOCOL_VERSION_DEFAULT = "2024-11-05"


async def _try_shared_tool_exec(tool_name: str, args: dict) -> tuple[bool, str]:
    payload = {
        "name": tool_name,
        "args": dict(args or {}),
        "tool_mode": MCP_SHARED_TOOL_MODE,
        "include_websearch": MCP_SHARED_INCLUDE_WEBSEARCH,
        "model_for_limits": str((args or {}).get("__model__") or MCP_SHARED_TOOL_MODEL),
    }
    headers = {}
    _internal_token = os.environ.get("HIVEMIND_INTERNAL_TOKEN", "").strip() or MCP_HTTP_TOKEN
    if _internal_token:
        headers["X-Hivemind-Internal-Token"] = _internal_token
    try:
        timeout = httpx.Timeout(connect=4.0, read=float(MCP_SHARED_TOOL_TIMEOUT_S), write=20.0, pool=20.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(MCP_SHARED_TOOL_URL, json=payload, headers=headers)
        if resp.status_code >= 400:
            return False, f"HTTP {resp.status_code}"
        data = resp.json() if resp.content else {}
        if not isinstance(data, dict) or "result" not in data:
            return False, "invalid shared tool response"
        return True, str(data.get("result", ""))
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:220]}"


def _audit_tool_call(tool_name: str, transport: str, *, ok: bool,
                     reason: str = "", elapsed_s: float = 0.0,
                     result_bytes: int = 0) -> None:
    """Eine Zeile pro Tool-Aufruf - bei einer IDE-Anbindung die einzige Spur."""
    try:
        print(
            f"[MCP-CALL] transport={transport} tool={tool_name or '?'} "
            f"ok={bool(ok)} elapsed={elapsed_s:.2f}s bytes={int(result_bytes)}"
            + (f" denied_reason={reason[:120]}" if reason else ""),
            file=sys.stderr,
            flush=True,
        )
    except (OSError, ValueError):
        pass


async def _dispatch_tool(tool_name: str, args: dict, local_handler=None,
                         transport: str = "stdio") -> str:
    """Zentraler Pfad fuer Tools, die nicht direkt aus tools/call kommen.

    HARDENING (2026-10-21): hier fehlte die Tier-Pruefung komplett - ein
    Write-Tool war ueber den Routing-Pfad ohne MCP_ALLOW_WRITE erreichbar.
    """
    allowed, reason = _tool_allowed(tool_name, transport)
    if not allowed:
        _audit_tool_call(tool_name, transport, ok=False, reason=reason)
        return _tool_error_response("TOOL_NOT_ALLOWED", reason, tool=tool_name)
    if tool_name in _MCP_SHARED_TOOL_NAMES:
        if not MCP_SHARED_TOOL_ENABLED:
            return _tool_error_response(
                "MCP_SHARED_DISABLED",
                "Shared tool dispatch is disabled for this MCP server instance.",
                tool=tool_name,
                retryable=False,
            )
        # ADVERTISED ALIAS (2026-10-31 handoff S4): IDEs call the documented
        # "shell"; the engine tool is run_bash. The tier check above already
        # treated it as exec, so forwarding cannot lift it into a lower tier.
        # Only the shared path aliases; the local handler keeps its name.
        _forward = "run_bash" if tool_name == "shell" else tool_name
        ok, result = await _try_shared_tool_exec(_forward, args)
        if ok:
            return result
        return _tool_error_response(
            "MCP_SHARED_UNAVAILABLE",
            f"Shared tool dispatch unavailable: {result}",
            tool=tool_name,
            retryable=True,
        )
    if local_handler is None:
        return _tool_error_response(
            "TOOL_NOT_AVAILABLE",
            f"No handler available for tool '{tool_name}'.",
            tool=tool_name,
            retryable=False,
        )
    return await local_handler(args)


# ── Tool-Implementierungen ─────────────────────────────────────────────────────

async def tool_query(query: str, mode: str = "auto", iterations: int = 2, notify=None) -> str:
    """Call the Hivemind pipeline and collect the SSE stream.

    notify: optional Callable(dict) for MCP progress notifications (stdio).
    """
    tokens = []
    _char_acc = 0
    _last_status = None
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream("POST", f"{HIVEMIND_URL}/stream", json={
                "q": query, "mode": mode, "iterations": iterations,
            }) as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    try:
                        d = json.loads(line[5:])
                    except Exception:
                        continue
                    if d.get("type") == "token" and d.get("content"):
                        tokens.append(d["content"])
                        _char_acc += len(str(d["content"]))
                    elif d.get("type") == "status" and d.get("content") and notify:
                        _status = str(d["content"])[:120]
                        if _status != _last_status:
                            _last_status = _status
                            notify({"progress": _char_acc, "status": _status})
                    elif d.get("type") == "done" and notify:
                        notify({"progress": _char_acc, "status": "done"})
    except Exception as e:
        return _tool_error_response(
            "QUERY_FAILED",
            f"Hivemind stream failed: {type(e).__name__}: {str(e)[:220]}",
            tool="query",
            retryable=True,
        )
    return "".join(tokens).strip()


async def tool_shell(cmd: str, timeout: int = 30) -> str:
    safe   = bool(SAFE_SHELL_RE.match(cmd.strip()))
    prefix = "" if safe else "⚠ Unsafe command: "
    try:
        if IS_WINDOWS:
            proc = await asyncio.create_subprocess_exec(
                "cmd.exe", "/c", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKSPACE),
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKSPACE),
            )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        result = _decode(out) + (f"\n[stderr]: {_decode(err)}" if err.strip() else "")
        return prefix + (result.strip() or "(no output)")
    except asyncio.TimeoutError:
        return _tool_error_response(
            "RUN_BASH_TIMEOUT",
            f"Command timed out after {timeout}s.",
            tool="run_bash",
            retryable=True,
        )
    except Exception as e:
        return _tool_error_response(
            "RUN_BASH_FAILED",
            f"Shell execution failed: {type(e).__name__}: {str(e)[:220]}",
            tool="run_bash",
            retryable=True,
        )


async def tool_read_file(path: str, max_kb: int = 64,
                         start_line: int | None = None,
                         end_line: int | None = None) -> str:
    try:
        p = _resolve_path(path)
        if not p.exists():
            return _tool_error_response(
                "FILE_NOT_FOUND",
                f"File not found: {p}",
                tool="read_file",
                retryable=True,
            )
        size = p.stat().st_size
        if size > max_kb * 1024:
            return _tool_error_response(
                "FILE_TOO_LARGE",
                f"File is too large: {size // 1024}KB > {max_kb}KB",
                tool="read_file",
                retryable=True,
                details={"path": str(p), "size_kb": int(size // 1024), "max_kb": int(max_kb)},
            )
        content = p.read_text(encoding="utf-8", errors="replace")
        # SCHEMA-DRIFT-FIX (2026-08-24, MCP-Audit 2.2): start_line/end_line —
        if start_line is not None or end_line is not None:
            lines = content.splitlines(keepends=True)
            total = len(lines)
            s = max(1, int(start_line or 1))
            e = min(total, int(end_line or total))
            if s > e:
                return _tool_error_response(
                    "INVALID_ARGUMENT",
                    f"start_line ({s}) > end_line ({e}) bei {total} Zeilen.",
                    tool="read_file",
                    retryable=False,
                )
            header = f"[Zeilen {s}-{e} von {total}]\n"
            return header + "".join(lines[s - 1:e])
        return content
    except Exception as e:
        return _tool_error_response(
            "FILE_READ_FAILED",
            f"Failed to read file '{path}': {type(e).__name__}: {str(e)[:220]}",
            tool="read_file",
            retryable=True,
        )


async def tool_write_file(path: str, content: str, mode: str = "write") -> str:
    try:
        p = _resolve_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append":
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
            return f"Appended: {p} (+{len(content)} chars)"
        else:
            p.write_text(content, encoding="utf-8")
            return f"Geschrieben: {p} ({len(content)} Zeichen)"
    except Exception as e:
        return _tool_error_response(
            "FILE_WRITE_FAILED",
            f"Failed to write file '{path}': {type(e).__name__}: {str(e)[:220]}",
            tool="write_file",
            retryable=True,
        )


async def tool_list_dir(path: str = ".", depth: int = 2) -> str:
    try:
        p = _resolve_path(path)
        if not p.exists():
            return _tool_error_response(
                "PATH_NOT_FOUND",
                f"Path not found: {p}",
                tool="list_dir",
                retryable=True,
            )
        lines: list[str] = []

        def _walk(d: Path, indent: int = 0):
            if indent // 2 >= depth:
                return
            try:
                items = sorted(d.iterdir())
            except PermissionError:
                return
            for item in items:
                if item.name.startswith(".") or item.name in {"__pycache__", "node_modules", ".venv", ".git"}:
                    continue
                lines.append("  " * indent + ("📁 " if item.is_dir() else "   ") + item.name)
                if item.is_dir():
                    _walk(item, indent + 2)

        _walk(p)
        return "\n".join(lines) or "(empty)"
    except Exception as e:
        return _tool_error_response(
            "LIST_DIR_FAILED",
            f"list_dir failed: {type(e).__name__}: {str(e)[:220]}",
            tool="list_dir",
            retryable=True,
        )


async def tool_search_code(pattern: str, path: str = ".", file_glob: str = "*") -> str:
    """rg.exe → findstr (Windows) → grep (Unix) → Python-Fallback."""
    p = _resolve_path(path)

    # 1. ripgrep (Windows: rg.exe, Unix: rg)
    rg_bin = "rg.exe" if IS_WINDOWS else "rg"
    try:
        proc = await asyncio.create_subprocess_exec(
            rg_bin, "--line-number", "--no-heading", "--color=never",
            f"--glob={file_glob}", pattern, str(p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        result = _decode(out).strip()
        if result:
            rows = result.split("\n")
            return "\n".join(rows[:50]) + (f"\n... ({len(rows) - 50} more)" if len(rows) > 50 else "")
        return "(no matches)"
    except FileNotFoundError:
        pass
    except Exception as e:
        return f"[rg error: {e}]"

    if IS_WINDOWS:
        try:
            glob_pat = file_glob if file_glob != "*" else "*.*"
            # ARGV-ONLY (2026-10-31 handoff S5): a quote in the pattern
            # escaped the shell quoting and injected commands.
            proc = await asyncio.create_subprocess_exec(
                "findstr", "/s", "/n", "/i", pattern, f"{p}\\{glob_pat}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(p),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
            return _decode(out).strip() or "(no matches)"
        except Exception as e:
            return f"[findstr error: {e}]"

    # 3. Unix: grep
    try:
        proc = await asyncio.create_subprocess_exec(
            "grep", "-rn", pattern, str(p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        return out.decode("utf-8", errors="replace").strip() or "(no matches)"
    except Exception as e:
        return f"[grep error: {e}]"


async def tool_find_files(pattern: str = "**/*", path: str = ".", max_results: int = 150) -> str:
    try:
        base = _resolve_path(path)
        if not base.exists():
            return _tool_error_response(
                "PATH_NOT_FOUND",
                f"Path not found: {base}",
                tool="find_files",
                retryable=True,
            )
        if not base.is_dir():
            return _tool_error_response(
                "NOT_A_DIRECTORY",
                f"Path is not a directory: {base}",
                tool="find_files",
                retryable=True,
            )
        max_results = max(1, min(2000, int(max_results or 150)))
        files = sorted([p for p in base.glob(pattern) if p.is_file()])
        if not files:
            return f"(no matches for '{pattern}' in {base})"
        lines = []
        for p in files[:max_results]:
            try:
                lines.append(str(p.relative_to(base)))
            except Exception:
                lines.append(str(p))
        if len(files) > max_results:
            lines.append(f"... ({len(files) - max_results} more)")
        return "\n".join(lines)
    except Exception as e:
        return _tool_error_response(
            "FIND_FILES_FAILED",
            f"find_files failed: {type(e).__name__}: {str(e)[:220]}",
            tool="find_files",
            retryable=True,
        )


async def tool_get_signatures(path: str, max_items: int = 400) -> str:
    try:
        p = _resolve_path(path)
        max_items = max(20, min(1200, int(max_items or 400)))
        return await asyncio.to_thread(get_signatures_report, p, max_items)
    except Exception as e:
        return _tool_error_response(
            "GET_SIGNATURES_FAILED",
            f"get_signatures failed: {type(e).__name__}: {str(e)[:220]}",
            tool="get_signatures",
            retryable=True,
        )


async def tool_find_references(symbol: str, path: str = ".", max_items: int = 160) -> str:
    try:
        base = _resolve_path(path)
        if not str(symbol or "").strip():
            return _tool_error_response(
                "INVALID_ARGUMENT",
                "find_references requires a non-empty symbol.",
                tool="find_references",
                retryable=True,
            )
        max_items = max(20, min(2000, int(max_items or 160)))
        return await asyncio.to_thread(find_references_report, base, str(symbol).strip(), max_items)
    except Exception as e:
        return _tool_error_response(
            "FIND_REFERENCES_FAILED",
            f"find_references failed: {type(e).__name__}: {str(e)[:220]}",
            tool="find_references",
            retryable=True,
        )


async def tool_edit_ast(path: str, target_type: str, target_name: str, new_code: str) -> str:
    try:
        p = _resolve_path(path)
        ok, msg = await asyncio.to_thread(edit_ast_file, p, target_type, target_name, new_code)
        if ok:
            return msg
        return _tool_error_response(
            "EDIT_AST_FAILED",
            msg,
            tool="edit_ast",
            retryable=True,
        )
    except Exception as e:
        return _tool_error_response(
            "EDIT_AST_EXCEPTION",
            f"edit_ast failed: {type(e).__name__}: {str(e)[:220]}",
            tool="edit_ast",
            retryable=True,
        )


async def tool_patch_file(path: str, old_str: str, new_str: str) -> str:
    try:
        p = _resolve_path(path)
        if not p.exists():
            return _tool_error_response(
                "FILE_NOT_FOUND",
                f"File not found: {p}",
                tool="patch_file",
                retryable=True,
            )
        text = p.read_text(encoding="utf-8", errors="replace")
        count = text.count(old_str)
        if count == 0:
            return _tool_error_response(
                "PATCH_SEARCH_NOT_FOUND",
                "old_str was not found in file.",
                tool="patch_file",
                retryable=True,
                details={"path": str(p)},
            )
        if count > 1:
            return _tool_error_response(
                "PATCH_SEARCH_AMBIGUOUS",
                f"old_str matched {count} locations; provide more unique context.",
                tool="patch_file",
                retryable=True,
                details={"path": str(p), "matches": count},
            )
        patched = text.replace(old_str, new_str, 1)
        p.write_text(patched, encoding="utf-8")
        return f"Patched: {p}"
    except Exception as e:
        return _tool_error_response(
            "PATCH_FILE_FAILED",
            f"patch_file failed: {type(e).__name__}: {str(e)[:220]}",
            tool="patch_file",
            retryable=True,
        )


async def tool_write_file_append(path: str, content: str) -> str:
    return await tool_write_file(path=path, content=content, mode="append")


async def tool_run_bash(cmd: str, timeout: int = 30) -> str:
    return await tool_shell(cmd=cmd, timeout=timeout)


async def tool_memory_get(key: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{HIVEMIND_URL}/memory")
            memories = {m["key"]: m["value"] for m in r.json().get("memories", [])}
            if key == "*":
                return "\n".join(f"{k}: {v}" for k, v in memories.items()) or "(nothing stored)"
            return memories.get(key) or f"(key '{key}' not found)"
    except Exception as e:
        return _tool_error_response(
            "MEMORY_GET_FAILED",
            f"memory_get failed: {type(e).__name__}: {str(e)[:220]}",
            tool="memory_get",
            retryable=True,
        )


async def tool_memory_set(key: str, value: str) -> str:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(f"{HIVEMIND_URL}/memory/{key}", json={"value": value})
            return f"Saved: {key} = {value[:80]}"
    except Exception as e:
        return _tool_error_response(
            "MEMORY_SET_FAILED",
            f"memory_set failed: {type(e).__name__}: {str(e)[:220]}",
            tool="memory_set",
            retryable=True,
        )


_GIT_SAFE_RE = re.compile(r"^git\s+(status|diff|log|show|blame)\b", re.IGNORECASE)
_GIT_META_CHARS = set(";|&<>\n\r`")


def _is_safe_git(cmd: str) -> bool:


    c = (cmd or "").strip()
    if not _GIT_SAFE_RE.match(c):
        return False
    if any(ch in _GIT_META_CHARS for ch in c):
        return False
    if "$(" in c or "%" in c:  # Subshell / cmd-Variablenexpansion
        return False
    return True


async def tool_git_status(cmd: str = "git status") -> str:
    if not _is_safe_git(cmd):
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "Only read-only git commands are allowed for git_status "
            "(status|diff|log|show|blame).",
            tool="git_status",
            retryable=True,
        )
    return await tool_shell(cmd)


async def tool_run_python(code: str, timeout: int = 15) -> str:


    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", encoding="utf-8", delete=False) as f:
        f.write(code)
        tmp = f.name
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(WORKSPACE),
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        result = _decode(out)
        if err.strip():
            result += f"\n[stderr]:\n{_decode(err)}"
        return result.strip() or "(no output)"
    except asyncio.TimeoutError:
        return _tool_error_response(
            "RUN_PYTHON_TIMEOUT",
            f"Python snippet timed out after {timeout}s.",
            tool="run_python",
            retryable=True,
        )
    except Exception as e:
        return _tool_error_response(
            "RUN_PYTHON_FAILED",
            f"run_python failed: {type(e).__name__}: {str(e)[:220]}",
            tool="run_python",
            retryable=True,
        )
    finally:
        Path(tmp).unlink(missing_ok=True)


# ── Phase 2 tools (2026-10-31): git_diff, git_commit, run_tests, model_status ─

async def _run_git(argv: list[str], cwd: Path, timeout: float = 60.0) -> tuple[int, str, str]:
    """One git invocation, argv-only (no shell), bounded by a timeout.

    Returns (returncode, stdout, stderr) decoded via _decode().
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        raise
    return (proc.returncode if proc.returncode is not None else -1,
            _decode(out), _decode(err))


def _git_root(workspace: str, tool: str) -> tuple[Path | None, str]:
    """The git root for a tool call: WORKSPACE, or a directory inside it.

    Returns (root, "") or (None, TOOL_ERROR envelope)."""
    raw = str(workspace or "").strip()
    try:
        base = _resolve_path(raw) if raw else WORKSPACE.resolve()
    except ToolPathError as exc:
        return None, _tool_error_response(
            "PATH_OUTSIDE_WORKSPACE", str(exc), tool=tool, retryable=False,
            details={"path": getattr(exc, "path", "")[:200]})
    if not base.is_dir():
        return None, _tool_error_response(
            "NOT_A_DIRECTORY", f"workspace is not a directory: {base}",
            tool=tool, retryable=False)
    return base, ""


_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/\-^~]{1,128}$")


def _clean_git_ref(ref: str) -> str | None:
    """A git commit-ish is data, never an option: no leading dash, no shell
    metacharacters (the argv is built without a shell, but git would still
    parse a leading '-' as a flag)."""
    r = str(ref or "").strip()
    if not r or r.startswith("-") or not _GIT_REF_RE.match(r):
        return None
    return r


async def tool_git_diff(path: str = "", ref: str = "", workspace: str = "",
                        max_chars: int = 8000) -> str:
    """Read-only git diff, argv-only, bounded output."""
    base, err_env = _git_root(workspace, "git_diff")
    if base is None:
        return err_env
    argv = ["diff", "--no-color", "--no-ext-diff"]
    ref_raw = str(ref or "").strip()
    if ref_raw:
        ref_clean = _clean_git_ref(ref_raw)
        if ref_clean is None:
            return _tool_error_response(
                "INVALID_ARGUMENT",
                "ref looks invalid (use a commit-ish like HEAD~1 or a range "
                "like main..HEAD, no leading dash).",
                tool="git_diff", retryable=False)
        argv.append(ref_clean)
    if str(path or "").strip():
        try:
            p = _resolve_path(path)
        except ToolPathError as exc:
            return _tool_error_response(
                "PATH_OUTSIDE_WORKSPACE", str(exc), tool="git_diff",
                retryable=False, details={"path": getattr(exc, "path", "")[:200]})
        try:
            rel = p.relative_to(base).as_posix()
        except ValueError:
            return _tool_error_response(
                "INVALID_ARGUMENT", f"path is outside the git root {base}",
                tool="git_diff", retryable=False)
        if rel != ".":
            argv += ["--", rel]
    try:
        cap = max(500, min(20000, int(max_chars or 8000)))
    except (TypeError, ValueError):
        cap = 8000
    try:
        code, out, errtxt = await _run_git(argv, base)
    except (asyncio.TimeoutError, OSError, ValueError) as exc:
        return _tool_error_response(
            "GIT_DIFF_FAILED",
            f"git diff failed: {type(exc).__name__}: {str(exc)[:200]}",
            tool="git_diff", retryable=True)
    if code != 0:
        return _tool_error_response(
            "GIT_DIFF_FAILED",
            f"git diff exited {code}: {(errtxt or out).strip()[:300]}",
            tool="git_diff", retryable=True, details={"exit_code": code})
    diff = out.strip()
    if not diff:
        return f"(no diff{' vs ' + ref_clean if ref_raw else ''})"
    if len(diff) > cap:
        diff = diff[:cap] + f"\n... [truncated at {cap} chars - raise max_chars if needed]"
    return diff


async def tool_git_commit(message: str, paths: list | None = None,
                          workspace: str = "") -> str:
    """Mutating: commit EXPLICITLY listed paths only.

    Runs `git add -- <paths>` + `git commit -m <message> -- <paths>`.
    A whole-index sweep (`git add -A`) is impossible by construction: the
    paths argument is required and the workspace root itself is refused.
    """
    msg = " ".join(str(message or "").split())[:500]
    if not msg:
        return _tool_error_response(
            "INVALID_ARGUMENT", "message is required.", tool="git_commit",
            retryable=False)
    raw_paths = [str(p or "").strip() for p in (paths if isinstance(paths, list) else [])
                 if str(p or "").strip()]
    if not raw_paths:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "paths is required: list the files/directories to commit "
            "explicitly (the whole index is never staged).",
            tool="git_commit", retryable=False)
    if len(raw_paths) > 50:
        return _tool_error_response(
            "INVALID_ARGUMENT", "too many paths (max 50 per commit).",
            tool="git_commit", retryable=False)
    base, err_env = _git_root(workspace, "git_commit")
    if base is None:
        return err_env
    rel_paths: list[str] = []
    missing: list[str] = []
    for raw in raw_paths:
        try:
            p = _resolve_path(raw)
        except ToolPathError as exc:
            return _tool_error_response(
                "PATH_OUTSIDE_WORKSPACE", str(exc), tool="git_commit",
                retryable=False, details={"path": getattr(exc, "path", "")[:200]})
        if p == base:
            return _tool_error_response(
                "INVALID_ARGUMENT",
                "committing the workspace root itself is not allowed - "
                "list explicit paths.",
                tool="git_commit", retryable=False)
        try:
            rel = p.relative_to(base).as_posix()
        except ValueError:
            return _tool_error_response(
                "INVALID_ARGUMENT", f"path is outside the git root {base}: {raw[:120]}",
                tool="git_commit", retryable=False)
        if p.exists():
            rel_paths.append(rel)
        else:
            missing.append(rel)
    if not rel_paths:
        return "ℹ️ git_commit: none of the listed paths exist - nothing to commit."
    code, _out, errtxt = await _run_git(["rev-parse", "--git-dir"], base)
    if code != 0:
        return _tool_error_response(
            "GIT_COMMIT_FAILED", f"{base} is not a git repository.",
            tool="git_commit", retryable=False)
    add_code, _a_out, add_err = await _run_git(["add", "--", *rel_paths], base)
    if add_code != 0:
        return _tool_error_response(
            "GIT_COMMIT_FAILED", f"git add failed: {add_err.strip()[:300]}",
            tool="git_commit", retryable=True)
    c_code, c_out, c_err = await _run_git(["commit", "-m", msg, "--", *rel_paths], base)
    if c_code != 0:
        err_low = (c_err or c_out).lower()
        if ("nothing to commit" in err_low or "no changes" in err_low
                or "nothing added to commit" in err_low):
            return "ℹ️ git_commit: no changes in the listed paths - no commit."
        return _tool_error_response(
            "GIT_COMMIT_FAILED",
            f"git commit failed: {(c_err or c_out).strip()[:300]}",
            tool="git_commit", retryable=True)
    h_code, h_out, _h_err = await _run_git(["rev-parse", "--short", "HEAD"], base)
    short = h_out.strip() if h_code == 0 else "?"
    note = f" (skipped, do not exist: {', '.join(missing[:5])})" if missing else ""
    return f"✅ Committed: [{short}] {msg} ({len(rel_paths)} path(s)){note}"


_TEST_CMD_RE = re.compile(
    r"^(python3?|py|pytest|pip|npm|npx|yarn|pnpm|node|cargo|go|dotnet|mvn|"
    r"gradle|make|ctest)\b", re.IGNORECASE)
# Parens are allowed (python -c "print(...)"), they cannot chain or
# substitute on their own; the real shell operators stay blocked.
_TEST_CMD_META = set(_GIT_META_CHARS) | set("$%")


def _is_safe_test_cmd(cmd: str) -> bool:
    """run_tests accepts a plain single test command - no chaining, no
    substitution, no redirects (same meta-class as _is_safe_git, stricter
    prefix allowlist)."""
    c = (cmd or "").strip()
    if not c or not _TEST_CMD_RE.match(c):
        return False
    return not any(ch in _TEST_CMD_META for ch in c)


def _detect_test_cmd(base: Path) -> str:
    """Auto-detect the test command from project markers (no execution)."""
    if ((base / "pyproject.toml").exists() or (base / "pytest.ini").exists()):
        return "python -m pytest -q"
    tests_dir = base / "tests"
    if tests_dir.is_dir() and any(tests_dir.glob("test_*.py")):
        return "python -m pytest -q"
    pkg = base / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            scripts = data.get("scripts") if isinstance(data, dict) else None
            if isinstance(scripts, dict) and isinstance(scripts.get("test"), str):
                return "npm test"
        except (OSError, ValueError):
            return ""
    if (base / "Cargo.toml").exists():
        return "cargo test"
    if (base / "go.mod").exists():
        return "go test ./..."
    return ""


async def tool_run_tests(cmd: str = "", path: str = ".", timeout: int = 120) -> str:
    """Exec tier: run the project's test suite (auto-detected or explicit cmd)
    with a bounded timeout and a bounded output (tail kept - failures are at
    the end)."""
    try:
        base = _resolve_path(path)
    except ToolPathError as exc:
        return _tool_error_response(
            "PATH_OUTSIDE_WORKSPACE", str(exc), tool="run_tests", retryable=False,
            details={"path": getattr(exc, "path", "")[:200]})
    if not base.is_dir():
        return _tool_error_response(
            "NOT_A_DIRECTORY", f"Path is not a directory: {base}",
            tool="run_tests", retryable=False)
    try:
        timeout = max(10, min(300, int(timeout or 120)))
    except (TypeError, ValueError):
        timeout = 120
    run_cmd = str(cmd or "").strip() or _detect_test_cmd(base)
    if not run_cmd:
        return ("ℹ️ run_tests: no test suite detected (looked for pytest, "
                "npm test, cargo test, go test). Pass an explicit 'cmd'.")
    if not _is_safe_test_cmd(run_cmd):
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "cmd must be a single plain test command (pytest / npm test / "
            "cargo test / go test / python -m pytest ...), without shell "
            "metacharacters or chaining.",
            tool="run_tests", retryable=False)
    t0 = time.monotonic()
    try:
        if IS_WINDOWS:
            proc = await asyncio.create_subprocess_exec(
                "cmd.exe", "/c", run_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(base),
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                run_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(base),
            )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        return _tool_error_response(
            "RUN_TESTS_TIMEOUT", f"Tests timed out after {timeout}s.",
            tool="run_tests", retryable=True)
    except (OSError, ValueError) as exc:
        return _tool_error_response(
            "RUN_TESTS_FAILED",
            f"Test run failed: {type(exc).__name__}: {str(exc)[:200]}",
            tool="run_tests", retryable=True)
    elapsed = time.monotonic() - t0
    output = _decode(out) + (f"\n[stderr]: {_decode(err)}" if err.strip() else "")
    output = output.strip()
    if len(output) > MCP_TEST_OUTPUT_MAX_CHARS:
        output = ("[...output truncated (kept the tail)...]\n"
                  + output[-MCP_TEST_OUTPUT_MAX_CHARS:])
    rc = proc.returncode if proc.returncode is not None else -1
    status = "passed" if rc == 0 else f"FAILED (exit {rc})"
    return (f"[TEST-RUN] cmd: {run_cmd}\nstatus: {status} ({elapsed:.1f}s)\n\n"
            + (output or "(no output)"))


async def tool_model_status() -> str:
    """Read-only snapshot: configured roles, loaded models + VRAM, ports.

    Loads NOTHING: it only reads runtime_models.json/settings.json and asks
    Ollama's /api/ps (a status endpoint), never a generate/load call.
    """
    lines = [f"engine: {HIVEMIND_URL}",
             f"ollama: {OLLAMA_URL}",
             f"mcp http: http://{MCP_HTTP_BIND}:{HTTP_PORT}/mcp"]
    try:
        reg = json.loads((Path(__file__).resolve().parent.parent
                          / "runtime_models.json").read_text(encoding="utf-8"))
        if isinstance(reg, dict):
            roles = ", ".join(f"{k}={v}" for k, v in sorted(reg.items())
                              if str(v or "").strip())
            lines.append(f"configured roles: {roles or '(none configured)'}")
    except (OSError, ValueError):
        lines.append("configured roles: (runtime_models.json not readable)")
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{OLLAMA_URL}/api/ps")
        data = resp.json()
        models = data.get("models") if isinstance(data, dict) else None
        if isinstance(models, list) and models:
            for m in models[:32]:
                if not isinstance(m, dict):
                    continue
                vram_gb = float(m.get("size_vram") or 0) / (1024 ** 3)
                total_gb = float(m.get("size") or 0) / (1024 ** 3)
                lines.append(f"loaded: {m.get('name', '?')}  "
                             f"vram={vram_gb:.2f}GB / model={total_gb:.2f}GB")
        else:
            lines.append("loaded models: (none resident)")
    except (httpx.HTTPError, OSError, ValueError):
        lines.append(f"loaded models: unknown (Ollama not reachable at {OLLAMA_URL})")
    if ROUTER_MODEL:
        lines.append(f"mcp router model: {ROUTER_MODEL}")
    return "\n".join(lines)


# ── Tool-Dispatch ──────────────────────────────────────────────────────────────

TOOL_FNS = {
    "query":       lambda a: tool_query(**a),
    "shell":       lambda a: _dispatch_tool("shell", a, lambda x: tool_shell(**x)),
    "run_bash":    lambda a: _dispatch_tool("run_bash", a, lambda x: tool_run_bash(**x)),
    "read_file":   lambda a: _dispatch_tool("read_file", a, lambda x: tool_read_file(**x)),
    "write_file":  lambda a: _dispatch_tool("write_file", a, lambda x: tool_write_file(**x)),
    "write_file_append": lambda a: _dispatch_tool("write_file_append", a, lambda x: tool_write_file_append(**x)),
    "patch_file":  lambda a: _dispatch_tool("patch_file", a, lambda x: tool_patch_file(**x)),
    "edit_file":   lambda a: _dispatch_tool("edit_file", a),
    "replace_lines": lambda a: _dispatch_tool("replace_lines", a),
    "list_dir":    lambda a: _dispatch_tool("list_dir", a, lambda x: tool_list_dir(**x)),
    "find_files":  lambda a: _dispatch_tool("find_files", a, lambda x: tool_find_files(**x)),
    "search_code": lambda a: _dispatch_tool("search_code", a, lambda x: tool_search_code(**x)),
    "get_signatures": lambda a: _dispatch_tool("get_signatures", a, lambda x: tool_get_signatures(**x)),
    "find_references": lambda a: _dispatch_tool("find_references", a, lambda x: tool_find_references(**x)),
    "edit_ast":    lambda a: _dispatch_tool("edit_ast", a, lambda x: tool_edit_ast(**x)),
    "memory_get":  lambda a: tool_memory_get(**a),
    "memory_set":  lambda a: tool_memory_set(**a),
    "git_status":  lambda a: _dispatch_tool("git_status", a, lambda x: tool_git_status(**x)),
    "run_python":  lambda a: _dispatch_tool("run_python", a, lambda x: tool_run_python(**x)),
    # Phase 2 (2026-10-31): local handlers only - these four are deliberately
    # NOT in _MCP_SHARED_TOOL_NAMES (no engine round-trip; git_commit must keep
    # its explicit-paths rule, the engine variant would sweep the index).
    "git_diff":     lambda a: _dispatch_tool("git_diff", a, lambda x: tool_git_diff(**x)),
    "git_commit":   lambda a: _dispatch_tool("git_commit", a, lambda x: tool_git_commit(**x)),
    "run_tests":    lambda a: _dispatch_tool("run_tests", a, lambda x: tool_run_tests(**x)),
    "model_status": lambda a: _dispatch_tool("model_status", a, lambda x: tool_model_status(**x)),
}

TOOL_SCHEMAS = [
    {
        "name": "query",
        "description": "Query the Hivemind multi-agent pipeline. mode='simple' for direct answers, 'pipeline' for deep analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query":      {"type": "string", "description": "Question or task"},
                "mode":       {"type": "string", "enum": ["auto", "simple", "pipeline"], "default": "auto"},
                "iterations": {"type": "integer", "default": 2},
            },
            "required": ["query"],
        },
    },
    {
        "name": "shell",
        "description": "Run a shell command. Windows: cmd.exe (dir, type, findstr, git, python, where, ...). Unsafe commands run with a warning.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd":     {"type": "string"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "run_bash",
        "description": "Alias for shell (compatibility with the Hivemind tool loop).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd":     {"type": "string"},
                "timeout": {"type": "integer", "default": 30},
            },
            "required": ["cmd"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file. Relative paths from the workspace root. Optional line ranges (1-indexed) for large files.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":       {"type": "string"},
                "max_kb":     {"type": "integer", "default": 64},
                "start_line": {"type": "integer", "description": "Start line (1-indexed, optional)"},
                "end_line":   {"type": "integer", "description": "End line (inclusive, optional)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write a file or append to it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
                "mode":    {"type": "string", "enum": ["write", "append"], "default": "write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "write_file_append",
        "description": "Append-only alias for write_file(mode='append').",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "patch_file",
        "description": "Exact single replacement (old_str -> new_str) in an existing file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "old_str": {"type": "string"},
                "new_str": {"type": "string"},
            },
            "required": ["path", "old_str", "new_str"],
        },
    },
    {
        "name": "edit_file",
        "description": "Surgical SEARCH/REPLACE edits in an existing file (same tool as the internal agent loop).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":  {"type": "string"},
                "edits": {"type": "string"},
            },
            "required": ["path", "edits"],
        },
    },
    {
        "name": "replace_lines",
        "description": "Replace a line range in a file (1-indexed, inclusive).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":        {"type": "string"},
                "start_line":  {"type": "integer"},
                "end_line":    {"type": "integer"},
                "replacement": {"type": "string"},
            },
            "required": ["path", "start_line", "end_line", "replacement"],
        },
    },
    {
        "name": "list_dir",
        "description": "Show the directory structure.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":  {"type": "string", "default": "."},
                "depth": {"type": "integer", "default": 2},
            },
        },
    },
    {
        "name": "find_files",
        "description": "Find files by glob (e.g. **/*.py).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern":     {"type": "string", "default": "**/*"},
                "path":        {"type": "string", "default": "."},
                "max_results": {"type": "integer", "default": 150},
            },
        },
    },
    {
        "name": "search_code",
        "description": "Search the codebase for patterns. Uses rg.exe if installed, else findstr (Windows) or grep.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern":   {"type": "string"},
                "path":      {"type": "string", "default": "."},
                "file_glob": {"type": "string", "default": "*"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "get_signatures",
        "description": "Compact file structure (classes/functions/methods/variables with lines).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":      {"type": "string"},
                "max_items": {"type": "integer", "default": 400},
            },
            "required": ["path"],
        },
    },
    {
        "name": "find_references",
        "description": "Lightweight symbol reference search with file+line (LSP-lite).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol":    {"type": "string"},
                "path":      {"type": "string", "default": "."},
                "max_items": {"type": "integer", "default": 160},
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "edit_ast",
        "description": "Robustly replace a Python AST node (function|class|variable) by name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":        {"type": "string"},
                "target_type": {"type": "string", "enum": ["function", "class", "variable"]},
                "target_name": {"type": "string"},
                "new_code":    {"type": "string"},
            },
            "required": ["path", "target_type", "target_name", "new_code"],
        },
    },
    {
        "name": "memory_get",
        "description": "Read a value from Hivemind long-term memory. key='*' returns all entries.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "memory_set",
        "description": "Store a value in Hivemind long-term memory.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key":   {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        },
    },
    {
        "name": "git_status",
        "description": "Git commands: git status, git diff, git log, git show, git blame.",
        "inputSchema": {
            "type": "object",
            "properties": {"cmd": {"type": "string", "default": "git status"}},
        },
    },
    {
        "name": "run_python",
        "description": "Run a Python snippet and return the output.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code":    {"type": "string"},
                "timeout": {"type": "integer", "default": 15},
            },
            "required": ["code"],
        },
    },
    {
        "name": "git_diff",
        "description": ("Read-only git diff of the workspace. Optional ref "
                        "(commit-ish like HEAD~1 or a range main..HEAD) and "
                        "path filter. Output is bounded (max_chars)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":      {"type": "string", "description": "Limit the diff to this file or directory (inside the workspace)"},
                "workspace": {"type": "string", "description": "Git root to diff (must be inside the workspace; default: workspace root)"},
                "ref":       {"type": "string", "description": "Commit-ish to diff against (default: working tree vs HEAD)"},
                "max_chars": {"type": "integer", "default": 8000, "description": "Output cap in characters (500-20000)"},
            },
        },
    },
    {
        "name": "git_commit",
        "description": ("Commit EXPLICITLY listed paths (git add -- <paths> + "
                        "git commit -- <paths>). The whole index is never "
                        "staged. Mutating: needs MCP_ALLOW_WRITE=1."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message":   {"type": "string", "description": "Commit message (first line is the subject)"},
                "paths":     {"type": "array", "items": {"type": "string"},
                              "description": "Explicit files/directories to commit (workspace-relative or absolute inside the workspace)"},
                "workspace": {"type": "string", "description": "Git root (must be inside the workspace; default: workspace root)"},
            },
            "required": ["message", "paths"],
        },
    },
    {
        "name": "run_tests",
        "description": ("Run the project's test suite (auto-detects pytest / "
                        "npm test / cargo test / go test) or an explicit cmd. "
                        "Bounded timeout and output. Exec tier: "
                        "MCP_ALLOW_EXEC=1, stdio only."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "cmd":     {"type": "string", "description": "Explicit test command (optional; auto-detected when omitted)"},
                "path":    {"type": "string", "description": "Project directory to run in (inside the workspace; default: workspace root)"},
                "timeout": {"type": "integer", "default": 120, "description": "Timeout in seconds (10-300)"},
            },
        },
    },
    {
        "name": "model_status",
        "description": ("Report configured model roles, currently loaded "
                        "models with VRAM (Ollama /api/ps) and the ports. "
                        "Read-only: loads nothing."),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# ── Tool annotations (MCP 2024-11-05, Phase 2) ────────────────────────────────
# Hints only - clients may ignore them, so existing behaviour is unchanged.
# destructiveHint is only meaningful when readOnlyHint is false; read-only
# tools always carry destructiveHint=false explicitly.

def _annotations(read_only: bool, *, destructive: bool = False,
                 idempotent: bool | None = None, open_world: bool = False) -> dict:
    if idempotent is None:
        idempotent = read_only
    return {
        "readOnlyHint": bool(read_only),
        "destructiveHint": bool(destructive) and not read_only,
        "idempotentHint": bool(idempotent),
        "openWorldHint": bool(open_world),
    }


TOOL_ANNOTATIONS = {
    "query":             _annotations(False, open_world=True),
    "shell":             _annotations(False, destructive=True, open_world=True),
    "run_bash":          _annotations(False, destructive=True, open_world=True),
    "read_file":         _annotations(True),
    "write_file":        _annotations(False, destructive=True),
    "write_file_append": _annotations(False),
    "patch_file":        _annotations(False, destructive=True),
    "edit_file":         _annotations(False, destructive=True),
    "replace_lines":     _annotations(False, destructive=True),
    "list_dir":          _annotations(True),
    "find_files":        _annotations(True),
    "search_code":       _annotations(True),
    "get_signatures":    _annotations(True),
    "find_references":   _annotations(True),
    "edit_ast":          _annotations(False, destructive=True),
    "memory_get":        _annotations(True),
    "memory_set":        _annotations(False, idempotent=True),
    "git_status":        _annotations(True),
    "run_python":        _annotations(False, destructive=True, open_world=True),
    "git_diff":          _annotations(True),
    "git_commit":        _annotations(False),
    "run_tests":         _annotations(False),
    "model_status":      _annotations(True),
}

for _tool in TOOL_SCHEMAS:
    _tool["annotations"] = dict(TOOL_ANNOTATIONS.get(_tool.get("name", ""))
                                or _annotations(False))


# ── MCP resources (Phase 2, 2026-10-31) ───────────────────────────────────────
# The workspace is the root: resources/list + resources/read expose bounded,
# text-only workspace files with strict path containment. Not the whole disk.
# Dirs that hold private or junk data are pruned; a small denylist keeps
# likely-secret files (settings.json can carry preset API keys, .env, key
# files) out of BOTH the listing and the read.

_RESOURCE_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                       ".idea", ".vscode", "dist", "build", "llama",
                       "chats", "sessions", "logs", "learning_logs"}

_RESOURCE_SECRET_RE = re.compile(
    r"(^|/)(\.env|\.env\..*|settings\.json|gateway\.toml|\.git-credentials|"
    r"\.netrc|.*\.pem|.*\.key|id_rsa.*|id_ed25519.*|id_ecdsa.*|"
    r"credentials.*|secrets?\..*)$",
    re.IGNORECASE)


class _ResourceError(Exception):
    """A resources/* failure that maps to a JSON-RPC error (code carried)."""

    def __init__(self, message: str, *, code: int = -32002) -> None:
        super().__init__(message)
        self.code = int(code)


def _is_blocked_secret(rel_posix: str) -> bool:
    return bool(_RESOURCE_SECRET_RE.search(str(rel_posix or "")))


def _guess_mime(p: Path) -> str:
    return mimetypes.guess_type(p.name)[0] or "text/plain"


def _walk_workspace_files(base: Path, max_entries: int,
                          max_depth: int) -> tuple[list[Path], bool]:
    """Files under base, breadth-first and sorted; junk/hidden dirs pruned.

    Returns (files, truncated). Never raises for unreadable directories.
    """
    out: list[Path] = []
    queue: list[tuple[Path, int]] = [(base, 0)]
    while queue and len(out) < max_entries:
        d, depth = queue.pop(0)
        try:
            entries = sorted(d.iterdir(), key=lambda x: x.name.lower())
        except OSError:
            continue
        subdirs: list[tuple[Path, int]] = []
        for item in entries:
            if item.name.startswith(".") or item.name.lower() in _RESOURCE_SKIP_DIRS:
                continue
            try:
                if item.is_dir():
                    if depth + 1 < max_depth:
                        subdirs.append((item, depth + 1))
                elif item.is_file():
                    if len(out) >= max_entries:
                        break
                    out.append(item)
            except OSError:
                continue
        queue.extend(subdirs)
    return out, len(out) >= max_entries


def _list_resources() -> list[dict]:
    try:
        root = WORKSPACE.resolve()
        files, _truncated = _walk_workspace_files(
            root, MCP_RESOURCE_MAX_ENTRIES, MCP_RESOURCE_MAX_DEPTH)
    except (OSError, ValueError):
        return []
    out: list[dict] = []
    for p in files:
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        if _is_blocked_secret(rel):
            continue
        try:
            size = int(p.stat().st_size)
        except OSError:
            size = 0
        out.append({"uri": p.as_uri(), "name": rel,
                    "mimeType": _guess_mime(p), "size": size})
    return out


RESOURCE_TEMPLATES = [
    {
        "uriTemplate": "hivemind://workspace/{path}",
        "name": "workspace-file",
        "description": ("Read a workspace file by its relative path, e.g. "
                        "hivemind://workspace/docs/mcp.md. Same containment, "
                        "denylist and size limits as resources/list."),
        "mimeType": "text/plain",
    },
]


def _read_resource(uri: str) -> dict:
    """One TextResourceContents for `uri`. Raises _ResourceError with the
    JSON-RPC code to answer (-32602 for bad input, -32002 for resource
    problems)."""
    raw = str(uri or "").strip()
    if not raw:
        raise _ResourceError("params.uri must be a non-empty string", code=-32602)
    parts = urlparse(raw)
    scheme = (parts.scheme or "").lower()
    if scheme == "file":
        # url2pathname unquotes %xx and maps "/C:/x" -> "C:\x" on Windows.
        p = Path(url2pathname(parts.path))
    elif scheme == "hivemind" and parts.netloc.lower() == "workspace":
        rel = unquote(parts.path).lstrip("/")
        if not rel:
            raise _ResourceError("empty hivemind://workspace path", code=-32602)
        p = WORKSPACE / rel
    else:
        raise _ResourceError(
            f"unsupported resource URI scheme: {scheme or '(none)'} "
            "(use file:// or hivemind://workspace/<path>)", code=-32602)
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as exc:
        raise _ResourceError(f"cannot resolve resource path: {exc}", code=-32602)
    try:
        root = WORKSPACE.resolve()
        rel = resolved.relative_to(root).as_posix()
    except ValueError:
        raise _ResourceError("resource is outside the workspace", code=-32602) from None
    except (OSError, ValueError) as exc:
        raise _ResourceError(f"cannot resolve resource path: {exc}", code=-32602)
    if not resolved.is_file():
        raise _ResourceError(f"resource not found: {raw[:200]}")
    if _is_blocked_secret(rel):
        raise _ResourceError(
            "resource is blocked as a potential secret (denylist)")
    try:
        size = resolved.stat().st_size
    except OSError as exc:
        raise _ResourceError(f"cannot stat resource: {exc}")
    if size > MCP_RESOURCE_MAX_BYTES:
        raise _ResourceError(
            f"resource too large: {size} bytes > {MCP_RESOURCE_MAX_BYTES} "
            "(MCP_RESOURCE_MAX_BYTES)")
    try:
        data = resolved.read_bytes()
    except OSError as exc:
        raise _ResourceError(f"cannot read resource: {exc}")
    if b"\x00" in data[:8192]:
        raise _ResourceError(
            "resource looks binary (NUL bytes) - text resources only")
    return {"uri": raw, "mimeType": _guess_mime(resolved),
            "text": data.decode("utf-8", errors="replace")}


# ── MCP prompts (Phase 2, 2026-10-31) ─────────────────────────────────────────
# Static templates with inert argument substitution. A prompt NEVER executes
# anything by itself - it only hands the IDE's model a task description.

PROMPT_DEFINITIONS = [
    {
        "name": "review_diff",
        "description": ("Review the current uncommitted changes (git_status "
                        "+ git_diff) and report findings by severity."),
        "arguments": [
            {"name": "ref", "description": "Optional commit-ish/range to review (default: working tree vs HEAD)", "required": False},
        ],
    },
    {
        "name": "explain_file",
        "description": "Explain one workspace file: purpose, structure, key functions and risks.",
        "arguments": [
            {"name": "path", "description": "File path inside the workspace", "required": True},
        ],
    },
    {
        "name": "fix_tests",
        "description": "Run the test suite, fix the failures with minimal edits and re-run until green.",
        "arguments": [
            {"name": "scope", "description": "Optional test scope/filter (e.g. 'tests/test_mcp_hardening.py')", "required": False},
        ],
    },
]

_PROMPT_ARG_MAX = 300


def _prompt_arg(args: dict, key: str, default: str = "") -> str:
    val = str((args or {}).get(key, "") or default)
    val = " ".join(val.split())          # flatten newlines: one text block
    return val[:_PROMPT_ARG_MAX]


class _PromptError(ValueError):
    """Bad prompts/get input (unknown name, missing required argument)."""


def _build_prompt(name: str, args: dict) -> tuple[str, str]:
    """Returns (description, prompt text). Raises _PromptError for bad input."""
    if name == "review_diff":
        ref = _prompt_arg(args, "ref", "HEAD")
        text = (
            "Review the current changes in this workspace.\n"
            "1. Run git_status (cmd: \"git status --short\") to see modified, "
            "staged and untracked files.\n"
            f"2. Run git_diff with ref '{ref}' to read the diff. If the diff is "
            "truncated, raise max_chars.\n"
            "3. Review for: correctness bugs, security problems (path "
            "traversal, injection, secret handling), missing error handling, "
            "leftover debug code, missing tests.\n"
            "4. Report findings as a numbered list ordered by severity, each "
            "with file:line, a one-sentence problem statement and a suggested "
            "fix.\n"
            "Do not modify any file - this is a review only."
        )
        return PROMPT_DEFINITIONS[0]["description"], text
    if name == "explain_file":
        path = _prompt_arg(args, "path")
        if not path:
            raise _PromptError("prompt 'explain_file' requires argument 'path'")
        text = (
            f"Explain the file {path} in this workspace.\n"
            f"1. Use get_signatures on {path} for its structure, then "
            f"read_file on the relevant line ranges (start_line/end_line for "
            "large files).\n"
            "2. Explain: what the file is for, its key classes/functions, and "
            "how it connects to the rest of the workspace (find_references / "
            "search_code where helpful).\n"
            "3. End with anything surprising, risky or worth a follow-up. "
            "Keep the whole answer under 300 words."
        )
        return PROMPT_DEFINITIONS[1]["description"], text
    if name == "fix_tests":
        scope = _prompt_arg(args, "scope")
        scope_clause = f" with scope '{scope}'" if scope else ""
        text = (
            "Run the project's tests and fix the failures.\n"
            f"1. Call run_tests{scope_clause}.\n"
            "2. For each failure: read the failing code, apply the smallest "
            "correct fix (edit_file or patch_file), then call run_tests "
            "again.\n"
            "3. Repeat until the suite passes or a failure needs a human "
            "decision.\n"
            "4. Summarize: what you changed (files + why), what still fails "
            "and why you did not touch it.\n"
            "Never weaken a test just to make it pass; if a test is wrong, "
            "say so instead."
        )
        return PROMPT_DEFINITIONS[2]["description"], text
    raise _PromptError(f"Unknown prompt: {name}")


# ── Router ─────────────────────────────────────────────────────────────────────

def _args_from_input(tool: str, text: str) -> dict:
    t = text.strip()
    if tool in ("shell", "run_bash", "git_status"):
        m = re.search(r"(?:run|exec|execute|git)\s+(.+)$", t, re.IGNORECASE)
        return {"cmd": m.group(1).strip() if m else t}
    if tool in ("read_file", "get_signatures"):
        m = re.search(r"(\S+\.\w{1,6})\s*$", t)
        return {"path": m.group(1) if m else t}
    if tool == "list_dir":
        m = re.search(r"([.\w/\\-]+)\s*$", t)
        return {"path": m.group(1).strip() if m else "."}
    if tool == "find_files":
        m = re.search(r"""['\"](.*?)['\"]""", t)
        return {"pattern": m.group(1) if m else "**/*"}
    if tool == "search_code":
        m = re.search(r"""['\"](.*?)['"]""", t)
        return {"pattern": m.group(1) if m else t}
    if tool == "find_references":
        m = re.search(r"([A-Za-z_][A-Za-z0-9_.]*)\s*$", t)
        return {"symbol": m.group(1) if m else t}
    if tool == "memory_get":
        m = re.search(r"(\w+)\s*$", t)
        return {"key": m.group(1) if m else t}
    if tool == "memory_set":
        m = re.search(r"(\w+)\s*[=:]\s*(.+)$", t)
        if m:
            return {"key": m.group(1), "value": m.group(2)}
    if tool == "run_python":
        return {"code": t}
    if tool in ("edit_file", "replace_lines"):
        return {}
    return {"query": t}


async def route(user_input: str, explicit_tool: str | None = None) -> tuple[str, dict]:
    if explicit_tool and explicit_tool in TOOL_FNS:
        return explicit_tool, {}
    match = _regex_route(user_input)
    if match:
        return match
    intent = await _router_model_classify(user_input)
    if intent and intent in TOOL_FNS:
        return intent, ({"query": user_input} if intent == "query" else _args_from_input(intent, user_input))
    return "query", {"query": user_input}


# ── MCP Protocol Handler ───────────────────────────────────────────────────────

async def handle_request(req: dict, transport: str = "stdio") -> dict | None:
    if not isinstance(req, dict):
        # S9 (2026-10-31 audit): a non-dict (e.g. a scalar batch item that
        # slipped through) used to raise AttributeError and the connection
        # closed without any answer.
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "request must be an object"}}
    method = req.get("method", "")
    req_id = req.get("id")
    if "id" not in req:
        # S9: a message without "id" is a notification - it must not be
        # answered (the server used to reply with id:null, which breaks
        # spec-strict clients). notifications/cancelled is registered on
        # the way (S8).
        if method == "notifications/cancelled":
            _remember_cancelled(req.get("params"))
        return None
    if req_id is not None and _pop_cancelled(req_id):
        # S8: the client cancelled this request before the server got to
        # it - do not execute, do not answer.
        return None

    def _parse_structured_tool_error(text: str) -> dict | None:
        if not isinstance(text, str):
            return None
        m = re.search(r"\[TOOL_ERROR\]\s*(\{.*\})", text, flags=re.S)
        if not m:
            return None
        try:
            d = json.loads(m.group(1))
        except Exception:
            return None
        if not isinstance(d, dict):
            return None
        err = d.get("error")
        if isinstance(err, dict):
            return err
        return d

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, msg):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

    if method == "initialize":
        # S14: echo the requested protocol version when we actually
        # implement it; answer with the baseline otherwise.
        _init_params = req.get("params")
        _req_ver = ""
        if isinstance(_init_params, dict):
            _req_ver = str(_init_params.get("protocolVersion", "") or "")
        return ok({
            "protocolVersion": (_req_ver if _req_ver in _SUPPORTED_PROTOCOL_VERSIONS
                                else _PROTOCOL_VERSION_DEFAULT),
            "serverInfo": {"name": "hivemind-mcp", "version": "2.0.0"},
            # Phase 2 (2026-10-31): resources + prompts advertised. Adding
            # capability keys is backward compatible - older clients ignore
            # what they do not know.
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
        })

    if method == "ping":
        return ok({})

    if method == "tools/list":
        visible = [t for t in TOOL_SCHEMAS if _tool_allowed(t.get("name", ""), transport)[0]]
        return ok({"tools": visible})

    if method == "tools/call":
        params = req.get("params", {})
        if not isinstance(params, dict):
            return err(-32602, "params must be an object")
        tool_name = params.get("name", "")
        args      = params.get("arguments", {})
        if not isinstance(args, dict):
            return err(-32602, "arguments must be an object")
        if not isinstance(tool_name, str) or not tool_name:
            return err(-32602, "tool name missing")
        if tool_name not in TOOL_FNS:
            # S9 (2026-10-31 audit): an unknown tool is Invalid Params
            # (-32602), not "Method not found" (-32601) - the tools/call
            # method itself exists.
            return err(-32602, f"Unknown tool: {tool_name}")
        allowed, reason = _tool_allowed(tool_name, transport)
        if not allowed:
            _audit_tool_call(tool_name, transport, ok=False, reason=reason)
            return err(-32001, f"TOOL_NOT_ALLOWED: {reason}")
        t0 = time.time()
        is_error = False
        err_meta = None
        _notify = None
        if tool_name == "query" and transport == "stdio":
            _notify = _make_stdio_notifier()
        try:
            if tool_name == "query":
                result = await tool_query(
                    args.get("query", ""),
                    args.get("mode", "auto"),
                    args.get("iterations", 2),
                    notify=_notify,
                )
            else:
                result = await TOOL_FNS[tool_name](args)
        except ToolPathError as e:
            is_error = True
            err_meta = {"code": "PATH_OUTSIDE_WORKSPACE",
                        "message": str(e)[:240], "tool": tool_name,
                        "path": str(getattr(e, "path", ""))[:200], "retryable": False}
            result = _tool_error_response(err_meta["code"], err_meta["message"],
                                          tool=tool_name, retryable=False,
                                          details={"path": err_meta["path"]})
        except Exception as e:
            is_error = True
            err_meta = {
                "code": "tool_exception",
                "message": f"{type(e).__name__}: {str(e)[:240]}",
                "tool": tool_name,
                "retryable": False,
            }
            result = f"[TOOL_ERROR] {json.dumps(err_meta, ensure_ascii=False)}"
        if not is_error:
            parsed = _parse_structured_tool_error(str(result))
            if parsed is not None:
                is_error = True
                err_meta = parsed
            elif _looks_like_error_text(str(result)):
                is_error = True
        _audit_tool_call(tool_name, transport, ok=not is_error,
                         reason=(err_meta or {}).get("code", "") if is_error else "",
                         elapsed_s=time.time() - t0,
                         result_bytes=len(str(result).encode("utf-8", "replace")))
        return ok({
            "content": [{"type": "text", "text": str(result)}],
            "isError": is_error,
            "_meta": {
                "tool": tool_name,
                "elapsed_s": round(time.time() - t0, 2),
                **({"tool_error": err_meta} if isinstance(err_meta, dict) else {}),
            },
        })

    if method == "resources/list":
        return ok({"resources": _list_resources()})

    if method == "resources/templates/list":
        return ok({"resourceTemplates": [dict(t) for t in RESOURCE_TEMPLATES]})

    if method == "resources/read":
        _rp = req.get("params")
        _uri = _rp.get("uri") if isinstance(_rp, dict) else None
        if not isinstance(_uri, str) or not _uri.strip():
            return err(-32602, "params.uri must be a non-empty string")
        try:
            _contents = _read_resource(_uri)
        except _ResourceError as e:
            return err(e.code, str(e))
        return ok({"contents": [_contents]})

    if method == "prompts/list":
        return ok({"prompts": [dict(p) for p in PROMPT_DEFINITIONS]})

    if method == "prompts/get":
        _pp = req.get("params")
        _pname = _pp.get("name") if isinstance(_pp, dict) else None
        if not isinstance(_pname, str) or not _pname.strip():
            return err(-32602, "params.name must be a non-empty string")
        _pargs = _pp.get("arguments", {}) if isinstance(_pp, dict) else {}
        if not isinstance(_pargs, dict):
            return err(-32602, "params.arguments must be an object")
        try:
            _pdesc, _ptext = _build_prompt(_pname, _pargs)
        except _PromptError as e:
            return err(-32602, str(e))
        return ok({
            "description": _pdesc,
            "messages": [{"role": "user",
                          "content": {"type": "text", "text": _ptext}}],
        })

    if method.startswith("notifications/"):
        return None

    return err(-32601, f"Unknown method: {method}")


# ── Startup ────────────────────────────────────────────────────────────────────

async def _find_router_model():
    global ROUTER_MODEL
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            available = {m["name"] for m in r.json().get("models", [])}
            for candidate in ROUTER_CANDIDATES:
                cand_base = candidate.split(":")[0]
                cand_tag  = candidate.split(":")[1] if ":" in candidate else ""
                for a in available:
                    a_base = a.split(":")[0]
                    if a_base == cand_base and (not cand_tag or cand_tag in a):
                        ROUTER_MODEL = a
                        print(f"[MCP] Router model: {ROUTER_MODEL}", file=sys.stderr)
                        return
    except Exception as e:
        print(f"[MCP] Ollama unreachable, router disabled: {e}", file=sys.stderr)
    print("[MCP] No router model - only regex routing active", file=sys.stderr)


# ── stdio Transport (Windows-kompatibel) ───────────────────────────────────────

def _make_stdio_notifier():
    """Progress notifier for stdio (server -> client notifications)."""
    lock = threading.Lock()

    def _notify(params: dict):
        # HARDENING: Progress und Antworten teilen sich stdout - ohne Lock
        # koennen zwei Schreiber eine Zeile vermischen und der Client bricht ab.
        try:
            _out = (json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/progress", "params": params},
                ensure_ascii=False,
            ) + "\n").encode("utf-8")
            with lock:
                sys.stdout.buffer.write(_out)
                sys.stdout.buffer.flush()
        except (OSError, ValueError) as exc:
            print(f"[MCP-stdio] notify failed: {exc}", file=sys.stderr)
    return _notify


async def _stdio_loop():


    loop = asyncio.get_event_loop()

    def _readline() -> bytes:
        return sys.stdin.buffer.readline()

    def _writeline(data: bytes):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    buf = ""
    # S12 (2026-10-31 audit): the 512 KB line cap rejected big write_file
    # payloads that the HTTP transport (MCP_MAX_BODY_BYTES, 2 MB) accepts -
    # the same IDE call failed cryptically over stdio and went through over
    # HTTP. Both transports now share the same limit.
    _buf_max = MCP_MAX_BODY_BYTES
    while True:
        try:
            line = await loop.run_in_executor(None, _readline)
            if not line:
                break
            text = line.decode("utf-8", errors="replace")
            if len(text) > _buf_max:
                # HARDENING: eine zu lange Zeile wird verworfen und beantwortet -
                # frueher wurde der Puffer komplett resettet (Request verloren).
                print(f"[MCP-stdio] request line too long ({len(text)} bytes) - dropped",
                      file=sys.stderr)
                _bad = json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600, "message": f"request too large (max {_buf_max} bytes)"}})
                await loop.run_in_executor(None, _writeline,
                                           (_bad + "\n").encode("utf-8"))
                buf = ""
                continue
            buf += text
            try:
                req = json.loads(buf.strip())
                buf = ""
            except json.JSONDecodeError:
                continue
            try:
                if MCP_REQUEST_TIMEOUT_S > 0:
                    # S8: stdio had no request timeout - a hung call blocked
                    # the sequential loop forever. The client can also
                    # cancel via notifications/cancelled (registry above).
                    response = await asyncio.wait_for(handle_request(req),
                                                      timeout=MCP_REQUEST_TIMEOUT_S)
                else:
                    response = await handle_request(req)
            except asyncio.TimeoutError:
                _t_rid = req.get("id") if isinstance(req, dict) else None
                _t_name = ""
                if (isinstance(req, dict) and req.get("method") == "tools/call"
                        and isinstance(req.get("params"), dict)):
                    _t_name = str(req["params"].get("name", "") or "")
                _audit_tool_call(_t_name, "stdio", ok=False,
                                 reason="request_timeout",
                                 elapsed_s=MCP_REQUEST_TIMEOUT_S)
                response = {"jsonrpc": "2.0", "id": _t_rid,
                            "error": {"code": -32000,
                                      "message": f"request timed out after "
                                                 f"{MCP_REQUEST_TIMEOUT_S:.0f}s"}}
            if response is not None:
                out = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                await loop.run_in_executor(None, _writeline, out)
        except Exception as e:
            print(f"[MCP-stdio] Error: {e}", file=sys.stderr)
            buf = ""


# ── HTTP Transport ─────────────────────────────────────────────────────────────

async def _handle_rpc_item(item: Any) -> dict | None:
    """One JSON-RPC message of an HTTP request (single or batch item).

    S9 (2026-10-31 audit): a non-dict item ([1, 2] in a batch) used to raise
    AttributeError inside the timeout handler and the connection closed
    without any answer. It now gets a clean -32600 response.
    """
    if not isinstance(item, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "request must be an object"}}
    try:
        if MCP_REQUEST_TIMEOUT_S > 0:
            return await asyncio.wait_for(handle_request(item, "http"),
                                          timeout=MCP_REQUEST_TIMEOUT_S)
        return await handle_request(item, "http")
    except asyncio.TimeoutError:
        rid = item.get("id")
        # S7 (2026-10-31 audit): the in-request [MCP-CALL] line never fires
        # because handle_request is cancelled before it writes its own audit
        # entry - the long calls, exactly the ones worth a record, went
        # unlogged.
        if item.get("method") == "tools/call":
            _p = item.get("params")
            _t_name = str(_p.get("name", "") or "") if isinstance(_p, dict) else ""
            _audit_tool_call(_t_name, "http", ok=False, reason="request_timeout",
                             elapsed_s=MCP_REQUEST_TIMEOUT_S)
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32000,
                          "message": f"request timed out after {MCP_REQUEST_TIMEOUT_S:.0f}s"}}


async def _http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """S13 (2026-10-31 audit): caps the number of open connections before any
    byte is read. _HTTP_SEM only bounds in-flight requests - unboundedly many
    (half-)open sockets used to be possible."""
    if _CONN_SEM.locked():
        try:
            writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
        return
    await _CONN_SEM.acquire()
    try:
        await _http_handle_connection(reader, writer)
    finally:
        _CONN_SEM.release()


async def _http_handle_connection(reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter):
    try:
        raw = b""
        try:
            while b"\r\n\r\n" not in raw:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
                if not chunk:
                    break
                raw += chunk
                if len(raw) > MCP_MAX_HEADER_BYTES:
                    print(f"[MCP-HTTP] header block too large ({len(raw)} bytes) - closing",
                          file=sys.stderr)
                    writer.write(b"HTTP/1.1 431 Request Header Fields Too Large\r\n"
                                 b"Content-Length: 0\r\n\r\n")
                    await writer.drain()
                    writer.close()
                    return
        except asyncio.TimeoutError:
            writer.close()
            return

        try:
            header_part, _, body_part = raw.partition(b"\r\n\r\n")
            first_line, *rest = header_part.decode(errors="replace").split("\r\n")
            method, path, _ = first_line.split(" ", 2)
            headers = {}
            for l in rest:
                if ":" in l:
                    k, v = l.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
        except Exception:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        path = path.split("?")[0]

        # ── Tier 0.1 Hardening (2026-08-24) ───────────────────────────────
        if not _host_allowed(headers.get("host", "")):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain(); writer.close(); return
        if not _auth_ok(headers):
            body = json.dumps({"error": "unauthorized",
                               "hint": "Authorization: Bearer <MCP_HTTP_TOKEN> required"}).encode()
            writer.write(
                f"HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Bearer\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain(); writer.close(); return
        cors = _cors_headers(headers.get("origin", ""))

        if method == "OPTIONS":
            writer.write(f"HTTP/1.1 204 No Content\r\n{cors}Content-Length: 0\r\n\r\n".encode())
            await writer.drain(); writer.close(); return

        if method == "GET" and path in ("/mcp/health", "/health"):
            body = json.dumps({
                "status": "ok", "server": "hivemind-mcp", "version": "2.0.0",
                "platform": "windows" if IS_WINDOWS else "unix",
            }).encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\n{cors}Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n".encode() + body
            )
            await writer.drain(); writer.close(); return

        if method == "POST" and path == "/mcp":
            try:
                content_length = int(headers.get("content-length", len(body_part)) or 0)
            except (TypeError, ValueError):
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain(); writer.close(); return
            if content_length < 0 or content_length > MCP_MAX_BODY_BYTES:
                resp_body = json.dumps({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32000,
                        "message": f"Request body too large (max {MCP_MAX_BODY_BYTES} bytes)",
                    },
                }).encode()
                writer.write(
                    f"HTTP/1.1 413 Payload Too Large\r\n{cors}Content-Type: application/json\r\n"
                    f"Content-Length: {len(resp_body)}\r\n\r\n".encode() + resp_body
                )
                await writer.drain(); writer.close(); return
            body_bytes = body_part
            if len(body_bytes) > MCP_MAX_BODY_BYTES:
                writer.write(f"HTTP/1.1 413 Payload Too Large\r\n{cors}Content-Length: 0\r\n\r\n".encode())
                await writer.drain(); writer.close(); return
            while len(body_bytes) < content_length:
                # S13: every body read has a timeout - a client that opens
                # the connection but never finishes the body used to hold
                # the slot forever.
                try:
                    more = await asyncio.wait_for(
                        reader.read(content_length - len(body_bytes)),
                        timeout=MCP_BODY_TIMEOUT_S)
                except asyncio.TimeoutError:
                    writer.write(b"HTTP/1.1 408 Request Timeout\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain(); writer.close(); return
                if not more:
                    break
                body_bytes += more
                if len(body_bytes) > MCP_MAX_BODY_BYTES:
                    writer.write(f"HTTP/1.1 413 Payload Too Large\r\n{cors}Content-Length: 0\r\n\r\n".encode())
                    await writer.drain(); writer.close(); return

            try:
                req = json.loads(body_bytes.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                resp_body = json.dumps({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "Parse error"}}).encode()
                writer.write(
                    f"HTTP/1.1 400 Bad Request\r\n{cors}Content-Type: application/json\r\n"
                    f"Content-Length: {len(resp_body)}\r\n\r\n".encode() + resp_body
                )
                await writer.drain(); writer.close(); return

            if isinstance(req, list):
                if len(req) > MCP_MAX_BATCH:
                    resp_body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                        "code": -32600,
                        "message": f"batch too large ({len(req)} > {MCP_MAX_BATCH})",
                    }}).encode()
                else:
                    async with _HTTP_SEM:
                        resps = [r for r in [await _handle_rpc_item(x) for x in req] if r is not None]
                    resp_body = json.dumps(resps, ensure_ascii=False).encode()
            else:
                async with _HTTP_SEM:
                    resp = await _handle_rpc_item(req)
                if resp is None:
                    writer.write(f"HTTP/1.1 202 Accepted\r\n{cors}Content-Length: 0\r\n\r\n".encode())
                    await writer.drain(); writer.close(); return
                resp_body = json.dumps(resp, ensure_ascii=False).encode()

            writer.write(
                f"HTTP/1.1 200 OK\r\n{cors}Content-Type: application/json\r\n"
                f"Content-Length: {len(resp_body)}\r\n\r\n".encode() + resp_body
            )
            await writer.drain(); writer.close(); return

        writer.write(f"HTTP/1.1 404 Not Found\r\n{cors}Content-Length: 0\r\n\r\n".encode())
        await writer.drain(); writer.close()

    except Exception as e:
        print(f"[MCP-HTTP] Error: {e}", file=sys.stderr)
        try:
            writer.close()
        except Exception:
            pass


async def _http_server(port: int):
    bind = (os.environ.get("MCP_HTTP_BIND", "") or MCP_HTTP_BIND).strip() or "127.0.0.1"
    if bind not in ("127.0.0.1", "localhost", "::1", "[::1]") and not MCP_HTTP_TOKEN:
        print(
            f"[MCP-HTTP] WARNING: bind on {bind} WITHOUT MCP_HTTP_TOKEN - "
            "anyone on the network can call tools. Set a token or bind to loopback!",
            file=sys.stderr,
        )
    server = await asyncio.start_server(_http_handler, bind, port)
    print(f"[MCP-HTTP] Lauscht auf http://{bind}:{port}/mcp", file=sys.stderr)
    print(f"[MCP-HTTP] Health:  http://localhost:{port}/mcp/health", file=sys.stderr)
    async with server:
        await server.serve_forever()


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(description="Hivemind MCP Server (Windows-native)")
    parser.add_argument("--http",  action="store_true", help="HTTP transport only (IntelliJ)")
    parser.add_argument("--both",  action="store_true", help="stdio + HTTP simultaneously")
    parser.add_argument("--port",  type=int, default=HTTP_PORT, help=f"HTTP port (default: {HTTP_PORT})")
    try:
        args = parser.parse_args()
    except SystemExit:
        args = argparse.Namespace(http=False, both=False, port=HTTP_PORT)

    print(f"[MCP] Hivemind MCP Server v2.0 (Windows-native)", file=sys.stderr)
    print(f"[MCP] Workspace : {WORKSPACE}",  file=sys.stderr)
    print(f"[MCP] Hivemind  : {HIVEMIND_URL}", file=sys.stderr)
    print(f"[MCP] Platform  : {'Windows' if IS_WINDOWS else 'Unix'}", file=sys.stderr)

    await _find_router_model()

    if args.http and not args.both:
        print(f"[MCP] Modus: HTTP (Port {args.port})", file=sys.stderr)
        await _http_server(args.port)
    elif args.both:
        print(f"[MCP] Modus: stdio + HTTP (Port {args.port})", file=sys.stderr)
        await asyncio.gather(_stdio_loop(), _http_server(args.port))
    else:
        print("[MCP] Modus: stdio", file=sys.stderr)
        await _stdio_loop()


if __name__ == "__main__":
    asyncio.run(main())
