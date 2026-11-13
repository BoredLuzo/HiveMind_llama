"""Tool handlers: file read/write/edit tools (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from utils.file import _inline_resolve_path, _inline_check_workspace
from tools.errors import tool_error_response as _tool_error_response
import asyncio
from tools.workspace import get_transaction
import json
import os
import re
import sys

from . import _shared

_JSON_EDIT_KEY_NEW = ("new_str", "new_string", "replace")

_JSON_EDIT_KEY_OLD = ("old_str", "old_string", "search")

from .linting import _auto_lint_result


def _recheck_workspace_before_replace(p, workspace_lock, tool_name):
    """T6 (audit r3): the containment check ran at handler entry; a junction
    swapped into p.parent between check and write redirected the write.
    Returns the tool-error response on violation, None when contained.
    Call sites sit BEFORE the write closures so nothing is written on a
    violation (and a violation surfaces as a tool error, not a crash)."""
    from utils.file import _inline_check_workspace as _c
    return _c(p, workspace_lock, tool_name)

_MAX_TMP_TRIES = 16


def _atomic_write(path, data: str) -> None:
    """Atomic file write: bounded temp file in the target dir + os.replace.
    Creates missing parent dirs; unlinks the temp file on any failure.
    THE write path for every file-editing tool (six hand-rolled copies
    collapsed here)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # MODE-PRESERVE (2026-10-27, audit): the temp file is created 0o600, so
    # os.replace used to strip the executable bit and group/other rights from
    # every rewritten file on POSIX. Windows has no such mode, hence unseen.
    _prev_mode = None
    try:
        _prev_mode = os.stat(path).st_mode & 0o7777
    except OSError:
        _prev_mode = None
    _tmp_fd, _tmp_path = _mkstemp_bounded(path.parent, ".tmp")
    try:
        with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
            _f.write(data)
        if _prev_mode is not None and os.name != "nt":
            try:
                os.chmod(_tmp_path, _prev_mode)
            except OSError:
                pass
        os.replace(_tmp_path, str(path))
    except Exception:
        try:
            os.unlink(_tmp_path)
        except Exception:
            pass
        raise


async def _checked_atomic_write(path, data: str, workspace_lock, tool: str):
    """Containment re-check BEFORE any byte is written (T6), then the
    atomic write off the event loop. Returns the error response when the
    re-check fails, else None."""
    _chk = _recheck_workspace_before_replace(path, workspace_lock, tool)
    if _chk:
        return _chk
    await asyncio.to_thread(_atomic_write, path, data)
    return None


def _write_error_code(exc: BaseException) -> str | None:
    """Classify an OS error from the atomic write into a tool error code.

    Linux: EROFS (read-only mount), EACCES/EPERM (Permissions), ENOSPC (disk
    full), ENOENT/ENOTDIR (parent vanished). Windows: PermissionError covers
    EACCES and the sharing violations (WinError 32), which is what an ACL or a
    locked-down sandbox produces. Anything else returns None so the caller
    re-raises - a real bug must not be dressed up as a permission problem.
    """
    import errno as _errno
    if isinstance(exc, PermissionError):
        return "WRITE_FILE_PERMISSION"
    _eno = getattr(exc, "errno", None)
    if _eno == getattr(_errno, "EROFS", None):
        return "WRITE_FILE_READ_ONLY"
    if _eno == getattr(_errno, "ENOSPC", None):
        return "WRITE_FILE_NO_SPACE"
    if _eno in (getattr(_errno, "ENOENT", None), getattr(_errno, "ENOTDIR", None)):
        return "WRITE_FILE_NO_DIR"
    return None


# FOLDER-HINT: a write to Desktop or Documents
# that fails with a real EACCES/WinError 32 is almost always one of two Windows
# features, not a path problem. Naming them saves the model a round of flailing.
_PROTECTED_FOLDER_HINTS = (
    "Common cause on Windows: Controlled Folder Access (Windows Security -> "
    "Ransomware protection) blocks non-whitelisted programs in Desktop, Documents "
    "and Pictures, and a folder synced by OneDrive can hold a file lock. "
    "Allow python.exe of the HiveMind venv there, or pick a workspace outside "
    "those folders (for example C:\\HiveMind_ws)."
)


def _protected_folder_hint(parent: str) -> str:
    """Extra sentence for Desktop/Documents/Pictures, empty elsewhere."""
    p = str(parent or "").replace("\\", "/").lower()
    for marker in ("/desktop", "/documents", "/pictures", "/onedrive"):
        if marker in p:
            return " " + _PROTECTED_FOLDER_HINTS
    return ""


_WRITE_ERROR_TEXT = {
    "WRITE_FILE_PERMISSION": (
        "the engine process has no write permission for '{parent}'. This is an "
        "environment restriction, not a path typo - choose a workspace inside a "
        "writable folder or start HiveMind outside the restricting sandbox."
        "{hint}"),
    "WRITE_FILE_READ_ONLY": (
        "'{parent}' is on a read-only filesystem - pick a writable workspace"),
    "WRITE_FILE_NO_SPACE": (
        "'{parent}' has no space left - free some disk and retry"),
    "WRITE_FILE_NO_DIR": (
        "the folder '{parent}' does not exist (or a path component is a file) - "
        "create it first or pick another path"),
}

def _mkstemp_bounded(parent: Path, suffix: str = ".tmp"):
    """Create a unique temp file in ``parent`` - bounded, never retries a denial.

    ``tempfile.mkstemp`` retries ``PermissionError`` up to ``TMP_MAX`` (2**31-1 on
    Windows) while ``os.access(parent, os.W_OK)`` says the folder is writable - and
    on Windows that check looks at the read-only attribute, not the ACL, so it is
    nearly always True. A process that may not write into the folder (AppContainer
    sandbox, ACL, locked-down drive) therefore spun forever: the write_file hang of
    2026-10-19 (11 minutes, 100 % CPU, no file on disk).

    This is the same creation semantics without the internal loop: at most 16
    names, a real ``PermissionError`` on the first attempt propagates immediately.
    """
    import secrets as _secrets
    last: Exception | None = None
    for _ in range(16):
        path = parent / f"hm-{_secrets.token_hex(8)}{suffix}"
        flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        try:
            return os.open(str(path), flags, 0o600), path
        except FileExistsError as exc:      # collision: try another name
            last = exc
            continue
        # PermissionError (and anything else) propagates: not a collision.
    raise last if last else OSError("could not create a temporary file")


def _read_binary_error(p) -> str:
    """READ-FIX helper: konsistente Binary-Fehlerantwort (sys.platform-aware)."""
    _inspect_hint = (
        "Use run_bash with 'Get-Content -Encoding Byte {p} | Select-Object -First 64' "
        "to inspect binary content."
        if sys.platform == "win32"
        else "Use run_bash with 'file {p}' to identify the file type, "
             "or 'xxd {p} | head' to inspect binary content."
    )
    return _tool_error_response(
        "BINARY_FILE",
        f"'{p}' appears to be a binary file (image, compiled binary, archive, etc.) "
        "and cannot be read as text. "
        + _inspect_hint,
        tool="read_file")


def _read_head_bytes(p: Path, n: int = 1024) -> bytes:
    """READ-FIX helper: liest nur den Dateianfang (kein Voll-Read)."""
    try:
        with open(p, "rb") as f:
            return f.read(n)
    except Exception:
        return b""


def _looks_binary_bytes(b: bytes) -> bool:
    """READ-FIX helper: Binary-Sniff auf Bytes (NUL + Steuerzeichen)."""
    if not b:
        return False
    _binary = sum(1 for c in b if c == 0 or (c < 32 and c not in (10, 13, 9)))
    return (_binary / len(b)) > 0.10


def _fast_newline_count(p: Path, cap: int = 401) -> tuple:
    """READ-FIX helper: gestreamter Newline-Count.

    Bricht bei cap ab -> liefert (cap, True). Liefert (count, False) sonst.
    Verhindert, dass eine grosse Datei nur zum Zaehlen komplett als String+Liste
    in den Speicher geladen wird.
    """
    _count = 0
    try:
        with open(p, "rb") as f:
            while True:
                _chunk = f.read(1 << 16)
                if not _chunk:
                    break
                _count += _chunk.count(b"\n")
                if _count >= cap:
                    return cap, True
    except Exception:
        return 0, False
    return _count, False


async def _inline_tool_read_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "read_file"):
        return err
    start_line = args.get("start_line")
    end_line = args.get("end_line")
    _full = start_line is None and end_line is None
    # RE-READ DEDUP (2026-10-08, user feedback: one round re-read one file 12 times): a full
    # read of an UNCHANGED file returns a compact confirmation - the previous
    # read's content is already in the model context and does not need to be
    # paid for twice. Range reads and first reads always return content.
    _rr_unchanged = False
    try:
        from tools.runner import (_files_read_in_run as _rr_read_set,
                                  _read_signatures as _rr_sig,
                                  _normalize_tool_path as _rr_ntp)
        # _read_signatures is a FLAT {path: sig} dict (runner.py seeds it
        # directly) — a .get(None) here copied the _files_read_in_run
        # ContextVar idiom, always missed, and the dedup never fired.
        # The signature dict is module-global (survives across runs): require
        # a read THIS RUN as well — the same gate as the fuzzy-edit stale-gate
        # below — or a fresh run gets "UNCHANGED" for content it never saw.
        _rr_key = _rr_ntp(str(p), workspace)
        _rr_prev = _rr_sig.get(_rr_key)
        _rr_st = p.stat()
        _rr_cur = (_rr_st.st_mtime_ns, _rr_st.st_size)
        _rr_this_run = _rr_key in (_rr_read_set.get(None) or set())
        if _rr_this_run and _rr_prev is not None and _rr_prev == _rr_cur:
            _rr_unchanged = True
    except Exception:
        pass
    if _full and _rr_unchanged:
        try:
            _rr_nl = await asyncio.to_thread(_fast_newline_count, p, 100000)
            # returns (count, capped) — show the count, not the tuple
            _rr_disp = str(_rr_nl[0]) if isinstance(_rr_nl, tuple) else str(_rr_nl)
        except Exception:
            _rr_disp = "?"
        return (f"[read_file: '{p.name}' is UNCHANGED since your last read "
                f"({_rr_disp} lines) - the content in your context above is still "
                "current. Use start_line/end_line to force a full re-read.]")
    try:
        # READ-EARLY-ABORT (2026-09-04, budget-driven 2026-10-14): full reads
        # (no range) are refused by ESTIMATED SIZE against the run's read
        # budget (core/budgets — derived from n_ctx, no fixed line staffel).
        # Cheap stat() up front, no streaming count needed anymore.
        from core.budgets import active as _bgt_active
        _bgt = _bgt_active()
        if _full:
            _head = await asyncio.to_thread(_read_head_bytes, p, 1024)
            if _looks_binary_bytes(_head):
                return _read_binary_error(p)
            _size_chars = p.stat().st_size
            if _size_chars > _bgt.read_char_cap:
                _sig_fn = _shared.get_signatures_report if callable(_shared.get_signatures_report) else None
                _sig = await asyncio.to_thread(_sig_fn, p, _bgt.outline_items) if _sig_fn else (
                    "(signature extraction unavailable) — use read_file with line_range=[1,50] to inspect the file header first."
                )
                return _tool_error_response(
                    "FILE_TOO_LARGE_NEED_RANGE",
                    f"File '{p}' is ~{_size_chars} chars — above this run's read budget "
                    f"({_bgt.read_char_cap} chars; derived from the context size). "
                    "THE FILE ON DISK IS COMPLETE - this is only a read-size guard, NOT a broken "
                    "or partial write. Do not rewrite or delete the file because a range read "
                    "shows only part of it. "
                    "Please use 'start_line' and 'end_line' inside read_file to read specific sections.\n\n"
                    f"File Outline / Signatures:\n{_sig}",
                    tool="read_file" )

        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace")
        lines = content.splitlines(keepends=True)

        # Binary file detection (range-reads; full reads were already caught
        # above via the head sniff). Check first 512 chars for NUL bytes and
        # control characters (excluding \n \r \t).
        _sample = content[:512]
        if _sample:
            _binary_chars = sum(
                1 for c in _sample
                if ord(c) == 0 or (ord(c) < 32 and c not in "\n\r\t")
            )
            if len(_sample) > 0 and (_binary_chars / len(_sample)) > 0.10:
                return _read_binary_error(p)

        # Note: full reads above the run's read budget abort early above
        # (READ-EARLY-ABORT) - only smaller files reach the char cap.

        # Hallucination guard: validate start_line/end_line against actual file length
        _actual_lines = len(lines)
        if start_line is not None and int(start_line) > _actual_lines:
            return _tool_error_response(
                "HALLUCINATION_GUARD",
                f"start_line={start_line} exceeds file length ({_actual_lines} lines). "
                f"Call read_file('{args.get('path')}') WITHOUT start_line/end_line to read the full file.",
                tool="read_file")
        if end_line is not None and int(end_line) > _actual_lines * 2:
            end_line = _actual_lines

        if start_line is not None or end_line is not None:
            s = max(0, int(start_line) - 1) if start_line else 0
            e = int(end_line) if end_line else len(lines)
            content_chunk = "".join(lines[s:e])
            _head = f"[{p} lines {s+1}-{min(e, len(lines))} / {len(lines)}]\n"
            if len(content_chunk) > _bgt.read_char_cap:
                # TRUNCATION-HONESTY (2026-10-15): header + note must fit
                # INSIDE the char cap together (the executor re-cap would
                # otherwise cut exactly this note off), and the note must
                # carry the full file's line AND char count plus the concrete
                # continuation line - a bare chunk read as "the write did not
                # complete" (live: 482-line write, 148-line range read).
                _cont = min(e, len(lines)) + 1
                _note = (
                    f"\n[TRUNCATED: showing the first {_bgt.read_char_cap} chars of this range — "
                    f"file total: {len(lines)} lines / {len(content)} chars — "
                    f"continue with start_line={_cont}]"
                )
                _avail = max(0, _bgt.read_char_cap - len(_head) - len(_note))
                _chunk = content_chunk[:_avail] + _note
            else:
                _chunk = content_chunk
            return _head + _chunk

        _chunk = content[:_bgt.read_char_cap]
        if len(content) > _bgt.read_char_cap:
            _chunk += f"\n[TRUNCATED: {len(content)-_bgt.read_char_cap} more chars omitted — use start_line/end_line to read sections]"
        return f"[{p} total lines: {len(lines)}]\n" + _chunk
    except Exception as e:
        return _tool_error_response(
            "FILE_READ_FAILED",
            f"Failed to read file '{p}': {e} — try list_dir(parent_directory) to check if the file exists, or find_files(pattern) to locate it.",
            tool="read_file" )


async def _inline_tool_write_file_append(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "write_file_append"):
        return err  # H-audit: containment BEFORE the exists probe (oracle)
    if not p.exists():
        return _tool_error_response(
            "FILE_NOT_FOUND",
            "write_file_append requires an EXISTING file — create it with write_file first.",
            tool="write_file_append")
    if p.is_dir():
        return _tool_error_response(
            "FILE_IS_DIRECTORY",
            f"write_file_append target '{p}' is a directory, not a file.",
            tool="write_file_append")
    content = str(args.get("content", ""))
    get_transaction().capture_before(p)

    if not str(content or ""):
        return _tool_error_response(
            "WRITE_FILE_APPEND_EMPTY",
            "write_file_append requires non-empty content.",
            tool="write_file_append")

    # Normal append: the content already arrived in full, so append it in one
    # go (no artificial chunking - splitting would only force the model to
    # regenerate the same content again).
    # NEWLINE-BOUNDARY (2026-09-15): if the file does not end with a newline
    # and the chunk does not start with one, the first appended line GLUED
    # onto the last existing line (looked like "append added only one line").
    # SEPARATE-WRITE-AND-REPORT (2026-09-30): the try below ends when the
    # append is durable. Nothing after it may produce a FAILED response
    # anymore — a post-write error made the model retry an append that HAD
    # succeeded and duplicate the content (live: _split_discard_note NameError
    # in e14f64f). _auto_lint_result is internally guarded and never raises.
    _boundary = {"note": ""}
    try:
        def _append_and_size() -> int:
            # T6-APPEND (audit 2026-11-03): append bypassed the containment
            # re-check the atomic writers perform; a junction swapped in after
            # the entry check redirected the write.
            _chk = _recheck_workspace_before_replace(
                p, workspace_lock, "write_file_append")
            if _chk:
                raise PermissionError("workspace containment re-check failed")
            if not p.is_file():
                raise FileNotFoundError(f"write_file_append target '{p}' is missing or not a file")
            _head = ""
            if p.stat().st_size:
                with open(p, "r", encoding="utf-8", errors="replace", newline="") as _rf:
                    _head = _rf.read()
            _body = content
            if _head and not _head.endswith("\n") and not _body.startswith("\n"):
                _body = "\n" + _body
                _boundary["note"] = " (newline boundary inserted)"
            with open(p, "a", encoding="utf-8", newline="") as f:
                f.write(_body)
            return p.stat().st_size

        total = await asyncio.to_thread(_append_and_size)
    except Exception as e:
        return _tool_error_response(
            "WRITE_FILE_APPEND_FAILED",
            f"write_file_append failed for '{p}': {e}",
            tool="write_file_append" )
    _appended = ("\n" + content) if _boundary["note"] else content
    lines = _appended.count("\n")
    _lint = await _auto_lint_result(p, workspace)
    return f"[Appended: {p} (+{lines} lines, total {total} bytes)]{_boundary['note']}{_lint}"


async def _inline_tool_patch_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "patch_file"):
        return err
    old_str = args.get("old_str", "")
    new_str = args.get("new_str", "")
    get_transaction().capture_before(p)
    if not old_str:
        return _tool_error_response(
            "PATCH_FILE_EMPTY_OLD_STR",
            "patch_file requires non-empty old_str.",
            tool="patch_file" )
    try:
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace", newline="")  # CRLF-PRESERVE (2026-09-13)
        _has_crlf = "\r\n" in content
        _cnorm = content.replace("\r\n", "\n")
        _onorm = old_str.replace("\r\n", "\n")
        _nnorm = new_str.replace("\r\n", "\n")
        count = _cnorm.count(_onorm)

        _fuzzy_applied = False
        if count == 0:
            def _strip_lines(s: str) -> str:
                return "\n".join(l.rstrip() for l in s.splitlines())

            _cnorm_stripped = _strip_lines(_cnorm)
            _onorm_stripped = _strip_lines(_onorm)
            _fuzzy_count = _cnorm_stripped.count(_onorm_stripped)

            if _fuzzy_count == 1:
                _first_line_stripped = _onorm.splitlines()[0].strip() if _onorm.strip() else ""
                _fuzzy_start = -1
                _old_line_count = _onorm.count("\n") + 1
                _clines = _cnorm.splitlines()
                for _li, _cl in enumerate(_clines):
                    if _cl.strip() == _first_line_stripped:
                        _old_lines_s = [l.strip() for l in _onorm.splitlines()]
                        _file_slice = [_clines[_li + i].strip() if _li + i < len(_clines) else "" for i in range(_old_line_count)]
                        if _old_lines_s == _file_slice:
                            _fuzzy_start = _li
                            break
                if _fuzzy_start >= 0:
                    _pre = "\n".join(_clines[:_fuzzy_start])
                    _post = "\n".join(_clines[_fuzzy_start + _old_line_count:])
                    _patched = (_pre + "\n" if _pre else "") + _nnorm + ("\n" + _post if _post else "")
                    new_content = _patched.replace("\n", "\r\n") if _has_crlf else _patched

                    _chk = await _checked_atomic_write(p, new_content, workspace_lock, "patch_file")
                    if _chk:
                        return _chk  # T6: containment re-check BEFORE any byte is written
                    added = new_str.count("\n") + (1 if new_str else 0)
                    removed = old_str.count("\n") + (1 if old_str else 0)
                    _fuzzy_applied = True
                    _lint = await _auto_lint_result(p, workspace)
                    _snippet = _old_str_snippet(old_str)
                    return f"[patch_file: {p} patched (+{added}/-{removed} lines, fuzzy-whitespace match)]{_snippet}{_lint}"
            elif _fuzzy_count > 1:
                return _tool_error_response(
                    "PATCH_FILE_NON_UNIQUE_MATCH",
                    (
                        f"patch_file old_str not unique after stripped matching ({_fuzzy_count} matches). "
                        "Add more context to old_str."
                    ),
                    tool="patch_file" )

        if count == 0 and not _fuzzy_applied:
            try:
                from tools.patch_2_fuzzy_edit import fuzzy_replace as _p2_fzr
                _p2_result = _p2_fzr(_cnorm, _onorm, _nnorm)
            except Exception:
                _p2_result = None
            if _p2_result is not None:
                _nc_p2 = _p2_result.replace("\n", "\r\n") if _has_crlf else _p2_result
                _chk = await _checked_atomic_write(p, _nc_p2, workspace_lock, "patch_file")
                if _chk:
                    return _chk  # T6: containment re-check BEFORE any byte is written
                _lint = await _auto_lint_result(p, workspace)
                _snippet = _old_str_snippet(old_str)
                return f"[patch_file: {p} patched (fuzzy-jaccard match)]{_snippet}{_lint}"
            _hint = old_str.splitlines()[0][:80] if old_str.strip() else "(empty)"
            return _tool_error_response(
                "PATCH_FILE_OLD_STR_NOT_FOUND",
                f"patch_file old_str not found in '{p}'.",
                tool="patch_file" ,
                details={"hint": _hint})
        if count > 1:
            return _tool_error_response(
                "PATCH_FILE_NON_UNIQUE_MATCH",
                f"patch_file old_str appears {count} times; it must be unique.",
                tool="patch_file" )

        _patched = _cnorm.replace(_onorm, _nnorm, 1)
        new_content = _patched.replace("\n", "\r\n") if _has_crlf else _patched

        _chk = await _checked_atomic_write(p, new_content, workspace_lock, "patch_file")
        if _chk:
            return _chk  # T6: containment re-check BEFORE any byte is written

        added = new_str.count("\n") + (1 if new_str else 0)
        removed = old_str.count("\n") + (1 if old_str else 0)
        _lint = await _auto_lint_result(p, workspace)
        _snippet = _old_str_snippet(old_str)
        return f"[patch_file: {p} patched (+{added}/-{removed} lines)]{_snippet}{_lint}"
    except Exception as e:
        return _tool_error_response(
            "PATCH_FILE_FAILED",
            f"patch_file failed for '{p}': {e}",
            tool="patch_file" )


_BIG_FILE_NOTE = ("\n"
    "NOTE: this file is now ~{_lines} lines (roughly above this run's read budget of "
    "{_abort} chars) - read_file will refuse a full read, so inspect it later with "
    "start_line/end_line ranges. The file on disk is complete.")


def _missing_top_level_py_symbols(p: Path, old_src: str, new_src: str) -> list[str]:
    if p.suffix.lower() != ".py" or not old_src or not new_src:
        return []
    try:
        import ast as _ast

        def _top_names(src: str) -> set[str]:
            names: set[str] = set()
            for node in _ast.parse(src).body:
                if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                    names.add(node.name)
                elif isinstance(node, _ast.Assign):
                    for t in node.targets:
                        if isinstance(t, _ast.Name):
                            names.add(t.id)
                elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
                    names.add(node.target.id)
                elif isinstance(node, _ast.ImportFrom):
                    for alias in node.names:
                        if alias.name != "*":
                            names.add(alias.asname or alias.name)
            return {n for n in names if not n.startswith("_")}

        return sorted(_top_names(old_src) - _top_names(new_src))
    except (SyntaxError, ValueError, TypeError):
        return []


def _symbol_definition_hints(old_src: str, names: list[str], limit: int = 3) -> str:
    """Return the original source line for up to `limit` dropped symbols.

    A bare name list ("MODE") tells a small model what it lost but not how to
    restore it. The original assignment/def line is a much stronger hint and
    prevents the retry loop seen on multi_config. Best-effort only: any parse
    problem means no hint, never a failed edit.
    """
    if not old_src or not names:
        return ""
    wanted = set(names[:limit])
    lines = old_src.splitlines()
    try:
        import ast as _ast
        found: dict[str, str] = {}
        for node in _ast.parse(old_src).body:
            _hint = ""
            _names: set[str] = set()
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                _names.add(node.name)
                _hint = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
            elif isinstance(node, _ast.Assign):
                for t in node.targets:
                    if isinstance(t, _ast.Name):
                        _names.add(t.id)
                _hint = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
            elif isinstance(node, _ast.AnnAssign) and isinstance(node.target, _ast.Name):
                _names.add(node.target.id)
                _hint = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
            elif isinstance(node, _ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        _names.add(alias.asname or alias.name)
                _hint = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
            for _n in _names & wanted:
                if _hint:
                    found[_n] = _hint[:160]
        if not found:
            return ""
        return "\nKeep definition: " + "; ".join(
            f"{_n}: {found[_n]}" for _n in names[:limit] if _n in found
        )
    except (SyntaxError, ValueError, TypeError, IndexError):
        return ""


async def _inline_tool_write_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    """Create or overwrite a file with the COMPLETE content.

    The primary write tool (2026-09-17 consolidation): the result on disk is
    exactly what the model wrote — no matching, no drift, no server-side
    splitting. Complete calls always land whole; truncated calls are salvaged
    upstream (duo_runner) at the last complete line.
    """
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "write_file"):
        return err
    content = str(args.get("content", ""))
    # BLOCK-FORMAT SNIFF (2026-09-17): SEARCH/REPLACE markers belong to
    # edit_file. A truncated/malformed marker call used to reach this handler
    # as "full content" and destroy the file (live: 1259-line collapse).
    if "<<<<<<<" in content or "<parameter=" in content:
        return _tool_error_response(
            "WRITE_FILE_BLOCK_FORMAT",
            "content contains SEARCH/REPLACE or foreign call markers — that format "
            "is gone. write_file expects the COMPLETE plain file content. To change "
            "parts of an existing file use edit_file (old_text/new_text).",
            tool="write_file")
    if not content.strip():
        return _tool_error_response(
            "WRITE_FILE_EMPTY",
            "write_file requires non-empty content.",
            tool="write_file")

    existed = p.exists()
    get_transaction().capture_before(p)
    _had_crlf = False
    _old_n = 0
    if existed:
        try:
            _old_raw = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace", newline="")
            # Dominant style decides (P2, deep audit 2026-11-03): a mixed file
            # keeps its majority EOL instead of being rewritten wholesale.
            _crlf_n = _old_raw.count("\r\n")
            _lf_n = _old_raw.replace("\r\n", "").count("\n")
            _had_crlf = _crlf_n > _lf_n
            _old_n = len([l for l in _old_raw.splitlines() if l.strip()])
        except Exception:
            _old_raw, _had_crlf, _old_n = "", False, 0
    else:
        _old_raw = ""

    if content.replace("\r\n", "\n").strip() == _old_raw.replace("\r\n", "\n").strip():
        return _tool_error_response(
            "WRITE_FILE_NOOP",
            f"no change — '{p}' already contains exactly this content.",
            tool="write_file")
    if existed and _old_n >= 30:
        _new_n = len([l for l in content.replace("\r\n", "\n").splitlines() if l.strip()])
        # PROPORTIONAL SHRINK (2026-10-31, live): a 11616-byte file was
        # replaced by a 1716-byte skeleton (~40 -> 9 content lines). The
        # absolute arms let that through at _old_n < 100; a quarter of the
        # previous body is the same "almost always truncated" signal at any
        # size (confirm_shrink still allows a deliberate collapse).
        if ((_new_n <= 3
             or (_old_n >= 100 and _new_n <= 10)
             or (_old_n >= 30 and _new_n <= _old_n // 4))
                and not args.get("confirm_shrink")):
            return _tool_error_response(
                "WRITE_FILE_SUSPICIOUS_SHRINK",
                f"This overwrite would shrink '{p}' from {_old_n} to {_new_n} content "
                "lines — almost always a truncated or mistaken write. If you REALLY "
                "want a tiny file: read_file it first, then resend with "
                "\"confirm_shrink\": true.",
                tool="write_file")
    if (existed and p.suffix.lower() == ".py" and _old_raw
            and not args.get("allow_overwrite") and not args.get("confirm_shrink")):
        _dropped_syms = _missing_top_level_py_symbols(p, _old_raw, content)
        if _dropped_syms:
            return _tool_error_response(
                "WOULD_DROP_SYMBOLS",
                f"write_file on existing '{p.name}' would drop top-level symbol(s): "
                f"{', '.join(_dropped_syms[:8])}. File was left unchanged. "
                "Use edit_file to modify existing files while preserving constants, "
                "classes, functions, and re-exports, or pass \"allow_overwrite\": true "
                "if removing those symbols is intentional."
                f"{_symbol_definition_hints(_old_raw, _dropped_syms)}",
                details={"dropped": _dropped_syms[:8]},
                tool="write_file")


    # EOL-PRESERVE (2026-11-03): the payload may already contain CRLF. The
    # old code re-expanded every "\n", turning "a\r\nb" into "a\r\r\nb" on
    # disk (A2, data corruption on the main platform). Normalize the incoming
    # side first, then match the style the file already uses. A NEW file keeps
    # the payload's style (a CRLF payload must not silently become LF).
    if _had_crlf:
        content = (content.replace("\r\n", "\n").replace("\r", "\n")
                   .replace("\n", "\r\n"))
    elif existed:
        content = content.replace("\r\n", "\n").replace("\r", "\n")

    _chk = _recheck_workspace_before_replace(p, workspace_lock, "write_file")

    if _chk:

        return _chk  # T6: containment re-check BEFORE any byte is written

    try:
        await asyncio.to_thread(_atomic_write, p, content)
    except OSError as _werr:
        # SANDBOX/ACL/RO-FS (2026-10-19): the process may not write into this
        # folder. Before, tempfile.mkstemp retried a Windows denial up to TMP_MAX -
        # an 11-minute hang at 100 % CPU with no file. The four realistic causes
        # (permission, read-only mount, no space, missing parent) get their own
        # message; anything else re-raises so a real bug stays visible.
        _wcode = _write_error_code(_werr)
        if not _wcode:
            raise
        return _tool_error_response(
            _wcode,
            "Cannot write '{p}': ".format(p=p)
            + _WRITE_ERROR_TEXT[_wcode].format(
                parent=p.parent,
                hint=_protected_folder_hint(str(p.parent))),
            tool="write_file")
    _lint = await _auto_lint_result(p, workspace)
    _lines = content.count("\n") + 1
    try:
        from core.budgets import active as _bgt_note
        _note_cap = _bgt_note().read_char_cap
    except Exception:
        _note_cap = 0
    if existed:
        return f"[write_file: rewrote '{p}' (+{_lines}/-{_old_n or '?'} lines)]{_lint}{_BIG_FILE_NOTE.format(_lines=_lines, _abort=_note_cap) if _note_cap and _lines * 80 > _note_cap else ""}"
    return f"[write_file: created '{p}' (+{_lines} lines)]{_lint}{_BIG_FILE_NOTE.format(_lines=_lines, _abort=_note_cap) if _note_cap and _lines * 80 > _note_cap else ""}"


def _unique_ws_normalized_replace(
    working: str,
    old_n: str,
    new_n: str,
    *,
    allow_indent_strip: bool = True,
) -> str | None:
    """Fallback exact-ish replacement tolerant of trailing/outer whitespace drift.

    Requires a single unique line-block match (trailing-whitespace `rstrip`
    first, then optional full-line `strip` on substantial blocks with uniform
    reindentation). Returns updated text or None if 0 or >1 matches exist.
    """
    if not old_n or not old_n.strip():
        return None
    if old_n.endswith("\n") and (new_n.endswith("\n") or new_n == ""):
        old_lines = old_n[:-1].split("\n")
        new_lines = [] if new_n == "" else new_n[:-1].split("\n")
    else:
        old_lines = old_n.split("\n")
        new_lines = new_n.split("\n")
    w_lines = working.split("\n")
    blen = len(old_lines)
    if blen == 0 or len(w_lines) < blen:
        return None
    limit = len(w_lines) - blen
    exp_r = [line.rstrip() for line in old_lines]
    w_r = [line.rstrip() for line in w_lines]
    used_strip = False
    matches = [p for p in range(limit + 1) if w_r[p:p + blen] == exp_r]
    if not matches and allow_indent_strip and (
        blen >= 2 or len(old_lines[0].strip()) >= 10
    ):
        exp_s = [line.strip() for line in old_lines]
        w_s = [line.strip() for line in w_lines]
        matches = [p for p in range(limit + 1) if w_s[p:p + blen] == exp_s]
        if matches:
            used_strip = True
    if len(matches) != 1:
        return None
    pos = matches[0]
    rep_lines = new_lines
    if used_strip:
        from tools.patch_diff import reindent_matched_block
        rep_lines = reindent_matched_block(
            w_lines[pos:pos + blen], old_lines, new_lines)
        if rep_lines is None:
            return None
    return "\n".join(w_lines[:pos] + rep_lines + w_lines[pos + blen:])


def _tolerant_replace(
    p: Path,
    working: str,
    old_n: str,
    new_n: str,
    args: dict,
    workspace: Path,
) -> tuple[str | None, bool, str]:
    """Fallback replacement ladder when exact match fails:
    trailing-WS rstrip -> STALE-GATE -> indent-strip WS -> fuzzy_replace.
    Returns (new_text_or_None, used_fuzzy, stale_note).
    """
    ws_rep = _unique_ws_normalized_replace(
        working, old_n, new_n, allow_indent_strip=False)
    if ws_rep is not None:
        return ws_rep, False, ""
    fuzzy_ok, stale_note = _can_fuzzy_edit(p, args, workspace)
    if not fuzzy_ok:
        return None, False, stale_note
    ws_rep = _unique_ws_normalized_replace(
        working, old_n, new_n, allow_indent_strip=True)
    if ws_rep is not None:
        return ws_rep, False, stale_note
    try:
        from tools.patch_2_fuzzy_edit import fuzzy_replace as _p2_fzr
        fzr = _p2_fzr(working, old_n, new_n)
    except ImportError:
        fzr = None
    return fzr, (fzr is not None), stale_note


def _can_fuzzy_edit(p: Path, args: dict, workspace: Path) -> tuple[bool, str]:
    """Check STALE-GATE before allowing fuzzy fallback on edit_file."""
    _fuzzy_ok = False
    _stale_note = ""
    try:
        from tools.runner import (
            _files_read_in_run as _fri,
            _files_seen_in_run as _fsi,
            _read_signatures as _rsig,
            _normalize_tool_path as _ntp,
        )
        _rs = _fri.get(None)
        _ek = _ntp(args.get("path", ""), workspace)
        _as = _fsi.get(None)
        _fuzzy_ok = bool((_rs and _ek in _rs) or (_as and _ek in _as))
        _sig = _rsig.get(_ek)
        if _sig:
            try:
                _st = p.stat()
                _sig_now = (_st.st_mtime_ns, _st.st_size)
                if _sig_now != _sig:
                    _stale_note = (
                        " The file changed on disk since your last read_file "
                        "(or was never read this run) — read_file it again to "
                        "see its current state.")
                    _fuzzy_ok = False
            except OSError:
                _fuzzy_ok = False
    except ImportError:
        _fuzzy_ok = False
    return _fuzzy_ok, _stale_note


def _build_edit_mismatch_anchor(p: Path, text: str, old_n: str) -> str:
    """Return a 15-20 line current-file snippet near the closest match."""
    try:
        _cur = text.splitlines()
        _anchor_center = -1
        _anchor_ratio = 0.0
        try:
            from tools.patch_2_fuzzy_edit import best_match_location as _bml
            _loc = _bml("\n".join(_cur), old_n)
            if _loc:
                _anchor_center, _anchor_ratio = int(_loc[0]), float(_loc[1])
        except (ImportError, ValueError, TypeError, AttributeError):
            _anchor_center = -1
        if _anchor_center >= 0:
            try:
                from core.budgets import active as _bgt_anchor
                _aw = max(int(_bgt_anchor().anchor_lines), 9)
            except (ImportError, AttributeError, TypeError, ValueError):
                _aw = 9
            _lo = max(0, _anchor_center - _aw)
            _hi = min(len(_cur), _anchor_center + max(old_n.count("\n"), 0) + _aw + 1)
            _head = "\n".join(_cur[_lo:_hi])
            if len(_head) > 1400:
                _head = _head[:1400] + "\n... [truncated]"
            return (
                "\n\n[CURRENT CONTENT of '" + p.name + "' lines "
                + str(_lo + 1) + "-" + str(_hi)
                + " (closest match to your old_text, ratio "
                + f"{_anchor_ratio:.2f}" + ") — build your next edit_file "
                "old_text from EXACTLY this:]\n" + _head
            )
        _head = "\n".join(_cur[:25])
        if len(_head) > 1400:
            _head = _head[:1400] + "\n... [truncated]"
        return (
            "\n\n[CURRENT CONTENT of '" + p.name + "' — first "
            + str(min(25, len(_cur))) + " lines (no close match found). "
            "read_file the file and COPY a passage verbatim:]\n" + _head
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return ""


async def _inline_tool_edit_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    """Exact-match edit: replace ONE unique occurrence of old_text with new_text.

    Accepts BOTH formats: plain old_text/new_text AND SEARCH/REPLACE marker
    blocks (the models emit these naturally from pre-training). If markers are
    found in old_text, they are parsed as a block and the extracted texts are
    used for the replacement.

    D3 (2026-11-09): an optional `edits` list applies several exact
    replacements to ONE file sequentially and writes once (atomic). The
    single old_text/new_text form is unchanged.
    """
    p = _inline_resolve_path(workspace, args.get("path", ""))
    if err := _inline_check_workspace(p, workspace_lock, "edit_file"):
        return err
    if not p.exists():
        return _tool_error_response(
            "EDIT_FILE_INVALID_PATH",
            f"'{args.get('path')}' does not exist. read_file it first (to create a "
            "new file use write_file).",
            tool="edit_file")
    if args.get("edits") is not None:
        return await _inline_tool_edit_file_batch(
            args, p, workspace, workspace_lock, args.get("edits"))
    old_text = str(args.get("old_text", ""))
    new_text = str(args.get("new_text", ""))

    # MARKER-PARSE (2026-09-17): models naturally emit SEARCH/REPLACE blocks —
    # accept them instead of rejecting. If old_text contains a block, extract
    # the search and replace text from within the markers.
    _block_re = re.compile(
        r"<{5,}\s*SEARCH\s*\n(.*?)\n[ \t]*={3,}[ \t]*\n(.*?)\n[ \t]*>{5,}\s*REPLACE",
        re.DOTALL)
    _bm = _block_re.search(old_text)
    if _bm:
        old_text = _bm.group(1)
        if not new_text.strip():
            new_text = _bm.group(2)

    # FOREIGN-TRUNCATION SNIFF: a cut-off call in a foreign marker style
    # (live: <parameter=SEARCH> with no REPLACE) is genuinely malformed —
    # reject with a clear error instead of writing a fragment.
    if "<parameter=" in old_text or "<parameter=" in new_text:
        return _tool_error_response(
            "EDIT_FILE_MALFORMED_BLOCK",
            "old_text/new_text contains <parameter= fragments from a truncated "
            "tool call. The file was NOT modified. read_file the current state "
            "and resend with the correct old_text/new_text.",
            tool="edit_file")
    get_transaction().capture_before(p)
    if not old_text.strip():
        return _tool_error_response(
            "EDIT_FILE_EMPTY_OLD_TEXT",
            "edit_file requires non-empty old_text (the exact text to replace). "
            "read_file the file first, then copy the passage verbatim.",
            tool="edit_file")
    if not new_text.strip():
        return _tool_error_response(
            "EDIT_FILE_EMPTY_NEW_TEXT",
            "new_text is empty — replace the passage with the corrected version "
            "instead, or use run_bash if you truly need an empty result.",
            tool="edit_file")

    try:
        content = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace", newline="")
    except Exception as e:
        return _tool_error_response(
            "EDIT_FILE_READ_FAILED",
            f"Failed to read '{p}': {e}",
            tool="edit_file")
    _has_crlf = "\r\n" in content
    working = content.replace("\r\n", "\n")
    old_n = old_text.replace("\r\n", "\n")
    new_n = new_text.replace("\r\n", "\n")

    count = working.count(old_n)
    _first_hint = old_n.splitlines()[0][:80] if old_n.strip() else "(empty)"
    _ws_replaced: str | None = None
    _used_fuzzy = False
    if count == 0:
        _ws_replaced, _used_fuzzy, _stale_note = _tolerant_replace(
            p, working, old_n, new_n, args, workspace)
        if _ws_replaced is None:
            _anchor = _build_edit_mismatch_anchor(p, working, old_n)
            return _tool_error_response(
                "EDIT_FILE_OLD_TEXT_NOT_FOUND",
                f"old_text not found in '{p}' (exact or fuzzy).\n"
                f"  Looking for: {_first_hint!r}\n"
                "  read_file the file and COPY the passage verbatim — check indentation "
                "and whitespace; add surrounding lines to make it unique."
                + (_stale_note or "") + _anchor,
                tool="edit_file")
    if count > 1:
        return _tool_error_response(
            "EDIT_FILE_NOT_UNIQUE",
            f"old_text appears {count} times in '{p}' — it must be unique. Add "
            "surrounding lines from read_file to make it unique.",
            tool="edit_file")

    # SHRINK-GUARD (2026-09-17; 2026-10-03 review fix): measure the file's
    # line count BEFORE the replacement - the old code measured AFTER and
    # compared the result with itself, so the guard never fired (a single
    # malformed edit could collapse a large file silently).
    _pre_n = len([l for l in working.splitlines() if l.strip()])
    working = _ws_replaced if _ws_replaced is not None else working.replace(old_n, new_n, 1)
    if working.strip() == content.replace("\r\n", "\n").strip():
        return _tool_error_response(
            "EDIT_FILE_NOOP",
            f"no change — '{p}' already contains exactly this content.",
            tool="edit_file")
    _after_n = len([l for l in working.splitlines() if l.strip()])
    if (not args.get("confirm_shrink") and _pre_n >= 30
            and (_after_n <= 3
                 or (_pre_n >= 100 and _after_n <= _pre_n // 10)
                 # same proportional arm as write_file (2026-10-31)
                 or _after_n <= _pre_n // 4)):
        return _tool_error_response(
            "EDIT_FILE_SUSPICIOUS_SHRINK",
            f"This replacement would shrink '{p}' from {_pre_n} to {_after_n} content "
            "lines — almost always a malformed edit, not a real refactor. If you "
            "REALLY want that: read_file first, then resend with "
            "\"confirm_shrink\": true.",
            tool="edit_file")

    final = working.replace("\n", "\r\n") if _has_crlf else working

    if not args.get("confirm_shrink") and not args.get("allow_overwrite"):
        _dropped_ed = _missing_top_level_py_symbols(p, content, working)
        if _dropped_ed:
            _dlist_ed = ", ".join(_dropped_ed[:10])
            return _tool_error_response(
                "WOULD_DROP_SYMBOLS",
                f"Edit to '{p.name}' would remove existing top-level symbol(s): "
                f"{_dlist_ed}. Keep existing module-level constants, functions, "
                "classes, and re-exports (or re-export moved symbols via "
                "'from new_module import symbol'). The file on disk was NOT modified."
                f"{_symbol_definition_hints(content, _dropped_ed)}",
                tool="edit_file")
    _chk = await _checked_atomic_write(p, final, workspace_lock, "edit_file")

    if _chk:

        return _chk  # T6: containment re-check BEFORE any byte is written

    _old_lines_n = content.count("\n") + 1
    _new_lines_n = final.count("\n") + 1
    _delta = _new_lines_n - _old_lines_n
    _lint = await _auto_lint_result(p, workspace)
    if _used_fuzzy:
        return f"[edit_file: '{p}' edited via fuzzy match]{_lint}"
    result = f"[edit_file: '{p}' replaced ({_delta:+d} lines)]"
    if _delta == 0:
        # 0-LINES-CLARITY: same line count, different content — report the
        # char delta so the UI does not misleadingly show "0 lines".
        _chard = len(new_n) - len(old_n)
        result += f", {_chard:+d} chars"
    return result + _lint


# MULTI-EDIT (D3, 2026-11-09): several exact replacements in ONE call, ONE
# atomic write. The cap keeps a malformed batch from being one giant payload
# (the output-token limit truncates exactly such calls); more changes belong
# in several edit_file calls.
_EDIT_BATCH_MAX = 20


def _validate_edit_batch(edits) -> list[tuple[str, str]] | str:
    """Normalize+validate the `edits` list. Returns [(old, new), ...] or an
    error-message string. old_text/new_text accept the patch_file key
    spellings as aliases (old_str/old_string/search, new_str/new_string/
    replace) — small models mix those up under token pressure."""
    if not isinstance(edits, list):
        return ("'edits' must be a LIST of {old_text, new_text} objects "
                f"(got {type(edits).__name__}).")
    if not edits:
        return "'edits' is empty — pass at least one {old_text, new_text} pair."
    if len(edits) > _EDIT_BATCH_MAX:
        return (f"'edits' carries {len(edits)} entries — the limit is "
                f"{_EDIT_BATCH_MAX}. Split the change across several "
                "edit_file calls.")
    parsed: list[tuple[str, str]] = []
    for i, e in enumerate(edits):
        if not isinstance(e, dict):
            return (f"edits[{i}] is not an object — each entry needs "
                    "old_text and new_text.")
        old_e = _first_dict_value(e, ("old_text",) + _JSON_EDIT_KEY_OLD)
        new_e = _first_present_str(e, ("new_text",) + _JSON_EDIT_KEY_NEW)
        if not old_e.strip():
            return (f"edits[{i}].old_text is empty — copy the exact text "
                    "to replace from read_file.")
        if new_e is None:
            return (f"edits[{i}].new_text is missing — provide the replacement "
                    "string (or '' to delete old_text).")
        if "<parameter=" in old_e or "<parameter=" in new_e:
            return (f"edits[{i}] contains <parameter= fragments from a "
                    "truncated tool call. Resend the batch with complete "
                    "old_text/new_text pairs.")
        parsed.append((old_e.replace("\r\n", "\n"), new_e.replace("\r\n", "\n")))
    return parsed


async def _inline_tool_edit_file_batch(args: dict, p: Path, workspace: Path,
                                       workspace_lock: str | None,
                                       edits) -> str:
    """D3 multi-edit: apply edits[] SEQUENTIALLY to one in-memory copy and
    write ONCE, atomically. Every validation (occurrence counts, empty/
    malformed entries, shrink, symbol loss) runs BEFORE any byte is written —
    a rejected batch always leaves the file untouched."""
    _batch_err = _validate_edit_batch(edits)
    if isinstance(_batch_err, str):
        return _tool_error_response(
            "EDIT_FILE_BAD_EDITS",
            _batch_err + " The file was NOT modified.",
            tool="edit_file")
    parsed = _batch_err
    get_transaction().capture_before(p)
    try:
        content = await asyncio.to_thread(
            p.read_text, encoding="utf-8", errors="replace", newline="")
    except OSError as e:
        return _tool_error_response(
            "EDIT_FILE_READ_FAILED",
            f"Failed to read '{p}': {e}",
            tool="edit_file")
    _has_crlf = "\r\n" in content
    working = content.replace("\r\n", "\n")

    for i, (old_n, new_n) in enumerate(parsed):
        count = working.count(old_n)
        _first_hint = old_n.splitlines()[0][:80] if old_n.strip() else "(empty)"
        if count == 0:
            _rep, _, _stale_note = _tolerant_replace(
                p, working, old_n, new_n, args, workspace)
            if _rep is not None:
                working = _rep
                continue
            _anchor = _build_edit_mismatch_anchor(p, working, old_n)
            return _tool_error_response(
                "EDIT_FILE_OLD_TEXT_NOT_FOUND",
                f"edits[{i}]: old_text not found in '{p}' (exact or whitespace-normalized match). "
                f"Looking for: {_first_hint!r}. NOTHING was written — "
                "read_file the file and copy every old_text verbatim."
                + (_stale_note or "") + _anchor,
                tool="edit_file")
        if count > 1:
            return _tool_error_response(
                "EDIT_FILE_NOT_UNIQUE",
                f"edits[{i}]: old_text appears {count} times in '{p}' — it "
                "must be unique. NOTHING was written; add surrounding lines "
                "to make each old_text unique.",
                tool="edit_file")
        working = working.replace(old_n, new_n, 1)

    if working.strip() == content.replace("\r\n", "\n").strip():
        return _tool_error_response(
            "EDIT_FILE_NOOP",
            f"no change — '{p}' already contains exactly this content.",
            tool="edit_file")

    # SHRINK-GUARD across the whole batch (same arms as the single edit).
    _pre_n = len([l for l in content.replace("\r\n", "\n").splitlines() if l.strip()])
    _after_n = len([l for l in working.splitlines() if l.strip()])
    if (not args.get("confirm_shrink") and _pre_n >= 30
            and (_after_n <= 3
                 or (_pre_n >= 100 and _after_n <= _pre_n // 10)
                 or _after_n <= _pre_n // 4)):
        return _tool_error_response(
            "EDIT_FILE_SUSPICIOUS_SHRINK",
            f"This batch would shrink '{p}' from {_pre_n} to {_after_n} content "
            "lines — almost always a malformed edit, not a real refactor. If you "
            "REALLY want that: read_file first, then resend with "
            "\"confirm_shrink\": true.",
            tool="edit_file")

    # SYMBOL-LOSS guard across the batch: compare the ORIGINAL content with
    # the final result, so a batch cannot smuggle out what two single calls
    # would each have been blocked from removing.
    if not args.get("confirm_shrink") and not args.get("allow_overwrite"):
        _dropped_ed = _missing_top_level_py_symbols(p, content, working)
        if _dropped_ed:
            _dlist_ed = ", ".join(_dropped_ed[:10])
            return _tool_error_response(
                "WOULD_DROP_SYMBOLS",
                f"Edit to '{p.name}' would remove existing top-level symbol(s): "
                f"{_dlist_ed}. Keep existing module-level constants, functions, "
                "classes, and re-exports (or re-export moved symbols via "
                "'from new_module import symbol'). The file on disk was NOT modified."
                f"{_symbol_definition_hints(content, _dropped_ed)}",
                tool="edit_file")

    final = working.replace("\n", "\r\n") if _has_crlf else working
    _chk = await _checked_atomic_write(p, final, workspace_lock, "edit_file")
    if _chk:
        return _chk  # T6: containment re-check BEFORE any byte is written
    _lint = await _auto_lint_result(p, workspace)
    _old_lines_n = content.count("\n") + 1
    _new_lines_n = final.count("\n") + 1
    _delta = _new_lines_n - _old_lines_n
    result = f"[edit_file: '{p}' applied {len(parsed)} edits ({_delta:+d} lines)]"
    if _delta == 0:
        _chard = len(final) - len(content)
        result += f", {_chard:+d} chars"
    return result + _lint


async def _inline_tool_replace_lines(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    """Removed from the coder toolset (2026-09-17 consolidation) — edit_file
    covers exact-replace (old_text/new_text). Kept dispatchable for old
    recorded sessions. Legacy sniffs (2026-09-17): marker fragments and
    truncated foreign calls are rejected, never written into files."""
    _legacy_payload = str(args.get("replacement", "")) + str(args.get("content", ""))
    for _frag in ("<<<<<<<", ">>>>>>>',", ">>>>>>>", "<parameter=", "======="):
        _f = _frag.replace("',", "")
        if _f in _legacy_payload:
            return _tool_error_response(
                "REPLACE_LINES_BLOCK_FORMAT",
                "replacement contains SEARCH/REPLACE or foreign call marker "
                "fragments — that format is gone and is never written into files.",
                tool="replace_lines")
    return _tool_error_response(
        "TOOL_REMOVED",
        "replace_lines was consolidated into edit_file: use edit_file with "
        "old_text/new_text (exact, copied verbatim from read_file).",
        tool="replace_lines")

async def _inline_tool_undo_last(args: dict, workspace: Path, workspace_lock: str | None) -> str:


    from tools.workspace import get_transaction

    _path_arg = str(args.get("path", "") or "").strip()

    if _shared._GIT_TOOLS_AVAILABLE:
        try:
            from hive_functions.git_tools import (
                exec_git_undo_full as _g_undo_full,
                exec_git_undo_file as _g_undo_file,
                get_last_checkpoint as _g_last_cp,
            )
            _cp = await _g_last_cp(str(workspace))
            if _cp:
                if _path_arg:
                    _rp = _inline_resolve_path(workspace, _path_arg)
                    if err := _inline_check_workspace(_rp, workspace_lock, "undo_last"):
                        return err
                    _g_out = await _g_undo_file(str(workspace), str(_rp))
                    if _g_out.startswith("❌"):
                        return _tool_error_response(
                            "UNDO_LAST_FAILED", _g_out, tool="undo_last")
                    if _g_out:
                        _lint = await _auto_lint_result(_rp, workspace)
                        return f"[undo_last: {_g_out}]{_lint}"
                else:
                    _g_out = await _g_undo_full(str(workspace))
                    if _g_out.startswith("❌"):
                        return _tool_error_response(
                            "UNDO_LAST_FAILED", _g_out, tool="undo_last")
                    return f"[undo_last: {_g_out}]"
        except Exception as _g_err:
            pass

    _tx = get_transaction()
    if not _tx.has_changes:
        return _tool_error_response(
            "UNDO_LAST_NO_CHANGES",
            "No file changes in the current round to undo.",
            tool="undo_last" )
    if _path_arg:
        _rp = _inline_resolve_path(workspace, _path_arg)
        if err := _inline_check_workspace(_rp, workspace_lock, "undo_last"):
            return err
        _ok, _info = _tx.undo_path(_rp)
        if _ok:
            _lint = await _auto_lint_result(_rp, workspace)
            return f"[undo_last: restored '{_info}']{_lint}"
        return _tool_error_response(
            "UNDO_LAST_PATH_NOT_FOUND",
            _info,
            tool="undo_last" )
    _restored = _tx.rollback()
    if _restored:
        return f"[undo_last: restored {len(_restored)} file(s): {', '.join(_restored[:10])}]"
    return _tool_error_response(
        "UNDO_LAST_NO_CHANGES",
        "No file changes in the current round to undo.",
        tool="undo_last" )


def _old_str_snippet(old_str: str) -> str:
    """Extract first 5 lines of old_str for 'changed near' context snippet."""
    if not old_str or not old_str.strip():
        return ""
    _lines = old_str.replace("\r\n", "\n").splitlines()
    _show = _lines[:5]
    _snippet = "\n".join(_l[:80] for _l in _show)
    return f"\n[changed near:\n{_snippet}]"


def _first_dict_value(e: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in e and e[k]:
            return str(e[k])
    return ""


def _first_present_str(e: dict, keys: tuple[str, ...]) -> str | None:
    """Return str(e[k]) for the first present non-None key (allows '' deletion)."""
    for k in keys:
        if k in e and e[k] is not None:
            return str(e[k])
    return None


def _looks_like_json_edit_args(edits_str: str) -> bool:


    s = edits_str.strip()
    if not s or s[0] not in "[{":
        return False
    return any(k in s for k in ('"old_string"', '"old_str"', '"new_string"', '"new_str"', '"search"', '"replace"'))


def _try_convert_json_edits(edits_str: str) -> str:

    try:
        parsed = json.loads(edits_str)
        if isinstance(parsed, list) and len(parsed) > 0 and isinstance(parsed[0], dict):
            _keys = parsed[0].keys()
            if any(k in _keys for k in _JSON_EDIT_KEY_OLD):
                blocks = []
                for e in parsed:
                    old = _first_present_str(e, _JSON_EDIT_KEY_OLD)
                    new = _first_present_str(e, _JSON_EDIT_KEY_NEW)
                    if old is None:
                        old = ""
                    if new is None:
                        new = ""
                    blocks.append(f"<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE")
                return "\n\n".join(blocks)
    except Exception:
        pass
    return edits_str
