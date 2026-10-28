"""MCP client (beta): use external MCP servers over stdio or Streamable HTTP.

HiveMind already *is* an MCP server (`infra/mcp_server.py`). This module is the
other direction: it starts the servers the user configured (stdio) or calls
them over HTTP (`url` entries) and offers their tools to the model.

Design rules (beta, deliberately small):
  - two transports: stdio (one process per configured server, started on
    first use, killed on shutdown or after a timeout) and Streamable HTTP
    (`url` entries; answers as JSON or SSE, no process, optional auth via
    `headers`/`token_env`).
  - `command` + `args` are executed WITHOUT a shell. Only `.cmd`/`.bat` are
    wrapped in `cmd /c` because Windows cannot exec them directly.
  - off by default (`mcp_client_enabled=false`) and closed to phone runs: the
    phone restriction gate rejects everything outside its safe set.
  - a broken server must never break a run: every failure comes back as a
    normal tool error string. Transport failures mark the connection dead;
    an HTTP 401/403 does not - a wrong token is a config mistake, and the
    connection must work again once the token is fixed.
  - tool names are `mcp_<server>_<tool>`, sanitized to [a-z0-9_], stable.

Protocol: JSON-RPC 2.0. stdio frames one JSON object per line; the HTTP
transport POSTs one request and reads the answer as JSON or an SSE stream.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import logging
import os
import random
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("hivemind.mcp_client")

PROTOCOL_VERSION = "2024-11-05"
def _client_info() -> dict:
    """Name and version for the handshake. The version is read from the engine
    so it cannot drift (a hardcoded "1.4.9" survived two releases)."""
    try:
        from server import HIVEMIND_VERSION as _ver
    except (ImportError, AttributeError):
        _ver = ""
    return {"name": "hivemind", "version": str(_ver or "")}


CLIENT_INFO = {"name": "hivemind", "version": ""}
INIT_TIMEOUT_S = 20.0
DEFAULT_TIMEOUT_S = 30.0
MAX_SERVERS = 8
MAX_TOOLS_PER_SERVER = 64
MAX_TOOL_PAGES = 10
MAX_OUTPUT_CHARS = 200_000
STDERR_KEEP = 4000
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_TOOL_NAME = 64
MAX_SCHEMA_CHARS = 8000
_NAME_OK = "abcdefghijklmnopqrstuvwxyz0123456789_"

# HTTP retry policy (Streamable HTTP only): up to MAX_HTTP_RETRIES retries
# after the first attempt, with these pauses before each retry. tools/call is
# the exception - see HttpConn._post_retry for what is retried there and why.
HTTP_RETRY_DELAYS_S = (0.5, 1.5, 3.0)
MAX_HTTP_RETRIES = 3
HTTP_RETRY_STATUSES = (502, 503, 504)
_HTTP_RETRYABLE = (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError)


class McpError(Exception):
    """Any MCP failure. Callers turn it into a tool error string."""


class McpAuthError(McpError):
    """The server rejected our auth (HTTP 401/403).

    A configuration mistake, not a transport failure: the connection must
    NOT be marked dead, so it keeps working once the token is fixed.
    """


class McpServerError(McpError):
    """The server answered a request with a JSON-RPC error object.

    Carries the error code so the handshake can recognise a protocol-version
    rejection (-32602) without parsing the message text.
    """

    def __init__(self, name: str, err: object) -> None:
        text = err.get("message") if isinstance(err, dict) else str(err)
        super().__init__(f"{name}: {text or 'request failed'}")
        self.code = err.get("code") if isinstance(err, dict) else None


def _settings(settings: dict | None = None) -> dict:
    """The one place that knows where settings come from.

    Callers may pass a dict (tests, the router) or nothing (tool layer), in
    which case the engine's settings are loaded. A failed import is not an
    error - the client simply stays disabled.
    """
    if isinstance(settings, dict):
        return settings
    try:
        from settings import load_settings
        return load_settings() or {}
    except (ImportError, OSError, ValueError):
        return {}


def _setting(settings: dict | None, key: str, default):
    loaded = _settings(settings)
    return loaded.get(key, default) if key in loaded else default


def _as_bool(value) -> bool:
    """`"false"` is a string, and bool("false") is True - be explicit."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off", "nein")
    return bool(value)


def _sanitize(text: str, limit: int = 24) -> str:
    out = "".join(c if c in _NAME_OK else "_" for c in str(text).lower())
    out = "_".join(p for p in out.split("_") if p)
    return (out or "x")[:limit]


def _tool_name(server: str, tool: str) -> str:
    """`mcp_<server>_<tool>`, never longer than 64 characters.

    OpenAI-compatible function names are capped at 64; 4 + 20 + 1 + 40 was 65
    and could make a provider reject the whole request.
    """
    name = f"mcp_{_sanitize(server, 20)}_{_sanitize(tool, 40)}"
    return name[:MAX_TOOL_NAME]


class ServerSpec:
    """One configured server. Validates lazily so a typo cannot break a run."""

    __slots__ = ("name", "command", "args", "env", "cwd", "url", "enabled",
                 "headers", "token_env")

    def __init__(self, raw: dict, index: int) -> None:
        self.name = str(raw.get("name") or f"server{index}")
        self.command = str(raw.get("command") or "").strip()
        args = raw.get("args")
        self.args = [str(a) for a in args] if isinstance(args, list) else []
        env = raw.get("env")
        self.env = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        self.cwd = str(raw.get("cwd") or "") or None
        # HTTP transport: a `url` entry speaks Streamable HTTP instead of
        # starting a process. Both shapes share the rest of the client.
        self.url = str(raw.get("url") or "").strip()
        # HTTP auth, ignored by stdio servers: `headers` go out verbatim,
        # `token_env` names an environment variable whose value becomes
        # `Authorization: Bearer <value>` (an explicit Authorization header
        # wins). The variable is resolved at start, never stored in the spec.
        headers = raw.get("headers")
        self.headers = ({str(k): str(v) for k, v in headers.items()}
                        if isinstance(headers, dict) else {})
        self.token_env = str(raw.get("token_env") or "").strip()
        self.enabled = _as_bool(raw.get("enabled", True))

    @property
    def kind(self) -> str:
        return "http" if self.url else "stdio"

    @property
    def has_auth(self) -> bool:
        """Whether auth is CONFIGURED. describe() may expose this flag, never
        a token value."""
        return bool(self.token_env) or _has_auth_header(self.headers)

    def argv(self) -> list[str]:
        if self.kind == "http":
            raise McpError("server uses 'url', not a process")
        if not self.command:
            raise McpError("server has no 'command'")
        exe = self.command
        if not Path(exe).is_absolute():
            found = shutil.which(exe)
            if not found:
                raise McpError(f"command not found: {self.command}")
            exe = found
        argv = [exe, *self.args]
        if sys.platform == "win32" and exe.lower().endswith((".cmd", ".bat")):
            # M5: cmd parses its command line. An argument like
            # https://x/?a=1&b=2 would split the command, and %VAR% would be
            # expanded. Quote every argument that holds a metacharacter, escape
            # the quote itself and always pass /d (no AutoRun).
            _META = set('&|<>^%!"()')
            args = []
            for a in self.args:
                if any(c in _META or c.isspace() for c in a):
                    a = '"' + a.replace('"', '\\"') + '"'
                args.append(a)
            argv = ["cmd", "/d", "/c", exe, *args]
        return argv

    def signature(self) -> tuple:
        """What identifies a LIVE connection. A reload with another signature
        restarts the server instead of keeping the old one (H4). The HTTP auth
        fields are part of it, so a token change rebuilds the connection."""
        return (self.kind, self.url, self.command, tuple(self.args),
                tuple(sorted(self.env.items())), self.cwd,
                tuple(sorted(self.headers.items())), self.token_env)

    def describe(self) -> dict:
        out = {"name": self.name, "enabled": self.enabled, "kind": self.kind}
        if self.kind == "http":
            out["url"] = self.url
            # the fact of auth, never the value: this dict lands in
            # GET /mcp/servers and is readable in the browser
            out["has_auth"] = self.has_auth
        else:
            out["command"] = self.command
            out["args"] = list(self.args)
        return out


def _has_auth_header(headers: dict) -> bool:
    """An explicit Authorization header (any casing). It wins over token_env,
    so a configured header must never be overwritten by a bearer token."""
    return any(str(k).lower() == "authorization" for k in headers)


def _resolve_bearer(spec: ServerSpec) -> str:
    """The value for `Authorization: Bearer ...`, or "" without token_env.

    A configured token_env whose variable is missing or empty raises: a
    silent unauthenticated start would only surface later as confusing
    401s at call time. An explicit Authorization in `headers` wins, so
    token_env is not even looked at there.
    """
    if not spec.token_env or _has_auth_header(spec.headers):
        return ""
    value = os.environ.get(spec.token_env, "").strip()
    if not value:
        raise McpError(
            f"{spec.name}: token_env '{spec.token_env}' is set but the "
            f"environment variable is empty or missing - not starting "
            f"unauthenticated")
    return value


def _auth_error(name: str, status: int) -> McpAuthError:
    """One message for every auth rejection: it names the server and what to
    check, and never contains the token itself."""
    return McpAuthError(
        f"{name}: HTTP {status} rejected the auth - the token is missing, "
        f"wrong or expired (check token_env / headers)")


class ServerConn:
    """A started server: process, JSON-RPC plumbing and its tool list."""

    def __init__(self, spec: ServerSpec, timeout_s: float) -> None:
        self.spec = spec
        self.timeout_s = timeout_s
        self._proc: asyncio.subprocess.Process | None = None
        self.loop = asyncio.get_running_loop()
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._stderr = ""
        self._buf = b""
        self._drainer: asyncio.Task | None = None
        self.tools: list[dict] = []
        self.dead = False
        self.last_error = ""
        # the protocol version the server accepted ("" until the handshake);
        # offered again on the next initialize instead of PROTOCOL_VERSION
        self.protocol_version = ""

    # -- process -------------------------------------------------------
    async def start(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            return
        argv = self.spec.argv()
        env = dict(os.environ)
        env.update(self.spec.env)
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.spec.cwd or None,
                env=env,
                # POSIX: own process group so the whole tree can be signalled.
                # Windows: no console window (the engine often runs under
                # pythonw) and a tree kill via taskkill.
                start_new_session=(sys.platform != "win32"),
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               if sys.platform == "win32" else 0),
            )
        except (OSError, ValueError) as exc:
            raise McpError(f"could not start {self.spec.name}: {exc}") from exc
        self._drainer = asyncio.create_task(self._drain_stderr())
        try:
            await self._handshake()
        except McpError:
            await self.close()
            raise

    async def _drain_stderr(self) -> None:
        """Keep the tail of stderr for error messages; never block the pipe."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.read(2048)
                if not chunk:
                    return
                self._stderr = (self._stderr + chunk.decode("utf-8", "replace"))[-STDERR_KEEP:]
        except (asyncio.CancelledError, OSError, ValueError):
            return

    async def _handshake(self) -> None:
        # the configured timeout wins, so a hanging server cannot hold a run
        # for the full 20 s default
        limit = min(INIT_TIMEOUT_S, self.timeout_s)
        await _handshake_initialize(self, limit)
        await self._notify("notifications/initialized")
        self.tools = await self._list_tools(limit)

    async def _list_tools(self, limit: float) -> list[dict]:
        """Every page of tools/list (MCP paginates with nextCursor)."""
        return await _collect_tools(self.request, limit)

    # -- json-rpc ------------------------------------------------------
    async def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise McpError(f"{self.spec.name} is not running")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except (OSError, ValueError) as exc:
            raise McpError(f"write to {self.spec.name} failed: {exc}") from exc

    async def _notify(self, method: str, params: dict | None = None) -> None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    def _next_request_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _die(self, reason: str) -> None:
        self.dead = True
        self.last_error = reason

    async def request(self, method: str, params: dict, *,
                      timeout: float | None = None) -> dict:
        """One request, one matching response. Raises McpError on any problem."""
        if self.dead:
            raise McpError(self.last_error or f"{self.spec.name} is marked dead")
        limit = float(timeout or self.timeout_s)
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=limit)
        except asyncio.TimeoutError:
            # M2: the connection is busy with another call. Do NOT mark it dead
            # (that would kill the other run's tool call as well).
            raise McpError(f"{self.spec.name} is busy with another call "
                           f"(waited {limit:.1f}s)") from None
        try:
            proc = self._proc
            if proc is None or proc.stdout is None or proc.returncode is not None:
                self._die(f"{self.spec.name} exited")
                raise McpError(f"{self.spec.name} is not running")
            rid = self._next_request_id()
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                              "params": params or {}})
            try:
                return await asyncio.wait_for(self._read_response(rid), timeout=limit)
            except asyncio.TimeoutError:
                self._die(f"{self.spec.name} timed out after {limit:.1f}s")
                await self._kill()
                raise McpError(f"{self.spec.name} timed out after {limit:.1f}s "
                               f"({method})") from None
            except McpError:
                raise
        finally:
            self._lock.release()

    async def _read_line(self) -> bytes | None:
        """One newline-terminated message, of ANY length.

        StreamReader.readline() raises ValueError once a line exceeds the
        64 KiB stream limit, so a large tool result (400k characters in the
        tests) killed the connection. Read chunks and split on the newline.
        """
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        while b"\n" not in self._buf:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                return None
            self._buf += chunk
            if len(self._buf) > MAX_LINE_BYTES:
                self._die(f"{self.spec.name} sent a message over "
                          f"{MAX_LINE_BYTES // 1024} KiB")
                await self._kill()
                raise McpError(f"{self.spec.name} sent a message that is too long")
        line, _sep, self._buf = self._buf.partition(b"\n")
        return line

    async def _read_response(self, rid: int) -> dict:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        skipped = 0
        while True:
            raw = await self._read_line()
            if raw is None:
                self._die(f"{self.spec.name} closed stdout")
                tail = self._stderr.strip().splitlines()[-3:]
                raise McpError(f"{self.spec.name} closed stdout"
                               + (f": {' | '.join(tail)}" if tail else ""))
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                skipped += 1
                if skipped > 50:
                    self._die(f"{self.spec.name} sent no valid JSON")
                    raise McpError(f"{self.spec.name} sent no valid JSON-RPC")
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") != rid:
                continue  # notification or a stale answer: ignore it
            if "error" in msg:
                raise McpServerError(self.spec.name, msg.get("error"))
            result = msg.get("result")
            return result if isinstance(result, dict) else {"value": result}

    # -- tools ---------------------------------------------------------
    async def call_tool(self, tool: str, arguments: dict,
                        timeout: float | None = None) -> str:
        result = await self.request("tools/call",
                                    {"name": tool, "arguments": arguments or {}},
                                    timeout=timeout)
        return _render_result(result)

    async def close(self) -> None:
        if self._drainer is not None:
            self._drainer.cancel()
            self._drainer = None
        await self._kill()

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        if self._drainer is not None:
            self._drainer.cancel()
            self._drainer = None
        if proc.returncode is None:
            # TREE-KILL (2026-10-26): npx, uvx and cmd /c start a grandchild.
            # Terminating only the direct child left node/python running and
            # holding the pipes, so a timeout leaked a process per call.
            _kill_tree(proc)
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
            return
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            pass


def _kill_tree(proc) -> None:
    """Best effort: kill the whole process tree of the child."""
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        pass


_VERSION_TOKEN = re.compile(r"\d{4}-\d{2}-\d{2}")


async def _handshake_initialize(conn: ServerConn | HttpConn, limit: float) -> None:
    """initialize with protocol-version negotiation, shared by both transports.

    The first offer is PROTOCOL_VERSION, or the version the server accepted
    on a previous connect (a reload must not re-run the rejection dance).
    Whatever version string the server answers with - including a newer one -
    is accepted and stored: MCP versions are downward compatible for tools.
    A server that rejects the offer (JSON-RPC error -32602, or a message
    naming protocolVersion) gets exactly ONE retry with the version its
    error advertised, else 2025-03-26. A negotiation that fails for good
    marks the connection dead, so a closed handshake cannot look healthy.
    """
    info = _client_info()
    if info["version"]:
        CLIENT_INFO["version"] = info["version"]

    def _params(version: str) -> dict:
        return {"protocolVersion": version, "capabilities": {},
                "clientInfo": CLIENT_INFO}

    offer = conn.protocol_version or PROTOCOL_VERSION
    try:
        result = await conn.request("initialize", _params(offer), timeout=limit)
    except McpError as exc:
        flat = re.sub(r"[^a-z0-9]", "", str(exc).lower())
        rejected = (getattr(exc, "code", None) == -32602
                    or "protocolversion" in flat)
        if not rejected:
            if not conn.dead:
                conn._die(str(exc))
            raise
        # the error may advertise the supported version(s); prefer the last
        # one named that differs from what we already tried
        tokens = [t for t in _VERSION_TOKEN.findall(str(exc)) if t != offer]
        retry_with = tokens[-1] if tokens else "2025-03-26"
        logger.info("[MCP] initialize retried with protocolVersion=%s",
                    retry_with)
        try:
            result = await conn.request("initialize", _params(retry_with),
                                        timeout=limit)
        except McpError as exc2:
            if not conn.dead:
                conn._die(str(exc2))
            raise
    version = result.get("protocolVersion")
    if isinstance(version, str) and version.strip():
        conn.protocol_version = version.strip()


class HttpConn:
    """One MCP server over Streamable HTTP (POST JSON-RPC, JSON or SSE back).

    Same surface as ServerConn so McpClient does not care which transport a
    server uses: tools / dead / last_error / spec / loop / timeout_s, start(),
    request(), call_tool(), close().
    """

    def __init__(self, spec: ServerSpec, timeout_s: float) -> None:
        self.spec = spec
        self.timeout_s = timeout_s
        self.loop = asyncio.get_running_loop()
        self.tools: list[dict] = []
        self.dead = False
        self.last_error = ""
        self._session_id = ""
        self._next_id = 0
        self._client: httpx.AsyncClient | None = None
        # the protocol version the server accepted ("" until the handshake)
        self.protocol_version = ""
        # resolved from token_env at start(); never logged, never exposed
        self._bearer = ""

    # -- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        url = self.spec.url
        if not url.lower().startswith(("http://", "https://")):
            raise McpError(f"{self.spec.name}: url must start with http:// or https://")
        # resolve the token BEFORE the first request: a token_env naming a
        # missing variable must fail the start, not send without auth
        try:
            self._bearer = _resolve_bearer(self.spec)
        except McpError as exc:
            self._die(str(exc))
            raise
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout_s,
                                             follow_redirects=True)
        try:
            await self._handshake()
        except McpError:
            await self.close()
            raise

    async def _handshake(self) -> None:
        limit = min(INIT_TIMEOUT_S, self.timeout_s)
        await _handshake_initialize(self, limit)
        await self._notify("notifications/initialized")
        self.tools = await _collect_tools(self.request, limit)

    async def close(self) -> None:
        self._session_id = ""
        if self._client is not None:
            try:
                await self._client.aclose()
            except (OSError, RuntimeError):
                pass
            self._client = None

    # -- json-rpc ------------------------------------------------------
    def _die(self, reason: str) -> None:
        self.dead = True
        self.last_error = reason

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream"}
        for key, value in self.spec.headers.items():
            h[key] = value
        # an explicit Authorization header wins over the token (see
        # _resolve_bearer), so the bearer is only added without one
        if self._bearer and not _has_auth_header(self.spec.headers):
            h["Authorization"] = f"Bearer {self._bearer}"
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    async def _post(self, payload: dict, timeout: float) -> httpx.Response:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
        return await self._client.post(self.spec.url, json=payload,
                                       headers=self._headers(), timeout=timeout)

    async def _post_retry(self, payload: dict, method: str,
                          timeout: float) -> httpx.Response:
        """POST with the HTTP retry policy; raises McpError on final failure.

        Retried: ConnectError, ReadTimeout, RemoteProtocolError and HTTP
        502/503/504. NOT for tools/call: a read timeout means the request was
        delivered and the server may already have executed the tool, so
        replaying it could run it twice - there only a ConnectError, which
        never reached the server, is retried. Pauses 0.5/1.5/3.0 s with
        +-20 % jitter, capped by the call timeout: when the remaining budget
        cannot afford the next pause, the last error wins. Every exhaustion
        marks the connection dead and names the attempt count.
        """
        deadline = self.loop.time() + float(timeout)

        def _pause_for(retry_no: int) -> float:
            # +-20 % jitter around the ladder rung for this retry
            rung = min(retry_no, len(HTTP_RETRY_DELAYS_S))
            return HTTP_RETRY_DELAYS_S[rung - 1] * random.uniform(0.8, 1.2)

        attempts = 0
        while True:
            attempts += 1
            try:
                resp = await self._post(payload,
                                        max(0.05, deadline - self.loop.time()))
            except _HTTP_RETRYABLE as exc:
                if method == "tools/call" and not isinstance(exc, httpx.ConnectError):
                    if isinstance(exc, httpx.ReadTimeout):
                        # the server may still be executing: ask it to stop,
                        # then report the failure as before (dead connection)
                        await self._cancel_request(payload.get("id"))
                        self._die(f"{self.spec.name} timed out after "
                                  f"{float(timeout):.1f}s")
                        raise McpError(f"{self.spec.name} timed out after "
                                       f"{float(timeout):.1f}s ({method})") from None
                    self._die(f"{self.spec.name} unreachable: {exc}")
                    raise McpError(f"{self.spec.name} unreachable: {exc}") from exc
                pause = _pause_for(attempts)
                if attempts > MAX_HTTP_RETRIES or \
                        deadline - self.loop.time() < pause:
                    self._die(f"{self.spec.name} unreachable after "
                              f"{attempts} attempts")
                    raise McpError(f"{self.spec.name} unreachable after "
                                   f"{attempts} attempts: {exc}") from exc
                await asyncio.sleep(pause)
                continue
            except httpx.TimeoutException:
                # ConnectTimeout, WriteTimeout, PoolTimeout: never retried
                self._die(f"{self.spec.name} timed out after {float(timeout):.1f}s")
                raise McpError(f"{self.spec.name} timed out after "
                               f"{float(timeout):.1f}s ({method})") from None
            except (httpx.HTTPError, OSError) as exc:
                self._die(f"{self.spec.name} unreachable: {exc}")
                raise McpError(f"{self.spec.name} unreachable: {exc}") from exc

            if resp.status_code in HTTP_RETRY_STATUSES and method != "tools/call":
                pause = _pause_for(attempts)
                if attempts > MAX_HTTP_RETRIES or \
                        deadline - self.loop.time() < pause:
                    self._die(f"{self.spec.name} answered HTTP "
                              f"{resp.status_code}")
                    raise McpError(f"{self.spec.name}: HTTP {resp.status_code} "
                                   f"after {attempts} attempts")
                await asyncio.sleep(pause)
                continue
            return resp

    async def _cancel_request(self, rid) -> None:
        """notifications/cancelled after a timed-out tool call (best effort).

        The call already failed, so an error here must never replace the
        real one; the short timeout keeps a stuck server from stretching
        the failure path. stdio cannot do this: its timed-out child is
        killed immediately, and a drain() on an unresponsive process could
        stall the timeout path.
        """
        if rid is None:
            return
        try:
            await self._post({"jsonrpc": "2.0",
                              "method": "notifications/cancelled",
                              "params": {"requestId": rid,
                                         "reason": "client timeout"}},
                             min(2.0, self.timeout_s))
        except (httpx.HTTPError, OSError) as exc:
            logger.info("[MCP] cancellation for %s failed: %s",
                        self.spec.name, exc)

    async def _notify(self, method: str, params: dict | None = None) -> None:
        body: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        resp = await self._post_retry(body, method,
                                      min(INIT_TIMEOUT_S, self.timeout_s))
        if resp.status_code in (401, 403):
            # an unauthorized notification must not pass silently: the whole
            # session would run on a rejected token
            raise _auth_error(self.spec.name, resp.status_code)

    async def request(self, method: str, params: dict, *,
                      timeout: float | None = None) -> dict:
        if self.dead:
            raise McpError(self.last_error or f"{self.spec.name} is marked dead")
        limit = float(timeout or self.timeout_s)
        self._next_id += 1
        rid = self._next_id
        payload = {"jsonrpc": "2.0", "id": rid, "method": method,
                   "params": params or {}}
        resp = await self._post_retry(payload, method, limit)

        sid = resp.headers.get("mcp-session-id")
        if sid:
            self._session_id = sid
        if resp.status_code in (401, 403):
            # AUTH, not transport: a wrong or missing token is a config
            # mistake. Marking the connection dead would keep it broken even
            # after the token is fixed, until the next reload.
            raise _auth_error(self.spec.name, resp.status_code)
        if resp.status_code >= 400:
            self._die(f"{self.spec.name} answered HTTP {resp.status_code}")
            raise McpError(f"{self.spec.name}: HTTP {resp.status_code}")
        ctype = resp.headers.get("content-type", "")
        if "text/event-stream" in ctype:
            msg = _sse_response(resp.text, rid)
        else:
            try:
                msg = resp.json()
            except ValueError:
                self._die(f"{self.spec.name} sent no JSON")
                raise McpError(f"{self.spec.name} sent no JSON") from None
        if not isinstance(msg, dict):
            raise McpError(f"{self.spec.name} sent an unexpected payload")
        if "error" in msg:
            raise McpServerError(self.spec.name, msg.get("error"))
        result = msg.get("result")
        return result if isinstance(result, dict) else {"value": result}

    async def call_tool(self, tool: str, arguments: dict,
                        timeout: float | None = None) -> str:
        result = await self.request("tools/call",
                                    {"name": tool, "arguments": arguments or {}},
                                    timeout=timeout)
        return _render_result(result)


def _sse_response(body: str, rid: int) -> dict:
    """Pick the JSON-RPC answer for `rid` out of an SSE body."""
    candidates: list[dict] = []
    for line in str(body or "").splitlines():
        if not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if not chunk:
            continue
        try:
            msg = json.loads(chunk)
        except ValueError:
            continue
        if isinstance(msg, dict):
            candidates.append(msg)
    for msg in candidates:
        if msg.get("id") == rid:
            return msg
    if candidates:
        return candidates[-1]
    raise McpError("SSE response carried no JSON-RPC message")


async def _collect_tools(request_fn, limit: float) -> list[dict]:
    """Every page of tools/list, up to MAX_TOOLS_PER_SERVER (shared)."""
    out: list[dict] = []
    cursor: str | None = None
    for _page in range(MAX_TOOL_PAGES):
        params = {"cursor": cursor} if cursor else {}
        result = await request_fn("tools/list", params, timeout=limit)
        page = result.get("tools") if isinstance(result, dict) else None
        for tool in page or []:
            if isinstance(tool, dict) and tool.get("name"):
                out.append(tool)
        if len(out) >= MAX_TOOLS_PER_SERVER:
            break
        nxt = result.get("nextCursor") if isinstance(result, dict) else None
        cursor = str(nxt) if isinstance(nxt, str) and nxt else None
        if not cursor:
            break
    return out[:MAX_TOOLS_PER_SERVER]

def _render_result(result: dict) -> str:
    """MCP content blocks -> plain text for the tool result."""
    blocks = result.get("content") if isinstance(result, dict) else None
    parts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif block.get("type") == "image":
                parts.append("[image content omitted]")
            else:
                parts.append(f"[{block.get('type') or 'unknown'} content omitted]")
    elif isinstance(result, dict) and isinstance(result.get("value"), str):
        parts.append(result["value"])
    text = "\n".join(p for p in parts if p).strip()
    if not text:
        text = "(no output)"
    if result.get("isError"):
        return f"[TOOL_ERROR: MCP_TOOL_ERROR]\n{text[:MAX_OUTPUT_CHARS]}"
    return text[:MAX_OUTPUT_CHARS]


class McpClient:
    """The configured servers, started on demand and cached."""

    def __init__(self) -> None:
        self._lifecycle = asyncio.Lock()
        # the settings of the last refresh: the reload tool must use the same
        # configuration, not whatever the engine's settings.json says now
        self._last_settings: dict | None = None
        self._server_errors: dict[str, str] = {}
        self._conns: dict[str, ServerConn] = {}
        # server name -> the protocol version its connection last accepted;
        # offered again when a reload has to rebuild the connection
        self._negotiated: dict[str, str] = {}
        self._defs: list[dict] | None = None
        self._names: dict[str, tuple[str, str]] = {}  # tool name -> (server, tool)

    # -- configuration ------------------------------------------------
    @staticmethod
    def _specs(settings: dict | None) -> list[ServerSpec]:
        raw = _setting(settings, "mcp_servers", [])
        if not isinstance(raw, list):
            return []
        out: list[ServerSpec] = []
        seen: set[str] = set()
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                continue
            spec = ServerSpec(entry, i)
            if not spec.enabled:
                continue  # disabled entries must not eat a slot (L4)
            if spec.name in seen:
                logger.info("[MCP] duplicate server name %s ignored", spec.name)
                continue
            if len(out) >= MAX_SERVERS:
                logger.info("[MCP] more than %d servers configured, %s ignored",
                            MAX_SERVERS, spec.name)
                continue
            seen.add(spec.name)
            out.append(spec)
        return out

    @staticmethod
    def enabled(settings: dict | None = None) -> bool:
        return _as_bool(_setting(settings, "mcp_client_enabled", False))

    @staticmethod
    def timeout_s(settings: dict | None = None) -> float:
        try:
            return max(1.0, float(_setting(settings, "mcp_client_timeout_s",
                                           DEFAULT_TIMEOUT_S)))
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT_S

    # -- lifecycle -----------------------------------------------------
    async def _conn(self, spec: ServerSpec, timeout_s: float):
        conn = self._conns.get(spec.name)
        if conn is not None:
            reuse = (not conn.dead
                     and conn.loop is asyncio.get_running_loop()
                     and conn.spec.signature() == spec.signature())
            if reuse:
                conn.timeout_s = timeout_s
                return conn
            # H4: command/args/env/cwd changed -> the old process is stale.
            # M4: a dead connection must be reaped, not just replaced.
            self._conns.pop(spec.name, None)
            await conn.close()
        conn = HttpConn(spec, timeout_s) if spec.kind == "http" else ServerConn(spec, timeout_s)
        # a server that already agreed on a version keeps it: the rebuilt
        # connection offers the stored protocol_version instead of the
        # default, so the handshake cannot re-run the rejection dance
        conn.protocol_version = self._negotiated.get(spec.name, "")
        self._conns[spec.name] = conn
        await conn.start()
        if conn.protocol_version:
            self._negotiated[spec.name] = conn.protocol_version
        return conn

    async def refresh(self, settings: dict | None = None) -> dict:
        """Start every enabled server and (re)build the tool definitions."""
        # M3: two overlapping refreshes (startup + a reload click) used to
        # interleave their cache writes.
        async with self._lifecycle:
            return await self._refresh_locked(settings)

    async def _refresh_locked(self, settings: dict | None = None) -> dict:
        settings = _settings(settings)
        self._last_settings = settings
        if not self.enabled(settings):
            await self.close_all()
            self._defs = []
            self._names = {}
            return {"enabled": False, "servers": []}
        timeout_s = self.timeout_s(settings)
        specs = self._specs(_settings(settings))
        # ORPHAN-CLOSE (2026-10-26): a server removed from the config (or set to
        # enabled:false) must not keep its child alive. Before, the process
        # stayed running until the engine shut down.
        wanted = {s.name for s in specs}
        for gone in [n for n in self._conns if n not in wanted]:
            conn = self._conns.pop(gone, None)
            self._negotiated.pop(gone, None)
            if conn is not None:
                try:
                    await conn.close()
                except (OSError, ValueError, McpError):
                    pass
                logger.info("[MCP] server %s left the config, process closed", gone)
        status: list[dict] = []
        self._defs = []
        self._names = {}
        for spec in specs:
            entry = spec.describe() | {"ok": False, "tools": 0, "error": "", "skipped": []}
            try:
                conn = await self._conn(spec, timeout_s)
            except McpError as exc:
                entry["error"] = str(exc)
                self._server_errors[spec.name] = str(exc)
                status.append(entry)
                continue
            self._server_errors.pop(spec.name, None)
            entry["ok"] = True
            entry["tools"] = len(conn.tools)
            status.append(entry)
            for tool in conn.tools:
                name = _tool_name(spec.name, tool["name"])
                if name in self._names:
                    # COLLISION (2026-10-26): two servers sanitize to the same
                    # name. Skip the later one instead of letting it silently
                    # own the call, and say so in the status.
                    other = self._names[name][0]
                    entry["skipped"].append(tool["name"])
                    logger.info("[MCP] tool name %s of %s collides with %s, "
                                "skipped", name, spec.name, other)
                    continue
                self._names[name] = (spec.name, tool["name"])
                self._defs.append(_tool_def(name, spec.name, tool))
        # RELOAD-TOOL (2026-10-27): the model can ask for a reload itself. Until
        # now only POST /mcp/reload could, so a server that died mid-run left
        # the model with "no such tool" and no way out.
        self._defs.append(RELOAD_TOOL_DEF)
        return {"enabled": True, "servers": status}

    async def close_all(self) -> None:
        # L6: the caches described tools that no longer exist after a close.
        self._defs = []
        self._names = {}
        self._negotiated.clear()
        conns = list(self._conns.values())
        self._conns = {}
        for conn in conns:
            try:
                await conn.close()
            except (OSError, ValueError, McpError):
                pass

    # -- status --------------------------------------------------------
    def status(self) -> list[dict]:
        """What is running right now, without touching a process."""
        return sorted(
            ({"name": name, "ok": not conn.dead, "tools": len(conn.tools),
              "error": conn.last_error}
             for name, conn in self._conns.items()),
            key=lambda entry: entry["name"])

    # -- tool layer ----------------------------------------------------
    def tool_defs(self) -> list[dict]:
        """OpenAI-style specs. Never starts a process: the caller refreshes."""
        if self._defs is None:
            return []
        return list(self._defs)

    async def call(self, name: str, arguments: dict,
                   timeout: float | None = None) -> str:
        if name == RELOAD_TOOL_NAME:
            try:
                status = await self.refresh(self._last_settings)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
                return f"[TOOL_ERROR: MCP_RELOAD_FAILED]\n{exc}"
            rows = status.get("servers") or []
            if not status.get("enabled"):
                return "[MCP] the client is disabled (settings: mcp_client_enabled)"
            lines = [f"- {r['name']}: {'ok, ' + str(r['tools']) + ' tool(s)' if r['ok'] else r['error'][:120]}"
                     for r in rows]
            return ("[MCP] reloaded.\n" + ("\n".join(lines) if lines else
                                          "no servers configured")
                    + f"\nAvailable tools: {len(self._defs) - 1}")
        target = self._names.get(name)
        if not target:
            hint = ""
            if self._server_errors:
                hint = " Server state: " + "; ".join(
                    f"{k}: {v}" for k, v in list(self._server_errors.items())[:3])
            return (f"[TOOL_ERROR: MCP_UNKNOWN_TOOL]\nNo MCP tool named '{name}'."
                    f"{hint} Ask the user to POST /mcp/reload after changing the "
                    f"server list.")
        server, tool = target
        conn = self._conns.get(server)
        if conn is None or conn.dead:
            return (f"[TOOL_ERROR: MCP_SERVER_DOWN]\n{server} is not running"
                    f"{': ' + conn.last_error if conn and conn.last_error else ''}. "
                    f"Fix the server config or disable it.")
        try:
            return await conn.call_tool(tool, arguments, timeout=timeout)
        except McpAuthError as exc:
            logger.info("[MCP] %s.%s failed: %s", server, tool, exc)
            return f"[TOOL_ERROR: MCP_AUTH_FAILED]\n{exc}"
        except McpError as exc:
            logger.info("[MCP] %s.%s failed: %s", server, tool, exc)
            return f"[TOOL_ERROR: MCP_CALL_FAILED]\n{exc}"


RELOAD_TOOL_NAME = "mcp_reload"
RELOAD_TOOL_DEF = {
    "type": "function",
    "function": {
        "name": RELOAD_TOOL_NAME,
        "description": ("[mcp] Restart the configured MCP servers and rebuild "
                        "the tool list. Use it when an MCP tool reports a dead "
                        "or missing server."),
        "parameters": {"type": "object", "properties": {}},
    },
}


def _tool_def(name: str, server: str, tool: dict) -> dict:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        schema = {"type": "object", "properties": {}}
    # L8: a broken server can send a schema of megabytes; it would blow up the
    # request. Keep 8k of JSON and drop the rest.
    if len(json.dumps(schema, ensure_ascii=False)) > MAX_SCHEMA_CHARS:
        logger.info("[MCP] schema of %s.%s is too large, truncated",
                    server, tool.get("name"))
        schema = {"type": "object", "properties": {},
                  "description": "(schema too large, parameters omitted)"}
    desc = str(tool.get("description") or "").strip()
    if len(desc) > 400:
        desc = desc[:400].rstrip() + "..."
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"[mcp:{server}] {desc}".strip(),
            "parameters": schema,
        },
    }


_client: McpClient | None = None


def get_client() -> McpClient:
    global _client
    if _client is None:
        _client = McpClient()
    return _client


def enabled(settings: dict | None = None) -> bool:
    """Module-level switch for the tool layer (mcp_client_enabled)."""
    return McpClient.enabled(settings)

def timeout_s(settings: dict | None = None) -> float:
    """Module-level timeout used by the status endpoint."""
    return McpClient.timeout_s(settings)


async def refresh(settings: dict | None = None) -> dict:
    return await get_client().refresh(settings)


async def close_all() -> None:
    await get_client().close_all()


def tool_defs(settings: dict | None = None) -> list[dict]:
    if not McpClient.enabled(settings or _settings()):
        return []
    return get_client().tool_defs()


def status() -> list[dict]:
    return get_client().status()


async def call(name: str, arguments: dict) -> str:
    return await get_client().call(name, arguments)
