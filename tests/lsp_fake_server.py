# -*- coding: utf-8 -*-
"""Tiny stdio LSP server for tests - stdlib only, Content-Length framing.

Implements exactly the subset infra/lsp_client.py needs: the initialize
handshake, didOpen/didChange with publishDiagnostics, definition,
references, hover, documentSymbol, workspace/symbol, rename, codeAction
and the shutdown/exit dance. Started as a subprocess by
tests/test_lsp_client.py; no real language server is required.

Modes (argv[1]):
  normal   - full feature set; publishes diagnostics 0.15 s after
             didOpen/didChange (small delay so the client's wait really
             waits, without making the suite slow)
  cfgprobe - on didOpen first sends workspace/configuration,
             client/registerCapability and window/workDoneProgress/create
             to the CLIENT and only publishes diagnostics once all three
             answers arrived - proves the client answers server->client
             requests instead of hanging them
  slowdiag - publishes diagnostics only after 0.6 s (tests the wait
             window: a short wait_s must return empty, a long one must not)
  hang     - initialize never answers (start timeout test)
  die      - exits during initialize (start failure test)
  noanswer - initialize works, textDocument/hover is never answered
             (request timeout test)
"""
import json
import sys
import time

MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
DIAG_DELAY_S = {"normal": 0.15, "slowdiag": 0.6}.get(MODE, 0.0)

state = {"shutdown": False, "texts": {}, "probe_id": 9000}
cfg_pending = set()
cfg_answered = 0


def read_message():
    """One framed message (header block + body), or None on EOF."""
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.rstrip(b"\r\n")
        if not stripped:
            break
        name, _sep, value = stripped.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    if not length:
        return None
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sys.stdin.buffer.read(length - len(chunks))
        if not chunk:
            return None
        chunks.extend(chunk)
    return json.loads(bytes(chunks).decode("utf-8"))


def send(payload):
    """One frame out - with a Content-Type header, so the client's header
    parser is exercised against multi-header blocks."""
    data = json.dumps(payload).encode("utf-8")
    head = (f"Content-Length: {len(data)}\r\n"
            f"Content-Type: application/vscode-jsonrpc; charset=utf-8\r\n\r\n")
    sys.stdout.buffer.write(head.encode("ascii") + data)
    sys.stdout.buffer.flush()


def reply(rid, result):
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def notify(method, params):
    send({"jsonrpc": "2.0", "method": method, "params": params})


def publish(uri):
    text = state["texts"].get(uri, "")
    name = str(uri).rsplit("/", 1)[-1]
    notify("textDocument/publishDiagnostics", {
        "uri": uri,
        "diagnostics": [{
            "range": {"start": {"line": 0, "character": 0},
                      "end": {"line": 0,
                              "character": min(5, max(1, len(text)))}},
            "severity": 1,
            "source": "fake-lsp",
            "message": f"fake diag {name}|{len(text)}"}]})


def publish_open(uri):
    """On didOpen: either probe the server->client request path first
    (cfgprobe) or publish after the mode's delay."""
    if MODE == "cfgprobe":
        for _method in ("workspace/configuration",
                        "client/registerCapability",
                        "window/workDoneProgress/create"):
            state["probe_id"] += 1
            pid = state["probe_id"]
            send({"jsonrpc": "2.0", "id": pid,
                  "method": _method,
                  "params": {"items": [{"section": "fake"}]}})
            cfg_pending.add(pid)
        return
    if DIAG_DELAY_S:
        time.sleep(DIAG_DELAY_S)
    publish(uri)


while True:
    msg = read_message()
    if msg is None:
        sys.exit(0)
    method = msg.get("method")
    rid = msg.get("id")
    if method is None:
        # a client answer to one of our probe requests
        if rid in cfg_pending:
            cfg_pending.discard(rid)
            cfg_answered += 1
            if not cfg_pending and cfg_answered >= 3:
                cfg_answered = 0
                for uri in state["texts"]:
                    publish(uri)
        continue
    params = msg.get("params") or {}
    if MODE == "noanswer" and method == "textDocument/hover":
        continue                     # never answered: request timeout test
    if method == "initialize":
        if MODE == "die":
            sys.exit(0)
        if MODE == "hang":
            time.sleep(60)
        root = str(params.get("rootUri") or "")
        if not root.startswith("file:") \
                or not isinstance(params.get("processId"), int):
            # the fake validates the client's handshake: a client that
            # sends no rootUri/processId fails its own start test
            send({"jsonrpc": "2.0", "id": rid, "error": {
                "code": -32602,
                "message": "bad initialize params (rootUri/processId)"}})
            continue
        reply(rid, {"capabilities": {"textDocumentSync": 1,
                                     "definitionProvider": True,
                                     "referencesProvider": True,
                                     "hoverProvider": True,
                                     "documentSymbolProvider": True,
                                     "workspaceSymbolProvider": True,
                                     "renameProvider": True,
                                     "codeActionProvider": True},
                     "serverInfo": {"name": "fake-lsp", "version": "0"}})
    elif method == "exit":
        sys.exit(0 if state["shutdown"] else 1)
    elif method == "shutdown":
        state["shutdown"] = True
        reply(rid, None)
    elif method in ("initialized", "textDocument/didSave",
                    "textDocument/didClose"):
        continue
    elif method == "textDocument/didOpen":
        doc = params.get("textDocument") or {}
        uri = doc.get("uri")
        state["texts"][uri] = doc.get("text", "")
        publish_open(uri)
    elif method == "textDocument/didChange":
        doc = params.get("textDocument") or {}
        uri = doc.get("uri")
        changes = params.get("contentChanges") or [{}]
        state["texts"][uri] = changes[0].get("text", "")
        if DIAG_DELAY_S:
            time.sleep(DIAG_DELAY_S)
        publish(uri)
    elif method == "textDocument/definition":
        pos = params.get("position") or {}
        line = pos.get("line") or 0
        reply(rid, {"uri": (params.get("textDocument") or {}).get("uri"),
                    "range": {"start": {"line": line + 1, "character": 0},
                              "end": {"line": line + 1, "character": 3}}})
    elif method == "textDocument/references":
        uri = (params.get("textDocument") or {}).get("uri")
        line = (params.get("position") or {}).get("line") or 0
        reply(rid, [
            {"uri": uri,
             "range": {"start": {"line": line + 1, "character": 0},
                       "end": {"line": line + 1, "character": 2}}},
            {"uri": uri,
             "range": {"start": {"line": line + 2, "character": 0},
                       "end": {"line": line + 2, "character": 2}}},
        ])
    elif method == "textDocument/hover":
        uri = (params.get("textDocument") or {}).get("uri")
        name = str(uri).rsplit("/", 1)[-1]
        reply(rid, {"contents": {"kind": "markdown",
                                 "value": f"fake hover {name}"},
                    "range": {"start": {"line": 0, "character": 0},
                              "end": {"line": 0, "character": 1}}})
    elif method == "textDocument/documentSymbol":
        uri = (params.get("textDocument") or {}).get("uri")
        rng = {"start": {"line": 0, "character": 0},
               "end": {"line": 1, "character": 0}}
        # one DocumentSymbol (hierarchical) and one SymbolInformation
        # (with location.uri) so both result shapes are decoded
        reply(rid, [
            {"name": "fake_func", "kind": 12,
             "range": rng, "selectionRange": rng},
            {"name": "fake_var", "kind": 6,
             "location": {"uri": uri, "range": rng}},
        ])
    elif method == "workspace/symbol":
        uri = next(iter(state["texts"]), "file:///fake")
        rng = {"start": {"line": 2, "character": 0},
               "end": {"line": 2, "character": 4}}
        reply(rid, [{"name": f"ws:{params.get('query', '')}", "kind": 12,
                     "location": {"uri": uri, "range": rng}}])
    elif method == "textDocument/rename":
        uri = (params.get("textDocument") or {}).get("uri")
        pos = params.get("position") or {}
        reply(rid, {"changes": {uri: [
            {"range": {"start": pos, "end": pos},
             "newText": params.get("newName", "")}]}})
    elif method == "textDocument/codeAction":
        uri = (params.get("textDocument") or {}).get("uri")
        ctx = params.get("context") or {}
        n = len(ctx.get("diagnostics") or [])
        rng = {"start": {"line": 0, "character": 0},
               "end": {"line": 0, "character": 1}}
        reply(rid, [{"title": f"fake quickfix ({n} diag)", "kind": "quickfix",
                     "edit": {"changes": {uri: [{"range": rng,
                                                 "newText": "fixed"}]}}}])
    elif method == "workspace/configuration":
        items = params.get("items") or []
        reply(rid, [None] * len(items))
    elif method in ("client/registerCapability",
                    "window/workDoneProgress/create"):
        reply(rid, None)
    elif rid is not None:
        send({"jsonrpc": "2.0", "id": rid,
              "error": {"code": -32601,
                        "message": f"fake: unknown method {method}"}})
