# -*- coding: utf-8 -*-
"""Tool handlers: LSP tools (language servers via infra/lsp_client).

LSP-TOOLS (2026-10-31): one broad, generic surface over whatever language
servers the user has installed. There are NO per-language on/off flags:
  - settings["lsp_enabled"] is the only switch (False -> LSP_DISABLED);
  - a server process starts LAZILY on the first lsp_* tool call for its
    files - never at boot, and lsp_status never starts one at all; with
    lsp_auto_warm_on_write (default on) a successful write/edit also warms
    the language server in the background for autonomous post-write
    diagnostics;
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
from typing import Literal, overload

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
    "lsp_idle_timeout_s": 600,
    "lsp_diagnostics_wait_s": 5.0,
    "lsp_auto_diagnostics": True,
    "lsp_auto_warm_on_write": True,
    "lsp_max_diagnostics": 50,
    "lsp_max_results": 50,
}


def _lsp_settings() -> dict:
    """The LSP-relevant settings.

    Precedence (F10, 2026-11-08): the per-run settings snapshot
    (tools.runner._run_settings_snapshot, lifted by chat_run.run_stream)
    wins over the live dict - a run keeps the values it STARTED with, and a
    body override (bench: lsp_auto_diagnostics=false) pins just that run.
    A snapshot key that is absent keeps the live/fallback value, and no
    snapshot at all (direct handler calls, unit tests, the external tool
    endpoint) behaves exactly as before: live values (core.state.settings)
    with the defaults above as last fallback.
    """
    try:
        from core.state import settings as _live
        live = _live if isinstance(_live, dict) else {}
    except (ImportError, AttributeError, TypeError):
        live = {}
    merged = {key: live.get(key, _LSP_FALLBACKS[key]) for key in _LSP_FALLBACKS}
    try:
        # Lazy: tools.runner imports this handler package - a module-level
        # import here would be circular.
        from tools.runner import _run_settings_snapshot as _snap_cv
        snap = _snap_cv.get(None)
    except (ImportError, AttributeError, TypeError):
        snap = None
    if isinstance(snap, dict):
        for key in merged:
            if key in snap:
                merged[key] = snap[key]
    return merged


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
_bg_close_tasks: set = set()   # tracked eviction closes (audit R2)


def _manager_key(workspace: Path, loop) -> tuple:
    """The (workspace, loop) cache key. One place (audit: the key logic was
    computed 3x here plus a 4th copy in server._shutdown)."""
    try:
        ws_key = str(Path(workspace).resolve())
    except OSError:
        ws_key = str(workspace)
    return (ws_key.lower() if os.name == "nt" else ws_key, id(loop))


def _get_manager(workspace: Path):
    """The cached LspManager for this workspace on THIS running loop.

    infra.lsp_client.LspManager is looked up at call time (not bound at
    import), so tests can swap the class on the module."""
    loop = asyncio.get_running_loop()
    key = _manager_key(workspace, loop)
    mgr = _MANAGERS.get(key)
    if mgr is None:
        settings = _lsp_settings()
        # Forward every knob the client reads (audit R4: lsp_idle_timeout_s
        # was documented but silently dropped here).
        mgr = _lsp.LspManager(workspace, {
            "lsp_servers": settings.get("lsp_servers") or {},
            "lsp_client_timeout_s": settings.get("lsp_client_timeout_s"),
            "lsp_idle_timeout_s": settings.get("lsp_idle_timeout_s"),
        })
        if len(_MANAGERS) >= _MANAGERS_CAP:
            # Closing the evicted managers is not optional: clearing the
            # dict alone orphaned their language-server processes (audit
            # 2026-10-31, P1-2). close_all() is async; schedule it and let
            # the new manager start immediately.
            _evicted = [(k, m) for k, m in list(_MANAGERS.items())
                        if isinstance(k, tuple) and len(k) == 2
                        and k[1] == id(loop)]
            _MANAGERS.clear()
            # Managers of OTHER loops (tests) cannot be closed cross-loop —
            # they are dropped on purpose. Current-loop closes are tracked
            # (audit R2).
            for _key, _old in _evicted:
                try:
                    # Cancel in-flight warm-ups first: a late start_language
                    # could spawn a server after close_all enumerated its
                    # connections (deep audit 2026-11-08).
                    for (_mid, _lang), _wt in list(_LSP_WARM_TASKS.items()):
                        if _mid == id(_old):
                            _wt.cancel()
                            _LSP_WARM_TASKS.pop((_mid, _lang), None)
                    _t = asyncio.create_task(_old.close_all())
                    _bg_close_tasks.add(_t)
                    _t.add_done_callback(_bg_close_tasks.discard)
                except RuntimeError:
                    pass
        _MANAGERS[key] = mgr
    return mgr


# ── Error rendering ──────────────────────────────────────────────────────────
# Install hints per language (2026-10-31). Presentation state for the
# LSP_UNAVAILABLE message only - NOT a per-language flag: a language without
# an entry simply gets the generic advice.
_INSTALL_HINTS: dict = {
    "python": "npm install -g pyright  (or: pip install python-lsp-server)",
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
                  "Install one, e.g.: npm install -g pyright (python) or "
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

    def _add(path: str, edits) -> None:
        rec = files.setdefault(str(path), {"n": 0, "lines": []})
        if not isinstance(edits, list):
            return
        rec["n"] += len(edits)
        for _e in edits:
            if not isinstance(_e, dict):
                continue
            _rng = _e.get("range")
            if not isinstance(_rng, dict):
                continue
            _line = _line_col(_rng)[0]
            if _line > 0:
                rec["lines"].append(_line)

    changes = edit.get("changes")
    if isinstance(changes, dict):
        for path, edits in changes.items():
            _add(str(path), edits)
    doc_changes = edit.get("documentChanges")
    if isinstance(doc_changes, list):
        for dc in doc_changes:
            if not isinstance(dc, dict):
                continue
            td = dc.get("textDocument") if isinstance(
                dc.get("textDocument"), dict) else {}
            path = str(dc.get("path") or td.get("path") or "(unknown file)")
            _add(path, dc.get("edits"))
    total = sum(int(rec["n"]) for rec in files.values())
    if not files or total == 0:
        return "(the server returned an empty rename plan - nothing to apply)"
    per_file = []
    for p, rec in sorted(files.items()):
        _extra = ""
        _lines = rec["lines"]
        if _lines:
            _shown = ", ".join(str(n) for n in _lines[:8])
            if len(_lines) > 8:
                _shown += f", +{len(_lines) - 8} more"
            _extra = f" @ line {_shown}"
        per_file.append(f"  - {_rel(workspace, p)}: {rec['n']} edit(s){_extra}")
    per_file = "\n".join(per_file)
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


def _truthy(value, default: bool = True) -> bool:
    """Settings values can arrive as bool, 0/1 or "false"/"off" strings."""
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "off", "no", "")
    if value is None:
        return bool(default)
    return bool(value)


# (path, mtime_ns, size) of the last automatic diagnostics run, so a retry
# of the same write does not pay the wait twice.
_AUTO_DIAG_CACHE: dict[str, tuple[int, int]] = {}

# AUTO-WARM (2026-11-08): post-write diagnostics should not depend on the
# model calling lsp_diagnostics first. One start task per (manager, language);
# the write waits briefly for it, then leaves it running in the background.
_LSP_WARM_TASKS: dict[tuple[int, str], asyncio.Task] = {}


def _warm_lsp_async(mgr, language: str) -> asyncio.Task:
    """Start (or join) a language-server warm-up without blocking the loop."""
    key = (id(mgr), language)
    task = _LSP_WARM_TASKS.get(key)
    if task is None or task.done():
        task = asyncio.create_task(mgr.start_language(language))
        _LSP_WARM_TASKS[key] = task

        def _done(t, k=key):
            _LSP_WARM_TASKS.pop(k, None)
            try:
                t.result()
            except (OSError, RuntimeError, ValueError, _lsp.LspError):
                pass
        task.add_done_callback(_done)
    return task


@overload
async def auto_diagnostics_block(workspace: Path, path: str, *,
                                 max_lines: int = 8,
                                 return_status: Literal[False] = False) -> str: ...


@overload
async def auto_diagnostics_block(workspace: Path, path: str, *,
                                 max_lines: int = 8,
                                 return_status: Literal[True]) -> tuple[str, bool]: ...


async def auto_diagnostics_block(workspace: Path, path: str,
                                 *, max_lines: int = 8,
                                 return_status: bool = False) -> str | tuple[str, bool]:
    """Compact diagnostics for a file the agent just wrote, or "".

    With ``return_status=True`` returns ``(text, handled)``: ``handled`` is
    True only when the LSP actually ran and published diagnostics (even when
    the file is clean). The write dispatcher uses that to decide whether the
    pre-existing lint fallback stays.

    Called from the shared write dispatcher. It is deliberately cheap:
    - only when lsp_enabled and lsp_auto_diagnostics are on;
    - a cold server is warmed in the background (lsp_auto_warm_on_write,
      default on); the write waits at most 3 s for it, then the next write
      gets the diagnostics;
    - short wait (max 1.5s), hard line cap, every failure is silent.
    """
    def _r(text: str, handled: bool):
        return (text, handled) if return_status else text

    if not _lsp_enabled():
        return _r("", False)
    settings = _lsp_settings()
    if not _truthy(settings.get("lsp_auto_diagnostics"), True):
        return _r("", False)
    raw = str(path or "").strip()
    if not raw:
        return _r("", False)
    p = _inline_resolve_path(workspace, raw)
    language = _registry.language_for_path(p)
    if language is None:
        return _r("", False)
    try:
        sig = (p.stat().st_mtime_ns, p.stat().st_size)
    except OSError:
        return _r("", False)
    key = str(p).lower() if os.name == "nt" else str(p)
    if _AUTO_DIAG_CACHE.get(key) == sig:
        return _r("", True)
    mgr = _get_manager(workspace)
    if not mgr.is_started(language):
        # AUTONOMOUS-WARM (2026-11-08): start the server from the write path.
        # Wait at most 3 s; a slower server keeps warming in the background
        # and the next write gets the diagnostics.
        if not _truthy(settings.get("lsp_auto_warm_on_write"), True):
            return _r("", False)
        _warm = _warm_lsp_async(mgr, language)
        try:
            await asyncio.wait_for(asyncio.shield(_warm), timeout=3.0)
        except (asyncio.TimeoutError, _lsp.LspError, OSError, RuntimeError):
            pass
        if not mgr.is_started(language):
            return _r("", False)
    wait_s = min(_coerce_float(settings.get("lsp_diagnostics_wait_s"), 5.0),
                 1.5)
    try:
        diags = await mgr.diagnostics(p, wait_s=wait_s, allow_pull=False)
    except (_lsp.LspError, OSError, asyncio.TimeoutError):
        return _r("", False)
    if not mgr.has_diagnostics(p):
        return _r("", False)  # still analyzing; the explicit tool can retry
    _AUTO_DIAG_CACHE[key] = sig
    if len(_AUTO_DIAG_CACHE) > 128:
        _AUTO_DIAG_CACHE.clear()
    if not diags:
        return _r("", True)
    cap = max(1, min(int(max_lines or 8), 20))
    body = _fmt_diagnostics(_rel(workspace, p), diags, cap)
    errors = sum(1 for d in diags
                 if isinstance(d, dict) and d.get("severity") == 1)
    if len(diags) > cap:
        body += f"\n(+{len(diags) - cap} more diagnostic(s))"
    text = (f"[LSP auto-diagnostics after writing {_rel(workspace, p)}: "
            f"{len(diags)} finding(s), {errors} error(s) - fix before "
            f"verification]\n" + body)
    return _r(text, True)


def merge_post_write_diagnostics(result: str, lsp_note: str,
                                 lsp_handled: bool) -> str:
    """LSP wins when it ran; otherwise the file_ops lint block is the fallback.

    file_ops always appends its lint block (`\\n[lint/...`). When the LSP
    published diagnostics - even a clean result - drop that block and use the
    LSP text while preserving trailing [AUTO-TEST] and [Diff] blocks.
    When the LSP was unavailable/cold, keep the lint block.
    """
    if not lsp_handled:
        return result
    _tail_markers = ("\n[USER NOTE]:", "\n[AUTO-TEST]", "\n\n[Diff \u2014 what changed]")
    while (_lint_at := result.find("\n[lint/")) != -1:
        _ends = [idx for m in _tail_markers if (idx := result.find(m, _lint_at + 1)) != -1]
        if _ends:
            result = result[:_lint_at] + result[min(_ends):]
        else:
            result = result[:_lint_at]
    if lsp_note:
        result = result.rstrip() + "\n\n" + lsp_note
    return result


async def _inline_tool_lsp_diagnostics(args: dict, workspace: Path,
                                       workspace_lock) -> str:
    tool = "lsp_diagnostics"
    mgr, p, _language, early = await _prepare(tool, args, workspace,
                                              workspace_lock)
    if early is not None:
        return early
    settings = _lsp_settings()
    wait_s = _coerce_float(settings.get("lsp_diagnostics_wait_s"), 5.0)
    cap = _coerce_int(settings.get("lsp_max_diagnostics"), 50, 1, 500)
    try:
        diags = await mgr.diagnostics(p, wait_s=wait_s)
    except _lsp.LspError as exc:
        return _err_failed(tool, exc)
    if not diags and not mgr.has_diagnostics(p):
        # Cold server: an empty list here means "no publication yet", not
        # "the file is clean". Say so instead of a false negative.
        return (f"(no diagnostics yet for {_rel(workspace, p)} - the "
                f"language server is still analyzing; call lsp_diagnostics "
                f"again in a moment)")
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


# ── lsp_format (2026-10-31, P2): server-driven formatting ────────────────────
def _apply_text_edits(text: str, edits: list[dict]) -> str:
    """Apply LSP TextEdit[] (0-based, UTF-16 columns) to text.

    LSP applies edits bottom-up; sorting by start position descending makes
    the offsets stable. Line/character -> offset via the split-lines map.
    """
    line_starts = [0]
    for i, ch in enumerate(text):
        if ch == "\n":
            line_starts.append(i + 1)
    lines = text.split("\n")

    def _offset(line: int, character: int) -> int:
        line = max(0, min(int(line), len(line_starts) - 1))
        base = line_starts[line]
        line_text = lines[line] if line < len(lines) else ""
        # LSP columns are UTF-16 code units; python str indices are code
        # points (audit R6): walking the line and counting astral characters
        # (surrogate pairs) as 2 units converts exactly. Beyond the line end
        # the remaining width is clamped to the line length.
        target = max(0, int(character))
        units = 0
        for idx, ch in enumerate(line_text):
            if units >= target:
                return base + idx
            units += 2 if 0x10000 <= ord(ch) <= 0x10FFFF else 1
        return base + len(line_text)

    out = text
    for edit in sorted(edits, key=lambda e: (
            (e.get("range", {}).get("start") or {}).get("line", 0),
            (e.get("range", {}).get("start") or {}).get("character", 0)),
            reverse=True):
        rng = edit.get("range") or {}
        start = rng.get("start") or {}
        end = rng.get("end") or {}
        s_off = _offset(start.get("line", 0), start.get("character", 0))
        e_off = _offset(end.get("line", 0), end.get("character", 0))
        if e_off < s_off:
            s_off, e_off = e_off, s_off
        out = out[:s_off] + str(edit.get("newText") or "") + out[e_off:]
    return out


async def _inline_tool_lsp_format(args: dict, workspace: Path,
                                  workspace_lock) -> str:
    """Mutating tool (approval-gated in runner._APPROVAL_TOOLS): format the
    file with its language server and WRITE the result."""
    tool = "lsp_format"
    mgr, p, language, early = await _prepare(tool, args, workspace,
                                             workspace_lock)
    if early is not None:
        return early
    edits = await mgr.format(p)
    if edits is None:
        return (f"(the {language} server does not offer formatting for "
                f"{_rel(workspace, p)} - nothing was changed)")
    if not edits:
        return (f"Formatted {_rel(workspace, p)}: the server returned no "
                f"edits - the file already matches the server's format.")
    try:
        original = p.read_text(encoding="utf-8", errors="replace")
        formatted = _apply_text_edits(original, edits)
        if formatted == original:
            return (f"Formatted {_rel(workspace, p)}: {len(edits)} edit(s) "
                    f"changed nothing - already formatted.")
        from tools.handlers.file_ops import _checked_atomic_write as _caw
        _werr = await _caw(p, formatted, workspace_lock, tool)
        if _werr:
            return _werr
        notify_watched_change(workspace, [p])
        return (f"Formatted {_rel(workspace, p)}: {len(edits)} edit(s) "
                f"applied, file rewritten "
                f"({len(original)} -> {len(formatted)} chars). "
                f"Re-run lsp_diagnostics to confirm it is still clean.")
    except OSError as exc:
        return _tool_error_response(
            "LSP_FAILED", f"{tool}: formatting result could not be written: "
            f"{exc}", tool=tool)


def notify_watched_change(workspace: Path, paths) -> int:
    """Fire workspace/didChangeWatchedFiles for files HiveMind wrote.

    Fire-and-forget from the write path: no manager / no live server / any
    error -> 0 notifications and no exception (the next lsp_* call still
    full-text-syncs from disk, so this is an index-freshness hint only).
    """
    try:
        import asyncio as _asyncio
        loop = _asyncio.get_running_loop()
        try:
            ws_key = str(Path(workspace).resolve())
        except OSError:
            ws_key = str(workspace)
        mgr = _MANAGERS.get((ws_key.lower() if os.name == "nt" else ws_key,
                             id(loop)))
        if mgr is None:
            return 0
        return mgr.broadcast_watched_change(list(paths or []))
    except Exception as exc:
        logger.debug("[LSP] watched-change notify skipped: %s", exc)
        return 0
