# -*- coding: utf-8 -*-
"""Generate ready-to-paste MCP client configs for HiveMind's OWN MCP server.

Connects an IDE (Claude Desktop, Cline, Zed, VS Code) to the stdio transport
of `infra/mcp_server.py` - the same server start_mcp.bat starts, but as a
child process of the IDE instead of a separate window. The command line is
replicated from start_mcp.bat with absolute paths resolved from THIS
checkout (the script lives in the repo, everything is derived via __file__):

    set "PY=<repo>\\.venv\\Scripts\\python.exe"   (fallback: bare `python`)
    "%PY%" infra/mcp_server.py                    (stdio transport)

start_mcp.bat additionally passes `--http --port 8090`; that is the
HTTP-only transport (an IDE client connecting over stdio would hang), so a
stdio config omits both flags. The port plays no role for stdio anyway -
the child reaches the engine on its own, `server_port` is read from
settings.json exactly like start_hivemind.bat does (shown in the summary
below for orientation only, never pinned into the config).

The generated `env` block carries `HIVEMIND_INTERNAL_TOKEN`, which the
server uses to authenticate its proxy tools against the running engine.
By default only a `<HIVEMIND_INTERNAL_TOKEN>` placeholder is written -
NEVER a real token. `--include-secret` embeds the value of the
HIVEMIND_INTERNAL_TOKEN environment variable. settings.json secrets are
never read into the output at all.

Usage:
    python tools/mcp_config_gen.py                     # summary + all formats
    python tools/mcp_config_gen.py --format vscode     # one format, JSON only
    python tools/mcp_config_gen.py --format vscode --out .vscode/mcp.json
    python tools/mcp_config_gen.py --check             # validate JSON output
    python tools/mcp_config_gen.py --format zed --include-secret
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_SCRIPT = REPO_ROOT / "infra" / "mcp_server.py"
SETTINGS_FILE = REPO_ROOT / "settings.json"

SERVER_NAME = "hivemind"
TOKEN_ENV = "HIVEMIND_INTERNAL_TOKEN"
TOKEN_PLACEHOLDER = "<HIVEMIND_INTERNAL_TOKEN>"

FORMATS = ("claude_desktop", "cline", "zed", "vscode")

# file each format is pasted into - used for the labels of the all-formats dump
_FORMAT_LABELS = {
    "claude_desktop": "claude_desktop_config.json",
    "cline": ".mcp.json (Cline / Continue / Cursor style)",
    "zed": "Zed settings.json (context_servers entry)",
    "vscode": ".vscode/mcp.json",
}


def _venv_python() -> str:
    """The python start_mcp.bat would use: the project .venv first, then a
    bare `python`. Both start scripts resolve the venv first because a bare
    `python` may lack the dependencies; the .venv of THIS checkout wins."""
    for rel in (Path(".venv") / "Scripts" / "python.exe",
                Path(".venv") / "bin" / "python"):
        cand = REPO_ROOT / rel
        if cand.is_file():
            return str(cand)
    return "python"


def _engine_port() -> int | None:
    """`server_port` from settings.json (the way start_hivemind.bat resolves
    it). Only shown in the human summary - the stdio child derives the engine
    URL itself, so a stale pin can never hide a port change. Missing or
    broken file -> None."""
    try:
        cfg = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    try:
        port = int(cfg.get("server_port") or 0)
    except (TypeError, ValueError):
        return None
    return port or None


def _token_value(include_secret: bool) -> str:
    """The token to write into the env block: the real value of the
    HIVEMIND_INTERNAL_TOKEN environment variable only with
    --include-secret, else the placeholder. A missing variable with
    --include-secret keeps the placeholder (nothing real to embed)."""
    if include_secret:
        value = os.environ.get(TOKEN_ENV, "").strip()
        if value:
            return value
        print(f"[mcp-config-gen] {TOKEN_ENV} is not set - keeping the "
              f"placeholder", file=sys.stderr)
    return TOKEN_PLACEHOLDER


def server_entry(include_secret: bool = False) -> dict:
    """The mcpServers entry shared by every format (command/args/env)."""
    return {
        "command": _venv_python(),
        "args": [str(SERVER_SCRIPT)],
        "env": {TOKEN_ENV: _token_value(include_secret)},
    }


def build(format_name: str, include_secret: bool = False) -> str:
    """One format as a JSON string (a full document, valid on its own)."""
    entry = server_entry(include_secret)
    if format_name == "claude_desktop":
        doc = {"mcpServers": {SERVER_NAME: entry}}
    elif format_name == "cline":
        doc = {"mcpServers": {SERVER_NAME: dict(entry, type="stdio")}}
    elif format_name == "zed":
        doc = {"context_servers": {SERVER_NAME: dict(entry, source="custom")}}
    elif format_name == "vscode":
        doc = {"servers": {SERVER_NAME: dict(entry, type="stdio")}}
    else:
        raise ValueError(f"unknown format: {format_name}")
    return json.dumps(doc, indent=2, ensure_ascii=False)


def summary_lines() -> list[str]:
    """Human orientation for the all-formats dump (stdout, not JSON)."""
    port = _engine_port()
    engine = f"http://localhost:{port}" if port else \
        "http://localhost:8001 (settings.json server_port not found)"
    return [
        f"# HiveMind MCP client config - checkout: {REPO_ROOT}",
        f"# stdio command : {_venv_python()} {SERVER_SCRIPT}",
        f"# engine        : {engine} (the server reads it itself; stdio needs no port)",
        f"# token         : env {TOKEN_ENV} -> '{TOKEN_PLACEHOLDER}' placeholder,"
        f" real value only with --include-secret",
    ]


def _write_out(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate MCP client configs for HiveMind's own MCP "
                    "server (stdio).")
    parser.add_argument("--format", choices=FORMATS, default=None,
                        help="emit only this format (default: all, labeled)")
    parser.add_argument("--out", default=None, metavar="PATH",
                        help="write the selected format to this file "
                             "(requires --format)")
    parser.add_argument("--include-secret", action="store_true",
                        help=f"embed the real {TOKEN_ENV} from the "
                             f"environment instead of the placeholder")
    parser.add_argument("--check", action="store_true",
                        help="validate that the generated output parses as "
                             "JSON, then exit 0/1")
    args = parser.parse_args(argv)

    if args.out and not args.format:
        parser.error("--out requires --format")
    if not SERVER_SCRIPT.is_file():
        print(f"[mcp-config-gen] WARNING: {SERVER_SCRIPT} does not exist in "
              f"this checkout - the generated config will not start",
              file=sys.stderr)

    wanted = [args.format] if args.format else list(FORMATS)
    outputs = {fmt: build(fmt, include_secret=args.include_secret)
               for fmt in wanted}

    if args.check:
        ok = True
        for fmt, text in outputs.items():
            try:
                json.loads(text)
                print(f"OK {fmt}: parses as JSON ({len(text)} bytes)")
            except ValueError as exc:
                ok = False
                print(f"FAIL {fmt}: {exc}")
        return 0 if ok else 1

    if args.out:
        _write_out(Path(args.out), outputs[args.format])
        print(f"[mcp-config-gen] wrote {args.out} ({args.format})",
              file=sys.stderr)
        return 0

    if args.format:
        print(outputs[args.format])
        return 0

    for line in summary_lines():
        print(line)
    print()
    for fmt in FORMATS:
        print(f"== {fmt}  ({_FORMAT_LABELS[fmt]}) ==")
        print(build(fmt, include_secret=args.include_secret))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
