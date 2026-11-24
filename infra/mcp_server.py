


from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shlex
import sys
import threading
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

# ── Sys.path-Bootstrap (MCP-Audit 2.2, gefixt 2026-08-24) ──────────────────────
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from infra.child_env import scrubbed_env as _scrubbed_env
from utils.file import _is_junction

import httpx
from hive_functions.hivemind_feature.ast_tools import (
    edit_ast_file,
    find_references_report,
    get_signatures_report,
)

# ── Platform ───────────────────────────────────────────────────────────────────
IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    _proactor_policy = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
    if _proactor_policy is not None:
        asyncio.set_event_loop_policy(_proactor_policy())

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
MCP_MAX_SSE = max(1, int(os.environ.get("MCP_MAX_SSE", "8")))
MCP_WRITE_TIMEOUT_S = max(1.0, float(os.environ.get("MCP_WRITE_TIMEOUT_S", "5") or 5))
_CONN_SEM = asyncio.Semaphore(MCP_MAX_CONNECTIONS)
_HTTP_SEM = asyncio.Semaphore(MCP_MAX_CONCURRENCY)
_SSE_SEM = asyncio.Semaphore(MCP_MAX_SSE)
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
    # P2 (audit 2026-11-03): an untiered tool used to be allowed by default.
    # Deny instead; a new tool must be classified before it is exposed.
    return False, f"Tool '{tool_name}' has no tier and is denied by default."


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


def _is_loopback_peer(host: str) -> bool:
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if h.startswith("::ffff:"):
        h = h[len("::ffff:"):]
    return h.split("%", 1)[0] in _LOOPBACK_HOSTS


def _auth_ok(headers: dict, *, require_token: bool = False) -> bool:
    token = (os.environ.get("MCP_HTTP_TOKEN", "") or "").strip()
    if not token:
        return not require_token
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    return hmac.compare_digest(auth[7:].strip(), token)


def _origin_allowed(headers: dict) -> bool:
    """Reject cross-site browser CSRF calls via Sec-Fetch-Site / Origin."""
    fetch_site = (headers.get("sec-fetch-site", "") or "").strip().lower()
    if fetch_site == "cross-site":
        return False
    origin = (headers.get("origin", "") or "").strip()
    if not origin:
        return True
    configured = {
        o.strip() for o in os.environ.get("MCP_HTTP_CORS_ORIGINS", "").split(",")
        if o.strip()
    }
    if origin in configured:
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    origin_netloc = (parsed.netloc or "").strip().lower()
    host_hdr = (headers.get("host", "") or "").strip().lower()
    return bool(origin_netloc and host_hdr and origin_netloc == host_hdr)


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
    env = os.environ.get("HIVEMIND_WORKSPACE", "").strip()
    if env and "\x00" not in env:
        try:
            cand = Path(env).expanduser().resolve()
            if not (cand.exists() and not cand.is_dir()):
                return cand
        except OSError:
            pass
    here = Path(__file__).parent.resolve()
    app_root = here if (here / "server.py").exists() else here.parent
    try:
        from utils.workspace_resolve import get_placeholder_workspace
        return get_placeholder_workspace(create=True)
    except (ImportError, OSError):
        fallback = (app_root / "default_workspace").resolve()
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return fallback


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


def _has_link_inside(raw_path: Path, root_raw: Path, root_resolved: Path) -> bool:
    """Return True if *raw_path* or any component inside workspace is a symlink/junction."""
    try:
        if raw_path.is_symlink() or _is_junction(raw_path):
            return True
    except OSError:
        return True
    for cand_root in (root_raw, root_resolved):
        try:
            rel = raw_path.relative_to(cand_root)
        except ValueError:
            continue
        cur = cand_root
        for part in rel.parts:
            cur = cur / part
            try:
                if cur.is_symlink() or _is_junction(cur):
                    return True
            except OSError:
                return True
        break
    return False


_WIN_DEVICE_NAME_RE = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)",
    re.IGNORECASE,
)


def _is_unc_or_device(raw: str) -> bool:
    """Reject UNC network paths (\\\\server\\share) and Windows DOS/device names before resolve()."""
    s = str(raw or "").strip()
    if not s:
        return False
    if s.startswith(("\\\\", "//")):
        return True
    if IS_WINDOWS:
        for seg in re.split(r"[\\/]+", s):
            if not seg:
                continue
            seg_clean = seg.split(":", 1)[0].rstrip(". ")
            if seg_clean and _WIN_DEVICE_NAME_RE.match(seg_clean):
                return True
    return False


def _resolve_path(path: str) -> Path:
    """Resolve *path* inside WORKSPACE or raise ToolPathError."""
    raw = str(path or "").strip().strip("'\"")
    if not raw:
        raise ToolPathError("empty path", path=raw)
    if "\x00" in raw:
        raise ToolPathError("path contains a NUL byte", path=raw)
    if _is_unc_or_device(raw):
        raise ToolPathError("UNC and device paths are not allowed", path=raw)
    p = Path(raw)
    if not p.is_absolute():
        p = WORKSPACE / p
    try:
        ws_root = WORKSPACE.resolve()
        resolved = p.resolve()
    except (OSError, RuntimeError) as exc:     # Symlink-Loop, kaputte Pfade
        raise ToolPathError(f"cannot resolve path: {exc}", path=raw) from exc
    if _is_unc_or_device(str(resolved)):
        raise ToolPathError("UNC and device paths are not allowed", path=raw)
    try:
        resolved.relative_to(ws_root)
    except ValueError:
        raise ToolPathError(
            f"path is outside the workspace ({resolved.name} not inside {WORKSPACE.name})",
            path=raw,
        ) from None
    if _has_link_inside(p, WORKSPACE, ws_root):
        raise ToolPathError("path traverses a symlink or junction inside workspace", path=raw)
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


# ── S14 (2026-10-31 audit) + 2025-06-18 (2026-10-31): initialize NEGOTIATES
# the protocol version instead of hardcoding "2024-11-05". The 2025 revisions
# get HTTP sessions and the GET-SSE stream (section below); the 2024-11-05
# baseline stays byte-identical for legacy clients. A revision newer than
# 2025-06-18 is never advertised - unknown offers fall back to the baseline.
_PROTOCOL_VERSION_BASELINE = "2024-11-05"
_PROTOCOL_VERSION_LATEST = "2025-06-18"
# Revisions that get sessions + the GET stream; both are echoed verbatim.
_PROTOCOL_VERSIONS_2025 = ("2025-03-26", "2025-06-18")
# MCP 2026-07-28 (minimal support, 2026-11-01): the revision removed
# sessions and the initialize handshake — every request carries its version
# in _meta and the server answers `server/discover`. Our baseline flow is
# already stateless, so 2026 support is: accept the version, answer
# server/discover, never create a session for it.
_PROTOCOL_VERSION_2026 = "2026-07-28"
_SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05",) + _PROTOCOL_VERSIONS_2025 \
    + (_PROTOCOL_VERSION_2026,)
_META_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
# 2026-07-28 error-code allocation: -32020..-32099 reserved for the spec.
_ERR_UNSUPPORTED_PROTOCOL_VERSION = -32022
_META_CLIENT_CAPS_KEY = "io.modelcontextprotocol/clientCapabilities"
# Audit F-1: an initialize inside a BATCH never creates a session (the
# session block below only inspects a dict-shaped top-level request). A 2025
# initialize smuggled through a batch must therefore be answered on the 2024
# baseline — never echo a 2025 version without a session behind it.
_BASELINE_VERSION = "2024-11-05"


def _baseline_batch_item(item: Any) -> Any:
    """Force a batched 2025+/2026 initialize down to the 2024 baseline.

    Audit F-1: a batched initialize never creates a session (the session
    block only inspects a dict-shaped top-level request), so a batched
    initialize must never echo a version that implies one. 2026-07-28 is
    stateless, but a batched offer is still answered on the baseline for a
    uniform table — a 2026 client does not need initialize at all (it uses
    `server/discover` + per-request _meta)."""
    if not isinstance(item, dict) or item.get("method") != "initialize":
        return item
    params = item.get("params")
    if not isinstance(params, dict):
        return item
    if str(params.get("protocolVersion", "") or "") in _SUPPORTED_PROTOCOL_VERSIONS \
            and str(params.get("protocolVersion", "")) != _BASELINE_VERSION:
        item = {**item, "params": {**params,
                                   "protocolVersion": _BASELINE_VERSION}}
    return item
_PROTOCOL_VERSION_DEFAULT = "2024-11-05"


# ── Protocol 2025-06-18: HTTP sessions + the GET-SSE stream ───────────────────
# A 2025+ initialize over HTTP creates a session (secrets.token_urlsafe(32)),
# returns it as the Mcp-Session-Id response header and requires it back on
# every subsequent POST of that generation (missing/unknown -> 404). Sessions
# expire after MCP_SESSION_TTL_S idle (lazy sweep on every session-table
# access), are capped at MCP_SESSION_MAX with oldest-first eviction, and end
# via DELETE /mcp (idempotent). Each session owns a bounded event ring (the
# last MCP_SESSION_EVENT_HISTORY server->client notifications under
# monotonically increasing ids) that the GET /mcp SSE stream replays after
# Last-Event-ID and then follows live. stdio is inherently one session: no id
# is required there.

MCP_SESSION_TTL_S = max(1.0, float(os.environ.get("MCP_SESSION_TTL_S", "3600") or 3600))
MCP_SESSION_MAX = max(1, int(os.environ.get("MCP_SESSION_MAX", "64")))
MCP_SSE_KEEPALIVE_S = max(0.05, float(os.environ.get("MCP_SSE_KEEPALIVE_S", "15") or 15))
MCP_SESSION_EVENT_HISTORY = 500


def _session_clock() -> float:
    """Monotonic session clock (injectable for TTL tests)."""
    return time.monotonic()


class _Session:
    """One 2025+ HTTP session: identity, idle clock, SSE event ring."""

    __slots__ = ("id", "protocol_version", "created", "last_seen",
                 "events", "next_event_id", "event_available", "ended")

    def __init__(self, sid: str, protocol_version: str) -> None:
        self.id = sid
        self.protocol_version = protocol_version
        self.created = _session_clock()
        self.last_seen = self.created
        self.events: deque = deque(maxlen=MCP_SESSION_EVENT_HISTORY)  # (seq, sse frame)
        self.next_event_id = 1
        self.event_available = asyncio.Event()
        self.ended = False


_SESSIONS: dict[str, _Session] = {}


def _session_end(sid: str) -> bool:
    """End the session (DELETE /mcp, eviction, sweep). Idempotent: an unknown
    id is a no-op. Wakes the SSE loop so an open stream closes cleanly."""
    s = _SESSIONS.pop(str(sid or ""), None)
    if s is None:
        return False
    s.ended = True
    s.event_available.set()
    return True


def _sessions_sweep() -> int:
    """Drop sessions idle for more than MCP_SESSION_TTL_S. Called lazily on
    every session-table access (per-request sweep)."""
    now = _session_clock()
    dead = [sid for sid, s in _SESSIONS.items() if now - s.last_seen > MCP_SESSION_TTL_S]
    for sid in dead:
        _session_end(sid)
    return len(dead)


def _session_create(protocol_version: str) -> _Session:
    """New session for a 2025+ initialize; enforces the cap by evicting the
    oldest session (insertion order) first."""
    _sessions_sweep()
    while len(_SESSIONS) >= MCP_SESSION_MAX:
        _session_end(next(iter(_SESSIONS)))      # dict order = oldest first
    s = _Session(secrets.token_urlsafe(32), protocol_version)
    _SESSIONS[s.id] = s
    return s


def _session_get_alive(sid: str) -> _Session | None:
    """The session for `sid`, if it exists and has not ended."""
    s = _SESSIONS.get(str(sid or "")) if sid else None
    if s is None or s.ended:
        return None
    return s


# Audit F-2: the replay ring bounds count AND size — a multi-MB progress
# notification must not sit in the ring verbatim.
_SSE_MAX_EVENT_BYTES = 64 * 1024


def _session_append_event(session: _Session, method: str, params: dict) -> int:
    """Fan a server->client notification into the session's SSE ring.
    Returns the monotonic id used as the SSE `id:` field."""
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params},
                         ensure_ascii=False)
    if len(payload) > _SSE_MAX_EVENT_BYTES:
        # Oversized notification: keep the envelope (and the id sequence);
        # replay clients get a marker instead of megabytes of retained data.
        payload = json.dumps(
            {"jsonrpc": "2.0", "method": method,
             "params": {"_truncated": True, "_original_bytes": len(payload)}},
            ensure_ascii=False)
    seq = session.next_event_id
    session.next_event_id += 1
    session.events.append((seq, f"event: message\ndata: {payload}\n\n"))
    session.event_available.set()
    return seq


def _make_session_notifier(session: _Session):
    """Progress notifier for 2025+ HTTP sessions: notifications fan out to
    the session's GET-SSE stream (the POST response itself is unchanged)."""

    def _notify(params: dict):
        try:
            _session_append_event(session, "notifications/progress",
                                  dict(params or {}))
        except (TypeError, ValueError) as exc:
            print(f"[MCP-session] notify failed: {exc}", file=sys.stderr)
    return _notify


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


def _shared_tool_secret_guard(tool_name: str, args: dict) -> str | None:
    """P0 (audit 2026-11-03): the shared tools run engine-side.

    The local handler guards never execute for a forwarded call, so the same
    denylist must be applied BEFORE the forward. Path and file_glob are the
    two carriers.
    """
    _p = ""
    for _k in ("path", "file", "filepath", "target"):
        _v = (args or {}).get(_k)
        if isinstance(_v, str) and _v.strip():
            _p = _v.strip()
            break
    if _p:
        if _is_blocked_secret(_p.replace("\\", "/")):
            return _tool_error_response(
                "SECRET_PATH_DENIED",
                f"'{_p}' is a protected secret file.",
                tool=tool_name)
        try:
            _rp = _resolve_path(_p)
        except ToolPathError:
            _rp = None
        if _rp is not None:
            _g = _secret_path_guard(_rp, tool_name)
            if _g:
                return _g
    _glob = str((args or {}).get("file_glob") or "")
    if _glob and _is_blocked_secret(_glob.replace("\\", "/")):
        return _tool_error_response(
            "SECRET_PATH_DENIED",
            f"'{_glob}' is a protected secret file.",
            tool=tool_name)
    return None


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
        _guard = _shared_tool_secret_guard(tool_name, args or {})
        if _guard:
            _audit_tool_call(tool_name, transport, ok=False,
                             reason="secret_path_denied")
            return _guard
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


_SCRUBBED_SHELL_ENV_KEYS = (
    "HIVEMIND_INTERNAL_TOKEN",
    "MCP_HTTP_TOKEN",
    "HIVEMIND_TG_TOKEN",
    "HF_TOKEN",
    "HUGGING_FACE_HUB_TOKEN",
)
_SCRUBBED_SHELL_ENV_RE = re.compile(
    r"(?:^|_)(?:TOKEN|SECRET|API_KEY|PASSWORD|PASSWD)(?:_|$)",
    re.IGNORECASE,
)


async def _drain_pipe(
    stream,
    max_bytes: int = 256 * 1024,
    *,
    keep_tail: bool = False,
) -> tuple[bytes, bool]:
    """Drain an async subprocess pipe while keeping buffered bytes capped."""
    if stream is None:
        return b"", False
    chunks: deque[bytes] = deque()
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(16384)
        if not chunk:
            break
        if keep_tail:
            chunks.append(chunk)
            total += len(chunk)
            while total > max_bytes and len(chunks) > 1:
                dropped = chunks.popleft()
                total -= len(dropped)
                truncated = True
            if total > max_bytes and len(chunks) == 1:
                only = chunks[0]
                chunks[0] = only[-max_bytes:]
                total = len(chunks[0])
                truncated = True
        elif not truncated:
            room = max_bytes - total
            if len(chunk) > room:
                if room > 0:
                    chunks.append(chunk[:room])
                    total += room
                truncated = True
            else:
                chunks.append(chunk)
                total += len(chunk)
    return b"".join(chunks), truncated


async def _kill_async_proc(proc: asyncio.subprocess.Process | None) -> None:
    """Terminate an async subprocess and its POSIX process group cleanly."""
    if proc is None:
        return
    try:
        if not IS_WINDOWS:
            _killpg = getattr(os, "killpg", None)
            _getpgid = getattr(os, "getpgid", None)
            _sigkill = getattr(__import__("signal"), "SIGKILL", None)
            if _killpg and _getpgid and _sigkill:
                _killpg(_getpgid(proc.pid), _sigkill)
            else:
                proc.kill()
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (asyncio.TimeoutError, OSError, ProcessLookupError):
        pass
    _tr = getattr(proc, "_transport", None)
    if _tr is not None:
        with contextlib.suppress(OSError, RuntimeError):
            _tr.close()


async def _run_bounded_proc(
    proc: asyncio.subprocess.Process,
    timeout: float,
    *,
    max_bytes: int = 512 * 1024,
    keep_tail: bool = False,
) -> tuple[bytes, bool, bytes, bool, int]:
    """Drain stdout/stderr within total *timeout* and reap/kill on timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(timeout)
    try:
        if (getattr(proc, "stdout", None) is None
                and getattr(proc, "stderr", None) is None
                and callable(getattr(proc, "communicate", None))):
            out, err = await asyncio.wait_for(proc.communicate(), timeout=float(timeout))
            _rc = proc.returncode
            rc = _rc if _rc is not None else -1
            return out or b"", False, err or b"", False, rc
        (out, out_trunc), (err, err_trunc) = await asyncio.wait_for(
            asyncio.gather(
                _drain_pipe(proc.stdout, max_bytes=max_bytes, keep_tail=keep_tail),
                _drain_pipe(proc.stderr, max_bytes=max_bytes, keep_tail=keep_tail),
            ),
            timeout=float(timeout),
        )
        rem = max(0.05, deadline - loop.time())
        await asyncio.wait_for(proc.wait(), timeout=rem)
    except asyncio.TimeoutError:
        await _kill_async_proc(proc)
        raise
    _tr = getattr(proc, "_transport", None)
    if _tr is not None:
        with contextlib.suppress(OSError, RuntimeError):
            _tr.close()
    rc = proc.returncode if proc.returncode is not None else -1
    return out, out_trunc, err, err_trunc, rc


async def tool_shell(cmd: str, timeout: int = 30) -> str:
    safe   = bool(SAFE_SHELL_RE.match(cmd.strip()))
    prefix = "" if safe else "⚠ Unsafe command: "
    _shell_env = _scrubbed_env()
    proc: asyncio.subprocess.Process | None = None
    try:
        if IS_WINDOWS:
            proc = await asyncio.create_subprocess_exec(
                "cmd.exe", "/c", cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKSPACE),
                env=_shell_env,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKSPACE),
                env=_shell_env,
                # Linux audit 2026-11-03: own process group so a timeout can
                # kill the whole tree instead of orphaning grandchildren.
                start_new_session=True,
            )
        out, out_trunc, err, err_trunc, _rc = await _run_bounded_proc(proc, float(timeout))
        result = _decode(out)
        if out_trunc:
            result += "\n... [output truncated]"
        if err.strip():
            result += f"\n[stderr]: {_decode(err)}"
            if err_trunc:
                result += "\n... [stderr truncated]"
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
        if _guard := _secret_path_guard(p, "read_file"):
            return _guard
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
        if _guard := _secret_path_guard(p, "write_file"):
            return _guard
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
    if _guard := _secret_path_guard(p, "search_code"):
        return _guard
    # P2 (deep audit 2026-11-03): a single-file glob (findstr) prints
    # "line:text" without a path, which the row filter cannot match.
    if _is_blocked_secret(str(file_glob or "").replace("\\", "/")):
        return _tool_error_response(
            "SECRET_PATH_DENIED",
            f"'{file_glob}' is a protected secret file.",
            tool="search_code")

    def _cap_rows(raw_out: str) -> str:
        filtered = _filter_secret_rows(raw_out.strip())
        if not filtered:
            return "(no matches)"
        rows = filtered.splitlines()
        return "\n".join(rows[:50]) + (f"\n... ({len(rows) - 50} more)" if len(rows) > 50 else "")

    # 1. ripgrep (Windows: rg.exe, Unix: rg)
    rg_bin = "rg.exe" if IS_WINDOWS else "rg"
    _sc_env = _scrubbed_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            rg_bin, "--line-number", "--no-heading", "--color=never",
            f"--glob={file_glob}", "--", pattern, str(p),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_sc_env,
            start_new_session=not IS_WINDOWS,
        )
        out, _t1, _err, _t2, rc = await _run_bounded_proc(proc, 15.0)
        if rc in (0, 1):
            result = _decode(out).strip()
            return _cap_rows(result) if result else "(no matches)"
    except FileNotFoundError:
        pass
    except Exception as e:
        return f"[rg error: {e}]"

    if IS_WINDOWS:
        try:
            glob_pat = file_glob if file_glob != "*" else "*.*"
            fs_target = str(p) if p.is_file() else f"{p}\\{glob_pat}"
            proc = await asyncio.create_subprocess_exec(
                "findstr", "/s", "/n", "/i", "/L", f"/C:{pattern}", fs_target,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(p.parent if p.is_file() else p),
                env=_sc_env,
            )
            out, _t1, _err, _t2, _rc = await _run_bounded_proc(proc, 15.0)
            return _cap_rows(_decode(out))
        except FileNotFoundError:
            pass
        except Exception as e:
            return f"[findstr error: {e}]"
    else:
        try:
            proc = await asyncio.create_subprocess_exec(
                "grep", "-rn", "--", pattern, str(p),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_sc_env,
                start_new_session=True,
            )
            out, _t1, _err, _t2, _rc = await _run_bounded_proc(proc, 15.0)
            return _cap_rows(out.decode("utf-8", errors="replace"))
        except FileNotFoundError:
            pass
        except Exception as e:
            return f"[grep error: {e}]"

    # 4. Pruned Python fallback (no external grep/rg/findstr needed)
    import fnmatch
    skip = {".git", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache", ".ruff_cache"}
    hits: list[str] = []
    targets: list[Path] = [p] if p.is_file() else []
    if p.is_dir():
        for dp, dns, fns in os.walk(p):
            dns[:] = [d for d in sorted(dns) if d not in skip]
            for fn in sorted(fns):
                if file_glob != "*" and not fnmatch.fnmatch(fn, file_glob):
                    continue
                targets.append(Path(dp) / fn)
                if len(targets) >= 2000:
                    break
            if len(targets) >= 2000:
                break
    for fp in targets:
        try:
            if fp.stat().st_size > 512_000:
                continue
            raw_b = fp.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw_b[:4096]:
            continue
        for ln, line in enumerate(raw_b.decode("utf-8", errors="replace").splitlines(), 1):
            if pattern in line:
                hits.append(f"{fp}:{ln}: {line.strip()[:200]}")
                if len(hits) >= 200:
                    break
        if len(hits) >= 200:
            break
    return _cap_rows("\n".join(hits)) if hits else "(no matches)"


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
        ws_root = WORKSPACE.resolve()
        files: list[Path] = []
        for p in sorted(base.glob(pattern)):
            if _has_link_inside(p, WORKSPACE, ws_root):
                continue
            try:
                p_res = p.resolve()
                rel_p = p_res.relative_to(ws_root).as_posix()
            except (OSError, ValueError):
                continue
            if p_res.is_file() and not _is_blocked_secret(rel_p) and not _is_blocked_secret(p.as_posix()):
                files.append(p)
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
        if _guard := _secret_path_guard(p, "get_signatures"):
            return _guard
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
        if _guard := _secret_path_guard(base, "find_references"):
            return _guard
        if not str(symbol or "").strip():
            return _tool_error_response(
                "INVALID_ARGUMENT",
                "find_references requires a non-empty symbol.",
                tool="find_references",
                retryable=True,
            )
        max_items = max(20, min(2000, int(max_items or 160)))
        return await asyncio.to_thread(
            find_references_report, base, str(symbol).strip(), max_items,
            exclude_fn=lambda _p: _is_blocked_secret(_p.as_posix()))
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
        if _guard := _secret_path_guard(p, "edit_ast"):
            return _guard
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
        if _guard := _secret_path_guard(p, "patch_file"):
            return _guard
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
    k = str(key or "").strip()
    if not k:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "memory_set requires a non-empty key.",
            tool="memory_set",
            retryable=False,
        )
    if (
        k in (".", "..")
        or ".." in k
        or "/" in k
        or "\\" in k
        or any(ch in k for ch in ("?", "#", "\x00"))
        or any(ord(ch) < 32 for ch in k)
    ):
        return _tool_error_response(
            "INVALID_ARGUMENT",
            f"Invalid memory key {k!r}: path separators and traversal tokens are not allowed.",
            tool="memory_set",
            retryable=False,
        )
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(f"{HIVEMIND_URL}/memory/{quote(k, safe='')}", json={"value": value})
            if getattr(r, "status_code", 200) >= 400:
                return _tool_error_response(
                    "MEMORY_SET_FAILED",
                    f"memory_set failed: HTTP {r.status_code}",
                    tool="memory_set",
                    retryable=True,
                )
            return f"Saved: {k} = {value[:80]}"
    except Exception as e:
        return _tool_error_response(
            "MEMORY_SET_FAILED",
            f"memory_set failed: {type(e).__name__}: {str(e)[:220]}",
            tool="memory_set",
            retryable=True,
        )


_GIT_SAFE_SUBCMDS = frozenset({"status", "diff", "log", "show", "blame"})
_GIT_META_CHARS = set(";|&<>\n\r`")
_GIT_FORBIDDEN = (
    "--output", "--ext-diff", "--textconv", "--exec",
    "--upload-pack", "--config", "--open-files-in-pager",
    "--no-index", "--contents", "--git-dir", "--work-tree",
    "--paginate", "--exec-path",
)


def _strip_pair_quotes(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in ('"', "'"):
        return tok[1:-1]
    return tok


def _safe_git_argv(cmd: str) -> list[str] | None:
    """Parse and validate a read-only git command string into a safe argv list (without leading 'git')."""
    c = (cmd or "").strip()
    if not c:
        return None
    if any(ch in _GIT_META_CHARS for ch in c):
        return None
    if "$(" in c or "%" in c:
        return None
    try:
        raw_tokens = shlex.split(c, posix=not IS_WINDOWS)
    except ValueError:
        return None
    tokens = [_strip_pair_quotes(t) for t in raw_tokens if t]
    if len(tokens) < 2:
        return None
    if tokens[0].lower() not in ("git", "git.exe"):
        return None
    subcmd = tokens[1].lower()
    if subcmd not in _GIT_SAFE_SUBCMDS:
        return None
    rest: list[str] = []
    for tok in tokens[2:]:
        low = tok.lower()
        if any(forb in low for forb in _GIT_FORBIDDEN):
            return None
        if low in ("-c", "-o", "-p") or low.startswith(("-c=", "-o=")) or low == "--pager":
            return None
        if not tok.startswith("-") and tok != "--":
            if _is_unc_or_device(tok):
                return None
            if ":" in tok:
                _rev, _rev_path = tok.split(":", 1)
                if _rev_path and _is_blocked_secret(_rev_path):
                    return None
            elif _is_blocked_secret(tok):
                return None
        rest.append(tok)
    if subcmd in ("diff", "show"):
        return [subcmd, "--no-color", "--no-ext-diff", "--no-textconv", *rest]
    return [subcmd, *rest]


def _is_safe_git(cmd: str) -> bool:
    return _safe_git_argv(cmd) is not None


async def tool_git_status(cmd: str = "git status") -> str:
    argv = _safe_git_argv(cmd)
    if argv is None:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "Only read-only git commands are allowed for git_status "
            "(status|diff|log|show|blame).",
            tool="git_status",
            retryable=True,
        )
    try:
        rc, out, err = await _run_git(argv, WORKSPACE.resolve(), timeout=30.0)
    except asyncio.TimeoutError:
        return _tool_error_response(
            "GIT_STATUS_TIMEOUT",
            "git command timed out after 30s.",
            tool="git_status",
            retryable=True,
        )
    except (OSError, ValueError) as exc:
        return _tool_error_response(
            "GIT_STATUS_FAILED",
            f"git command failed: {type(exc).__name__}: {str(exc)[:200]}",
            tool="git_status",
            retryable=True,
        )
    if argv[0] in ("diff", "show"):
        out = _filter_diff_secrets(out)
    else:
        out = _filter_secret_rows(out)
    text = out.strip()
    if err.strip():
        text = f"{text}\n[stderr]: {err.strip()}".strip() if text else f"[stderr]: {err.strip()}"
    if rc != 0 and not text:
        return _tool_error_response(
            "GIT_STATUS_FAILED",
            f"git {argv[0]} exited {rc}.",
            tool="git_status",
            retryable=True,
            details={"exit_code": rc},
        )
    return text or "(no output)"


async def tool_run_python(code: str, timeout: int = 15) -> str:
    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", encoding="utf-8", delete=False) as f:
        f.write(code)
        tmp = f.name
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, tmp,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(WORKSPACE),
            env=_scrubbed_env(),
            start_new_session=not IS_WINDOWS,
        )
        out, out_trunc, err, err_trunc, _rc = await _run_bounded_proc(proc, float(timeout), max_bytes=256 * 1024)
        result = _decode(out)
        if out_trunc:
            result += "\n... [output truncated]"
        if err.strip():
            result += f"\n[stderr]:\n{_decode(err)}"
            if err_trunc:
                result += "\n... [stderr truncated]"
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
    """One git invocation, argv-only (no shell), bounded output and timeout."""
    safe_argv = [
        "-c", "core.fsmonitor=false",
        "-c", "diff.external=",
        "-c", "protocol.ext.allow=never",
        *argv,
    ]
    proc = await asyncio.create_subprocess_exec(
        "git", *safe_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=_scrubbed_env(),
        start_new_session=not IS_WINDOWS,
    )
    out, out_trunc, err, err_trunc, rc = await _run_bounded_proc(proc, float(timeout), max_bytes=512 * 1024)
    out_str = _decode(out) + ("\n... [output truncated]" if out_trunc else "")
    err_str = _decode(err) + ("\n... [stderr truncated]" if err_trunc else "")
    return rc, out_str, err_str


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
    argv = ["diff", "--no-color", "--no-ext-diff", "--no-textconv"]
    ref_raw = str(ref or "").strip()
    ref_clean = ""  # prebound: only read when ref_raw is set (guarded below)
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
        if _guard := _secret_path_guard(p, "git_diff"):
            return _guard
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
    diff = _filter_diff_secrets(out.strip())
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
        if _guard := _secret_path_guard(p, "git_commit"):
            return _guard
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
        if _is_blocked_secret(rel):
            return _tool_error_response(
                "SECRET_PATH_DENIED",
                f"'{raw[:120]}' is a protected secret file and cannot be committed.",
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
    proc: asyncio.subprocess.Process | None = None
    try:
        if IS_WINDOWS:
            proc = await asyncio.create_subprocess_exec(
                "cmd.exe", "/c", run_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(base),
                env=_scrubbed_env(),
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                run_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(base),
                env=_scrubbed_env(),
                start_new_session=True,
            )
        out, _ot, err, _et, rc = await _run_bounded_proc(
            proc,
            float(timeout),
            max_bytes=max(256 * 1024, MCP_TEST_OUTPUT_MAX_CHARS * 4),
            keep_tail=True,
        )
    except asyncio.TimeoutError:
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
        "description": "Surgical verbatim replacements in an existing file: pass old_text+new_text or edits=[{old_text,new_text}].",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path":           {"type": "string"},
                "old_text":       {"type": "string"},
                "new_text":       {"type": "string"},
                "edits":          {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_text": {"type": "string"},
                            "new_text": {"type": "string"},
                        },
                        "required": ["old_text", "new_text"],
                    },
                },
                "confirm_shrink": {"type": "boolean"},
            },
            "required": ["path"],
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
    r"\.git/config|\.git/hooks/.*|"
    r"\.netrc|\.npmrc|\.pypirc|\.kube/config|.*\.kdbx|.*\.pem|.*\.key|"
    r"id_rsa.*|id_ed25519.*|id_ecdsa.*|"
    r"credentials.*|secrets?\..*)$",
    re.IGNORECASE)
_ANSI_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


class _ResourceError(Exception):
    """A resources/* failure that maps to a JSON-RPC error (code carried)."""

    def __init__(self, message: str, *, code: int = -32002) -> None:
        super().__init__(message)
        self.code = int(code)


def _norm_secret_seg(seg: str) -> str:
    s = str(seg or "")
    # Preserve Windows drive designator ("C:") while stripping ":stream" ADS.
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        head, tail = s[:2], s[2:]
        if ":" in tail:
            tail = tail.split(":", 1)[0]
        s = head + tail
    elif ":" in s:
        s = s.split(":", 1)[0]
    return s.rstrip(". ")


def _is_blocked_secret(rel_posix: str) -> bool:
    rel = str(rel_posix or "").replace("\\", "/")
    norm_segs = [_norm_secret_seg(seg) for seg in rel.split("/") if seg]
    parts_low = [seg.lower() for seg in Path(rel).parts if seg]
    norm_parts_low = [_norm_secret_seg(seg).lower() for seg in Path(rel).parts if seg]
    if any(part in {"sessions", "chats"} for part in (*parts_low, *norm_parts_low)):
        return True
    norm_rel = "/".join(seg for seg in norm_segs if seg)
    if _RESOURCE_SECRET_RE.search(rel) or _RESOURCE_SECRET_RE.search(norm_rel):
        return True
    if norm_segs:
        leaf = norm_segs[-1]
        if len(leaf) >= 2 and leaf[1] == ":" and leaf[0].isalpha():
            leaf = leaf[2:]
        if leaf and _RESOURCE_SECRET_RE.search(leaf):
            return True
    return False


def _secret_path_guard(p: Path, tool: str) -> str | None:
    """B3 (audit 2026-11-03): the tool API shares the resources denylist.

    `_default_workspace()` is the install root, so settings.json and
    gateway.toml live inside the workspace; without this check every MCP
    client could read the bot token and the git token through read_file.
    """
    try:
        rel = p.resolve().relative_to(WORKSPACE.resolve()).as_posix()
    except (OSError, ValueError):
        rel = p.as_posix()
    parts_norm = [_norm_secret_seg(part).lower() for part in Path(rel).parts]
    if any(part in {"sessions", "chats"} for part in parts_norm):
        return _tool_error_response(
            "SECRET_PATH_DENIED",
            f"'{p.name}' is inside a private transcript directory.",
            tool=tool)
    if _is_blocked_secret(rel) or _is_blocked_secret(p.name):
        return _tool_error_response(
            "SECRET_PATH_DENIED",
            f"'{p.name}' is a protected secret file and cannot be read or "
            "written through the MCP tool API.",
            tool=tool)
    return None


def _filter_secret_rows(text: str) -> str:
    """Drop search hits whose file part is a protected secret (B3)."""
    kept = []
    for line in (text or "").splitlines():
        clean = _ANSI_ESC_RE.sub("", line)
        m = re.match(r"^(.+):(?:\d+:|$)", clean)
        if m and _is_blocked_secret(m.group(1).replace("\\", "/")):
            continue
        kept.append(line)
    return "\n".join(kept)


def _filter_diff_secrets(diff_text: str) -> str:
    """Strip git diff sections targeting protected secret/transcript paths."""
    if not diff_text:
        return diff_text
    kept_blocks: list[str] = []
    cur_block: list[str] = []
    cur_blocked = False
    for line in diff_text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if cur_block and not cur_blocked:
                kept_blocks.extend(cur_block)
            cur_block = [line]
            cur_blocked = False
            m = re.match(r"^diff --git a/(.+?) b/(.+?)\r?\n?$", line)
            if m and (_is_blocked_secret(m.group(1)) or _is_blocked_secret(m.group(2))):
                cur_blocked = True
        else:
            cur_block.append(line)
    if cur_block and not cur_blocked:
        kept_blocks.extend(cur_block)
    return "".join(kept_blocks).strip()


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
                if item.is_symlink() or _is_junction(item):
                    continue
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
    if not raw or "\x00" in raw:
        raise _ResourceError("params.uri must be a non-empty string", code=-32602)
    parts = urlparse(raw)
    scheme = (parts.scheme or "").lower()
    if scheme == "file":
        if parts.netloc not in ("", "localhost"):
            raise _ResourceError("file:// remote hosts are not allowed", code=-32602)
        # url2pathname unquotes %xx and maps "/C:/x" -> "C:\x" on Windows.
        p = Path(url2pathname(parts.path))
    elif scheme == "hivemind" and parts.netloc.lower() == "workspace":
        rel = unquote(parts.path).lstrip("/")
        if not rel or "\x00" in rel:
            raise _ResourceError("empty hivemind://workspace path", code=-32602)
        p = WORKSPACE / rel
    else:
        raise _ResourceError(
            f"unsupported resource URI scheme: {scheme or '(none)'} "
            "(use file:// or hivemind://workspace/<path>)", code=-32602)
    try:
        root = WORKSPACE.resolve()
        resolved = p.resolve()
    except (OSError, RuntimeError) as exc:
        raise _ResourceError(f"cannot resolve resource path: {exc}", code=-32602)
    try:
        rel = resolved.relative_to(root).as_posix()
    except ValueError:
        raise _ResourceError("resource is outside the workspace", code=-32602) from None
    except (OSError, ValueError) as exc:
        raise _ResourceError(f"cannot resolve resource path: {exc}", code=-32602)
    if _has_link_inside(p, WORKSPACE, root):
        raise _ResourceError("symlinks and junctions are not allowed", code=-32602)
    rel_parts = resolved.relative_to(root).parts
    if any(":" in part or (IS_WINDOWS and part != part.rstrip(". ")) for part in rel_parts):
        raise _ResourceError("invalid resource path segment", code=-32602)
    raw_rel = ""
    for cand_root in (WORKSPACE, root):
        try:
            raw_rel = p.relative_to(cand_root).as_posix()
            break
        except ValueError:
            continue
    if _is_blocked_secret(rel) or (raw_rel and _is_blocked_secret(raw_rel)) or _is_blocked_secret(p.name):
        raise _ResourceError(
            "resource is blocked as a potential secret (denylist)")
    if not resolved.is_file():
        raise _ResourceError(f"resource not found: {raw[:200]}")
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

async def handle_request(req: dict, transport: str = "stdio",
                         session: "_Session | None" = None) -> dict | None:
    """One JSON-RPC message. `session` is the 2025+ HTTP session (if any) -
    only used to fan progress notifications out to its GET-SSE stream."""
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

    # MCP 2026-07-28 (minimal): a request may carry its protocol version in
    # `_meta` instead of initializing. An unsupported version is rejected
    # with the spec's UnsupportedProtocolVersionError; a supported one just
    # proceeds stateless (the baseline flow has no per-request state).
    _req_meta = (req.get("params") or {}).get("_meta") \
        if isinstance(req.get("params"), dict) else None
    if isinstance(_req_meta, dict):
        _meta_ver = str(_req_meta.get(_META_VERSION_KEY, "") or "")
        if _meta_ver and _meta_ver not in _SUPPORTED_PROTOCOL_VERSIONS:
            return err(_ERR_UNSUPPORTED_PROTOCOL_VERSION,
                       f"unsupported protocol version {_meta_ver!r} "
                       f"(supported: {', '.join(_SUPPORTED_PROTOCOL_VERSIONS)})")

    if method == "server/discover":
        # 2026-07-28: servers MUST answer this RPC with identity, supported
        # versions and capabilities. Clients may probe it instead of
        # initializing (and MUST on stdio for version selection).
        return ok({
            "serverInfo": {"name": "hivemind-mcp", "version": "2.0.0"},
            "protocolVersions": list(_SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
        })

    if method == "initialize":
        # S14: echo the requested protocol version when we actually
        # implement it; answer with the baseline otherwise. 2025-03-26 /
        # 2025-06-18 offers are echoed verbatim (the HTTP layer attaches
        # the Mcp-Session-Id header, see _http_handle_... below). A
        # 2026-07-28 offer is echoed too but stays stateless (no session —
        # 2026 removed sessions; clients use server/discover + _meta).
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
        if tool_name == "query":
            if transport == "stdio":
                _notify = _make_stdio_notifier()
            elif session is not None:
                # 2025-06-18: progress notifications fan out to the
                # session's GET-SSE stream; the POST response is unchanged.
                _notify = _make_session_notifier(session)
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
    loop = asyncio.get_running_loop()
    buf = ""
    buf_lines = 0
    _buf_max = MCP_MAX_BODY_BYTES

    def _readline_bounded() -> tuple[bytes, bool]:
        line = sys.stdin.buffer.readline(_buf_max + 1)
        if not line:
            return b"", False
        if len(line) > _buf_max:
            if not line.endswith(b"\n"):
                while True:
                    tail = sys.stdin.buffer.readline(65536)
                    if not tail or tail.endswith(b"\n"):
                        break
            return b"", True
        return line, False

    def _writeline(data: bytes):
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    while True:
        try:
            line, too_large = await loop.run_in_executor(None, _readline_bounded)
            if not line and not too_large:
                break
            text = "" if too_large else line.decode("utf-8", errors="replace")
            if too_large or len(text.encode("utf-8")) > _buf_max:
                print(f"[MCP-stdio] request line too long ({len(text)} bytes) - dropped",
                      file=sys.stderr)
                _bad = json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600, "message": f"request too large (max {_buf_max} bytes)"}})
                await loop.run_in_executor(None, _writeline,
                                           (_bad + "\n").encode("utf-8"))
                buf = ""
                buf_lines = 0
                continue
            buf += text
            buf_lines += 1
            if len(buf.encode("utf-8")) > _buf_max:
                print(f"[MCP-stdio] accumulated request too large ({len(buf)} bytes) - dropped",
                      file=sys.stderr)
                _bad = json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                    "code": -32600,
                    "message": f"request too large (max {_buf_max} bytes)"}})
                await loop.run_in_executor(None, _writeline,
                                           (_bad + "\n").encode("utf-8"))
                buf = ""
                buf_lines = 0
                continue
            try:
                req = json.loads(buf.strip())
                buf = ""
                buf_lines = 0
            except json.JSONDecodeError:
                if not buf.lstrip():
                    buf = ""
                    buf_lines = 0
                elif not buf.lstrip().startswith(("{", "[")) or buf_lines >= 64:
                    buf = ""
                    buf_lines = 0
                    _bad_json = json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                        "code": -32700, "message": "Parse error"}})
                    await loop.run_in_executor(None, _writeline,
                                               (_bad_json + "\n").encode("utf-8"))
                continue
            try:
                if MCP_REQUEST_TIMEOUT_S > 0:
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


# ── HTTP Transport ─────────────────────────────────────────────────────────────

async def _handle_rpc_item(item: Any, session: "_Session | None" = None) -> dict | None:
    """One JSON-RPC message of an HTTP request (single or batch item).

    S9 (2026-10-31 audit): a non-dict item ([1, 2] in a batch) used to raise
    AttributeError inside the timeout handler and the connection closed
    without any answer. It now gets a clean -32600 response.
    """
    if not isinstance(item, dict):
        return {"jsonrpc": "2.0", "id": None,
                "error": {"code": -32600, "message": "request must be an object"}}
    # `session` is only forwarded when set, so a monkeypatched 2-arg
    # handle_request (tests) keeps working on the legacy path.
    _kwargs = {"session": session} if session is not None else {}
    try:
        if MCP_REQUEST_TIMEOUT_S > 0:
            return await asyncio.wait_for(handle_request(item, "http", **_kwargs),
                                          timeout=MCP_REQUEST_TIMEOUT_S)
        return await handle_request(item, "http", **_kwargs)
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


_SINGLE_VALUE_HTTP_HEADERS = frozenset({
    "content-length", "transfer-encoding", "host",
    "authorization", "origin", "sec-fetch-site", "mcp-session-id",
})


async def _send_http(
    writer: asyncio.StreamWriter,
    status: str,
    extra_headers: str = "",
    body: bytes = b"",
) -> None:
    """Write a complete non-SSE HTTP/1.1 response with Connection: close and drain timeout."""
    head = (
        f"HTTP/1.1 {status}\r\n"
        f"{extra_headers}"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n\r\n"
    ).encode("ascii", "replace")
    try:
        writer.write(head + body)
        await asyncio.wait_for(writer.drain(), timeout=MCP_WRITE_TIMEOUT_S)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            writer.close()


async def _http_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """S13 (2026-10-31 audit): caps the number of open connections before any
    byte is read. _HTTP_SEM only bounds in-flight requests - unboundedly many
    (half-)open sockets used to be possible."""
    if _CONN_SEM.locked():
        await _send_http(writer, "503 Service Unavailable")
        return
    await _CONN_SEM.acquire()
    try:
        await _http_handle_connection(reader, writer)
    finally:
        _CONN_SEM.release()


async def _handle_sse_stream(session: "_Session", writer: asyncio.StreamWriter,
                             cors: str, last_event_id: str = "") -> None:
    """GET /mcp for a 2025+ session: the SSE stream with server->client messages."""
    try:
        writer.write(
            f"HTTP/1.1 200 OK\r\n{cors}"
            "Content-Type: text/event-stream\r\n"
            "Cache-Control: no-cache\r\n"
            "Connection: close\r\n\r\n".encode()
        )
        await asyncio.wait_for(writer.drain(), timeout=MCP_WRITE_TIMEOUT_S)
    except (ConnectionError, OSError, asyncio.TimeoutError):
        with contextlib.suppress(OSError, RuntimeError):
            writer.close()
        return

    since = -1
    _raw = str(last_event_id or "").strip()
    if _raw.isdigit():
        since = int(_raw)
    if since >= session.next_event_id:
        since = -1
    cursor = since if since >= 0 else 0
    session.event_available.clear()
    for seq, frame in list(session.events):
        if seq <= cursor:
            continue
        try:
            writer.write(f"id: {seq}\n{frame}".encode("utf-8", "replace"))
            await asyncio.wait_for(writer.drain(), timeout=MCP_WRITE_TIMEOUT_S)
        except (ConnectionError, OSError, asyncio.TimeoutError):
            with contextlib.suppress(OSError, RuntimeError):
                writer.close()
            return
        cursor = seq
        session.last_seen = _session_clock()

    while not session.ended:
        try:
            await asyncio.wait_for(session.event_available.wait(),
                                   timeout=MCP_SSE_KEEPALIVE_S)
        except (TimeoutError, asyncio.TimeoutError):
            pass                      # idle tick -> keep-alive comment below
        session.event_available.clear()
        if session.ended:
            break
        try:
            wrote = False
            for seq, frame in list(session.events):
                if seq > cursor:
                    writer.write(f"id: {seq}\n{frame}".encode("utf-8", "replace"))
                    cursor = seq
                    wrote = True
            if not wrote:
                writer.write(b": keep-alive\n\n")
            await asyncio.wait_for(writer.drain(), timeout=MCP_WRITE_TIMEOUT_S)
            if wrote:
                session.last_seen = _session_clock()
        except (ConnectionError, OSError, asyncio.TimeoutError):
            break
    with contextlib.suppress(OSError, RuntimeError):
        writer.close()


async def _http_handle_connection(reader: asyncio.StreamReader,
                                  writer: asyncio.StreamWriter):
    loop = asyncio.get_running_loop()
    try:
        raw = b""
        hdr_deadline = loop.time() + 10.0
        try:
            while b"\r\n\r\n" not in raw:
                rem = hdr_deadline - loop.time()
                if rem <= 0:
                    raise asyncio.TimeoutError
                chunk = await asyncio.wait_for(reader.read(4096), timeout=rem)
                if not chunk:
                    break
                raw += chunk
                if len(raw) > MCP_MAX_HEADER_BYTES:
                    print(f"[MCP-HTTP] header block too large ({len(raw)} bytes) - closing",
                          file=sys.stderr)
                    await _send_http(writer, "431 Request Header Fields Too Large")
                    return
        except asyncio.TimeoutError:
            await _send_http(writer, "408 Request Timeout")
            return

        try:
            header_part, _, body_part = raw.partition(b"\r\n\r\n")
            first_line, *rest = header_part.decode(errors="replace").split("\r\n")
            method, path, _ = first_line.split(" ", 2)
            headers: dict[str, str] = {}
            for hdr_line in rest:
                if not hdr_line:
                    continue
                if ":" not in hdr_line:
                    raise ValueError("malformed header")
                k, v = hdr_line.split(":", 1)
                k_low = k.strip().lower()
                if not k_low or any(ch.isspace() for ch in k_low):
                    raise ValueError("invalid header name")
                if k_low in _SINGLE_VALUE_HTTP_HEADERS and k_low in headers:
                    raise ValueError(f"duplicate header {k_low}")
                headers[k_low] = v.strip()
        except ValueError:
            await _send_http(writer, "400 Bad Request")
            return

        if "transfer-encoding" in headers:
            await _send_http(writer, "400 Bad Request")
            return

        path = path.split("?")[0]

        # ── Tier 0.1 Hardening (2026-08-24) ───────────────────────────────
        if not _host_allowed(headers.get("host", "")) or not _origin_allowed(headers):
            await _send_http(writer, "403 Forbidden")
            return
        cors = _cors_headers(headers.get("origin", ""))
        if method == "OPTIONS":
            await _send_http(writer, "204 No Content", extra_headers=cors)
            return

        _bind_host = (os.environ.get("MCP_HTTP_BIND", "") or MCP_HTTP_BIND).strip().lower() or "127.0.0.1"
        _peer = (writer.get_extra_info("peername") or ("",))[0]
        _require_tok = (
            _bind_host not in ("127.0.0.1", "localhost", "::1", "[::1]")
        ) or (bool(_peer) and not _is_loopback_peer(str(_peer)))
        if not _auth_ok(headers, require_token=_require_tok):
            body = json.dumps({"error": "unauthorized",
                               "hint": "Authorization: Bearer <MCP_HTTP_TOKEN> required"}).encode()
            await _send_http(
                writer,
                "401 Unauthorized",
                extra_headers="WWW-Authenticate: Bearer\r\nContent-Type: application/json\r\n",
                body=body,
            )
            return

        if method == "GET" and path in ("/mcp/health", "/health"):
            body = json.dumps({
                "status": "ok", "server": "hivemind-mcp", "version": "2.0.0",
                "platform": "windows" if IS_WINDOWS else "unix",
            }).encode()
            await _send_http(
                writer,
                "200 OK",
                extra_headers=f"{cors}Content-Type: application/json\r\n",
                body=body,
            )
            return

        if method == "GET" and path == "/mcp":
            _sessions_sweep()
            _gsess = _session_get_alive(headers.get("mcp-session-id", ""))
            if _gsess is not None:
                if _SSE_SEM.locked():
                    await _send_http(writer, "503 Service Unavailable", extra_headers=cors)
                    return
                _gsess.last_seen = _session_clock()
                async with _SSE_SEM:
                    await _handle_sse_stream(_gsess, writer, cors,
                                             headers.get("last-event-id", ""))
                return

        if method == "DELETE" and path == "/mcp":
            _sessions_sweep()
            _session_end(headers.get("mcp-session-id", ""))
            await _send_http(writer, "204 No Content", extra_headers=cors)
            return

        if method == "POST" and path == "/mcp":
            _ct_raw = (headers.get("content-type", "") or "").split(";", 1)[0].strip().lower()
            if _ct_raw and _ct_raw != "application/json":
                await _send_http(writer, "415 Unsupported Media Type")
                return
            try:
                content_length = int(headers.get("content-length", len(body_part)) or 0)
            except (TypeError, ValueError):
                await _send_http(writer, "400 Bad Request")
                return
            if content_length < 0 or content_length > MCP_MAX_BODY_BYTES:
                resp_body = json.dumps({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32000,
                        "message": f"Request body too large (max {MCP_MAX_BODY_BYTES} bytes)",
                    },
                }).encode()
                await _send_http(
                    writer,
                    "413 Payload Too Large",
                    extra_headers=f"{cors}Content-Type: application/json\r\n",
                    body=resp_body,
                )
                return
            body_bytes = body_part[:content_length]
            body_deadline = loop.time() + MCP_BODY_TIMEOUT_S
            while len(body_bytes) < content_length:
                rem = body_deadline - loop.time()
                if rem <= 0:
                    await _send_http(writer, "408 Request Timeout")
                    return
                try:
                    more = await asyncio.wait_for(
                        reader.read(min(65536, content_length - len(body_bytes))),
                        timeout=rem,
                    )
                except asyncio.TimeoutError:
                    await _send_http(writer, "408 Request Timeout")
                    return
                if not more:
                    break
                body_bytes += more
                if len(body_bytes) > MCP_MAX_BODY_BYTES:
                    await _send_http(writer, "413 Payload Too Large", extra_headers=cors)
                    return

            try:
                req = json.loads(body_bytes.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                resp_body = json.dumps({"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "Parse error"}}).encode()
                await _send_http(
                    writer,
                    "400 Bad Request",
                    extra_headers=f"{cors}Content-Type: application/json\r\n",
                    body=resp_body,
                )
                return

            _mcp_sid = headers.get("mcp-session-id", "")
            _mcp_proto = headers.get("mcp-protocol-version", "")
            http_session = None
            new_session_id = ""
            if isinstance(req, dict) and req.get("method") == "initialize":
                _init_params = req.get("params")
                _req_ver = (str(_init_params.get("protocolVersion", "") or "")
                            if isinstance(_init_params, dict) else "")
                if _req_ver in _PROTOCOL_VERSIONS_2025:
                    http_session = _session_create(_req_ver)
                    new_session_id = http_session.id
            elif _mcp_sid or _mcp_proto in _PROTOCOL_VERSIONS_2025:
                _sessions_sweep()
                http_session = _session_get_alive(_mcp_sid)
                if http_session is None:
                    _err_id = req.get("id") if isinstance(req, dict) else None
                    resp_body = json.dumps({
                        "jsonrpc": "2.0", "id": _err_id,
                        "error": {"code": -32001,
                                  "message": "session not found or expired "
                                             "(re-initialize for a new Mcp-Session-Id)"},
                    }).encode()
                    await _send_http(
                        writer,
                        "404 Not Found",
                        extra_headers=f"{cors}Content-Type: application/json\r\n",
                        body=resp_body,
                    )
                    return
                http_session.last_seen = _session_clock()

            if isinstance(req, list):
                if len(req) > MCP_MAX_BATCH:
                    resp_body = json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                        "code": -32600,
                        "message": f"batch too large ({len(req)} > {MCP_MAX_BATCH})",
                    }}).encode()
                else:
                    async with _HTTP_SEM:
                        resps = [r for r in [await _handle_rpc_item(
                                            _baseline_batch_item(x),
                                            session=http_session)
                                            for x in req] if r is not None]
                    resp_body = json.dumps(resps, ensure_ascii=False).encode()
            else:
                async with _HTTP_SEM:
                    resp = await _handle_rpc_item(req, session=http_session)
                if resp is None:
                    await _send_http(writer, "202 Accepted", extra_headers=cors)
                    return
                resp_body = json.dumps(resp, ensure_ascii=False).encode()

            _sess_hdr = f"Mcp-Session-Id: {new_session_id}\r\n" if new_session_id else ""
            await _send_http(
                writer,
                "200 OK",
                extra_headers=f"{cors}{_sess_hdr}Content-Type: application/json\r\n",
                body=resp_body,
            )
            return

        await _send_http(writer, "404 Not Found", extra_headers=cors)

    except Exception as e:
        print(f"[MCP-HTTP] Error: {e}", file=sys.stderr)
        with contextlib.suppress(OSError, RuntimeError):
            writer.close()


async def _http_server(port: int):
    bind = (os.environ.get("MCP_HTTP_BIND", "") or MCP_HTTP_BIND).strip() or "127.0.0.1"
    _tok = (os.environ.get("MCP_HTTP_TOKEN", "") or MCP_HTTP_TOKEN).strip()
    if bind.lower() not in ("127.0.0.1", "localhost", "::1", "[::1]") and not _tok:
        msg = (
            f"[MCP-HTTP] ERROR: refusing bind on {bind} WITHOUT MCP_HTTP_TOKEN - "
            "set MCP_HTTP_TOKEN or bind to loopback."
        )
        print(msg, file=sys.stderr)
        raise RuntimeError(f"Refusing non-loopback MCP HTTP bind on {bind} without MCP_HTTP_TOKEN")
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
