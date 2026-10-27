"""MCP client (beta): use external MCP servers over stdio.

HiveMind already *is* an MCP server (`infra/mcp_server.py`). This module is the
other direction: it starts the servers the user configured and offers their
tools to the model.

Design rules (beta, deliberately small):
  - stdio transport only. One process per configured server, started on first
    use, killed on shutdown or after a timeout. No SSE, no HTTP transports.
  - `command` + `args` are executed WITHOUT a shell. Only `.cmd`/`.bat` are
    wrapped in `cmd /c` because Windows cannot exec them directly.
  - off by default (`mcp_client_enabled=false`) and closed to phone runs: the
    phone restriction gate rejects everything outside its safe set.
  - a broken server must never break a run: every failure comes back as a
    normal tool error string, the connection is marked dead and stays closed.
  - tool names are `mcp_<server>_<tool>`, sanitized to [a-z0-9_], stable.

Protocol: JSON-RPC 2.0, one JSON object per line (MCP stdio framing).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
MAX_OUTPUT_CHARS = 200_000
STDERR_KEEP = 4000
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_TOOL_NAME = 64
MAX_SCHEMA_CHARS = 8000
_NAME_OK = "abcdefghijklmnopqrstuvwxyz0123456789_"


class McpError(Exception):
    """Any MCP failure. Callers turn it into a tool error string."""


def _setting(settings: dict | None, key: str, default):
    try:
        if isinstance(settings, dict) and key in settings:
            return settings[key]
    except TypeError:
        pass
    return default


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

    __slots__ = ("name", "command", "args", "env", "cwd", "enabled")

    def __init__(self, raw: dict, index: int) -> None:
        self.name = str(raw.get("name") or f"server{index}")
        self.command = str(raw.get("command") or "").strip()
        args = raw.get("args")
        self.args = [str(a) for a in args] if isinstance(args, list) else []
        env = raw.get("env")
        self.env = {str(k): str(v) for k, v in env.items()} if isinstance(env, dict) else {}
        self.cwd = str(raw.get("cwd") or "") or None
        self.enabled = _as_bool(raw.get("enabled", True))

    def argv(self) -> list[str]:
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
        """What identifies a RUNNING process. A reload with another signature
        restarts the server instead of keeping the old one (H4)."""
        return (self.command, tuple(self.args),
                tuple(sorted(self.env.items())), self.cwd)

    def describe(self) -> dict:
        return {"name": self.name, "command": self.command,
                "args": list(self.args), "enabled": self.enabled}


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
        info = _client_info()
        if info["version"]:
            CLIENT_INFO["version"] = info["version"]
        await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }, timeout=limit)
        await self._notify("notifications/initialized")
        result = await self.request("tools/list", {}, timeout=limit)
        tools = result.get("tools") if isinstance(result, dict) else None
        self.tools = [t for t in (tools or []) if isinstance(t, dict) and t.get("name")]
        if len(self.tools) > MAX_TOOLS_PER_SERVER:
            self.tools = self.tools[:MAX_TOOLS_PER_SERVER]

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
                err = msg.get("error") or {}
                text = err.get("message") if isinstance(err, dict) else str(err)
                raise McpError(f"{self.spec.name}: {text or 'request failed'}")
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
        self._server_errors: dict[str, str] = {}
        self._conns: dict[str, ServerConn] = {}
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
        if settings is None:
            try:
                from settings import load_settings
                settings = load_settings() or {}
            except (ImportError, OSError, ValueError):
                return False
        return bool(_setting(settings, "mcp_client_enabled", False))

    @staticmethod
    def timeout_s(settings: dict | None = None) -> float:
        if settings is None:
            try:
                from settings import load_settings
                settings = load_settings() or {}
            except (ImportError, OSError, ValueError):
                settings = {}
        try:
            return max(1.0, float(_setting(settings, "mcp_client_timeout_s",
                                           DEFAULT_TIMEOUT_S)))
        except (TypeError, ValueError):
            return DEFAULT_TIMEOUT_S

    # -- lifecycle -----------------------------------------------------
    async def _conn(self, spec: ServerSpec, timeout_s: float) -> ServerConn:
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
        conn = ServerConn(spec, timeout_s)
        self._conns[spec.name] = conn
        await conn.start()
        return conn

    async def refresh(self, settings: dict | None = None) -> dict:
        """Start every enabled server and (re)build the tool definitions."""
        # M3: two overlapping refreshes (startup + a reload click) used to
        # interleave their cache writes.
        async with self._lifecycle:
            return await self._refresh_locked(settings)

    async def _refresh_locked(self, settings: dict | None = None) -> dict:
        settings = settings if settings is not None else _load()
        if not self.enabled(settings):
            await self.close_all()
            self._defs = []
            self._names = {}
            return {"enabled": False, "servers": []}
        timeout_s = self.timeout_s(settings)
        specs = self._specs(settings)
        # ORPHAN-CLOSE (2026-10-26): a server removed from the config (or set to
        # enabled:false) must not keep its child alive. Before, the process
        # stayed running until the engine shut down.
        wanted = {s.name for s in specs}
        for gone in [n for n in self._conns if n not in wanted]:
            conn = self._conns.pop(gone, None)
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
        return {"enabled": True, "servers": status}

    async def close_all(self) -> None:
        # L6: the caches described tools that no longer exist after a close.
        self._defs = []
        self._names = {}
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
        except McpError as exc:
            logger.info("[MCP] %s.%s failed: %s", server, tool, exc)
            return f"[TOOL_ERROR: MCP_CALL_FAILED]\n{exc}"


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


def _load() -> dict:
    try:
        from settings import load_settings
        return load_settings() or {}
    except (ImportError, OSError, ValueError):
        return {}


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
    if not McpClient.enabled(settings):
        return []
    return get_client().tool_defs()


def status() -> list[dict]:
    return get_client().status()


async def call(name: str, arguments: dict) -> str:
    return await get_client().call(name, arguments)
