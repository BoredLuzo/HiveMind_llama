"""Tool handlers: code-intelligence tools (signatures/references/search) (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from utils.file import (
    _inline_resolve_path,
    _inline_check_workspace,
    _is_junction,
    _is_protected_path,
    is_secret_path,
    filter_secret_search_rows,
)
from tools.errors import tool_error_response as _tool_error_response
import asyncio
import fnmatch
import os
import re
import shutil
import stat
import sys
import time

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
    try:
        max_items = int(args.get("max_items") or 400)
    except (TypeError, ValueError):
        max_items = 400
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
    try:
        max_items = int(args.get("max_items") or 160)
    except (TypeError, ValueError):
        max_items = 160
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


_FIND_LIMIT = 150
_MAX_SEARCH_BYTES = 256 * 1024


def _is_unsafe_regex(pattern: str) -> bool:
    """Detect common catastrophic-backtracking (ReDoS) regex shapes."""
    if not pattern or len(pattern) > 500:
        return len(pattern) > 500
    if re.search(r"(?:\.\*|\.\+)\s*(?:\.\*|\.\+)", pattern):
        return True
    if re.search(r"\\[1-9](?:[*+?]|\{\d)", pattern):
        return True
    stack: list[bool] = []
    in_class = False
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if in_class:
            if ch == "]":
                in_class = False
            i += 1
            continue
        if ch == "[":
            in_class = True
            if i + 1 < n and pattern[i + 1] == "^":
                i += 2
            else:
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            continue
        if ch == "(":
            stack.append(False)
            i += 1
            continue
        if ch == ")" and stack:
            inner_q = stack.pop()
            nxt = pattern[i + 1] if i + 1 < n else ""
            has_outer_rep = nxt in "*+" or (
                nxt == "{" and i + 2 < n and pattern[i + 2].isdigit()
            )
            if inner_q and has_outer_rep:
                return True
            if (inner_q or has_outer_rep) and stack:
                stack[-1] = True
            i += 1
            continue
        if stack and (
            ch in "*+"
            or (ch == "{" and i + 1 < n and pattern[i + 1].isdigit())
        ):
            stack[-1] = True
        i += 1
    return False


def _lstat_regular_nonlink(fp: Path) -> bool:
    """Return True iff *fp* is a non-symlink, non-reparse regular file in 1 syscall."""
    try:
        st = os.lstat(fp)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    if sys.platform == "win32" and (getattr(st, "st_file_attributes", 0) & 0x400):
        return False
    return stat.S_ISREG(st.st_mode)


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
            if is_secret_path(rel.as_posix()):
                return False
            cur = base
            for part in rel.parts[:-1]:
                cur = cur / part
                if cur.is_symlink() or _is_junction(cur):
                    return False
            if not _lstat_regular_nonlink(fp):
                return False
            try:
                fp_res = fp.resolve()
            except (OSError, RuntimeError):
                return False
            if not (fp_res.is_relative_to(base_resolved) and fp_res.is_relative_to(lock_resolved)):
                return False
            if _is_protected_path(fp_res):
                return False
            return not is_secret_path(fp_res, base_resolved)

        def _dir_allowed(dp: Path) -> bool:
            if dp.is_symlink() or _is_junction(dp):
                return False
            try:
                dp_res = dp.resolve()
            except (OSError, RuntimeError):
                return False
            return dp_res.is_relative_to(base_resolved) and dp_res.is_relative_to(lock_resolved)

        def _collect() -> tuple[list[Path], bool]:
            found: list[Path] = []
            if "**" not in pattern:
                for m in sorted(base.glob(pattern)):
                    if _entry_allowed(m):
                        found.append(m)
                        if len(found) > _FIND_LIMIT:
                            return found[:_FIND_LIMIT], True
                return found, False

            _has_full_match = hasattr(PurePosixPath, "full_match")
            _glob_rx = None
            if not _has_full_match:
                _pat_rx = re.escape(pattern).replace(r"/\*\*/", r"(?:/|/.+/)")
                if _pat_rx.startswith(r"\*\*/"):
                    _pat_rx = r"(?:.+/)?" + _pat_rx[5:]
                _pat_rx = _pat_rx.replace(r"\*\*", r".*").replace(r"\*", r"[^/]*").replace(r"\?", r"[^/]")
                _glob_rx = re.compile(rf"^{_pat_rx}$")
            for dp, dns, fns in os.walk(base, followlinks=False):
                dp_path = Path(dp)
                dns[:] = [
                    d for d in sorted(dns)
                    if d not in _SKIP_SEARCH_DIRS and _dir_allowed(dp_path / d)
                ]
                for fn in sorted(fns):
                    fp = dp_path / fn
                    rel_s = fp.relative_to(base).as_posix()
                    _matched_fn = (
                        PurePosixPath(rel_s).full_match(pattern)
                        if _has_full_match
                        else bool(_glob_rx.match(rel_s))
                    )
                    if not _matched_fn and "/" not in pattern:
                        _matched_fn = fnmatch.fnmatch(fn, pattern)
                    if not _matched_fn:
                        continue
                    if is_secret_path(rel_s):
                        continue
                    if not _lstat_regular_nonlink(fp):
                        continue
                    if _is_protected_path(fp):
                        continue
                    found.append(fp)
                    if len(found) > _FIND_LIMIT:
                        return found[:_FIND_LIMIT], True
            return found, False

        files, has_more = await asyncio.to_thread(_collect)
        if not files:
            return f"(no matches for '{pattern}' in {base})"
        lines = []
        for m in files:
            try:
                rel = m.relative_to(base)
            except ValueError:
                rel = m
            lines.append(str(rel))
        result = "\n".join(lines)
        if has_more:
            result += f"\n… (more than {_FIND_LIMIT} matches — narrow pattern or path)"
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
    _sc_raw = _inline_resolve_path(workspace, args.get("path", "."))
    if err := _inline_check_workspace(_sc_raw, _workspace_lock, "search_code"):
        return err
    try:
        _sc_p = _sc_raw.resolve()
    except OSError:
        _sc_p = _sc_raw
    if err := _inline_check_workspace(_sc_p, _workspace_lock, "search_code"):
        return err
    path = str(_sc_p)
    _is_dir = _sc_p.is_dir()
    _is_win = sys.platform == "win32"

    # ARGV, NOT SHELL (audit 2026-10-17): search_code is a READ-ONLY tool that
    # runs without an approval prompt, so it must never hand a shell a string
    # built from model input. Every backend runs directly via create_subprocess_exec
    # argv (no shell metacharacter stripping needed).
    cmds: list[list[str]] = []
    if shutil.which("rg"):
        cmds.append(["rg", "--no-heading", "-n", "--", pattern, path])
    if _is_win:
        if not _is_dir and not _HAS_REGEX_META.search(pattern) and "\n" not in pattern and "\r" not in pattern:
            cmds.append(["findstr", "/n", "/L", f"/C:{pattern}", path])
    else:
        if _is_dir:
            cmds.append(["grep", "-rn", "--", pattern, path])
        else:
            cmds.append(["grep", "-n", "--", pattern, path])

    _any_executed = False
    _backend_completed = False
    _LINE_CAP = 50

    async def _drain_stream(stream, limit: int, stop_on_cap: bool = True) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        chunks: list[bytes] = []
        total = 0
        capped = False
        while True:
            chunk = await stream.read(16384)
            if not chunk:
                break
            if not capped:
                room = limit - total
                if len(chunk) >= room:
                    if room > 0:
                        chunks.append(chunk[:room])
                        total += room
                    capped = True
                    if stop_on_cap:
                        break
                else:
                    chunks.append(chunk)
                    total += len(chunk)
        return b"".join(chunks), capped

    async def _run_one(argv: list[str]):
        nonlocal _any_executed, _backend_completed
        r = None
        try:
            r = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _capped_out = False
            if getattr(r, "stdout", None) is not None:
                _t_out = asyncio.create_task(_drain_stream(r.stdout, _MAX_SEARCH_BYTES, True))
                _t_err = asyncio.create_task(_drain_stream(r.stderr, 8192, False))
                try:
                    out, _capped_out = await asyncio.wait_for(_t_out, 10)
                    if _capped_out:
                        from tools.sandbox import kill_tree_and_reap as _ci_kill_reap
                        await _ci_kill_reap(r)
                    else:
                        await asyncio.wait_for(r.wait(), 5)
                    await asyncio.wait_for(_t_err, 2)
                finally:
                    for _t in (_t_out, _t_err):
                        if not _t.done():
                            _t.cancel()
                    await asyncio.gather(_t_out, _t_err, return_exceptions=True)
                    _tr = getattr(r, "_transport", None)
                    if _tr is not None:
                        _tr.close()
            else:
                out, _ = await asyncio.wait_for(r.communicate(), 10)
            _any_executed = True
            if _capped_out or r.returncode in (0, 1):
                _backend_completed = True
            raw_text = out.decode(errors="replace").strip()
            if not raw_text:
                return None
            safe_text = (
                filter_secret_search_rows(raw_text, _workspace_lock or workspace)
                if _is_dir else raw_text
            ).strip()
            if not safe_text:
                return None
            lines = safe_text.splitlines()
            capped = "\n".join(lines[:_LINE_CAP])
            if len(lines) > _LINE_CAP:
                capped += f"\n... ({len(lines) - _LINE_CAP} more)"
            elif _capped_out:
                capped += "\n... (output truncated)"
            return capped
        except Exception:
            if r is not None and getattr(r, "returncode", 0) is None:
                from tools.sandbox import kill_tree_and_reap as _ci_kill_reap
                await _ci_kill_reap(r)
        return None

    for _argv in cmds:
        _r = await _run_one(_argv)
        if _r:
            if len(_r) > 8000:
                _r = _r[:7960] + "\n... (output truncated)"
            return f"[search_code: {pattern}]\n{_r}"
        if _backend_completed:
            return "(no matches — search completed, nothing found)"

    # PYTHON-FALLBACK (3rd backend): original pattern, ReDoS- and time-bounded
    _fb_error = False
    try:
        _fb = await asyncio.to_thread(_python_content_search, _sc_p, pattern)
    except ValueError as _ve:
        return _tool_error_response("INVALID_REGEX", str(_ve), tool="search_code")
    except Exception:
        _fb = None
        _fb_error = True
    if _fb:
        if len(_fb) > 8000:
            _fb = _fb[:7960] + "\n... (output truncated)"
        return f"[search_code: {pattern}]\n{_fb}"
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
    max_seconds: float = 5.0,
    max_line_chars: int = 4096,
) -> str | None:
    if _is_unsafe_regex(pattern):
        raise ValueError(
            "Unsafe regex pattern (potential catastrophic backtracking / ReDoS); "
            "simplify nested quantifiers or use a literal search."
        )
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
        try:
            if root.stat().st_size > max_file_bytes:
                return None
        except OSError:
            return None
        files = [root]
    else:
        files = []
    if not files:
        for dp, dns, fns in os.walk(root, followlinks=False):
            dp_path = Path(dp)
            _kept_dirs: list[str] = []
            for d in sorted(dns):
                if d in _SKIP_SEARCH_DIRS:
                    continue
                sub = dp_path / d
                if not sub.is_symlink() and not _is_junction(sub):
                    _kept_dirs.append(d)
            dns[:] = _kept_dirs
            for fn in sorted(fns):
                if len(files) >= max_files:
                    break
                p = dp_path / fn
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
    deadline = time.monotonic() + max_seconds
    hits: list[str] = []
    for fp in files:
        if time.monotonic() > deadline:
            if hits:
                hits.append("... (search timed out)")
                return "\n".join(hits)
            return None
        try:
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
            if (ln & 255) == 0 and time.monotonic() > deadline:
                if hits:
                    hits.append("... (search timed out)")
                    return "\n".join(hits)
                return None
            if rx.search(line[:max_line_chars]):
                hits.append(f"{fp}:{ln}: {line.strip()[:200]}")
                if len(hits) >= max_results:
                    hits.append("... (truncated)")
                    return "\n".join(hits)
    return "\n".join(hits) if hits else None
