"""Tool handlers: code-intelligence tools (signatures/references/search) (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from utils.file import (
    _inline_resolve_path,
    _inline_check_workspace,
    _is_junction,
    is_secret_path,
    filter_secret_search_rows,
)
from tools.errors import tool_error_response as _tool_error_response
import asyncio
import os
import re
import shutil
import sys

_SKIP_SEARCH_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".idea", ".vscode", ".pytest_cache", ".ruff_cache", "dist", "build", "$RECYCLE.BIN",
})

from . import _shared

_HAS_REGEX_META = re.compile(r"[.^$*+?()\[\]{}|\\]")

from .linting import _auto_lint_result

async def _inline_tool_get_signatures(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "get_signatures"):
        return err
    max_items = int(args.get("max_items") or 400)
    max_items = max(20, min(400, max_items))  # H-audit: schema says max 400
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
    max_items = max(20, min(160, max_items))  # H-audit: schema says max 160
    _fr_fn = _shared.find_references_report if callable(_shared.find_references_report) else None
    if _fr_fn is None:
        return _tool_error_response(
            "FIND_REFERENCES_UNAVAILABLE",
            "find_references: runtime dependencies not initialized.",
            tool="find_references" )
    try:
        _sec_root = workspace_lock or workspace
        return await asyncio.to_thread(
            _fr_fn, p, symbol, max_items,
            exclude_fn=lambda fp: is_secret_path(fp, _sec_root),
        )
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
        base_resolved = base.resolve()
        lock_resolved = Path(workspace_lock).resolve() if workspace_lock is not None else base_resolved

        def _entry_allowed(fp: Path) -> bool:
            try:
                rel = fp.relative_to(base)
            except ValueError:
                return False
            if any(part in _SKIP_SEARCH_DIRS for part in rel.parts):
                return False
            cur = base
            for part in rel.parts:
                cur = cur / part
                if cur.is_symlink() or _is_junction(cur):
                    return False
            try:
                fp_res = fp.resolve()
            except (OSError, RuntimeError):
                return False
            if not fp_res.is_file():
                return False
            if not (fp_res.is_relative_to(base_resolved) and fp_res.is_relative_to(lock_resolved)):
                return False
            if _inline_check_workspace(fp, workspace_lock, "find_files") is not None:
                return False
            if _inline_check_workspace(fp_res, workspace_lock, "find_files") is not None:
                return False
            if is_secret_path(fp, base) or is_secret_path(fp_res, base_resolved):
                return False
            return True

        def _dir_allowed(dp: Path) -> bool:
            if dp.is_symlink() or _is_junction(dp):
                return False
            try:
                dp_res = dp.resolve()
            except (OSError, RuntimeError):
                return False
            return dp_res.is_relative_to(base_resolved) and dp_res.is_relative_to(lock_resolved)

        def _collect() -> list[Path]:
            if "**" not in pattern:
                return [
                    m for m in sorted(base.glob(pattern))
                    if _entry_allowed(m)
                ]
            import fnmatch
            sub_pat = pattern[3:] if pattern.startswith("**/") else pattern
            found: list[Path] = []
            for dp, dns, fns in os.walk(base, followlinks=False):
                dns[:] = [
                    d for d in sorted(dns)
                    if d not in _SKIP_SEARCH_DIRS and _dir_allowed(Path(dp) / d)
                ]
                for fn in sorted(fns):
                    fp = Path(dp) / fn
                    rel_s = fp.relative_to(base).as_posix()
                    if (fnmatch.fnmatch(rel_s, pattern)
                            or fnmatch.fnmatch(fn, sub_pat)
                            or fnmatch.fnmatch(rel_s, sub_pat)):
                        if _entry_allowed(fp):
                            found.append(fp)
            return sorted(found)

        files = await asyncio.to_thread(_collect)
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

    # ARGV, NOT SHELL (audit 2026-10-17): search_code is a READ-ONLY tool that
    # runs without an approval prompt, so it must never hand a shell a string
    # built from model input. The old shell form needed "2>nul" / "| head -50"
    # and left %VAR% expansion on Windows open (cmd expands %NAME% even inside
    # double quotes). Every backend is now an argument list and the line cap is
    # applied here.
    cmds: list[list[str]] = []
    if shutil.which("rg"):
        cmds.append(["rg", "--no-heading", "-n", "--", pattern, path])
    if _is_win:
        if not _HAS_REGEX_META.search(pattern):
            if _is_dir:
                cmds.append(["findstr", "/s", "/n", "/i", "/L", f"/C:{_safe_pat}",
                             _safe_path.rstrip("\\") + "\\*"])
            else:
                cmds.append(["findstr", "/n", "/i", "/L", f"/C:{_safe_pat}", _safe_path])
    else:
        if _is_dir:
            cmds.append(["grep", "-rn", "--", _safe_pat, path])
        else:
            cmds.append(["grep", "-n", "--", _safe_pat, path])

    _any_executed = False
    _backend_completed = False
    _LINE_CAP = 50

    async def _run_one(argv: list[str]):
        nonlocal _any_executed, _backend_completed
        try:
            r = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(r.communicate(), 10)
            _any_executed = True
            if r.returncode in (0, 1):
                _backend_completed = True
            raw_text = out.decode(errors="replace").strip()
            if not raw_text:
                return None
            safe_text = filter_secret_search_rows(raw_text).strip()
            if not safe_text:
                return None
            lines = safe_text.splitlines()
            capped = "\n".join(lines[:_LINE_CAP])
            if len(lines) > _LINE_CAP:
                capped += f"\n... ({len(lines) - _LINE_CAP} more)"
            return capped
        except Exception:
            pass
        return None

    for _argv in cmds:
        _r = await _run_one(_argv)
        if _r:
            return f"[search_code: {pattern}]\n{_r[:8000]}"
        if _backend_completed:
            return "(no matches — search completed, nothing found)"

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
    try:
        _abs_root = root.resolve()
    except (OSError, ValueError):
        return None
    if root.is_file():
        if root.is_symlink() or _is_junction(root) or is_secret_path(root, root.parent):
            return None
        files = [root]
    else:
        files = []
    if not files:
        for dp, dns, fns in os.walk(root, followlinks=False):
            dns[:] = [
                d for d in sorted(dns)
                if d not in _SKIP_SEARCH_DIRS
                and not (Path(dp) / d).is_symlink()
                and not _is_junction(Path(dp) / d)
            ]
            for fn in sorted(fns):
                if len(files) >= max_files:
                    break
                p = Path(dp) / fn
                if p.is_symlink() or _is_junction(p):
                    continue
                try:
                    p.resolve().relative_to(_abs_root)
                except (OSError, ValueError):
                    continue
                if is_secret_path(p, root):
                    continue
                try:
                    if p.stat().st_size > max_file_bytes:
                        continue
                except OSError:
                    continue
                files.append(p)
            if len(files) >= max_files:
                break
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
