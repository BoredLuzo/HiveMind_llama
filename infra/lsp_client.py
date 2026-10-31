# -*- coding: utf-8 -*-
"""LSP client: drive any installed language server over stdio (JSON-RPC).

HiveMind already *is* an MCP server; this module is a broad, generic LSP
CLIENT so the agent can use the language servers the user already has
installed (pyright, gopls, rust-analyzer, clangd, ...). There are no
per-language feature flags: for every language a server command is
auto-detected via the registry (`infra/lsp_registry.py`), overridable as a
whole through settings["lsp_servers"] (language -> command string/list).

Design rules (2026-10-31, mirroring infra/mcp_client.py where sensible):
  - one process per (workspace, language), started LAZILY on first use and
    shut down on close_all(). A dead connection (timeout, crash) is reaped
    and transparently restarted on the next request - no reload step.
  - Content-Length framing per the LSP spec, one JSON-RPC message per
    frame. A broken server must never break a run: transport/protocol
    failures raise LspError, which the tool layer renders as a tool error.
  - documents are synced full-text (didOpen/didChange/didSave/didClose) -
    full sync is accepted by every server and needs no diff machinery.
  - publishDiagnostics are stored per URI; diagnostics() waits up to
    wait_s for a FRESH publication (a per-URI generation counter closes
    the race where the server publishes between didOpen and the wait).
  - server->client requests are answered (workspace/configuration with
    nulls, client/registerCapability, window/workDoneProgress/create,
    workspace/applyEdit declined) so servers do not hang on the handshake;
    progress and other notifications are ignored.
  - shutdown is robust: shutdown request, exit notification, then
    terminate/taskkill-tree/kill - no zombie processes on Windows.

Positions are 0-based LSP-native (line/character); the tool layer converts
from 1-based editor positions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from infra import lsp_registry as registry

logger = logging.getLogger("hivemind.lsp_client")

INIT_TIMEOUT_S = 20.0        # initialize handshake cap (a hung server must not hold a run)
DEFAULT_TIMEOUT_S = 30.0     # per-request cap unless settings say otherwise
SHUTDOWN_TIMEOUT_S = 3.0     # shutdown request / final exit grace
KILL_WAIT_S = 5.0            # wait for a cooperative exit after terminate
KILL_WAIT_HARD_S = 3.0       # wait after kill
MAX_MESSAGE_BYTES = 16 * 1024 * 1024   # one frame; a source file result is far below this
STDERR_KEEP = 4000           # stderr tail kept for error messages (see mcp_client)
MAX_DOC_CHARS = 4_000_000    # full-text sync cap: refuse absurd files instead of ballooning RAM
DIAG_POLL_S = 0.05           # diagnostics wait poll interval

# Declared client capabilities (2026-10-31): exactly what this client
# actually implements - full-sync text sync, the feature requests below,
# workspace/configuration + workspaceFolders, and workspace symbols.
# positionEncodings pins the LSP default (UTF-16 code units), so servers
# with LSP 3.17 support do not switch our offsets to UTF-8 bytes.
CLIENT_CAPABILITIES: dict = {
    "textDocument": {
        "synchronization": {"dynamicRegistration": False, "didSave": True,
                            "willSave": False, "willSaveWaitUntil": False},
        "publishDiagnostics": {"relatedInformation": True,
                               "versionSupport": True,
                               "codeDescriptionSupport": False,
                               "dataSupport": False},
        "hover": {"contentFormat": ["markdown", "plaintext"]},
        "definition": {"linkSupport": True},
        "references": {},
        "documentSymbol": {"symbolKind": {"valueSet": list(range(1, 27))},
                           "hierarchicalDocumentSymbolSupport": True},
        "rename": {"prepareProvider": False, "honorsChangeAnnotations": False},
        "codeAction": {"isPreferredSupport": True,
                       "codeActionLiteralSupport": {"codeActionKind": {
                           "valueSet": ["", "quickfix", "refactor",
                                        "refactor.extract", "refactor.inline",
                                        "refactor.rewrite", "source",
                                        "source.organizeImports"]}}},
    },
    "workspace": {
        "configuration": True,
        "workspaceFolders": True,
        "symbol": {"dynamicRegistration": False},
        "didChangeConfiguration": {"dynamicRegistration": False},
    },
    "general": {"positionEncodings": ["utf-16"]},
}


class LspError(Exception):
    """Any LSP client failure (transport, protocol, timeout). Callers turn
    it into a tool error string."""


class LspServerError(LspError):
    """The server answered a request with a JSON-RPC error object."""

    def __init__(self, language_id: str, err: object) -> None:
        text = err.get("message") if isinstance(err, dict) else str(err)
        super().__init__(f"{language_id}: {text or 'request failed'}")
        self.code = err.get("code") if isinstance(err, dict) else None


_VERSION: str | None = None


def _client_version() -> str:
    """Engine version for clientInfo, read lazily like mcp_client does so
    it cannot drift (a hardcoded version survived two releases there)."""
    global _VERSION
    if _VERSION is None:
        try:
            from server import HIVEMIND_VERSION as _ver
        except (ImportError, AttributeError):
            _ver = ""
        _VERSION = str(_ver or "")
    return _VERSION


# -- URIs ---------------------------------------------------------------

def path_to_uri(path: str | Path) -> str:
    """Local path -> file URI. as_uri() produces the correct
    file:///C:/... shape for Windows drive paths (and %XX-encodes spaces,
    umlauts and friends)."""
    return Path(str(path)).resolve().as_uri()


def uri_to_path(uri: str) -> str:
    """file URI -> local path (url2pathname knows Windows drive letters).
    Non-file schemes (untitled:, jdt:, ...) have no local path - they pass
    through unchanged so the caller still sees something useful."""
    parsed = urlparse(str(uri))
    if parsed.scheme != "file":
        return str(uri)
    raw = url2pathname(parsed.path)
    # Pyright encodes the drive colon ("file:///c%3A/..."), which
    # url2pathname turns into "\\c:\\...". Strip the extra separator so the
    # path is a real Windows drive path again.
    if sys.platform == "win32" and re.match(r"^[/\\][A-Za-z]:[/\\]", raw):
        raw = raw[1:]
    return raw


def canonical_uri(uri: str) -> str:
    """Canonical key for a document URI.

    Servers disagree on casing and encoding: pyright publishes
    ``file:///c%3A/Users/...`` while the client opens
    ``file:///C:/Users/...``. Both name the same file; without
    normalization the diagnostics land under a key nobody reads.
    Non-file schemes and unresolvable paths are returned unchanged."""
    text = str(uri or "")
    parsed = urlparse(text)
    if parsed.scheme != "file":
        return text
    try:
        raw = uri_to_path(text)
        path = Path(raw)
        if not path.is_absolute():
            return text
        return path_to_uri(path)
    except (OSError, ValueError):
        return text


# URI keys in LSP result objects -> the local-path key we return instead.
_URI_KEYS = {"uri": "path", "targetUri": "targetPath"}


def _decode_uris(obj):
    """Recursively replace LSP uri fields with local paths, so callers never
    deal with file:///C:/... strings (2026-10-31: definition, references,
    symbols and code actions all carry them; ranges stay untouched)."""
    if isinstance(obj, list):
        return [_decode_uris(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for key, value in obj.items():
            if key in _URI_KEYS and isinstance(value, str):
                out[_URI_KEYS[key]] = uri_to_path(value)
            else:
                out[key] = _decode_uris(value)
        return out
    return obj


def _decode_workspace_edit(edit):
    """Decode a rename WorkspaceEdit: the keys of `changes` are document
    URIs (not values), so they need their own pass; documentChanges entries
    carry the uri as a value and are covered by _decode_uris."""
    if not isinstance(edit, dict):
        return edit
    out = _decode_uris(edit)
    changes = out.get("changes")
    if isinstance(changes, dict):
        out["changes"] = {uri_to_path(k): v for k, v in changes.items()}
    return out


def _content_length(header: bytes) -> int | None:
    """Parse Content-Length out of a frame header block (case-insensitive,
    multiple headers tolerated - servers send Content-Type too)."""
    for line in header.split(b"\r\n"):
        if not line:
            continue
        name, _sep, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                return int(value.strip())
            except ValueError:
                return None
    return None


class _LspConnection:
    """One language server process: framing, dispatch, document state."""

    def __init__(self, language_id: str, argv: list[str], workspace: Path,
                 timeout_s: float) -> None:
        self.language_id = language_id
        self.argv = argv
        self.workspace = workspace
        self.timeout_s = timeout_s
        self.loop = asyncio.get_running_loop()
        self._proc: asyncio.subprocess.Process | None = None
        # per-client lifecycle lock: one request at a time, start/close
        # excluded by the manager's lifecycle lock
        self._lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._answer_tasks: set[asyncio.Task] = set()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_tail = ""
        # publishDiagnostics per URI + a per-URI generation counter so a
        # wait can tell "fresh publication for THIS document" from noise
        self.diagnostics: dict[str, list[dict]] = {}
        self._diag_gen: dict[str, int] = {}
        # Document version the publication was computed for (None when the
        # server does not send one). Lets collect_diagnostics ignore a
        # delayed publication for the pre-didChange text.
        self._diag_version: dict[str, int | None] = {}
        self.open_docs: dict[str, int] = {}   # uri -> sync version
        self._doc_text: dict[str, str] = {}
        self.init_params: dict = {}
        self.server_capabilities: dict = {}
        self.dead = False
        self.last_error = ""

    # -- process -------------------------------------------------------
    async def start(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace) if self.workspace.is_dir() else None,
                # POSIX: own process group so the whole tree can be signalled.
                # Windows: no console window (the engine often runs under
                # pythonw); the tree kill itself happens in _kill().
                start_new_session=(sys.platform != "win32"),
                creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0)
                               if sys.platform == "win32" else 0),
            )
        except (OSError, ValueError) as exc:
            raise LspError(f"could not start {self.language_id} server "
                           f"({' '.join(self.argv)}): {exc}") from exc
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        try:
            await self._initialize()
        except (LspError, asyncio.TimeoutError, OSError) as exc:
            await self.close()
            raise LspError(f"initializing {self.language_id} server failed: "
                           f"{exc}") from exc

    async def _initialize(self) -> None:
        root_uri = path_to_uri(self.workspace)
        self.init_params = {
            "processId": os.getpid(),
            "clientInfo": {"name": "hivemind", "version": _client_version()},
            "rootUri": root_uri,
            # deprecated since LSP 3.0, old servers still read it
            "rootPath": str(self.workspace),
            "workspaceFolders": [{"uri": root_uri,
                                  "name": self.workspace.name}],
            "initializationOptions": {},
            "capabilities": CLIENT_CAPABILITIES,
        }
        # the configured timeout wins, so a hung server cannot hold the
        # full 20 s default (same rule as the MCP handshake)
        limit = min(INIT_TIMEOUT_S, self.timeout_s)
        result = await self.request("initialize", self.init_params,
                                    timeout=limit)
        caps = result.get("capabilities") if isinstance(result, dict) else None
        self.server_capabilities = caps if isinstance(caps, dict) else {}
        await self.notify("initialized")

    async def _drain_stderr(self) -> None:
        """Keep stderr flowing (a full pipe stalls some servers) and log it
        at debug; the tail is kept for error messages."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while True:
                chunk = await proc.stderr.read(2048)
                if not chunk:
                    return
                text = chunk.decode("utf-8", "replace")
                self._stderr_tail = (self._stderr_tail + text)[-STDERR_KEEP:]
                logger.debug("[LSP:%s] stderr: %s", self.language_id,
                             text.rstrip())
        except (asyncio.CancelledError, OSError, ValueError):
            return

    # -- json-rpc over stdio -------------------------------------------
    async def _send(self, payload: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise LspError(f"{self.language_id} server is not running")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            # one frame = header block + JSON body, LSP Content-Length style
            proc.stdin.write(f"Content-Length: {len(data)}\r\n\r\n"
                             .encode("ascii") + data)
            await proc.stdin.drain()
        except (OSError, ValueError) as exc:
            raise LspError(f"write to {self.language_id} server failed: "
                           f"{exc}") from exc

    async def notify(self, method: str, params: dict | None = None) -> None:
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def request(self, method: str, params: dict | None = None,
                      timeout: float | None = None):
        """One request, one matching response. Raises LspError on any
        problem; a timeout marks the connection dead and kills the process
        (a late response must not be able to confuse the next request)."""
        if self.dead:
            raise LspError(self.last_error
                           or f"{self.language_id} server is marked dead")
        limit = float(timeout or self.timeout_s)
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=limit)
        except asyncio.TimeoutError:
            raise LspError(f"{self.language_id} server is busy with another "
                           f"request (waited {limit:.1f}s)") from None
        rid = None
        try:
            proc = self._proc
            if proc is None or proc.returncode is not None:
                self._die(f"{self.language_id} server exited")
                raise LspError(f"{self.language_id} server is not running")
            self._next_id += 1
            rid = self._next_id
            fut = self.loop.create_future()
            self._pending[rid] = fut
            await self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                              "params": params if params is not None else {}})
            try:
                return await asyncio.wait_for(fut, timeout=limit)
            except asyncio.TimeoutError:
                self._die(f"{self.language_id} server timed out after "
                          f"{limit:.1f}s ({method})")
                await self._kill()
                raise LspError(f"{self.language_id} server timed out after "
                               f"{limit:.1f}s ({method})") from None
        finally:
            if rid is not None:
                self._pending.pop(rid, None)
            self._lock.release()

    async def _read_loop(self) -> None:
        while True:
            msg = await self._read_message()
            if msg is None:
                # EOF or protocol garbage (already marked dead there):
                # fail pending requests NOW instead of letting them run
                # into their timeouts
                self._fail_pending()
                return
            self._dispatch(msg)

    async def _read_message(self) -> dict | None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return None
        try:
            header = await proc.stdout.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:
            return None                       # clean EOF: the server is gone
        except (asyncio.LimitOverrunError, ValueError):
            self._die(f"{self.language_id} server sent an oversized or "
                      f"malformed header block")
            return None
        except OSError:
            self._die(f"{self.language_id} server output pipe failed")
            return None
        length = _content_length(header)
        if length is None:
            self._die(f"{self.language_id} server sent a frame without "
                      f"Content-Length")
            return None
        if length > MAX_MESSAGE_BYTES:
            self._die(f"{self.language_id} server sent a frame over "
                      f"{MAX_MESSAGE_BYTES // (1024 * 1024)} MiB")
            return None
        try:
            body = await proc.stdout.readexactly(length)
        except (asyncio.IncompleteReadError, OSError):
            self._die(f"{self.language_id} server closed the stream "
                      f"mid-frame")
            return None
        try:
            msg = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            self._die(f"{self.language_id} server sent invalid JSON")
            return None
        if not isinstance(msg, dict):
            self._die(f"{self.language_id} server sent a non-object message")
            return None
        return msg

    def _dispatch(self, msg: dict) -> None:
        method = msg.get("method")
        if "id" in msg and method is not None:
            # server->client request: answered off the reader loop so a
            # slow answer never blocks incoming messages
            self._spawn(self._answer_server_request(msg))
        elif "id" in msg:
            self._resolve_response(msg)
        elif method == "textDocument/publishDiagnostics":
            params = msg.get("params")
            params = params if isinstance(params, dict) else {}
            uri = params.get("uri")
            diags = params.get("diagnostics")
            if isinstance(uri, str):
                uri = canonical_uri(uri)
                _ver = params.get("version")
                if isinstance(_ver, bool) or not isinstance(_ver, int):
                    _ver = None
                _prev = self._diag_version.get(uri)
                if _ver is not None and isinstance(_prev, int) and _ver < _prev:
                    # STALE-PUBLICATION (2026-10-31): a check started before
                    # the last didChange can land afterwards. Never let it
                    # overwrite the newer document's diagnostics or satisfy
                    # a freshness wait.
                    logger.debug("[LSP:%s] ignoring stale diagnostics v%s < v%s",
                                 self.language_id, _ver, _prev)
                    return
                self.diagnostics[uri] = ([d for d in diags if isinstance(d, dict)]
                                         if isinstance(diags, list) else [])
                if _ver is not None:
                    self._diag_version[uri] = _ver
                elif uri not in self._diag_version:
                    self._diag_version[uri] = None
                self._diag_gen[uri] = self._diag_gen.get(uri, 0) + 1
        # everything else ($/progress, window/logMessage, telemetry, ...)
        # is ignored by design

    def _resolve_response(self, msg: dict) -> None:
        rid = msg.get("id")
        key = None
        if isinstance(rid, bool):            # bool is an int subclass - never ours
            key = None
        elif isinstance(rid, int):
            key = rid
        elif isinstance(rid, str) and rid.strip().isdigit():
            key = int(rid.strip())           # a server echoing ids as strings
        fut = self._pending.pop(key, None) if key is not None else None
        if fut is None:
            logger.debug("[LSP:%s] dropping response for unknown id %r",
                         self.language_id, rid)
            return
        if "error" in msg:
            if not fut.done():
                fut.set_exception(LspServerError(self.language_id,
                                                  msg.get("error")))
            return
        if not fut.done():
            fut.set_result(msg.get("result"))

    async def _answer_server_request(self, msg: dict) -> None:
        """Answer the server->client requests this client supports. A null
        result is the spec-correct "no opinion" for registerCapability and
        workDoneProgress/create; configuration is answered with nulls
        matching params.items so servers get a well-formed answer without
        us inventing settings."""
        rid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "workspace/configuration":
            items = params.get("items")
            result = [None] * len(items) if isinstance(items, list) else None
        elif method in ("client/registerCapability",
                        "window/workDoneProgress/create"):
            result = None
        elif method == "workspace/applyEdit":
            # decline server-initiated edits: the agent owns the workspace
            result = {"applied": False,
                      "failureReason": "client does not apply "
                                       "server-initiated edits"}
        else:
            await self._try_send({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32601,
                                            "message": f"method not "
                                                       f"supported by the "
                                                       f"client: {method}"}})
            return
        await self._try_send({"jsonrpc": "2.0", "id": rid, "result": result})

    async def _try_send(self, payload: dict) -> None:
        try:
            await self._send(payload)
        except (LspError, OSError) as exc:
            logger.debug("[LSP:%s] answer dropped: %s", self.language_id, exc)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._answer_tasks.add(task)
        task.add_done_callback(self._answer_tasks.discard)

    def _die(self, reason: str) -> None:
        if not self.dead:
            logger.info("[LSP] %s: %s", self.language_id, reason)
        self.dead = True
        self.last_error = reason

    def _fail_pending(self) -> None:
        pending = list(self._pending.values())
        self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(LspError(
                    self.last_error
                    or f"{self.language_id} server is gone"))

    # -- documents -----------------------------------------------------
    def has_publication(self, uri: str) -> bool:
        """True after the server published diagnostics for this URI once."""
        return self._diag_gen.get(canonical_uri(uri), 0) > 0

    def _read_doc(self, path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LspError(f"cannot read {path}: {exc}") from exc
        if len(text) > MAX_DOC_CHARS:
            raise LspError(f"{path} is over {MAX_DOC_CHARS // 1024} Ki chars "
                           f"- too large to sync with a language server")
        return text

    async def ensure_open(self, path: Path, uri: str | None = None,
                          language_id: str | None = None) -> tuple[str, bool]:
        """didOpen on first use, didChange (+didSave) when the file changed
        on disk since. Full sync: the change carries the whole text.
        Returns (uri, changed) - changed=True iff a sync was sent, so the
        diagnostics wait only runs after an actual open/change.

        language_id overrides the document language for TSX/JSX
        (typescriptreact/javascriptreact) while the server connection stays
        keyed by the base language from the registry."""
        if uri is None:
            uri = path_to_uri(path)
        doc_language = language_id or self.language_id
        text = self._read_doc(path)
        if uri in self.open_docs:
            if text != self._doc_text.get(uri):
                version = self.open_docs[uri] + 1
                self.open_docs[uri] = version
                self._doc_text[uri] = text
                await self.notify("textDocument/didChange", {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}]})
                await self.notify("textDocument/didSave", {
                    "textDocument": {"uri": uri}, "text": text})
                return uri, True
            return uri, False
        self.open_docs[uri] = 1
        self._doc_text[uri] = text
        await self.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": doc_language,
                             "version": 1, "text": text}})
        return uri, True

    async def collect_diagnostics(self, path: Path, wait_s: float,
                                  language_id: str | None = None) -> list[dict]:
        """Open/refresh the document, then - only when a sync was actually
        sent - wait up to wait_s for a diagnostics publication NEWER than
        this call (per-URI generation counter: the publication may arrive
        between didOpen and the wait, it still counts as fresh). For an
        unchanged already-open document the stored diagnostics are current,
        so they return without waiting.

        Version-aware servers: a publication for an older document version
        is discarded in _dispatch; the wait additionally requires the
        expected version, which closes the pre-didChange race.
        """
        uri = canonical_uri(path_to_uri(path))
        gen0 = self._diag_gen.get(uri, 0)
        _uri, changed = await self.ensure_open(path, uri, language_id)
        if changed:
            expected = self.open_docs.get(uri)
            deadline = self.loop.time() + wait_s
            while self.loop.time() < deadline:
                if self._diag_gen.get(uri, 0) > gen0:
                    got = self._diag_version.get(uri)
                    if got is None or expected is None or got >= expected:
                        break
                await asyncio.sleep(DIAG_POLL_S)
        return [dict(d) for d in self.diagnostics.get(uri, [])]

    # -- shutdown ------------------------------------------------------
    async def close(self) -> None:
        """Robust shutdown: polite goodbye first (didClose, shutdown
        request, exit notification), then terminate/taskkill/kill so no
        zombie survives - on Windows via the tree kill."""
        proc = self._proc
        if proc is not None and proc.returncode is None and not self.dead:
            await self._goodbye()
        if self._reader_task is not None:
            self._reader_task.cancel()
            self._reader_task = None
        for task in list(self._answer_tasks):
            task.cancel()
        self._answer_tasks.clear()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            self._stderr_task = None
        await self._kill()

    async def _goodbye(self) -> None:
        """LSP-polite goodbye, best effort: the kill path owns the failure
        cases, so every error here is swallowed narrowly."""
        for uri in list(self.open_docs):
            try:
                await self.notify("textDocument/didClose",
                                  {"textDocument": {"uri": uri}})
            except (LspError, OSError):
                break     # a broken pipe means the process is already gone
        self.open_docs.clear()
        self._doc_text.clear()
        try:
            await self.request("shutdown", {}, timeout=SHUTDOWN_TIMEOUT_S)
        except (LspError, asyncio.TimeoutError, OSError):
            pass
        try:
            await self.notify("exit")
        except (LspError, OSError):
            pass
        proc = self._proc
        if proc is None:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=SHUTDOWN_TIMEOUT_S)
        except (asyncio.TimeoutError, OSError, ProcessLookupError):
            pass

    async def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except (OSError, ValueError):
            pass
        if proc.returncode is None:
            # TREE-KILL first (see _kill_tree): taskkill /T on Windows,
            # killpg on POSIX, then terminate() as the direct-child fallback
            await kill_tree(proc)
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_WAIT_S)
            return
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            pass
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=KILL_WAIT_HARD_S)
        except (asyncio.TimeoutError, ProcessLookupError, OSError):
            pass


async def kill_tree(proc) -> None:
    """Best effort kill of the whole child process tree.

    Mirrors infra/mcp_client._kill_tree (audit C6, 2026-10-31) but is kept
    local so the LSP client stays stdlib-only (mcp_client pulls httpx).
    taskkill /T reaches grandchildren (npx-style wrappers), and it runs via
    to_thread so its latency never freezes the event loop.
    """
    pid = getattr(proc, "pid", None)
    if not pid:
        return
    try:
        if sys.platform == "win32":
            await asyncio.to_thread(
                subprocess.run,
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (OSError, subprocess.SubprocessError, ProcessLookupError):
        pass


class LspManager:
    """Language servers for ONE workspace, started lazily per language.

    All positions are 0-based LSP-native (the tool layer converts from
    1-based). Feature methods raise LspError on transport/protocol/timeout
    failures - the tool layer renders those; only start_language() is
    exception-free (returns False) because a missing server is a normal
    state, not an error.
    """

    def __init__(self, workspace: str | Path,
                 settings: dict | None = None) -> None:
        ws = Path(str(workspace)).expanduser()
        if not ws.is_absolute():
            ws = Path.cwd() / ws
        self.workspace = ws.resolve()
        settings = settings if isinstance(settings, dict) else {}
        self._override = registry.normalize_override(
            settings.get("lsp_servers"))
        try:
            self._timeout_s = max(1.0, float(
                settings.get("lsp_client_timeout_s", DEFAULT_TIMEOUT_S)))
        except (TypeError, ValueError):
            self._timeout_s = DEFAULT_TIMEOUT_S
        self._lifecycle = asyncio.Lock()
        self._conns: dict[str, _LspConnection] = {}

    # -- connections ---------------------------------------------------
    async def _conn(self, language_id: str) -> _LspConnection:
        """The live connection for a language, started on first use. A dead
        connection is reaped and restarted here (lazy self-healing)."""
        async with self._lifecycle:
            conn = self._conns.get(language_id)
            if conn is not None:
                if not conn.dead:
                    return conn
                self._conns.pop(language_id, None)
                await conn.close()
            argv = registry.resolve(language_id, self._override)
            if argv is None:
                tried = registry.candidates(language_id, self._override)
                names = ", ".join(" ".join(c) for c in tried) or "none"
                raise LspError(f"no language server found for "
                               f"'{language_id}' (tried: {names})")
            conn = _LspConnection(language_id, argv, self.workspace,
                                  self._timeout_s)
            try:
                await conn.start()
            except LspError as exc:
                logger.info("[LSP] starting %s failed: %s", language_id, exc)
                raise
            self._conns[language_id] = conn
            logger.info("[LSP] started %s: %s", language_id,
                        " ".join(argv))
            return conn

    async def start_language(self, language_id: str) -> bool:
        """Start (or reuse) the server for a language. False - never an
        exception - when no command was found or the server failed to
        start; a missing server is a normal state for a status tool."""
        try:
            await self._conn(str(language_id).strip().lower())
        except LspError as exc:
            logger.info("[LSP] start_language(%s) failed: %s",
                        language_id, exc)
            return False
        return True

    def available(self) -> list[dict]:
        """One row per known language: language, command, found. Never
        starts a process - the status tool can call it freely."""
        return registry.available(dict(self._override))

    def is_started(self, language_id: str) -> bool:
        """True when a live server for this language is already running."""
        conn = self._conns.get(str(language_id).strip().lower())
        return conn is not None and not conn.dead

    def has_diagnostics(self, path) -> bool:
        """True when the server has published at least one diagnostics set
        for this document (empty set counts - the server did answer)."""
        p = self._resolve_path(path)
        lang = self._language_for(p)
        if lang is None:
            return False
        conn = self._conns.get(lang)
        if conn is None or conn.dead:
            return False
        return conn.has_publication(canonical_uri(path_to_uri(p)))

    def _resolve_path(self, path) -> Path:
        p = Path(str(path)).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        return p.resolve()

    def _language_for(self, path: Path) -> str | None:
        lang = registry.language_for_path(path)
        if lang is None:
            logger.info("[LSP] no language mapping for %s - request "
                        "answered empty", path)
        return lang

    async def _open_for(self, path) -> tuple[_LspConnection | None, str | None]:
        """Connection + open document URI for a path, or (None, None) when
        the extension has no language mapping."""
        p = self._resolve_path(path)
        lang = self._language_for(p)
        if lang is None:
            return None, None
        conn = await self._conn(lang)
        uri, _changed = await conn.ensure_open(
            p, language_id=registry.document_language_for_path(p))
        return conn, uri

    # -- features ------------------------------------------------------
    async def diagnostics(self, path, wait_s: float = 2.0) -> list[dict]:
        """The diagnostics for one document, waiting up to wait_s for the
        server's publication after open/change. Unknown extension -> []."""
        p = self._resolve_path(path)
        lang = self._language_for(p)
        if lang is None:
            return []
        conn = await self._conn(lang)
        return await conn.collect_diagnostics(
            p, max(0.0, float(wait_s)),
            language_id=registry.document_language_for_path(p))

    async def definition(self, path, line: int, character: int) -> list[dict]:
        conn, uri = await self._open_for(path)
        if conn is None or uri is None:
            return []
        result = await conn.request("textDocument/definition", {
            "textDocument": {"uri": uri},
            "position": {"line": int(line), "character": int(character)}})
        # LSP allows Location | Location[] | LocationLink[] | null
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list):
            return []
        return [_decode_uris(x) for x in result if isinstance(x, dict)]

    async def references(self, path, line: int, character: int,
                         include_declaration: bool = True) -> list[dict]:
        conn, uri = await self._open_for(path)
        if conn is None or uri is None:
            return []
        result = await conn.request("textDocument/references", {
            "textDocument": {"uri": uri},
            "position": {"line": int(line), "character": int(character)},
            "context": {"includeDeclaration": bool(include_declaration)}})
        if not isinstance(result, list):
            return []
        return [_decode_uris(x) for x in result if isinstance(x, dict)]

    async def hover(self, path, line: int, character: int) -> dict | None:
        conn, uri = await self._open_for(path)
        if conn is None or uri is None:
            return None
        result = await conn.request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": int(line), "character": int(character)}})
        return _decode_uris(result) if isinstance(result, dict) else None

    async def document_symbols(self, path) -> list[dict]:
        conn, uri = await self._open_for(path)
        if conn is None or uri is None:
            return []
        result = await conn.request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri}})
        if not isinstance(result, list):
            return []
        return [_decode_uris(x) for x in result if isinstance(x, dict)]

    async def workspace_symbols(self, query: str) -> list[dict]:
        """workspace/symbol needs SOME server, not a document: prefer one
        that is already running, else start in preference order (python
        first - HiveMind's home turf - then the rest alphabetically)."""
        conn = await self._conn_for_workspace_query()
        if conn is None:
            return []
        result = await conn.request("workspace/symbol",
                                    {"query": str(query or "")})
        if not isinstance(result, list):
            return []
        return [_decode_uris(x) for x in result if isinstance(x, dict)]

    async def _conn_for_workspace_query(self) -> _LspConnection | None:
        async with self._lifecycle:
            for conn in self._conns.values():
                if not conn.dead:
                    return conn
        order = ["python"] + sorted(k for k in registry.SERVER_CANDIDATES
                                    if k != "python")
        for lang in order:
            try:
                return await self._conn(lang)
            except LspError:
                continue      # not installed - try the next language
        return None

    async def rename(self, path, line: int, character: int,
                     new_name: str) -> dict | None:
        conn, uri = await self._open_for(path)
        if conn is None or uri is None:
            return None
        result = await conn.request("textDocument/rename", {
            "textDocument": {"uri": uri},
            "position": {"line": int(line), "character": int(character)},
            "newName": str(new_name)})
        # null = the server refuses the rename (e.g. not a symbol)
        return _decode_workspace_edit(result) if isinstance(result, dict) else None

    async def code_actions(self, path, start_line: int, start_char: int,
                           end_line: int, end_char: int) -> list[dict]:
        conn, uri = await self._open_for(path)
        if conn is None or uri is None:
            return []
        # the stored diagnostics go into the context: quickfixes are
        # computed FROM the diagnostics the editor would show
        result = await conn.request("textDocument/codeAction", {
            "textDocument": {"uri": uri},
            "range": {"start": {"line": int(start_line),
                                "character": int(start_char)},
                      "end": {"line": int(end_line),
                              "character": int(end_char)}},
            "context": {"diagnostics": list(conn.diagnostics.get(uri, []))}})
        if not isinstance(result, list):
            return []
        return [_decode_uris(x) for x in result if isinstance(x, dict)]

    # -- shutdown ------------------------------------------------------
    async def close_all(self) -> None:
        """Shut every running language server down (engine exit, tests)."""
        async with self._lifecycle:
            conns = list(self._conns.values())
            self._conns = {}
        for conn in conns:
            try:
                await conn.close()
            except (OSError, LspError) as exc:
                logger.info("[LSP] closing %s failed: %s",
                            conn.language_id, exc)
