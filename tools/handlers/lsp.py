# -*- coding: utf-8 -*-
"""Tool handlers: LSP tools (language servers via infra/lsp_client).

LSP-TOOLS (2026-10-31): one broad, generic surface over whatever language
servers the user has installed. There are NO per-language on/off flags:
  - settings["lsp_enabled"] is the only switch (False -> LSP_DISABLED);
  - a server process starts LAZILY on the first lsp_* tool call for its
    files - never at boot, and lsp_status never starts one at all;
  - a language without a found server command answers LSP_UNAVAILABLE with
    an install hint instead of silently degrading.

One LspManager per (workspace, event loop) is kept alive across calls, so
the started servers persist for the engine's lifetime; close_all() is only
for engine exit, not per call. Every manager feature method can raise
LspError - this layer renders those as tool errors, so a broken server can
never break a run (mirrors the MCP client's error contract).

All tool positions are 1-BASED (editor convention); the LSP-native 0-based
conversion happens here, not in the model's head.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from infra import lsp_client as _lsp
from infra import lsp_registry as _registry
from tools.errors import tool_error_response as _tool_error_response
from utils.file import _inline_check_workspace, _inline_resolve_path

logger = logging.getLogger("hivemind.tools")

# ── Settings ─────────────────────────────────────────────────────────────────
# Fallbacks for contexts without the engine's live settings dict (unit tests,
# direct API calls). Keep the values in sync with settings.DEFAULT_SETTINGS
# (2026-10-31); the engine always has them merged.
_LSP_FALLBACKS: dict = {
    "lsp_enabled": True,
    "lsp_servers": {},
    "lsp_client_timeout_s": 30,
    "lsp_diagnostics_wait_s": 2.0,
    "lsp_max_diagnostics": 50,
    "lsp_max_results": 50,
}


def _lsp_settings() -> dict:
    """The LSP-relevant settings: live values (core.state.settings) with the
    defaults above as fallback."""
    try:
        from core.state import settings as _live
        live = _live if isinstance(_live, dict) else {}
    except (ImportError, AttributeError, TypeError):
        live = {}
    return {key: live.get(key, _LSP_FALLBACKS[key]) for key in _LSP_FALLBACKS}


def _lsp_enabled() -> bool:
    """The single switch. Tolerates the string forms a settings.json can
    carry (same rule as git_tools_enabled)."""
    raw = _lsp_settings().get("lsp_enabled", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in ("0", "false", "off", "no")
    return bool(raw)


def _coerce_float(value, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return float(default)


def _coerce_int(value, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        n = int(default)
    return max(lo, min(hi, n))


# ── Manager cache: one per (workspace, event loop) ───────────────────────────
# The engine has ONE persistent loop, so its manager - and with it every
# started language server - lives for the process lifetime. Other loops
# (asyncio.run in tests/tools) get their own manager, which keeps the
# manager's asyncio.Lock objects loop-correct.
_MANAGERS: dict = {}
_MANAGERS_CAP = 8


def _get_manager(workspace: Path):
    """The cached LspManager for this workspace on THIS running loop.

    infra.lsp_client.LspManager is looked up at call time (not bound at
    import), so tests can swap the class on the module."""
    loop = asyncio.get_running_loop()
    try:
        ws_key = str(Path(workspace).resolve())
    except OSError:
        ws_key = str(workspace)
    key = (ws_key.lower() if os.name == "nt" else ws_key, id(loop))
    mgr = _MANAGERS.get(key)
    if mgr is None:
        settings = _lsp_settings()
        mgr = _lsp.LspManager(workspace, {
            "lsp_servers": settings.get("lsp_servers") or {},
            "lsp_client_timeout_s": settings.get("lsp_client_timeout_s"),
        })
        if len(_MANAGERS) >= _MANAGERS_CAP:
            _MANAGERS.clear()   # a stale loop's manager is dead weight only
        _MANAGERS[key] = mgr
    return mgr


# ── Error rendering ──────────────────────────────────────────────────────────
# Install hints per language (2026-10-31). Presentation state for the
# LSP_UNAVAILABLE message only - NOT a per-language flag: a language without
# an entry simply gets the generic advice.
_INSTALL_HINTS: dict = {
    "python": "pip install pyright  (or: pip install python-lsp-server)",
    "typescript": "npm install -g typescript typescript-language-server",
    "javascript": "npm install -g typescript typescript-language-server",
    "go": "go install golang.org/x/tools/gopls@latest",
    "rust": "rustup component add rust-analyzer",
    "c": "install clangd (e.g. apt install clangd / winget install LLVM.LLVM)",
    "cpp": "install clangd (e.g. apt install clangd / winget install LLVM.LLVM)",
    "bash": "npm install -g bash-language-server",
    "lua": "install lua-language-server (e.g. luarocks install lua-language-server)",
    "json": "npm install -g vscode-langservers-extracted",
    "yaml": "npm install -g yaml-language-server",
    "html": "npm install -g vscode-langservers-extracted",
    "css": "npm install -g vscode-langservers-extracted",
}


def _err_disabled(tool: str) -> str:
    return _tool_error_response(
        "LSP_DISABLED",
        "The LSP tools are switched off (settings: lsp_enabled=false). "
        "Enable lsp_enabled to use language-server features.",
        tool=tool)


def _err_unavailable(tool: str, language: str | None = None) -> str:
    """The clear 'server missing' answer: what was tried, how to install it
    and where the override lives."""
    override = _lsp_settings().get("lsp_servers") or {}
    if language:
        tried = "; ".join(" ".join(c)
                          for c in _registry.candidates(language, override))
        tried = tried or "(none)"
        hint = _INSTALL_HINTS.get(str(language),
                                  "install a language server for it")
        detail = (f"No language server found for '{language}' "
                  f"(tried: {tried}). Install one, e.g.: {hint}. ")
    else:
        detail = ("No language server found for any known language. "
                  "Install one, e.g.: pip install pyright (python) or "
                  "npm install -g typescript typescript-language-server "
                  "(ts/js). ")
    return _tool_error_response(
        "LSP_UNAVAILABLE",
        detail + "Run lsp_status for the full availability list; "
                 "settings['lsp_servers'] can point at a custom command.",
        tool=tool)


def _err_failed(tool: str, exc: Exception) -> str:
    return _tool_error_response(
        "LSP_FAILED",
        f"{tool}: language-server request failed: {exc}",
        tool=tool)


class _ArgError(ValueError):
    """Invalid tool argument, rendered as INVALID_ARGUMENT by the caller."""


# ── Shared preparation ───────────────────────────────────────────────────────
async def _prepare(tool: str, args: dict, workspace: Path, workspace_lock):
    """enabled gate -> workspace path -> language -> running server.

    Returns (mgr, path, language, None) to continue, or
    (None, None, None, result_string) when the call ends here (disabled,
    bad path, unmapped extension, missing server)."""
    if not _lsp_enabled():
        return None, None, None, _err_disabled(tool)
    raw = str((args or {}).get("path", "") or "").strip()
    if not raw:
        return None, None, None, _tool_error_response(
            "INVALID_ARGUMENT", f"{tool} requires a file path.", tool=tool)
    p = _inline_resolve_path(workspace, raw)
    if err := _inline_check_workspace(p, workspace_lock, tool):
        return None, None, None, err
    language = _registry.language_for_path(p)
    if language is None:
        suffix = p.suffix or "(no extension)"
        return None, None, None, (
            f"(no language server mapping for '{suffix}' - the lsp tools "
            f"cover source files; nothing to do)")
    mgr = _get_manager(workspace)
    if not await mgr.start_language(language):
        return None, None, None, _err_unavailable(tool, language)
    return mgr, p, language, None


def _one_based(args: dict, key: str, tool: str) -> int:
    """Read a 1-based position argument. 0/negative clamps to 1 so an
    off-by-one model call lands on the first line instead of erroring."""
    raw = (args or {}).get(key, 1)
    try:
        val = int(float(raw))
    except (TypeError, ValueError):
        raise _ArgError(f"{tool} requires an integer {key} (1-based).") \
            from None
    return max(1, val)


def _positions(args: dict, tool: str, keys: tuple) -> list[int]:
    """All position arguments of one call, 1-based. Raises _ArgError."""
    return [_one_based(args, k, tool) for k in keys]


# ── Formatting helpers ───────────────────────────────────────────────────────
def _rel(workspace: Path, p) -> str:
    """Workspace-relative display path (absolute when outside)."""
    try:
        return Path(str(p)).resolve().relative_to(
            Path(workspace).resolve()).as_posix()
    except (ValueError, OSError):
        return str(p)


def _line_col(rng) -> tuple[int, int]:
    """LSP range -> 1-based (line, column)."""
    start = (rng or {}).get("start") or {}
    try:
        line = int(start.get("line", 0)) + 1
    except (TypeError, ValueError):
        line = 1
    try:
        col = int(start.get("character", 0)) + 1
    except (TypeError, ValueError):
        col = 1
    return line, col


def _end_line(rng) -> int:
    end = (rng or {}).get("end") or {}
    try:
        return int(end.get("line", 0)) + 1
    except (TypeError, ValueError):
        return 0


def _fmt_locations(workspace: Path, locations, cap: int,
                   empty: str = "(no locations)") -> str:
    """Location[] -> 'relpath:line:col' lines, deduplicated and capped."""
    distinct: list = []
    seen: set = set()
    for loc in locations or []:
        if not isinstance(loc, dict):
            continue
        path = str(loc.get("path") or loc.get("targetPath") or "")
        if not path:
            continue
        line, col = _line_col(loc.get("range"))
        entry = f"{_rel(workspace, path)}:{line}:{col}"
        if entry in seen:
            continue
        seen.add(entry)
        distinct.append(entry)
    if not distinct:
        return empty
    shown = distinct[:cap]
    out = "\n".join(shown)
    if len(distinct) > cap:
        out += (f"\n(+{len(distinct) - cap} more - narrow the query or "
                f"raise lsp_max_results)")
    return out


_SEVERITY = {1: "ERROR", 2: "WARNING", 3: "INFO", 4: "HINT"}


def _fmt_diagnostics(display_path: str, diags, cap: int) -> str:
    """Diagnostics -> '[ERROR] relpath:line:col message (source)' lines."""
    if not diags:
        return "(no diagnostics)"
    lines = []
    for d in diags[:cap]:
        if not isinstance(d, dict):
            continue
        sev = _SEVERITY.get(d.get("severity"), "DIAG")
        line, col = _line_col(d.get("range"))
        msg = str(d.get("message") or "").splitlines() or [""]
        msg = msg[0][:300]
        source = str(d.get("source") or "")
        if not source and d.get("code") is not None:
            source = str(d.get("code"))
        lines.append(f"[{sev}] {display_path}:{line}:{col} {msg}"
                     + (f" ({source})" if source else ""))
    if not lines:
        return "(no diagnostics)"
    return "\n".join(lines)


def _hover_text(hover) -> str:
    """Hover.contents (MarkupContent | {language,value} | string | list) ->
    plain text."""
    if not isinstance(hover, dict):
        return ""
    contents = hover.get("contents")
    if isinstance(contents, dict):
        return str(contents.get("value", ""))
    if isinstance(contents, list):
        parts = [str(c.get("value", "")) if isinstance(c, dict) else str(c)
                 for c in contents]
        return "\n".join(p for p in parts if p)
    return str(contents or "")


_SYMBOL_KINDS = {1: "File", 2: "Module", 3: "Namespace", 4: "Package",
                 5: "Class", 6: "Method", 7: "Property", 8: "Field",
                 9: "Constructor", 10: "Enum", 11: "Interface",
                 12: "Function", 13: "Variable", 14: "Constant",
                 15: "String", 16: "Number", 17: "Boolean", 18: "Array",
                 19: "Object", 20: "Key", 21: "Null", 22: "EnumMember",
                 23: "Struct", 24: "Event", 25: "Operator",
                 26: "TypeParameter"}


def _kind_name(kind) -> str:
    try:
        return _SYMBOL_KINDS.get(int(kind), "Symbol")
    except (TypeError, ValueError):
        return "Symbol"


def _fmt_document_symbols(symbols, cap: int) -> str:
    """Hierarchical DocumentSymbol trees AND flat SymbolInformation entries
    render as 'name (Kind) lines' (children indented); capped."""
    lines: list = []
    truncated = False

    def walk(items, depth):
        nonlocal truncated
        for s in items or []:
            if len(lines) >= cap:
                truncated = True
                return
            if not isinstance(s, dict):
                continue
            name = str(s.get("name") or "(anonymous)")
            kind = _kind_name(s.get("kind"))
            location = s.get("location")
            where = ""
            if isinstance(location, dict) and location.get("path"):
                # SymbolInformation: another file may be involved
                line, _col = _line_col(location.get("range"))
                where = f" @:{line}"
            else:
                rng = s.get("range")
                if isinstance(rng, dict):
                    sl, _c = _line_col(rng)
                    el = _end_line(rng)
                    where = f" L{sl}" + (f"-{el}" if el > sl else "")
            lines.append(f"{'  ' * depth}{name} ({kind}){where}")
            walk(s.get("children"), depth + 1)

    walk(symbols, 0)
    if not lines:
        return "(no symbols)"
    out = "\n".join(lines)
    if truncated:
        out += f"\n(+more - lsp_max_results={cap})"
    return out


def _fmt_workspace_symbols(workspace: Path, symbols, cap: int,
                           query: str) -> str:
    """workspace/symbol results -> 'name (Kind) relpath:line:col' lines."""
    lines = []
    truncated = False
    for s in symbols or []:
        if len(lines) >= cap:
            truncated = True
            break
        if not isinstance(s, dict):
            continue
        name = str(s.get("name") or "(anonymous)")
        kind = _kind_name(s.get("kind"))
        loc = s.get("location") if isinstance(s.get("location"), dict) else {}
        path = str(loc.get("path") or "")
        line, col = _line_col(loc.get("range"))
        where = f"{_rel(workspace, path)}:{line}:{col}" if path else "?"
        lines.append(f"{name} ({kind}) {where}")
    if not lines:
        return f"(no workspace symbols matching '{query}')"
    out = "\n".join(lines)
    if truncated:
        out += f"\n(+more - lsp_max_results={cap})"
    return out


def _fmt_code_actions(actions, cap: int) -> str:
    """CodeAction[] -> 'title (kind)' lines. TITLES ONLY - nothing here is
    applied; the agent implements a chosen action with edit_file."""
    if not actions:
        return "(no code actions for this range)"
    lines = []
    for a in actions[:cap]:
        if not isinstance(a, dict):
            continue
        title = str(a.get("title") or "(untitled action)")
        kind = str(a.get("kind") or "")
        preferred = " [preferred]" if a.get("isPreferred") else ""
        lines.append(title + (f" ({kind})" if kind else "") + preferred)
    if not lines:
        return "(no code actions for this range)"
    return ("\n".join(lines)
            + "\n(Actions are listed only - nothing is applied. "
              "Implement the chosen fix with edit_file.)")


def _summarize_rename(workspace: Path, edit: dict, new_name: str) -> str:
    """WorkspaceEdit -> 'N edits across M files'. NOTHING is written here:
    the plan is returned so the agent (or user) applies it deliberately via
    edit_file - a rename must never touch files as a side effect."""
    files: dict = {}
    changes = edit.get("changes")
    if isinstance(changes, dict):
        for path, edits in changes.items():
            n = len(edits) if isinstance(edits, list) else 0
            files[str(path)] = files.get(str(path), 0) + n
    doc_changes = edit.get("documentChanges")
    if isinstance(doc_changes, list):
        for dc in doc_changes:
            if not isinstance(dc, dict):
                continue
            td = dc.get("textDocument") if isinstance(
                dc.get("textDocument"), dict) else {}
            path = str(dc.get("path") or td.get("path") or "(unknown file)")
            edits = dc.get("edits")
            n = len(edits) if isinstance(edits, list) else 0
            files[path] = files.get(path, 0) + n
    total = sum(files.values())
    if not files or total == 0:
        return "(the server returned an empty rename plan - nothing to apply)"
    per_file = "\n".join(f"  - {_rel(workspace, p)}: {n} edit(s)"
                         for p, n in sorted(files.items()))
    return (f"Rename plan for '{new_name}': {total} edit(s) across "
            f"{len(files)} file(s) - NOT applied:\n{per_file}\n"
            "The files are untouched; apply the changes yourself "
            "(edit_file) if you agree with the plan.")


# ── Tool handlers ────────────────────────────────────────────────────────────
async def _inline_tool_lsp_status(args: dict, workspace: Path,
                                  workspace_lock) -> str:
    """Availability report. NEVER starts a server process - the manager's
    available() is which()-only."""
    tool = "lsp_status"
    if not _lsp_enabled():
        return _err_disabled(tool)
    mgr = _get_manager(workspace)
    try:
        rows = mgr.available()
    except (TypeError, ValueError) as exc:
        return _err_failed(tool, exc)
    if not rows:
        return "(no languages known to the LSP registry)"
    lines = []
    for row in rows:
        lang = str(row.get("language", "?"))
        cmd = str(row.get("command", "?"))
        if row.get("found"):
            lines.append(f"{lang}: {cmd} [found]")
        else:
            hint = _INSTALL_HINTS.get(lang, "install a language server for it")
            lines.append(f"{lang}: {cmd} [missing - install: {hint}]")
    found_n = sum(1 for r in rows if r.get("found"))
    head = (f"Language servers: {found_n}/{len(rows)} found. "
            f"A server starts lazily on the first lsp_* call for its files.")
    return head + "\n" + "\n".join(lines)


async def _inline_tool_lsp_diagnostics(args: dict, workspace: Path,
                                       workspace_lock) -> str:
    tool = "lsp_diagnostics"
    mgr, p, _language, early = await _prepare(tool, args, workspace,
                                              workspace_lock)
    if early is not None:
        return early
    settings = _lsp_settings()
    wait_s = _coerce_float(settings.get("lsp_diagnostics_wait_s"), 2.0)
    cap = _coerce_int(settings.get("lsp_max_diagnostics"), 50, 1, 500)
    try:
        diags = await mgr.diagnostics(p, wait_s=wait_s)
    except _lsp.LspError as exc:
        return _err_failed(tool, exc)
    out = _fmt_diagnostics(_rel(workspace, p), diags, cap)
    if len(diags) > cap:
        out += (f"\n(+{len(diags) - cap} more diagnostics - "
                f"lsp_max_diagnostics={cap})")
    return out


async def _inline_tool_lsp_definition(args: dict, workspace: Path,
                                      workspace_lock) -> str:
    tool = "lsp_definition"
    mgr, p, _language, early = await _prepare(tool, args, workspace,
                                              workspace_lock)
    if early is not None:
        return early
    try:
        line, char = _positions(args, tool, ("line", "character"))
    except _ArgError as exc:
        return _tool_error_response("INVALID_ARGUMENT", str(exc), tool=tool)
    try:
        # tool args 1-based -> LSP 0-based
        locs = await mgr.definition(p, line - 1, char - 1)
    except _lsp.LspError as exc:
        return _err_failed(tool, exc)
    cap = _coerce_int(_lsp_settings().get("lsp_max_results"), 50, 1, 500)
    display = f"{_rel(workspace, p)}:{line}:{char}"
    return _fmt_locations(workspace, locs, cap,
                          empty=f"(no definition found at {display})")


async def _inline_tool_lsp_references(args: dict, workspace: Path,
                                      workspace_lock) -> str:
    tool = "lsp_references"
    mgr, p, _language, early = await _prepare(tool, args, workspace,
                                              workspace_lock)
    if early is not None:
        return early
    try:
        line, char = _positions(args, tool, ("line", "character"))
    except _ArgError as exc:
        return _tool_error_response("INVALID_ARGUMENT", str(exc), tool=tool)
    try:
        locs = await mgr.references(p, line - 1, char - 1)
    except _lsp.LspError as exc:
        return _err_failed(tool, exc)
    cap = _coerce_int(_lsp_settings().get("lsp_max_results"), 50, 1, 500)
    return _fmt_locations(workspace, locs, cap, empty="(no references found)")


async def _inline_tool_lsp_hover(args: dict, workspace: Path,
                                 workspace_lock) -> str:
    tool = "lsp_hover"
    mgr, p, _language, early = await _prepare(tool, args, workspace,
                                              workspace_lock)
    if early is not None:
        return early
    try:
        line, char = _positions(args, tool, ("line", "character"))
    except _ArgError as exc:
        return _tool_error_response("INVALID_ARGUMENT", str(exc), tool=tool)
    try:
        hover = await mgr.hover(p, line - 1, char - 1)
    except _lsp.LspError as exc:
        return _err_failed(tool, exc)
    text = _hover_text(hover).strip()
    if not text:
        return (f"(no hover information at {_rel(workspace, p)}:"
                f"{line}:{char})")
    return text[:4000]


async def _inline_tool_lsp_symbols(args: dict, workspace: Path,
                                   workspace_lock) -> str:
    tool = "lsp_symbols"
    mgr, p, _language, early = await _prepare(tool, args, workspace,
                                              workspace_lock)
    if early is not None:
        return early
    try:
        syms = await mgr.document_symbols(p)
    except _lsp.LspError as exc:
        return _err_failed(tool, exc)
    cap = _coerce_int(_lsp_settings().get("lsp_max_results"), 50, 1, 500)
    return _fmt_document_symbols(syms, cap)


async def _inline_tool_lsp_workspace_symbols(args: dict, workspace: Path,
                                             workspace_lock) -> str:
    tool = "lsp_workspace_symbols"
    if not _lsp_enabled():
        return _err_disabled(tool)
    query = str((args or {}).get("query", "") or "").strip()
    if not query:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "lsp_workspace_symbols requires a non-empty query.",
            tool=tool)
    mgr = _get_manager(workspace)
    try:
        rows = mgr.available()
    except (TypeError, ValueError) as exc:
        return _err_failed(tool, exc)
    if not any(r.get("found") for r in rows):
        return _err_unavailable(tool)
    try:
        syms = await mgr.workspace_symbols(query)
    except _lsp.LspError as exc:
        return _err_failed(tool, exc)
    cap = _coerce_int(_lsp_settings().get("lsp_max_results"), 50, 1, 500)
    return _fmt_workspace_symbols(workspace, syms, cap, query)


async def _inline_tool_lsp_code_actions(args: dict, workspace: Path,
                                        workspace_lock) -> str:
    tool = "lsp_code_actions"
    mgr, p, _language, early = await _prepare(tool, args, workspace,
                                              workspace_lock)
    if early is not None:
        return early
    try:
        sl, sc, el, ec = _positions(args, tool, ("start_line", "start_char",
                                                 "end_line", "end_char"))
    except _ArgError as exc:
        return _tool_error_response("INVALID_ARGUMENT", str(exc), tool=tool)
    try:
        # 1-based range -> 0-based LSP range; actions are LISTED only
        actions = await mgr.code_actions(p, sl - 1, sc - 1, el - 1, ec - 1)
    except _lsp.LspError as exc:
        return _err_failed(tool, exc)
    cap = _coerce_int(_lsp_settings().get("lsp_max_results"), 50, 1, 500)
    return _fmt_code_actions(actions, cap)


async def _inline_tool_lsp_rename(args: dict, workspace: Path,
                                  workspace_lock) -> str:
    """Mutating tool (approval-gated in runner._APPROVAL_TOOLS), but the
    write itself never happens here: the handler returns the summarized
    WorkspaceEdit and the agent applies it via edit_file."""
    tool = "lsp_rename"
    mgr, p, _language, early = await _prepare(tool, args, workspace,
                                              workspace_lock)
    if early is not None:
        return early
    new_name = str((args or {}).get("new_name", "") or "").strip()
    if not new_name:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "lsp_rename requires a non-empty new_name.",
            tool=tool)
    try:
        line, char = _positions(args, tool, ("line", "character"))
    except _ArgError as exc:
        return _tool_error_response("INVALID_ARGUMENT", str(exc), tool=tool)
    try:
        edit = await mgr.rename(p, line - 1, char - 1, new_name)
    except _lsp.LspError as exc:
        return _err_failed(tool, exc)
    if not isinstance(edit, dict):
        return (f"(no symbol to rename at {_rel(workspace, p)}:"
                f"{line}:{char} - the server refused the rename; "
                f"nothing was changed)")
    return _summarize_rename(workspace, edit, new_name)
