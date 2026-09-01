


from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

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
MCP_HTTP_TOKEN = os.environ.get("MCP_HTTP_TOKEN", "").strip()


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


# ── Governance-Tiers (Tier 1.5, 2026-08-24) ───────────────────────────────────
_TIER_READ = {
    "read_file", "list_dir", "find_files", "search_code",
    "get_signatures", "find_references", "memory_get", "git_status",
}
_TIER_WRITE = {
    "write_file", "write_file_append", "patch_file", "edit_file",
    "replace_lines", "edit_ast", "memory_set",
}
_TIER_EXEC = {"shell", "run_bash", "run_python"}
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
    (re.compile(r"(?:read|open|cat|type|lies?|lese?|zeig|öffne?)\s+['\"]?(.+?\.[a-z]{1,6})['\"]?", re.I),
     "read_file", lambda m: {"path": m.group(1).strip()}),

    (re.compile(r"(?:list|ls|dir|zeig\s+(?:mir\s+)?(?:alle\s+)?(?:dateien|files)|verzeichnis)\s*['\"]?([./\\\w-]*)['\"]?", re.I),
     "list_dir", lambda m: {"path": m.group(1).strip() or "."}),

    # Git
    (re.compile(r"git\s+(?:status|diff|log|show|blame)", re.I),
     "git_status", lambda m: {"cmd": m.group(0).strip()}),

    # Shell direkt
    (re.compile(r"^(?:run|exec|execute|führe?\s+aus?)\s+(.+)$", re.I),
     "shell", lambda m: {"cmd": m.group(1).strip()}),

    # Read memory
    (re.compile(r"(?:memory\s+get|get\s+from\s+memory|was\s+weißt\s+du\s+über|erinnere?\s+dich\s+an|hole\s+aus\s+memory)\s+['\"]?(\w+)['\"]?", re.I),
     "memory_get", lambda m: {"key": m.group(1).strip()}),

    # Code suchen
    (re.compile(r"(?:search|grep|find|suche?)\s+(?:for\s+|nach\s+)?['\"](.+?)['\"]", re.I),
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

def _resolve_path(path: str) -> Path:
    """
    Resolve path safely within WORKSPACE.
    SECURITY: Path-traversal prevention — all paths must land within WORKSPACE.
    Even explicitly absolute paths outside WORKSPACE are rejected to prevent
    unintended writes to system directories.
    """
    p = Path(path.strip().strip("'\""))
    if not p.is_absolute():
        p = WORKSPACE / p
    resolved = p.resolve()
    
    # Security FIX: STRICT workspace containment check
    # Rejects any path (absolute or relative) that escapes WORKSPACE
    try:
        resolved.relative_to(WORKSPACE.resolve())
        return resolved
    except ValueError:
        # Path escapes WORKSPACE — fall back to making it relative within workspace
        # This prevents attacks like write_file("C:\Windows\System32\malware.exe")
        fallback = WORKSPACE / p.name
        return fallback.resolve()


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


async def _dispatch_tool(tool_name: str, args: dict, local_handler=None) -> str:
    if tool_name in _MCP_SHARED_TOOL_NAMES:
        if not MCP_SHARED_TOOL_ENABLED:
            return _tool_error_response(
                "MCP_SHARED_DISABLED",
                "Shared tool dispatch is disabled for this MCP server instance.",
                tool=tool_name,
                retryable=False,
            )
        ok, result = await _try_shared_tool_exec(tool_name, args)
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
    prefix = "" if safe else "⚠ Unsicherer Befehl: "
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
        return prefix + (result.strip() or "(kein Output)")
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
        return "\n".join(lines) or "(leer)"
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
            proc = await asyncio.create_subprocess_exec(
                "cmd.exe", "/c",
                f'findstr /s /n /i "{pattern}" "{p}\\{glob_pat}"',
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
        proc = await asyncio.create_subprocess_shell(
            f'grep -rn "{pattern}" "{p}"',
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
        return result.strip() or "(kein Output)"
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
                "start_line": {"type": "integer", "description": "Erste Zeile (1-indexed, optional)"},
                "end_line":   {"type": "integer", "description": "Letzte Zeile inklusive (optional)"},
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
        "description": "Surgische SEARCH/REPLACE-Edits in einer bestehenden Datei (gleiches Tool wie im internen Agent-Loop).",
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
        "description": "Ersetzt einen Zeilenbereich in einer Datei (1-indexed, inklusiv).",
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
        "description": "Dateien per Glob finden (z.B. **/*.py).",
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
        "description": "Leichte Symbol-Referenzsuche mit Datei+Zeile (LSP-lite).",
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
        "description": "Ersetzt einen Python-AST-Knoten (function|class|variable) robust per Name.",
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
]


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
    method = req.get("method", "")
    req_id = req.get("id")

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

    def _looks_like_error_text(text: str) -> bool:
        if not isinstance(text, str):
            return False
        t = text.strip().lower()
        if not t.startswith("["):
            return False
        markers = (
            "error",
            "fehler",
            "timeout",
            "not found",
            "nicht gefunden",
        )
        return any(mk in t for mk in markers)

    def ok(result):
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def err(code, msg):
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}

    if method == "initialize":
        return ok({
            "protocolVersion": "2024-11-05",
            "serverInfo": {"name": "hivemind-mcp", "version": "2.0.0"},
            "capabilities": {"tools": {}},
        })

    if method == "ping":
        return ok({})

    if method == "tools/list":
        visible = [t for t in TOOL_SCHEMAS if _tool_allowed(t.get("name", ""), transport)[0]]
        return ok({"tools": visible})

    if method == "tools/call":
        params    = req.get("params", {})
        tool_name = params.get("name", "")
        args      = params.get("arguments", {})
        if tool_name not in TOOL_FNS:
            return err(-32601, f"Unbekanntes Tool: {tool_name}")
        allowed, reason = _tool_allowed(tool_name, transport)
        if not allowed:
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
        return ok({
            "content": [{"type": "text", "text": str(result)}],
            "isError": is_error,
            "_meta": {
                "tool": tool_name,
                "elapsed_s": round(time.time() - t0, 2),
                **({"tool_error": err_meta} if isinstance(err_meta, dict) else {}),
            },
        })

    if method.startswith("notifications/"):
        return None

    return err(-32601, f"Unbekannte Methode: {method}")


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
    def _notify(params: dict):
        try:
            _out = (json.dumps(
                {"jsonrpc": "2.0", "method": "notifications/progress", "params": params},
                ensure_ascii=False,
            ) + "\n").encode("utf-8")
            sys.stdout.buffer.write(_out)
            sys.stdout.buffer.flush()
        except Exception:
            pass
    return _notify


async def _stdio_loop():


    loop = asyncio.get_event_loop()

    def _readline() -> bytes:
        return sys.stdin.buffer.readline()

    def _writeline(data: bytes):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    buf = ""
    _buf_max = 1024 * 512
    while True:
        try:
            line = await loop.run_in_executor(None, _readline)
            if not line:
                break
            buf += line.decode("utf-8", errors="replace")
            if len(buf) > _buf_max:
                print(f"[MCP-stdio] Buffer overflow ({len(buf)} bytes) — reset", file=sys.stderr)
                buf = ""
                continue
            try:
                req = json.loads(buf.strip())
                buf = ""
            except json.JSONDecodeError:
                continue
            response = await handle_request(req)
            if response is not None:
                out = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                await loop.run_in_executor(None, _writeline, out)
        except Exception as e:
            print(f"[MCP-stdio] Error: {e}", file=sys.stderr)
            buf = ""


# ── HTTP Transport ─────────────────────────────────────────────────────────────

async def _http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        raw = b""
        try:
            while b"\r\n\r\n" not in raw:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=10.0)
                if not chunk:
                    break
                raw += chunk
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
            content_length = int(headers.get("content-length", len(body_part)))
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
                more = await reader.read(content_length - len(body_bytes))
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
                resps = [r for r in [await handle_request(x, "http") for x in req] if r is not None]
                resp_body = json.dumps(resps, ensure_ascii=False).encode()
            else:
                resp = await handle_request(req, "http")
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
