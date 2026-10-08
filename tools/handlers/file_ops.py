"""Tool handlers: file read/write/edit tools (part of tools/handlers, extracted from tools/handlers.py)."""

from __future__ import annotations

from pathlib import Path
from utils.file import fuzzy_resolve_path as _fuzzy_resolve_path, _inline_resolve_path, _inline_check_workspace
from tools.errors import tool_error_response as _tool_error_response
import asyncio
from tools.workspace import get_transaction
import json
import os
import re
import sys
import tempfile
import time

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

def _atomic_write_text(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
            _f.write(text)
        os.replace(_tmp_path, str(p))
    except Exception:
        try:
            os.unlink(_tmp_path)
        except Exception:
            pass
        raise


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
    # RE-READ DEDUP (2026-10-08, owner: "91 read_file, main.ts 12x"): a full
    # read of an UNCHANGED file returns a compact confirmation - the previous
    # read's content is already in the model context and does not need to be
    # paid for twice. Range reads and first reads always return content.
    _rr_unchanged = False
    try:
        from tools.runner import _read_signatures as _rr_sig, _normalize_tool_path as _rr_ntp
        _rr_prev = (_rr_sig.get(None) or {}).get(_rr_ntp(str(p), workspace))
        _rr_st = p.stat()
        _rr_cur = (_rr_st.st_mtime_ns, _rr_st.st_size)
        if _rr_prev is not None and _rr_prev == _rr_cur:
            _rr_unchanged = True
    except Exception:
        pass
    if _full and _rr_unchanged:
        try:
            _rr_nl = await asyncio.to_thread(_fast_newline_count, p, 100000)
            _rr_disp = str(_rr_nl)
        except Exception:
            _rr_disp = "?"
        return (f"[read_file: '{p.name}' is UNCHANGED since your last read "
                f"({_rr_disp} lines) - the content in your context above is still "
                "current. Use start_line/end_line to force a full re-read.]")
    try:
        # READ-EARLY-ABORT (2026-09-04): full reads (no range) no longer read
        # a huge file completely just to reject it as "too large" afterwards.
        # Instead: cheap binary sniff up front + streamed newline count
        # (aborts at line 401).
        if _full:
            _head = await asyncio.to_thread(_read_head_bytes, p, 1024)
            if _looks_binary_bytes(_head):
                return _read_binary_error(p)
            _nl, _capped = await asyncio.to_thread(_fast_newline_count, p, 401)
            if _nl > 400:
                _sig_fn = _shared.get_signatures_report if callable(_shared.get_signatures_report) else None
                _sig = await asyncio.to_thread(_sig_fn, p, 200) if _sig_fn else (
                    "(signature extraction unavailable) — use read_file with line_range=[1,50] to inspect the file header first."
                )
                _disp = f"{_nl}+" if _capped else str(_nl)
                return _tool_error_response(
                    "FILE_TOO_LARGE_NEED_RANGE",
                    f"File '{p}' is too large ({_disp} lines) to read fully into limited context. "
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

        # Note: full reads with >400 lines abort early above
        # (READ-EARLY-ABORT) - len(lines) <= 400 is guaranteed here.

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
            _chunk = content_chunk[:32000]
            if len(content_chunk) > 32000:
                _chunk += f"\n[TRUNCATED: {len(content_chunk)-32000} more chars omitted — use smaller line range]"
            return f"[{p} lines {s+1}-{min(e, len(lines))} / {len(lines)}]\n" + _chunk
        
        _MAX_READ_LINES = 200
        if len(lines) > _MAX_READ_LINES:
            _chunk = "".join(lines[:_MAX_READ_LINES])
            _chunk += f"\n[FILE TRUNCATED — {len(lines)} lines total. Use search_code for specific sections.]"
            return f"[{p} total lines: {len(lines)}]\n" + _chunk
        _chunk = content[:32000]
        if len(content) > 32000:
            _chunk += f"\n[TRUNCATED: {len(content)-32000} more chars omitted — use start_line/end_line to read sections]"
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
    target = workspace / args.get("path", "")
    if not target.exists():
        return _tool_error_response(
            "FILE_NOT_FOUND",
            "write_file_append requires an EXISTING file — create it with write_file first.",
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
            p.parent.mkdir(parents=True, exist_ok=True)
            _head = p.read_text(encoding="utf-8", errors="replace", newline="") if p.stat().st_size else ""
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

                    def _write_fuzzy_patch() -> None:
                        _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
                        try:
                            with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                                _f.write(new_content)
                            os.replace(_tmp_path, str(p))
                            _recheck_workspace_before_replace(p, workspace_lock, "_write_fuzzy_patch")
                        except Exception:
                            try:
                                os.unlink(_tmp_path)
                            except Exception:
                                pass
                            raise

                    _chk = _recheck_workspace_before_replace(p, workspace_lock, "patch_file")

                    if _chk:

                        return _chk  # T6: containment re-check BEFORE any byte is written

                    await asyncio.to_thread(_write_fuzzy_patch)
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
                def _write_p2_patch() -> None:
                    _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
                    try:
                        with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                            _f.write(_nc_p2)
                        os.replace(_tmp_path, str(p))
                        _recheck_workspace_before_replace(p, workspace_lock, "_write_p2_patch")
                    except Exception:
                        try:
                            os.unlink(_tmp_path)
                        except Exception:
                            pass
                        raise
                _chk = _recheck_workspace_before_replace(p, workspace_lock, "patch_file")
                if _chk:
                    return _chk  # T6: containment re-check BEFORE any byte is written
                await asyncio.to_thread(_write_p2_patch)
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

        def _write_patch_result() -> None:
            _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
            try:
                with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                    _f.write(new_content)
                os.replace(_tmp_path, str(p))
                _recheck_workspace_before_replace(p, workspace_lock, "_write_patch_result")
            except Exception:
                try:
                    os.unlink(_tmp_path)
                except Exception:
                    pass
                raise

        _chk = _recheck_workspace_before_replace(p, workspace_lock, "patch_file")

        if _chk:

            return _chk  # T6: containment re-check BEFORE any byte is written

        await asyncio.to_thread(_write_patch_result)
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
            "EDIT_FILE_EMPTY_EDITS",
            "write_file requires non-empty content.",
            tool="write_file")

    existed = p.exists()
    get_transaction().capture_before(p)
    _had_crlf = False
    _old_n = 0
    if existed:
        try:
            _old_raw = await asyncio.to_thread(p.read_text, encoding="utf-8", errors="replace", newline="")
            _had_crlf = "\r\n" in _old_raw
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
        if (_new_n <= 3 or (_old_n >= 100 and _new_n <= 10)) and not args.get("confirm_shrink"):
            return _tool_error_response(
                "WRITE_FILE_SUSPICIOUS_SHRINK",
                f"This overwrite would shrink '{p}' from {_old_n} to {_new_n} content "
                "lines — almost always a truncated or mistaken write. If you REALLY "
                "want a tiny file: read_file it first, then resend with "
                "\"confirm_shrink\": true.",
                tool="write_file")


    if _had_crlf:
        content = content.replace("\n", "\r\n")

    def _write_now() -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                _f.write(content)
            os.replace(_tmp_path, str(p))
            _recheck_workspace_before_replace(p, workspace_lock, "_write_now")
        except Exception:
            try: os.unlink(_tmp_path)
            except Exception: pass
            raise

    _chk = _recheck_workspace_before_replace(p, workspace_lock, "write_file")

    if _chk:

        return _chk  # T6: containment re-check BEFORE any byte is written

    await asyncio.to_thread(_write_now)
    _lint = await _auto_lint_result(p, workspace)
    _lines = content.count("\n") + 1
    if existed:
        return f"[write_file: rewrote '{p}' (+{_lines}/-{_old_n or '?'} lines)]{_lint}"
    return f"[write_file: created '{p}' (+{_lines} lines)]{_lint}"


async def _inline_tool_edit_file(args: dict, workspace: Path, workspace_lock: str | None) -> str:
    """Exact-match edit: replace ONE unique occurrence of old_text with new_text.

    Accepts BOTH formats: plain old_text/new_text AND SEARCH/REPLACE marker
    blocks (the models emit these naturally from pre-training). If markers are
    found in old_text, they are parsed as a block and the extracted texts are
    used for the replacement.
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
    if count == 0:
        # Fuzzy fallback: ONE best window, line-level ratio >= 0.85, spliced at
        # line granularity. STALE-GATE (2026-09-17): fuzzy only when the model
        # has actually READ this file this run AND the file is unchanged since
        # that read (mtime+size signature) — editing a never-read or externally
        # modified version must fail clearly, not fuzzy-match "something".
        _fuzzy_ok = False
        _stale_note = ""
        try:
            from tools.runner import _files_read_in_run as _fri, _read_signatures as _rsig, _normalize_tool_path as _ntp
            _rs = _fri.get(None)
            _ek = _ntp(args.get("path", ""), workspace)
            _fuzzy_ok = bool(_rs and _ek in _rs)
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
        _fzr = None
        if _fuzzy_ok:
            try:
                from tools.patch_2_fuzzy_edit import fuzzy_replace as _p2_fzr
                _fzr = _p2_fzr(working, old_n, new_n)
            except ImportError:
                _fzr = None
        if _fzr is not None:
            working = _fzr
            final = working.replace("\n", "\r\n") if _has_crlf else working

            def _write_fuzzy() -> None:
                _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
                try:
                    with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                        _f.write(final)
                    os.replace(_tmp_path, str(p))
                    _recheck_workspace_before_replace(p, workspace_lock, "_write_fuzzy")
                except Exception:
                    try: os.unlink(_tmp_path)
                    except Exception: pass
                    raise
            _chk = _recheck_workspace_before_replace(p, workspace_lock, "edit_file")
            if _chk:
                return _chk  # T6: containment re-check BEFORE any byte is written
            await asyncio.to_thread(_write_fuzzy)
            _lint = await _auto_lint_result(p, workspace)
            return f"[edit_file: '{p}' edited via fuzzy match]{_lint}"
        # OLD-TEXT RE-ANCHOR (2026-10-08, owner: the old_text loop): the
        # model edits from memory after context compression and repeats the
        # same failed edit. Attach the CURRENT head of the file to the error
        # so the very next edit can be built from real content.
        _anchor = ""
        try:
            _cur = p.read_text(encoding="utf-8", errors="replace").splitlines()
            _head = "\n".join(_cur[:60])
            if len(_head) > 4000:
                _head = _head[:4000] + "\n... [truncated]"
            _anchor = ("\n\n[CURRENT CONTENT of '" + p.name + "' — first "
                       + str(min(60, len(_cur))) + " lines. Build your next "
                       "edit_file old_text from EXACTLY this:]\n" + _head)
        except Exception:
            pass
        return _tool_error_response(
            "EDIT_FILE_OLD_TEXT_NOT_FOUND",
            f"old_text not found in '{p}' (exact or fuzzy).\n"
            f"  Looking for: {_first_hint!r}\n"
            "  read_file the file and COPY the passage verbatim — check indentation "
            "and whitespace; add surrounding lines to make it unique."
            + (_stale_note if _stale_note else "") + _anchor,
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
    working = working.replace(old_n, new_n, 1)
    if working.strip() == content.replace("\r\n", "\n").strip():
        return _tool_error_response(
            "EDIT_FILE_NOOP",
            f"no change — '{p}' already contains exactly this content.",
            tool="edit_file")
    _after_n = len([l for l in working.splitlines() if l.strip()])
    if (not args.get("confirm_shrink") and _pre_n >= 30
            and (_after_n <= 3 or (_pre_n >= 100 and _after_n <= _pre_n // 10))):
        return _tool_error_response(
            "EDIT_FILE_SUSPICIOUS_SHRINK",
            f"This replacement would shrink '{p}' from {_pre_n} to {_after_n} content "
            "lines — almost always a malformed edit, not a real refactor. If you "
            "REALLY want that: read_file first, then resend with "
            "\"confirm_shrink\": true.",
            tool="edit_file")

    final = working.replace("\n", "\r\n") if _has_crlf else working

    def _write_exact() -> None:
        _tmp_fd, _tmp_path = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
        try:
            with os.fdopen(_tmp_fd, "w", encoding="utf-8", newline="") as _f:
                _f.write(final)
            os.replace(_tmp_path, str(p))
            _recheck_workspace_before_replace(p, workspace_lock, "_write_exact")
        except Exception:
            try: os.unlink(_tmp_path)
            except Exception: pass
            raise

    _chk = _recheck_workspace_before_replace(p, workspace_lock, "edit_file")

    if _chk:

        return _chk  # T6: containment re-check BEFORE any byte is written

    await asyncio.to_thread(_write_exact)
    _old_lines_n = content.count("\n") + 1
    _new_lines_n = final.count("\n") + 1
    _delta = _new_lines_n - _old_lines_n
    _lint = await _auto_lint_result(p, workspace)
    result = f"[edit_file: '{p}' replaced ({_delta:+d} lines)]"
    if _delta == 0:
        # 0-LINES-CLARITY: same line count, different content — report the
        # char delta so the UI does not misleadingly show "0 lines".
        _chard = len(new_n) - len(old_n)
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
                    old = _first_dict_value(e, _JSON_EDIT_KEY_OLD)
                    new = _first_dict_value(e, _JSON_EDIT_KEY_NEW)
                    blocks.append(f"<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE")
                return "\n\n".join(blocks)
    except Exception:
        pass
    return edits_str
