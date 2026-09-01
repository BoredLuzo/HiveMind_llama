"""Tool handlers: code-intelligence tools (signatures/references/search) (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from utils.file import fuzzy_resolve_path as _fuzzy_resolve_path, _inline_resolve_path, _inline_check_workspace
from tools.errors import tool_error_response as _tool_error_response
import asyncio
import re
import shutil
import sys

from . import _shared

_HAS_REGEX_META = re.compile(r"[.^$*+?()\[\]{}|\\]")

from .linting import _auto_lint_result

async def _inline_tool_get_signatures(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "get_signatures"):
        return err
    max_items = int(args.get("max_items") or 400)
    max_items = max(20, min(1200, max_items))
    _sig_fn = _shared.get_signatures_report if callable(_shared.get_signatures_report) else None
    if _sig_fn is None:
        return _tool_error_response(
            "GET_SIGNATURES_UNAVAILABLE",
            "get_signatures: runtime dependencies not initialized.",
            tool="get_signatures" )
    try:
        return await asyncio.to_thread(_sig_fn, p, max_items)
    except Exception as e:
        return _tool_error_response(
            "GET_SIGNATURES_FAILED",
            f"get_signatures failed for '{p}': {e}",
            tool="get_signatures" )


async def _inline_tool_find_references(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    symbol = str(args.get("symbol", "")).strip()
    if not symbol:
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "find_references requires a non-empty symbol.",
            tool="find_references" )
    try:
        p = _inline_resolve_path(workspace, args.get("path", "."))
    except Exception:
        p = workspace
    try:
        p = p.resolve()
    except Exception:
        p = workspace
    if err := _inline_check_workspace(p, workspace_lock, "find_references"):
        return err
    max_items = int(args.get("max_items") or 160)
    max_items = max(20, min(2000, max_items))
    _fr_fn = _shared.find_references_report if callable(_shared.find_references_report) else None
    if _fr_fn is None:
        return _tool_error_response(
            "FIND_REFERENCES_UNAVAILABLE",
            "find_references: runtime dependencies not initialized.",
            tool="find_references" )
    try:
        return await asyncio.to_thread(_fr_fn, p, symbol, max_items)
    except Exception as e:
        return _tool_error_response(
            "FIND_REFERENCES_FAILED",
            f"find_references failed for '{symbol}': {e}",
            tool="find_references" )


async def _inline_tool_edit_ast(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "edit_ast"):
        return err

    target_type = str(args.get("target_type", "")).strip().lower()
    target_name = str(args.get("target_name", "")).strip()
    new_code = args.get("new_code", "")

    _ast_fn = _shared.edit_ast_file if callable(_shared.edit_ast_file) else None
    if _ast_fn is None:
        return _tool_error_response(
            "EDIT_AST_UNAVAILABLE",
            "edit_ast: runtime dependencies not initialized.",
            tool="edit_ast" )
    ok, msg = await asyncio.to_thread(_ast_fn, p, target_type, target_name, new_code)
    if not ok:
        return msg

    _lint = await _auto_lint_result(p, workspace)
    return msg + _lint


async def _inline_tool_list_dir(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", "."))
    if err := _inline_check_workspace(p, workspace_lock, "list_dir"):
        return err
    try:
        items = await asyncio.to_thread(lambda: sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name)))
        _notice = None
        if len(items) > 200:
            _notice = f"[OUTPUT TRUNCATED: showing 200 of {len(items)} entries. Use list_dir on a subdirectory or find_files to narrow scope.]"
            items = items[:200]
        _out = "\n".join(("📄" if i.is_file() else "📁") + " " + i.name for i in items)
        if _notice:
            _out += "\n" + _notice
        return _out
    except Exception as e:
        return _tool_error_response(
            "LIST_DIR_FAILED",
            f"Failed to list directory '{p}': {e}",
            tool="list_dir" )


async def _inline_tool_find_files(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    pattern = args.get("pattern", "**/*")
    base = _inline_resolve_path(workspace, args.get("path", "."))
    if err := _inline_check_workspace(base, workspace_lock, "find_files"):
        return err
    try:
        matches = await asyncio.to_thread(lambda: sorted(base.glob(pattern)))
        files = [m for m in matches if m.is_file()]
        if not files:
            return f"(no matches for '{pattern}' in {base})"
        lines = []
        for m in files[:150]:
            try:
                rel = m.relative_to(base)
            except ValueError:
                rel = m
            lines.append(str(rel))
        result = "\n".join(lines)
        if len(files) > 150:
            result += f"\n… ({len(files) - 150} more)"
        return result
    except Exception as e:
        return _tool_error_response(
            "FIND_FILES_FAILED",
            f"find_files failed: {e}",
            tool="find_files" )


async def _inline_tool_search_code(args: dict, workspace: Path, _workspace_lock: str | None) -> str:
    pattern = args.get("pattern", "")
    if not pattern.strip():
        return _tool_error_response(
            "INVALID_ARGUMENT",
            "search_code requires a non-empty pattern. Provide a search term or regex.",
            tool="search_code")
    try:
        _sc_p = (workspace / args.get("path", ".")).resolve()
    except Exception:
        _sc_p = Path(workspace / args.get("path", "."))
    if err := _inline_check_workspace(_sc_p, _workspace_lock, "search_code"):
        return err
    path = str(_sc_p)
    _is_dir = _sc_p.is_dir()
    _is_win = sys.platform == "win32"
    # Shell-injection safe, regex-preserving sanitizer: Only strip $
    # (subexpression/variable expansion), ` (escape char), and quotes.
    # Parentheses, braces, ^, etc. are safe inside PowerShell double-quoted
    _strip_chars = "$`\"'"
    _safe_pat = pattern
    for _c in _strip_chars:
        _safe_pat = _safe_pat.replace(_c, '')
    _safe_path = path
    for _c in _strip_chars:
        _safe_path = _safe_path.replace(_c, '')

    cmds = []
    if shutil.which("rg"):
        if _is_win:
            _rg_pat = pattern.replace('"', "").replace("'", "")
            cmds.append(f'rg --no-heading -n "{_rg_pat}" "{_safe_path}" 2>nul')
        else:
            import shlex as _shlex
            cmds.append(f"rg --no-heading -n {_shlex.quote(pattern)} {_shlex.quote(path)} 2>/dev/null | head -50")
    if _is_win:
        if not _HAS_REGEX_META.search(pattern):
            if _is_dir:
                cmds.append(f'findstr /s /n /i "{_safe_pat}" "{_safe_path}\\*" 2>nul')
            else:
                cmds.append(f'findstr /n /i "{_safe_pat}" "{_safe_path}" 2>nul')
    else:
        # SHELL-INJECTION FIX: shlex.quote() both pattern and path to prevent
        # command injection via crafted filenames or search patterns.
        import shlex as _shlex
        _q_pat = _shlex.quote(_safe_pat)
        _q_path = _shlex.quote(path)
        if _is_dir:
            cmds.append(f"grep -rn {_q_pat} {_q_path} 2>/dev/null | head -50")
        else:
            cmds.append(f"grep -n {_q_pat} {_q_path} 2>/dev/null | head -50")

    _any_executed = False

    async def _run_one(cmd: str):
        nonlocal _any_executed
        try:
            r = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, _ = await asyncio.wait_for(r.communicate(), 10)
            _any_executed = True
            result = out.decode(errors="replace").strip()
            if result:
                return result
        except Exception:
            pass
        return None

    for _cmd in cmds:
        _r = await _run_one(_cmd)
        if _r:
            return f"[search_code: {pattern}]\n{_r[:8000]}"

    # PYTHON-FALLBACK (3. Backend): Original-Pattern, garantiert korrekt —
    _fb_error = False
    try:
        _fb = await asyncio.to_thread(_python_content_search, _sc_p, pattern)
    except Exception:
        _fb = None
        _fb_error = True
    if _fb:
        return f"[search_code: {pattern}]\n{_fb[:8000]}"
    if _fb_error and not _any_executed:
        return _tool_error_response(
            "SEARCH_FAILED",
            "search_code: all search backends failed to execute. "
            "Use find_files for file names or read_file on likely candidates.",
            tool="search_code")
    return "(no matches — search completed, nothing found)"


def _python_content_search(
    root: Path,
    pattern: str,
    max_results: int = 80,
    max_file_bytes: int = 512_000,
    max_files: int = 2000,
) -> str | None:


    try:
        rx = re.compile(pattern)
    except re.error:
        rx = re.compile(re.escape(pattern))
    _skip_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv",
                  ".idea", ".vscode", "$RECYCLE.BIN"}
    files: list[Path] = [root] if root.is_file() else []
    if not files:
        for p in root.rglob("*"):
            if len(files) >= max_files:
                break
            if not p.is_file():
                continue
            if any(part in _skip_dirs for part in p.parts[:-1]):
                continue
            try:
                if p.stat().st_size > max_file_bytes:
                    continue
            except OSError:
                continue
            files.append(p)
    hits: list[str] = []
    for fp in files:
        try:
            if fp.stat().st_size > max_file_bytes:
                continue
            data = fp.read_bytes()
        except OSError:
            continue
        if b"\x00" in data[:4096]:
            continue  # binary
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")
        for ln, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{fp}:{ln}: {line.strip()[:200]}")
                if len(hits) >= max_results:
                    hits.append("... (truncated)")
                    return "\n".join(hits)
    return "\n".join(hits) if hits else None
